"""Area edits: only the highlighted part changes, bad highlights cost nothing, and nobody edits another user's image."""
from __future__ import annotations

import io

from PIL import Image, ImageDraw

from studio import editing

from .conftest import Harness, png_bytes

RED = (220, 40, 40)
FAKE_RESULT = (30, 58, 52)  # FakeGateway paints its images this colour


def mask_png(size=(400, 300), box=(0, 0, 200, 300), fmt="PNG") -> bytes:
    m = Image.new("L", size, 0)
    if box:
        ImageDraw.Draw(m).rectangle(box, fill=255)
    buf = io.BytesIO()
    m.convert("RGB").save(buf, fmt)
    return buf.getvalue()


def upload_edit(c, prompt="Make it blue", mask=None, image=None):
    files = {"mask": ("mask.png", mask if mask is not None else mask_png(), "image/png"),
             "image": ("photo.png", image if image is not None else png_bytes((400, 300), RED), "image/png")}
    return c.post("/api/generations/edit", data={"prompt": prompt}, files=files)


def close(a, b, tolerance=12):
    return all(abs(x - y) <= tolerance for x, y in zip(a, b))


def test_only_the_highlighted_area_changes(tmp_path):
    h = Harness(tmp_path)
    c = h.client()
    r = upload_edit(c)
    assert r.status_code == 200
    h.run_jobs()
    g = c.get(f"/api/generations/{r.json()['id']}").json()
    assert g["status"] == "succeeded" and g["model_label"] == "Nano Banana 2.1"
    out = Image.open(io.BytesIO(c.get(g["media_url"]).content)).convert("RGB")
    assert out.size == (400, 300)
    assert close(out.getpixel((60, 250)), FAKE_RESULT)   # inside the highlight: the model's result
    assert close(out.getpixel((380, 150)), RED)          # well outside it: the original, untouched


def test_model_gets_the_original_and_a_marked_copy(tmp_path):
    h = Harness(tmp_path)
    upload_edit(h.client(), prompt="Swap the mug for a <script>glass</script> cup")
    h.run_jobs()
    call = h.gateway.calls[-1]
    assert call["model"] == "nano-banana-2.1" and call["refs"] == 2 and call["aspect"] == "4:3"
    assert "<change>Swap the mug for a scriptglass/script cup</change>" in call["prompt"]


def test_editing_your_own_result_uses_one_image(tmp_path):
    h = Harness(tmp_path)
    c = h.client()
    parent = h.template_job(c).json()["id"]
    h.run_jobs()
    before = c.get("/api/me").json()["images_left"]
    size = Image.open(io.BytesIO(c.get(f"/api/media/{parent}").content)).size
    r = c.post("/api/generations/edit", data={"prompt": "Add steam", "source_id": parent},
               files={"mask": ("m.png", mask_png(size, (0, 0, size[0] // 2, size[1] // 2)), "image/png")})
    assert r.status_code == 200
    h.run_jobs()
    g = c.get(f"/api/generations/{r.json()['id']}").json()
    assert g["status"] == "succeeded" and g["parent_id"] == parent and g["title"] == "Edit: Add steam"
    assert c.get("/api/me").json()["images_left"] == before - 1


def test_bad_highlights_are_refused_and_cost_nothing(tmp_path):
    h = Harness(tmp_path)
    c = h.client()
    for mask in (mask_png(box=None), mask_png(box=(0, 0, 400, 300)), mask_png(fmt="JPEG"), b"not an image", b""):
        r = upload_edit(c, mask=mask)
        assert r.status_code == 400, mask[:10]
    assert upload_edit(c, prompt=" ").status_code == 400
    assert c.post("/api/generations/edit", data={"prompt": "Make it blue"},
                  files={"mask": ("m.png", mask_png(), "image/png")}).status_code == 400
    assert c.get("/api/me").json()["images_left"] == 10
    assert c.get("/api/generations").json()["generations"] == []


def test_nobody_can_edit_another_users_image(tmp_path):
    h = Harness(tmp_path)
    owner = h.client("owner@example.com")
    parent = h.template_job(owner).json()["id"]
    h.run_jobs()
    other = h.client("other@example.com")
    r = other.post("/api/generations/edit", data={"prompt": "Make it blue", "source_id": parent},
                   files={"mask": ("m.png", mask_png(), "image/png")})
    assert r.status_code == 404


def test_videos_cannot_be_edited(tmp_path):
    h = Harness(tmp_path)
    c = h.client()
    parent = h.template_job(c).json()["id"]
    h.run_jobs()
    video = c.post(f"/api/generations/{parent}/animate", json={}).json()["id"]
    h.run_jobs()
    r = c.post("/api/generations/edit", data={"prompt": "Make it blue", "source_id": video},
               files={"mask": ("m.png", mask_png(), "image/png")})
    assert r.status_code == 400


def test_editing_needs_sign_in(tmp_path):
    assert upload_edit(Harness(tmp_path).client(email=None)).status_code == 401


def test_failed_edit_refunds(tmp_path):
    h = Harness(tmp_path)
    c = h.client()
    from studio.gemini import GenerationError

    h.gateway.fail_with = GenerationError("busy")
    gen_id = upload_edit(c).json()["id"]
    h.run_jobs()
    assert c.get(f"/api/generations/{gen_id}").json()["status"] == "failed"
    assert c.get("/api/me").json()["images_left"] == 10


def test_marked_copy_tints_only_the_area():
    blue = (30, 60, 200)
    source = png_bytes((400, 300), blue)
    marked = Image.open(io.BytesIO(editing.marked_copy(source, editing.clean_mask(mask_png(), (400, 300))))).convert("RGB")
    assert not close(marked.getpixel((60, 150)), blue, 40) and close(marked.getpixel((380, 150)), blue)
