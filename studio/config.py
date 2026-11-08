"""Settings, read once from the environment. Production refuses to start if anything is unsafe."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from . import redact

ROOT = Path(__file__).resolve().parent.parent


class UnsafeConfig(RuntimeError):
    pass


def _load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split(" #")[0].strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError as exc:
        raise UnsafeConfig(f"{name} must be a whole number") from exc


@dataclass(frozen=True)
class Settings:
    app_env: str
    gemini_mode: str
    gemini_key_file: str
    public_origin: str
    database_url: str
    data_dir: Path
    storage_backend: str
    supabase_url: str
    supabase_anon_key: str
    supabase_service_key_file: str
    supabase_bucket: str
    turnstile_site_key: str
    free_image_allowance: int
    free_video_allowance: int
    global_daily_image_cap: int
    global_daily_video_cap: int
    max_active_jobs: int
    copy_per_hour: int
    generations_per_hour: int
    worker_threads: int
    image_model_pro: str
    image_model_21: str
    video_model: str
    text_model: str

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def uses_https(self) -> bool:
        return self.public_origin.startswith("https://")

    @property
    def dev_login_enabled(self) -> bool:
        return self.app_env in ("local", "test") and not self.supabase_url

    def cookie(self, name: str) -> str:
        return f"__Host-{name}" if self.uses_https else name


def _database_url(env) -> str:
    """The connection string holds a password, so production reads it from a Secret Manager file."""
    path = env.get("DATABASE_URL_FILE", "")
    if not path:
        return env.get("DATABASE_URL", "")
    url = Path(path).read_text(encoding="utf-8").strip()
    redact.register_secret(url)
    return url


def load_settings() -> Settings:
    _load_dotenv(ROOT / ".env")
    env = os.environ
    s = Settings(
        app_env=env.get("APP_ENV", "local"),
        gemini_mode=env.get("GEMINI_MODE", "fake"),
        gemini_key_file=env.get("GEMINI_API_KEY_FILE", ""),
        public_origin=env.get("PUBLIC_ORIGIN", "http://127.0.0.1:8010").rstrip("/"),
        database_url=_database_url(env),
        data_dir=Path(env.get("DATA_DIR", str(ROOT / ".local"))),
        storage_backend=env.get("STORAGE_BACKEND", "local"),
        supabase_url=env.get("SUPABASE_URL", "").rstrip("/"),
        supabase_anon_key=env.get("SUPABASE_ANON_KEY", ""),
        supabase_service_key_file=env.get("SUPABASE_SERVICE_KEY_FILE", ""),
        supabase_bucket=env.get("SUPABASE_BUCKET", "studio-private"),
        turnstile_site_key=env.get("TURNSTILE_SITE_KEY", ""),
        free_image_allowance=_int("FREE_IMAGE_ALLOWANCE", 10),
        free_video_allowance=_int("FREE_VIDEO_ALLOWANCE", 1),
        global_daily_image_cap=_int("GLOBAL_DAILY_IMAGE_CAP", 200),
        global_daily_video_cap=_int("GLOBAL_DAILY_VIDEO_CAP", 20),
        max_active_jobs=_int("MAX_ACTIVE_JOBS_PER_USER", 3),
        copy_per_hour=_int("COPY_PER_HOUR", 30),
        generations_per_hour=_int("GENERATIONS_PER_HOUR", 20),
        worker_threads=_int("WORKER_THREADS", 4),
        image_model_pro=env.get("NANO_BANANA_PRO_MODEL", "nano-banana-pro-preview"),
        image_model_21=env.get("NANO_BANANA_21_MODEL", "gemini-nano-banana-2.1"),
        video_model=env.get("OMNI_MODEL", "gemini-omni-1.1-flash"),
        text_model=env.get("TEXT_MODEL", "gemini-3.6-flash"),
    )
    validate(s)
    return s


def validate(s: Settings) -> None:
    if s.app_env not in ("local", "test", "production"):
        raise UnsafeConfig("APP_ENV must be local, test or production")
    if s.gemini_mode not in ("fake", "real"):
        raise UnsafeConfig("GEMINI_MODE must be fake or real")
    if os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"):
        # Environment variables leak through crash reports, child processes and dashboards.
        raise UnsafeConfig(
            "Remove GEMINI_API_KEY/GOOGLE_API_KEY from the environment. "
            "Use Credential Manager locally and a Secret Manager file mount in production."
        )
    if not s.is_production:
        return
    problems = []
    origin = urlparse(s.public_origin)
    if origin.scheme != "https" or not origin.hostname or origin.hostname in ("localhost", "127.0.0.1"):
        problems.append("PUBLIC_ORIGIN must be your public https address")
    if s.gemini_mode != "real":
        problems.append("GEMINI_MODE must be real")
    if not s.gemini_key_file:
        problems.append("GEMINI_API_KEY_FILE must point at the Secret Manager mount")
    if os.environ.get("DATABASE_URL"):
        problems.append("use DATABASE_URL_FILE, not DATABASE_URL, so the password stays out of the environment")
    if not s.database_url.startswith("postgresql"):
        problems.append("DATABASE_URL_FILE must hold the Supabase Postgres connection")
    if s.storage_backend != "supabase":
        problems.append("STORAGE_BACKEND must be supabase")
    if not s.supabase_url.startswith("https://") or not s.supabase_anon_key:
        problems.append("SUPABASE_URL and SUPABASE_ANON_KEY are required")
    if not s.supabase_service_key_file:
        problems.append("SUPABASE_SERVICE_KEY_FILE must point at the Secret Manager mount")
    if problems:
        raise UnsafeConfig("Refusing to start in production: " + "; ".join(problems))
