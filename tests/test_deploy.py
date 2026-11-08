"""Deployment files keep secrets out of the image and lock down the database."""
from __future__ import annotations

from scripts import export_schema
from studio.config import ROOT
from studio.db import metadata


def lines(name: str) -> list[str]:
    return [x.strip() for x in (ROOT / name).read_text(encoding="utf-8").splitlines() if x.strip() and not x.startswith("#")]


def test_supabase_sql_matches_the_tables():
    assert (ROOT / "deploy" / "supabase.sql").read_text(encoding="utf-8") == export_schema.build()


def test_every_table_is_locked_down():
    sql = export_schema.build()
    for table in metadata.sorted_tables:
        assert f"alter table {table.name} enable row level security;" in sql
        assert f"revoke all on table {table.name} from anon, authenticated;" in sql
    assert "create policy" not in sql.lower()


def test_storage_bucket_is_private():
    sql = export_schema.build()
    assert "'studio-private', false)" in sql and "set public = false" in sql


def test_build_context_is_an_allow_list():
    ignore = lines(".dockerignore")
    assert ignore[0] == "*"
    allowed = {x for x in ignore if x.startswith("!")}
    assert allowed == {"!requirements.txt", "!studio/", "!templates/", "!web/", "!references/"}


def test_upload_to_google_is_an_allow_list_that_keeps_references():
    ignore = lines(".gcloudignore")
    assert ignore[0] == "*"
    assert "!references/**" in ignore and "!.env" not in ignore
    for name in ("tests", "scripts", ".local", ".git", ".env"):
        assert f"!{name}" not in ignore and f"!{name}/" not in ignore


def test_container_has_no_secrets_and_does_not_run_as_root():
    docker = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "USER studio" in docker
    for word in ("ARG ", "GEMINI_API_KEY", "SUPABASE_SERVICE", "DATABASE_URL", "COPY . "):
        assert word not in docker


def test_local_only_tools_are_not_in_production():
    production = (ROOT / "requirements.txt").read_text(encoding="utf-8").lower()
    assert "keyring" not in production and "pytest" not in production


def test_deploy_guide_mounts_secrets_as_files():
    guide = (ROOT / "deploy" / "README.md").read_text(encoding="utf-8")
    assert "/secrets/gemini/key=gemini-api-key:latest" in guide
    assert "--no-cpu-throttling" in guide
    assert "GEMINI_API_KEY=" not in guide and "DATABASE_URL=" not in guide


def test_production_app_starts_without_local_only_tools(tmp_path, monkeypatch):
    """Everything except the real network: the Postgres driver loads, and keyring isn't needed."""
    import dataclasses
    import sys

    from fastapi.testclient import TestClient

    from studio.main import create_app

    from .conftest import make_settings
    from .test_security import PROD_OK

    monkeypatch.setitem(sys.modules, "keyring", None)
    key_file = tmp_path / "service-key"
    key_file.write_text("service-key-for-this-test-only", encoding="utf-8")
    settings = dataclasses.replace(make_settings(tmp_path, **PROD_OK), supabase_service_key_file=str(key_file))
    client = TestClient(create_app(settings, start_worker=False), raise_server_exceptions=False, base_url="https://studio.example.com")

    assert client.get("/healthz").json() == {"ok": True}
    config = client.get("/api/config").json()
    assert config["auth"] == "supabase" and config["test_mode"] is False
    # A real sign-in route replaces the local one.
    headers = {"x-requested-with": "studio", "origin": "https://studio.example.com"}
    assert client.post("/auth/dev-login", json={"email": "a@b.co"}, headers=headers).status_code in (404, 405)
    for path in ("/references/1104226402442168005.jpg", "/samples/../../references/1104226402442168005.jpg", "/templates/big-bite.json"):
        assert b"\xff\xd8\xff" not in client.get(path).content[:3]
