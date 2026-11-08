"""The product flow, end to end, against the fake model gateway."""
from __future__ import annotations

import pytest

from studio.templates_store import load_templates

from .conftest import png_bytes

TEMPLATES = load_templates()


def test_five_templates_from_five_industries():
    assert len(TEMPLATES) == 5
    assert len({t.industry for t in TEMPLATES.values()}) == 5
    for t in TEMPLATES.values():
        assert t.model in ("nano-banana-pro", "nano-banana-2.1")
        for slot in ("{{BRAND}}", "{{PRODUCT}}", "{{HEADLINE}}", "{{SUBHEAD}}", "{{CTA}}", "{{COLORS}}"):
            assert slot in t.prompt, (t.id, slot)
        assert t.motion_prompt and t.copy_brief


@pytest.mark.parametrize("template_id", sorted(TEMPLATES))
def test_template_to_image_uses_its_stored_model_and_shape(h, template_id):
    c = h.client()
    r = h.template_job(c, template_id=template_id, subhead="A small line", cta="Try it")
    assert r.status_code == 200
    h.run_jobs()
    g = c.get(f"/api/generations/{r.json()['id']}").json()
    assert g["status"] == "succeeded" and g["template_name"] == TEMPLATES[template_id].name
    call = h.gateway.calls[-1]
    assert call["model"] == TEMPLATES[template_id].model and call["aspect"] == TEMPLATES[template_id].aspect
    assert "Glow now" in call["prompt"] and "Acme" in call["prompt"] and "A small line" in call["prompt"]
    media = c.get(g["media_url"])
    assert media.status_code == 200 and media.headers["content-type"] == "image/png"


def test_logo_is_sent_after_product(h):
    c = h.client()
    r = c.post("/api/generations/template",
               data={"template_id": "giant-type", "brand": "A", "product": "B", "headline": "Go"},
               files={"product_photo": ("p.png", png_bytes(), "image/png"), "logo": ("l.png", png_bytes(), "image/png")})
    assert r.status_code == 200
    h.run_jobs()
    call = h.gateway.calls[-1]
    assert "Image 2 is the brand LOGO" in call["prompt"] and call["refs"] >= 2


def test_ai_copy_returns_editable_fields(h):
    c = h.client()
    r = c.post("/api/copy", json={"template_id": "big-bite", "brand": "Acme", "product": "Serum", "category": "Beauty"})
    assert r.status_code == 200 and set(r.json()) == {"headline", "subhead", "cta"}
    assert "Acme" in h.gateway.calls[-1]["prompt"]


def test_animate_image_to_video(h):
    c = h.client()
    image_id = h.template_job(c).json()["id"]
    h.run_jobs()
    r = c.post(f"/api/generations/{image_id}/animate", json={"aspect": "9:16", "duration": 6})
    assert r.status_code == 200
    h.run_jobs()
    video = c.get(f"/api/generations/{r.json()['id']}").json()
    assert video["kind"] == "video" and video["status"] == "succeeded" and video["parent_id"] == image_id
    assert video["model_label"] == "Omni Flash 1.1"
    call = h.gateway.calls[-1]
    assert call["refs"] == 1 and call["duration"] == 6
    assert TEMPLATES["big-bite"].motion_prompt in call["prompt"]
    assert c.get(video["media_url"]).headers["content-type"] == "video/mp4"
    assert c.get("/api/me").json()["videos_left"] == 0


def test_animate_needs_a_finished_image(h):
    c = h.client()
    image_id = h.template_job(c).json()["id"]
    assert c.post(f"/api/generations/{image_id}/animate", json={}).status_code == 400


@pytest.mark.parametrize("model", ["nano-banana-pro", "nano-banana-2.1"])
def test_free_form_image(h, model):
    c = h.client()
    r = c.post("/api/generations/free", data={"kind": "image", "prompt": "A red bicycle at dawn", "model": model, "aspect": "1:1"},
               files=[("references", ("r.png", png_bytes(), "image/png"))])
    assert r.status_code == 200
    h.run_jobs()
    g = c.get(f"/api/generations/{r.json()['id']}").json()
    assert g["status"] == "succeeded" and g["user_prompt"] == "A red bicycle at dawn"
    assert h.gateway.calls[-1]["model"] == model and h.gateway.calls[-1]["refs"] == 1


def test_free_form_video(h):
    c = h.client()
    r = c.post("/api/generations/free", data={"kind": "video", "prompt": "Waves at dusk", "aspect": "16:9", "duration": 4})
    assert r.status_code == 200
    h.run_jobs()
    assert c.get(f"/api/generations/{r.json()['id']}").json()["status"] == "succeeded"


@pytest.mark.parametrize("data", [
    {"kind": "image", "prompt": "x y z", "model": "some-other-model", "aspect": "1:1"},
    {"kind": "image", "prompt": "x y z", "model": "nano-banana-pro", "aspect": "7:3"},
    {"kind": "video", "prompt": "x y z", "aspect": "1:1"},
    {"kind": "video", "prompt": "x y z", "aspect": "9:16", "duration": 600},
    {"kind": "audio", "prompt": "x y z"},
    {"kind": "image", "prompt": " ", "model": "nano-banana-pro", "aspect": "1:1"},
])
def test_free_form_rejects_anything_outside_the_three_models(h, data):
    c = h.client()
    assert c.post("/api/generations/free", data=data).status_code == 400
    assert c.get("/api/me").json()["images_left"] == 10


def test_library_lists_reruns_and_deletes(h):
    c = h.client()
    first = h.template_job(c).json()["id"]
    h.run_jobs()
    second = c.post(f"/api/generations/{first}/rerun").json()["id"]
    h.run_jobs()
    ids = [g["id"] for g in c.get("/api/generations").json()["generations"]]
    assert ids[0] == second and first in ids
    assert c.get(f"/api/media/{first}?download=1").headers["content-disposition"].startswith("attachment")
    assert c.get("/api/me").json()["images_left"] == 8
    assert c.delete(f"/api/generations/{first}").status_code == 200
    assert c.get(f"/api/generations/{first}").status_code == 404
    assert c.get(f"/api/media/{first}").status_code == 404
    assert first not in [g["id"] for g in c.get("/api/generations").json()["generations"]]


def test_cannot_delete_running_job(h):
    c = h.client()
    gen_id = h.template_job(c).json()["id"]
    assert c.delete(f"/api/generations/{gen_id}").status_code == 409


def test_background_worker_runs_jobs(tmp_path):
    import time

    from fastapi.testclient import TestClient

    from studio.gemini import FakeGateway
    from studio.main import create_app

    from .conftest import HEADERS, make_settings

    app = create_app(make_settings(tmp_path), FakeGateway(), start_worker=True)
    with TestClient(app, headers=HEADERS) as c:
        c.post("/auth/dev-login", json={"email": "w@example.com"})
        gen_id = c.post("/api/generations/free", data={"kind": "image", "prompt": "abc", "model": "nano-banana-pro", "aspect": "1:1"}).json()["id"]
        for _ in range(50):
            if c.get(f"/api/generations/{gen_id}").json()["status"] == "succeeded":
                break
            time.sleep(0.1)
        assert c.get(f"/api/generations/{gen_id}").json()["status"] == "succeeded"
