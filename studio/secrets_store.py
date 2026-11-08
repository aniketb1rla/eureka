"""The only place a secret is read from. Nothing here is ever returned by an API or logged."""
from __future__ import annotations

from pathlib import Path

from . import redact
from .config import Settings

KEYRING_SERVICE = "eureka-studio"
KEYRING_GEMINI = "gemini-api-key"


class MissingSecret(RuntimeError):
    """Deliberately carries no value and no path detail."""


def _read_file(path: str) -> str:
    return Path(path).read_text(encoding="utf-8").strip()


def load_gemini_key(s: Settings) -> str:
    key = ""
    if s.gemini_key_file:
        key = _read_file(s.gemini_key_file)
    elif not s.is_production:
        import keyring  # local only; not installed in the production image

        key = (keyring.get_password(KEYRING_SERVICE, KEYRING_GEMINI) or "").strip()
    if not key:
        raise MissingSecret("Gemini key is not configured on the server")
    redact.register_secret(key)
    return key


def load_supabase_service_key(s: Settings) -> str:
    key = _read_file(s.supabase_service_key_file)
    redact.register_secret(key)
    return key
