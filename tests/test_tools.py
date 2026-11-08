"""The secret scan, the model test script and rules about the browser code."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone

import pytest

from scripts import bakeoff, secret_scan
from studio import db
from studio.config import ROOT, UnsafeConfig, validate
from studio.gemini import IMAGE_MODELS, FakeGateway, GenerationError
from studio.templates_store import load_templates

from .conftest import Harness, make_settings
from .test_security import PROD_OK

# Built at runtime, so this file holds no key-shaped text for the real scan to trip over.
GOOGLE_SHAPED = "AI" + "za" + "Sy" + "A1b2C3d4" * 5
JWT_SHAPED = ".".join(["ey" + "J" + "abcdefghijklmnop"] * 2 + ["abcdefghijklmnop"])


@pytest.mark.parametrize("text,rule", [
    (f"key = '{GOOGLE_SHAPED}'", "Google API key"),
    (JWT_SHAPED, "JSON web token"),
    ("sb_" + "secret_abcdefghijklmnop", "Supabase secret key"),
    ("GEMINI_API_KEY" + "=abc123", "Key assigned in a config line"),
    ("postgresql://" + "user:hunter2hunter2@db.host/x", "Database password"),
    ("-----BEGIN " + "PRIVATE KEY-----", "Private key"),
])
def test_scan_catches_each_kind_of_secret(text, rule):
    assert rule in [name for _, name in secret_scan.scan_text(f"first line\n{text}\n")]


def test_scan_reports_the_line_but_never_the_value():
    found = secret_scan.scan_text(f"a\nb\n{GOOGLE_SHAPED}\n")
    assert found == [(3, "Google API key")]
    assert GOOGLE_SHAPED not in repr(found)


def test_scan_finds_the_exact_stored_key_whatever_it_looks_like():
    assert secret_scan.scan_text("x\nunusual-key-format\n", exact="unusual-key-format") == [(2, "Your stored Gemini key")]


def test_scan_ignores_placeholders_in_docs():
    assert secret_scan.scan_text("GEMINI_API_KEY_FILE=/secrets/gemini/key\npostgresql://user:[PASSWORD]@host/db\n") == []


@pytest.mark.parametrize("path", [".env", ".env.production", "references/1.jpg", ".local/studio.db", "secrets/a.txt",
                                  "deploy/server.pem", "my-service-account.json"])
def test_private_paths_cannot_be_committed(path):
    assert secret_scan.forbidden_path(path)


@pytest.mark.parametrize("path", [".env.example", "references/.gitkeep", "studio/main.py", "templates/big-bite.json"])
def test_normal_paths_are_allowed(path):
    assert not secret_scan.forbidden_path(path)


def test_this_project_passes_its_own_scan():
    assert secret_scan.scan(staged=False) == []


def test_pre_commit_hook_fails_closed():
    hook = (ROOT / ".githooks" / "pre-commit").read_text(encoding="utf-8")
    assert "scripts.secret_scan --staged" in hook and "exit 1" in hook


# ---------- browser code ----------

def test_browser_code_never_builds_markup_from_strings():
    code = re.compile(r"\.(innerHTML|outerHTML)\s*=|insertAdjacentHTML|document\.write|\beval\(|new Function")
    for path in (ROOT / "web" / "assets").glob("*.js"):
        assert not code.search(path.read_text(encoding="utf-8")), path


def test_times_are_sent_as_utc(tmp_path):
    assert db.iso(datetime(2026, 1, 2, 3, 4, 5)) == "2026-01-02T03:04:05Z"
    assert db.iso(datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)) == "2026-01-02T03:04:05Z"


def test_oversized_request_gets_a_clear_error(tmp_path):
    client = Harness(tmp_path).client()
    r = client.post("/api/generations/free", headers={"content-length": str(31 * 1024 * 1024)}, content=b"")
    assert r.status_code == 413 and "30 MB" in r.json()["error"]["message"]


def test_production_refuses_a_database_password_in_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h/db")
    with pytest.raises(UnsafeConfig):
        validate(make_settings(tmp_path, **PROD_OK))


# ---------- model test script ----------

def test_every_template_has_a_made_up_sample():
    assert set(bakeoff.SAMPLES) == set(load_templates())


def test_full_model_test_is_fifteen_images(tmp_path):
    assert sum(len(bakeoff.steps_for(t, tmp_path)) for t in load_templates().values()) == 15


def test_run_makes_all_three_images_with_the_right_inputs(tmp_path):
    t = load_templates()["big-bite"]
    gateway = FakeGateway()
    assert bakeoff.make_template(t, gateway, tmp_path) == []
    shot, *renders = gateway.calls
    assert shot["model"] == bakeoff.PRODUCT_SHOT_MODEL and shot["refs"] == 0
    assert {r["model"] for r in renders} == set(IMAGE_MODELS)
    for r in renders:
        assert r["aspect"] == t.aspect and r["refs"] == (2 if t.reference_image() else 1)
        assert "Ollo Burger Co" in r["prompt"] and "{{" not in r["prompt"]
    assert (tmp_path / "big-bite-compare.jpg").is_file()


def test_rerun_never_pays_twice(tmp_path):
    t = load_templates()["big-bite"]
    bakeoff.make_template(t, FakeGateway(), tmp_path)
    again = FakeGateway()
    assert bakeoff.steps_for(t, tmp_path) == []
    bakeoff.make_template(t, again, tmp_path)
    assert again.calls == []


def test_failed_product_shot_spends_nothing_more(tmp_path):
    t = load_templates()["big-bite"]
    gateway = FakeGateway()
    gateway.fail_with = GenerationError("busy")
    assert len(bakeoff.make_template(t, gateway, tmp_path)) == 1
    assert len(gateway.calls) == 1


def test_pick_stores_the_winner_and_publishes_only_our_render(tmp_path):
    out, templates_dir, samples = tmp_path / "out", tmp_path / "templates", tmp_path / "samples"
    templates_dir.mkdir()
    templates = load_templates()
    t = templates["big-bite"]
    (templates_dir / "big-bite.json").write_text((ROOT / "templates" / "big-bite.json").read_text(encoding="utf-8"), encoding="utf-8")
    bakeoff.make_template(t, FakeGateway(), out)
    bakeoff.pick(templates, ["big-bite=nano-banana-2.1"], out, templates_dir, samples)
    saved = json.loads((templates_dir / "big-bite.json").read_text(encoding="utf-8"))
    assert saved["model"] == "nano-banana-2.1" and saved["model_status"] == "tested"
    assert [p.name for p in samples.iterdir()] == ["big-bite.jpg"]


@pytest.mark.parametrize("choice", ["big-bite=gpt", "nope=nano-banana-pro", "big-bite=nano-banana-pro"])
def test_pick_refuses_bad_choices(tmp_path, choice):
    with pytest.raises(SystemExit):
        bakeoff.pick(load_templates(), [choice], tmp_path, tmp_path, tmp_path / "samples")
    assert not (tmp_path / "samples").exists()


@pytest.mark.parametrize("key", ["../x", "a/../../x", "/etc/passwd", "a" + chr(92) + "b", "C:/x", "", "a//b", "./a"])
def test_storage_refuses_keys_that_could_escape_its_folder(tmp_path, key):
    from studio.storage import LocalStorage

    with pytest.raises(ValueError):
        LocalStorage(tmp_path).put(key, b"x", "image/png")
