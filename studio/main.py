"""The web app: security middleware, sign-in routes, generation API and static files."""
from __future__ import annotations

import json
import logging
import uuid
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import List, Optional

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import select, update
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import auth as auth_mod
from . import db, media, redact
from .config import ROOT, Settings, load_settings
from .gemini import (IMAGE_ASPECTS, IMAGE_MODELS, IMAGE_RESOLUTIONS, VIDEO_ASPECTS, VIDEO_DURATIONS, VIDEO_MODEL_KEY,
                     VIDEO_MODEL_LABEL, VIDEO_RESOLUTIONS, Gateway, GenerationError, build_gateway)
from .jobs import Worker
from .secrets_store import MissingSecret
from .storage import build_storage
from .templates_store import (LIMITS, SAMPLES_DIR, Template, build_copy_prompt, build_image_prompt, clean_text,
                              load_templates, parse_copy)

log = logging.getLogger("studio")

WEB_DIR = ROOT / "web"
MAX_BODY_BYTES = 30 * 1024 * 1024  # Cloud Run rejects anything over 32 MB before it reaches the app
MAX_FREE_REFS = 6
PROMPT_LIMIT = 2000
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
DEFAULT_MOTION = ("Bring this ad to life with one slow, confident camera move and subtle natural motion. "
                  "Keep the product and all text exactly as they appear. Add no new text or logos.")


def error_json(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)


def content_security_policy(s: Settings) -> str:
    media_hosts = f" {s.supabase_url}" if s.storage_backend == "supabase" else ""
    turnstile = " https://challenges.cloudflare.com" if s.turnstile_site_key else ""
    return "; ".join([
        "default-src 'none'",
        f"script-src 'self'{turnstile}",
        "style-src 'self'",
        f"img-src 'self' blob:{media_hosts}",
        f"media-src 'self'{media_hosts}",
        "font-src 'self'",
        "connect-src 'self'",
        f"frame-src{turnstile or ' ' + chr(39) + 'none' + chr(39)}",
        "form-action 'self'",
        "base-uri 'none'",
        "frame-ancestors 'none'",
    ])


class LoginBody(BaseModel):
    email: str
    password: str


class SignupBody(LoginBody):
    captcha: str = ""


class DevLoginBody(BaseModel):
    email: str


class CopyBody(BaseModel):
    template_id: str
    brand: str
    product: str
    category: str = ""


class AnimateBody(BaseModel):
    prompt: str = ""
    aspect: str = "9:16"
    resolution: str = "720p"
    duration: int = 8


def create_app(settings: Optional[Settings] = None, gateway: Optional[Gateway] = None, start_worker: bool = True) -> FastAPI:
    redact.install()
    s = settings or load_settings()
    engine = db.build_engine(s)
    storage = build_storage(s)
    templates: dict[str, Template] = load_templates()
    gw: Gateway = gateway or build_gateway(s)
    auth = auth_mod.build_auth(s)
    worker = Worker(engine, s, gw, storage, templates)
    csp = content_security_policy(s)
    allowed_origins = {s.public_origin}
    if not s.is_production:
        allowed_origins.add(s.public_origin.replace("127.0.0.1", "localhost"))

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if start_worker:
            worker.start()
        yield
        worker.stop()

    # No interactive docs and no schema: nothing public describes the API.
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.settings, app.state.engine, app.state.worker, app.state.gateway = s, engine, worker, gw
    app.state.storage = storage

    @app.middleware("http")
    async def security(request: Request, call_next):
        length = request.headers.get("content-length", "0")
        if length.isdigit() and int(length) > MAX_BODY_BYTES:
            return error_json(413, "too_large", "Those files are too large together. Keep them under 30 MB in total.")
        if request.method not in SAFE_METHODS:
            # Cross-site request protection, on top of SameSite cookies.
            origin = request.headers.get("origin")
            fetch_site = request.headers.get("sec-fetch-site")
            if (origin and origin not in allowed_origins) or (fetch_site and fetch_site not in ("same-origin", "none")) \
                    or request.headers.get("x-requested-with") != "studio":
                return error_json(403, "forbidden", "This request isn't allowed.")
        response = await call_next(request)
        h = response.headers
        h["Content-Security-Policy"] = csp
        h["X-Content-Type-Options"] = "nosniff"
        h["X-Frame-Options"] = "DENY"
        h["Referrer-Policy"] = "no-referrer"
        h["Cross-Origin-Opener-Policy"] = "same-origin"
        h["Cross-Origin-Resource-Policy"] = "same-origin"
        h["Permissions-Policy"] = "camera=(), microphone=(), geolocation=(), payment=()"
        if s.uses_https:
            h["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains; preload"
        if request.url.path.startswith(("/api/", "/auth/")):
            h["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(HTTPException)
    @app.exception_handler(StarletteHTTPException)
    async def http_error(_req: Request, exc):
        message = exc.detail if isinstance(exc.detail, str) else "Request failed."
        return error_json(exc.status_code, "error", message)

    @app.exception_handler(Exception)
    async def unhandled(_req: Request, exc: Exception):
        # Never echo an exception to a client.
        log.error("unhandled %s: %s", type(exc).__name__, redact.redact(exc)[:500])
        return error_json(500, "server_error", "Something went wrong on our side. Please try again.")

    @app.exception_handler(db.LimitReached)
    async def limit_reached(_req: Request, exc: db.LimitReached):
        return error_json(429, exc.code, exc.public_message)

    @app.exception_handler(media.BadUpload)
    async def bad_upload(_req: Request, exc: media.BadUpload):
        return error_json(400, "bad_upload", str(exc))

    @app.exception_handler(GenerationError)
    async def generation_error(_req: Request, exc: GenerationError):
        return error_json(502, "generation_failed", exc.public_message)

    @app.exception_handler(MissingSecret)
    async def missing_secret(_req: Request, _exc: MissingSecret):
        log.error("Gemini key is not configured")
        return error_json(503, "not_set_up", "The studio isn't fully set up yet.")

    @app.exception_handler(RequestValidationError)
    async def validation_error(_req: Request, _exc):
        # Pydantic errors echo the submitted input, including passwords. Return none of it.
        return error_json(422, "invalid", "Some of those details aren't valid.")

    # ---------- helpers ----------

    def current_user(request: Request, response: Response) -> auth_mod.User:
        user = auth.current(request, response)
        if not user:
            raise HTTPException(401, detail="Please sign in.")
        db.ensure_user(engine, s, user.id, user.email)
        return user

    def limit_ip(request: Request, action: str, limit: int, minutes: int) -> None:
        ip = auth_mod.client_ip(request, s)
        if db.hit_rate_limit(engine, f"{action}:{ip}", limit, timedelta(minutes=minutes)):
            raise HTTPException(429, detail="Too many attempts. Please wait a few minutes and try again.")

    def valid_email(email: str) -> str:
        email = email.strip().lower()
        if not auth_mod.EMAIL_RE.match(email) or len(email) > 320:
            raise HTTPException(400, detail="Enter a valid email address.")
        return email

    async def read_image(upload: UploadFile, keep_alpha: bool = False) -> tuple[bytes, str]:
        raw = await upload.read(media.MAX_UPLOAD_BYTES + 1)
        return await run_in_threadpool(media.clean_image, raw, keep_alpha)

    def store_input(user_id: str, data: bytes, mime: str) -> dict:
        ext = "png" if mime == "image/png" else "jpg"
        key = f"users/{user_id}/inputs/{uuid.uuid4().hex}.{ext}"
        storage.put(key, data, mime)
        return {"key": key, "mime": mime}

    def new_row(user: auth_mod.User, **values) -> dict:
        return {
            "id": uuid.uuid4().hex, "user_id": user.id, "status": "queued", "refunded": False, "deleted": False,
            "created_at": db.now(), "input_keys": [], "template_id": None, "parent_id": None, **values,
        }

    def public_generation(g) -> dict:
        """Whitelist of what a browser may see. The built prompt for a template is never included."""
        p = g.params or {}
        t = templates.get(g.template_id) if g.template_id else None
        label = VIDEO_MODEL_LABEL if g.kind == "video" else IMAGE_MODELS.get(g.model, {}).get("label", g.model)
        return {
            "id": g.id,
            "kind": g.kind,
            "source": g.source,
            "status": g.status,
            "model_label": label,
            "template_id": g.template_id,
            "template_name": t.name if t else None,
            "parent_id": g.parent_id,
            "aspect": p.get("aspect"),
            "title": p.get("title") or "",
            "user_prompt": p.get("user_prompt") or "",
            "error": g.error_public,
            "media_url": f"/api/media/{g.id}" if g.status == "succeeded" else None,
            "created_at": db.iso(g.created_at),
        }

    def get_owned(gen_id: str, user: auth_mod.User):
        with engine.connect() as c:
            g = c.execute(select(db.generations).where(
                db.generations.c.id == gen_id, db.generations.c.user_id == user.id,
                db.generations.c.deleted.is_(False))).first()
        if not g:
            # Same answer for someone else's job and no job at all.
            raise HTTPException(404, detail="Not found.")
        return g

    # ---------- public ----------

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.get("/api/config")
    def public_config():
        return {
            "auth": "supabase" if s.supabase_url else "dev",
            "turnstile_site_key": s.turnstile_site_key or None,
            "test_mode": s.gemini_mode == "fake",
            "image_models": [{"key": k, "label": v["label"], "max_refs": min(v["max_refs"], MAX_FREE_REFS)} for k, v in IMAGE_MODELS.items()],
            "video_model": {"key": VIDEO_MODEL_KEY, "label": VIDEO_MODEL_LABEL},
            "image_aspects": sorted(IMAGE_ASPECTS),
            "image_resolutions": sorted(IMAGE_RESOLUTIONS),
            "video_aspects": sorted(VIDEO_ASPECTS),
            "video_resolutions": sorted(VIDEO_RESOLUTIONS),
            "video_durations": sorted(VIDEO_DURATIONS),
        }

    @app.get("/api/templates")
    def list_templates():
        return {"templates": [t.public_view() for t in templates.values()]}

    # ---------- sign-in ----------

    if s.dev_login_enabled:
        @app.post("/auth/dev-login")
        def dev_login(body: DevLoginBody, request: Request, response: Response):
            # Local development only, and only from this machine.
            host = request.client.host if request.client else ""
            if not auth_mod.is_loopback(host):
                raise HTTPException(404, detail="Not found.")
            auth.issue(response, valid_email(body.email))
            return {"ok": True}

    if s.supabase_url:
        @app.post("/auth/login")
        def login(body: LoginBody, request: Request, response: Response):
            limit_ip(request, "login", 10, 10)
            auth.login(valid_email(body.email), body.password, response)
            return {"ok": True}

        @app.post("/auth/signup")
        def signup(body: SignupBody, request: Request, response: Response):
            limit_ip(request, "signup", 5, 60)
            if len(body.password) < 10:
                raise HTTPException(400, detail="Use a password of at least 10 characters.")
            auth.signup(valid_email(body.email), body.password, body.captcha, response)
            return {"ok": True, "check_email": True}

        @app.get("/auth/google")
        def google(request: Request):
            limit_ip(request, "google", 20, 10)
            redirect = RedirectResponse("/", status_code=303)
            redirect.headers["location"] = auth.google_redirect_url(redirect)
            return redirect

        @app.get("/auth/callback")
        def callback(request: Request, code: str = ""):
            verifier = request.cookies.get(auth.cookies.pkce, "")
            # Opened in another browser: the email is confirmed, they just need to sign in here.
            if not code or not verifier:
                return RedirectResponse("/login?confirmed=1", status_code=303)
            redirect = RedirectResponse("/", status_code=303)
            if not auth.exchange_code(code, verifier, redirect):
                return RedirectResponse("/login?confirmed=1", status_code=303)
            redirect.delete_cookie(auth.cookies.pkce, path="/", secure=s.uses_https, httponly=True, samesite="lax")
            return redirect

    @app.post("/auth/logout")
    def logout(request: Request, response: Response):
        if s.supabase_url:
            auth.logout(request)
        auth.cookies.clear(response)
        return {"ok": True}

    @app.get("/api/me")
    def me(user: auth_mod.User = Depends(current_user)):
        with engine.connect() as c:
            u = c.execute(select(db.users).where(db.users.c.id == user.id)).first()
        return {
            "email": user.email,
            "images_left": max(u.image_allowance - u.images_used, 0),
            "videos_left": max(u.video_allowance - u.videos_used, 0),
            "image_allowance": u.image_allowance,
            "video_allowance": u.video_allowance,
        }

    # ---------- generation ----------

    @app.post("/api/copy")
    def write_copy(body: CopyBody, user: auth_mod.User = Depends(current_user)):
        t = templates.get(body.template_id)
        if not t:
            raise HTTPException(404, detail="Not found.")
        if not clean_text(body.brand, 60) or not clean_text(body.product, 100):
            raise HTTPException(400, detail="Add your brand and product first.")
        if db.hit_rate_limit(engine, f"copy:{user.id}", s.copy_per_hour, timedelta(hours=1)):
            raise HTTPException(429, detail="That's a lot of drafts for one hour. Please try again later.")
        return parse_copy(gw.write_copy(build_copy_prompt(t, body.brand, body.product, body.category)))

    @app.post("/api/generations/template")
    async def generate_from_template(
        template_id: str = Form(...), brand: str = Form(...), product: str = Form(...), category: str = Form(""),
        headline: str = Form(...), subhead: str = Form(""), cta: str = Form(""), colors: str = Form(""),
        product_photo: UploadFile = File(...), logo: Optional[UploadFile] = File(None),
        user: auth_mod.User = Depends(current_user),
    ):
        t = templates.get(template_id)
        if not t:
            raise HTTPException(404, detail="Not found.")
        fields = {
            "BRAND": clean_text(brand, LIMITS["BRAND"]), "PRODUCT": clean_text(product, LIMITS["PRODUCT"]),
            "CATEGORY": clean_text(category, LIMITS["CATEGORY"]) or clean_text(product, LIMITS["PRODUCT"]),
            "HEADLINE": clean_text(headline, LIMITS["HEADLINE"]), "SUBHEAD": clean_text(subhead, LIMITS["SUBHEAD"]),
            "CTA": clean_text(cta, LIMITS["CTA"]), "COLORS": clean_text(colors, LIMITS["COLORS"]) or t.default_colors,
        }
        if not fields["BRAND"] or not fields["PRODUCT"] or not fields["HEADLINE"]:
            raise HTTPException(400, detail="Brand, product and headline are required.")
        inputs = [store_input(user.id, *await read_image(product_photo))]
        has_logo = bool(logo and logo.filename)
        if has_logo:
            inputs.append(store_input(user.id, *await read_image(logo, keep_alpha=True)))
        use_reference = t.reference_image() is not None
        row = new_row(
            user, kind="image", source="template", template_id=t.id, model=t.model, input_keys=inputs,
            params={
                "prompt": build_image_prompt(t, fields, has_logo, use_reference), "use_reference": use_reference,
                "aspect": t.aspect, "resolution": "1K", "title": f"{fields['BRAND']}: {fields['HEADLINE']}",
            },
        )
        await run_in_threadpool(db.reserve_and_create, engine, s, row)
        return {"id": row["id"]}

    @app.post("/api/generations/free")
    async def generate_free(
        kind: str = Form(...), prompt: str = Form(...), model: str = Form(""), aspect: str = Form("4:5"),
        resolution: str = Form(""), duration: int = Form(8), references: List[UploadFile] = File(default=[]),
        user: auth_mod.User = Depends(current_user),
    ):
        text = prompt.strip()[:PROMPT_LIMIT]
        if len(text) < 3:
            raise HTTPException(400, detail="Describe what you'd like to make.")
        if len(references) > MAX_FREE_REFS:
            raise HTTPException(400, detail=f"Use up to {MAX_FREE_REFS} reference images.")
        if kind == "image":
            if model not in IMAGE_MODELS or aspect not in IMAGE_ASPECTS:
                raise HTTPException(400, detail="Choose a valid model and shape.")
            params = {"resolution": resolution if resolution in IMAGE_RESOLUTIONS else "1K"}
            model_key = model
        elif kind == "video":
            if aspect not in VIDEO_ASPECTS or duration not in VIDEO_DURATIONS:
                raise HTTPException(400, detail="Choose a valid shape and length.")
            params = {"resolution": resolution if resolution in VIDEO_RESOLUTIONS else "720p", "duration": duration}
            model_key = VIDEO_MODEL_KEY
        else:
            raise HTTPException(400, detail="Choose image or video.")
        inputs = [store_input(user.id, *await read_image(f)) for f in references if f.filename]
        row = new_row(
            user, kind=kind, source="free", model=model_key, input_keys=inputs,
            params={**params, "prompt": text, "user_prompt": text, "aspect": aspect, "use_reference": False, "title": text[:80]},
        )
        await run_in_threadpool(db.reserve_and_create, engine, s, row)
        return {"id": row["id"]}

    @app.post("/api/generations/{gen_id}/animate")
    def animate(gen_id: str, body: AnimateBody, user: auth_mod.User = Depends(current_user)):
        parent = get_owned(gen_id, user)
        if parent.kind != "image" or parent.status != "succeeded":
            raise HTTPException(400, detail="Only a finished image can be animated.")
        if body.aspect not in VIDEO_ASPECTS or body.resolution not in VIDEO_RESOLUTIONS or body.duration not in VIDEO_DURATIONS:
            raise HTTPException(400, detail="Choose a valid shape, quality and length.")
        t = templates.get(parent.template_id) if parent.template_id else None
        user_prompt = body.prompt.strip()[:PROMPT_LIMIT]
        motion = user_prompt or (t.motion_prompt if t else DEFAULT_MOTION)
        row = new_row(
            user, kind="video", source="animate", template_id=parent.template_id, parent_id=parent.id, model=VIDEO_MODEL_KEY,
            input_keys=[{"key": parent.output_key, "mime": parent.output_mime}],
            params={
                "prompt": f"Use the given image as the opening frame and exact visual reference. {motion}",
                "user_prompt": user_prompt, "aspect": body.aspect, "resolution": body.resolution,
                "duration": body.duration, "title": (parent.params or {}).get("title", ""),
            },
        )
        db.reserve_and_create(engine, s, row)
        return {"id": row["id"]}

    @app.post("/api/generations/{gen_id}/rerun")
    def rerun(gen_id: str, user: auth_mod.User = Depends(current_user)):
        g = get_owned(gen_id, user)
        row = new_row(user, kind=g.kind, source=g.source, template_id=g.template_id, parent_id=g.parent_id, model=g.model,
                      input_keys=g.input_keys, params=g.params)
        db.reserve_and_create(engine, s, row)
        return {"id": row["id"]}

    @app.get("/api/generations")
    def list_generations(limit: int = 30, before: str = "", user: auth_mod.User = Depends(current_user)):
        limit = max(1, min(limit, 60))
        query = select(db.generations).where(db.generations.c.user_id == user.id, db.generations.c.deleted.is_(False))
        if before:
            with engine.connect() as c:
                pivot = c.execute(select(db.generations.c.created_at).where(
                    db.generations.c.id == before, db.generations.c.user_id == user.id)).first()
            if pivot:
                query = query.where(db.generations.c.created_at < pivot.created_at)
        with engine.connect() as c:
            rows = c.execute(query.order_by(db.generations.c.created_at.desc()).limit(limit)).fetchall()
        return {"generations": [public_generation(g) for g in rows]}

    @app.get("/api/generations/{gen_id}")
    def get_generation(gen_id: str, user: auth_mod.User = Depends(current_user)):
        return public_generation(get_owned(gen_id, user))

    @app.delete("/api/generations/{gen_id}")
    def delete_generation(gen_id: str, user: auth_mod.User = Depends(current_user)):
        g = get_owned(gen_id, user)
        if g.status in ("queued", "running"):
            raise HTTPException(409, detail="Wait for this one to finish before deleting it.")
        with engine.begin() as c:
            c.execute(update(db.generations).where(db.generations.c.id == g.id).values(deleted=True))
        # An animation may still use this image as an input, so keep its file if anything points at it.
        with engine.connect() as c:
            children = c.execute(select(db.generations.c.id).where(
                db.generations.c.parent_id == g.id, db.generations.c.deleted.is_(False))).first()
        if g.output_key and not children:
            storage.delete(g.output_key)
        return {"ok": True}

    @app.get("/api/media/{gen_id}")
    def get_media(gen_id: str, response: Response, download: int = 0, user: auth_mod.User = Depends(current_user)):
        g = get_owned(gen_id, user)
        if g.status != "succeeded" or not g.output_key:
            raise HTTPException(404, detail="Not found.")
        path = storage.local_path(g.output_key)
        if path is not None:
            if not path.is_file():
                raise HTTPException(404, detail="Not found.")
            result = FileResponse(path, media_type=g.output_mime, filename=f"eureka-{g.id[:8]}{path.suffix}",
                                  content_disposition_type="attachment" if download else "inline",
                                  headers={"Cache-Control": "private, max-age=3600"})
        else:
            name = f"eureka-{g.id[:8]}.{g.output_key.rsplit('.', 1)[-1]}" if download else None
            result = RedirectResponse(storage.signed_url(g.output_key, download=name), status_code=302)
        # A refreshed session set by the sign-in check must survive returning a response directly.
        for name, value in response.headers.items():
            if name.lower() == "set-cookie":
                result.headers.append("set-cookie", value)
        return result

    # ---------- the page ----------

    index_file = WEB_DIR / "index.html"

    def page() -> FileResponse:
        return FileResponse(index_file, media_type="text/html", headers={"Cache-Control": "no-cache"})

    for route in ("/", "/login", "/studio", "/library"):
        app.add_api_route(route, page, methods=["GET"], include_in_schema=False)

    @app.get("/templates/{template_id}", include_in_schema=False)
    def template_page(template_id: str):
        return page()

    SAMPLES_DIR.mkdir(parents=True, exist_ok=True)
    app.mount("/assets", StaticFiles(directory=WEB_DIR / "assets"), name="assets")
    app.mount("/samples", StaticFiles(directory=SAMPLES_DIR), name="samples")
    return app


def app_factory() -> FastAPI:
    return create_app()
