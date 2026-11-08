"""Sign-in. The browser only ever holds httpOnly cookies, so page scripts can't read a session.

Production uses Supabase Auth through this server (email + password, Google via PKCE).
Local runs without Supabase get a dev login that only answers on the loopback address.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import logging
import re
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode

import httpx
from fastapi import HTTPException, Request, Response

from .config import Settings

log = logging.getLogger("studio.auth")

ACCESS_MAX_AGE = 60 * 60
REFRESH_MAX_AGE = 60 * 60 * 24 * 30
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}\.[^@\s]{2,}$")


@dataclass(frozen=True)
class User:
    id: str
    email: str


def client_ip(request: Request, s: Settings) -> str:
    if s.is_production:
        # Cloud Run appends the real client as the last entry; earlier entries are client-supplied.
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",")[-1].strip()
    return request.client.host if request.client else "unknown"


def is_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == "testclient"


class SessionCookies:
    def __init__(self, s: Settings):
        self.s = s
        self.access = s.cookie("studio_session")
        self.refresh = s.cookie("studio_refresh")
        self.pkce = s.cookie("studio_pkce")

    def set(self, response: Response, name: str, value: str, max_age: int) -> None:
        response.set_cookie(name, value, max_age=max_age, path="/", secure=self.s.uses_https, httponly=True, samesite="lax")

    def set_session(self, response: Response, access: str, refresh: str) -> None:
        self.set(response, self.access, access, ACCESS_MAX_AGE)
        if refresh:
            self.set(response, self.refresh, refresh, REFRESH_MAX_AGE)

    def clear(self, response: Response) -> None:
        for name in (self.access, self.refresh, self.pkce):
            response.delete_cookie(name, path="/", secure=self.s.uses_https, httponly=True, samesite="lax")


class DevAuth:
    """Signed local cookie. Only reachable when dev_login_enabled, which production can never set."""

    def __init__(self, s: Settings):
        self.s = s
        self.cookies = SessionCookies(s)
        path: Path = s.data_dir / "dev_session_secret"
        s.data_dir.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text(secrets.token_urlsafe(48), encoding="utf-8")
        self._secret = path.read_bytes()

    def _sign(self, payload: str) -> str:
        return hmac.new(self._secret, payload.encode(), hashlib.sha256).hexdigest()

    def issue(self, response: Response, email: str) -> None:
        user_id = "dev-" + hashlib.sha256(email.encode()).hexdigest()[:24]
        payload = base64.urlsafe_b64encode(f"{user_id}|{email}".encode()).decode()
        self.cookies.set(response, self.cookies.access, f"{payload}.{self._sign(payload)}", REFRESH_MAX_AGE)

    def current(self, request: Request, response: Response) -> Optional[User]:
        raw = request.cookies.get(self.cookies.access, "")
        if "." not in raw:
            return None
        payload, sig = raw.rsplit(".", 1)
        if not hmac.compare_digest(self._sign(payload), sig):
            return None
        user_id, email = base64.urlsafe_b64decode(payload).decode().split("|", 1)
        return User(user_id, email)


class SupabaseAuth:
    """Talks to Supabase Auth from the server. Passwords and tokens never touch logs or page scripts."""

    def __init__(self, s: Settings):
        self.s = s
        self.cookies = SessionCookies(s)
        self._base = f"{s.supabase_url}/auth/v1"
        self._cache: dict[str, tuple[float, User]] = {}
        self._lock = threading.Lock()

    def _headers(self, bearer: Optional[str] = None) -> dict:
        h = {"apikey": self.s.supabase_anon_key, "Content-Type": "application/json"}
        if bearer:
            h["Authorization"] = f"Bearer {bearer}"
        return h

    def _user_from_token(self, token: str) -> Optional[User]:
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self._lock:
            hit = self._cache.get(digest)
            if hit and hit[0] > time.time():
                return hit[1]
        try:
            r = httpx.get(f"{self._base}/user", headers=self._headers(token), timeout=10)
        except httpx.HTTPError:
            raise HTTPException(503, detail="Sign-in is temporarily unavailable.") from None
        if r.status_code != 200:
            return None
        data = r.json()
        # Free allowances only go to confirmed addresses.
        if not (data.get("email_confirmed_at") or data.get("confirmed_at")):
            return None
        user = User(data["id"], data.get("email") or "")
        with self._lock:
            if len(self._cache) > 5000:
                self._cache.clear()
            self._cache[digest] = (time.time() + 60, user)
        return user

    def _refresh(self, refresh_token: str, response: Response) -> Optional[User]:
        try:
            r = httpx.post(f"{self._base}/token?grant_type=refresh_token", headers=self._headers(),
                           json={"refresh_token": refresh_token}, timeout=10)
        except httpx.HTTPError:
            return None
        if r.status_code != 200:
            return None
        tokens = r.json()
        self.cookies.set_session(response, tokens["access_token"], tokens.get("refresh_token", ""))
        return self._user_from_token(tokens["access_token"])

    def current(self, request: Request, response: Response) -> Optional[User]:
        token = request.cookies.get(self.cookies.access)
        user = self._user_from_token(token) if token else None
        if user:
            return user
        refresh_token = request.cookies.get(self.cookies.refresh)
        return self._refresh(refresh_token, response) if refresh_token else None

    def login(self, email: str, password: str, response: Response) -> None:
        try:
            r = httpx.post(f"{self._base}/token?grant_type=password", headers=self._headers(),
                           json={"email": email, "password": password}, timeout=15)
        except httpx.HTTPError:
            raise HTTPException(503, detail="Sign-in is temporarily unavailable.") from None
        if r.status_code != 200:
            if "not confirmed" in r.text.lower():
                raise HTTPException(403, detail="Confirm your email first. Check your inbox for the link.")
            raise HTTPException(401, detail="That email and password don't match.")
        tokens = r.json()
        self.cookies.set_session(response, tokens["access_token"], tokens.get("refresh_token", ""))

    def signup(self, email: str, password: str, captcha: str, response: Response) -> None:
        verifier, challenge = _pkce_pair()
        body = {"email": email, "password": password, "code_challenge": challenge, "code_challenge_method": "s256"}
        if captcha:
            body["gotrue_meta_security"] = {"captcha_token": captcha}
        query = urlencode({"redirect_to": f"{self.s.public_origin}/auth/callback"})
        try:
            r = httpx.post(f"{self._base}/signup?{query}", headers=self._headers(), json=body, timeout=15)
        except httpx.HTTPError:
            raise HTTPException(503, detail="Sign-up is temporarily unavailable.") from None
        if r.status_code >= 500:
            raise HTTPException(503, detail="Sign-up is temporarily unavailable.")
        if r.status_code >= 400:
            # Same reply whether or not the address exists, so emails can't be probed.
            log.info("signup rejected with status %s", r.status_code)
            if "password" in r.text.lower():
                raise HTTPException(400, detail="Choose a stronger password.")
            if "captcha" in r.text.lower():
                raise HTTPException(400, detail="Please complete the verification and try again.")
            return
        self.cookies.set(response, self.cookies.pkce, verifier, 60 * 60)

    def google_redirect_url(self, response: Response) -> str:
        verifier, challenge = _pkce_pair()
        self.cookies.set(response, self.cookies.pkce, verifier, 600)
        query = urlencode({
            "provider": "google", "redirect_to": f"{self.s.public_origin}/auth/callback",
            "code_challenge": challenge, "code_challenge_method": "s256",
        })
        return f"{self._base}/authorize?{query}"

    def exchange_code(self, code: str, verifier: str, response: Response) -> bool:
        try:
            r = httpx.post(f"{self._base}/token?grant_type=pkce", headers=self._headers(),
                           json={"auth_code": code, "code_verifier": verifier}, timeout=15)
        except httpx.HTTPError:
            return False
        if r.status_code != 200:
            return False
        tokens = r.json()
        self.cookies.set_session(response, tokens["access_token"], tokens.get("refresh_token", ""))
        return True

    def logout(self, request: Request) -> None:
        token = request.cookies.get(self.cookies.access)
        if token:
            try:
                httpx.post(f"{self._base}/logout", headers=self._headers(token), timeout=5)
            except httpx.HTTPError:
                pass


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode()).digest()
    return verifier, base64.urlsafe_b64encode(digest).decode().rstrip("=")


def build_auth(s: Settings):
    return SupabaseAuth(s) if s.supabase_url else DevAuth(s)
