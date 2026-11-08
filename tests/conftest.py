from __future__ import annotations

import dataclasses
import io
import os

import pytest
from fastapi.testclient import TestClient
from PIL import Image

os.environ.pop("GEMINI_API_KEY", None)
os.environ.pop("GOOGLE_API_KEY", None)

from studio.config import Settings  # noqa: E402
from studio.gemini import FakeGateway  # noqa: E402
from studio.main import create_app  # noqa: E402

ORIGIN = "http://testserver"
HEADERS = {"x-requested-with": "studio"}


def make_settings(tmp_path, **overrides) -> Settings:
    base = dict(
        app_env="test", gemini_mode="fake", gemini_key_file="", public_origin=ORIGIN, database_url="",
        data_dir=tmp_path, storage_backend="local", supabase_url="", supabase_anon_key="",
        supabase_service_key_file="", supabase_bucket="b", turnstile_site_key="",
        free_image_allowance=10, free_video_allowance=1, global_daily_image_cap=200, global_daily_video_cap=20,
        max_active_jobs=3, copy_per_hour=30, generations_per_hour=20, worker_threads=2,
        image_model_pro="pro", image_model_21="v21", video_model="omni", text_model="text",
    )
    base.update(overrides)
    return Settings(**base)


def png_bytes(size=(200, 200), color=(10, 120, 90), fmt="PNG", **save) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, fmt, **save)
    return buf.getvalue()


class Harness:
    def __init__(self, tmp_path, **overrides):
        self.settings = make_settings(tmp_path, **overrides)
        self.gateway = FakeGateway()
        self.app = create_app(self.settings, self.gateway, start_worker=False)
        self.worker = self.app.state.worker
        self.engine = self.app.state.engine

    def client(self, email: str | None = "a@example.com") -> TestClient:
        c = TestClient(self.app, raise_server_exceptions=False, headers=HEADERS)
        if email:
            assert c.post("/auth/dev-login", json={"email": email}).status_code == 200
        return c

    def run_jobs(self) -> None:
        """Run queued jobs inline, so tests are deterministic."""
        while True:
            gen_id = self.worker.claim_next()
            if not gen_id:
                return
            self.worker.process(gen_id)

    def template_job(self, c: TestClient, template_id="big-bite", **fields):
        data = {"template_id": template_id, "brand": "Acme", "product": "Glow serum", "category": "skincare",
                "headline": "Glow now", "subhead": "", "cta": "Shop", **fields}
        return c.post("/api/generations/template", data=data, files={"product_photo": ("p.png", png_bytes(), "image/png")})


@pytest.fixture
def h(tmp_path):
    return Harness(tmp_path)
