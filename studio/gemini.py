"""Gateway to the models. This is the only module that holds a Gemini client.

Callers get bytes or a GenerationError with a safe public message. Raw SDK errors never cross
this boundary, so nothing they contain (URLs, headers, request echoes) can reach a user.
Video bytes are fetched here, so a model download URL is never handed to a browser.
"""
from __future__ import annotations

import base64
import io
import json
import logging
import threading
from dataclasses import dataclass
from typing import Optional, Protocol

from PIL import Image, ImageDraw

from . import redact, secrets_store
from .config import Settings

log = logging.getLogger("studio.gemini")

IMAGE_MODELS = {
    "nano-banana-pro": {"label": "Nano Banana Pro", "max_refs": 6},
    "nano-banana-2.1": {"label": "Nano Banana 2.1", "max_refs": 14},
}
VIDEO_MODEL_KEY = "omni-flash-1.1"
VIDEO_MODEL_LABEL = "Omni Flash 1.1"
IMAGE_ASPECTS = {"1:1", "4:5", "3:4", "2:3", "9:16", "16:9"}
# Every shape the image models accept. Edits keep the source photo's shape, so they may use any of them.
MODEL_ASPECTS = IMAGE_ASPECTS | {"4:3", "3:2", "5:4", "21:9"}
IMAGE_RESOLUTIONS = {"1K", "2K"}
VIDEO_ASPECTS = {"9:16", "16:9"}
VIDEO_RESOLUTIONS = {"720p", "1080p"}
VIDEO_DURATIONS = {4, 6, 8, 10}
MAX_VIDEO_REFS = 7


class GenerationError(Exception):
    """public_message is safe to show a user. Nothing else from the failure ever is."""

    def __init__(self, public_message: str):
        super().__init__(public_message)
        self.public_message = public_message


BLOCKED = "The safety filter blocked this request, so nothing was made. Your allowance wasn't used. Try rewording it or using a different photo."
BUSY = "The model is busy right now. Your allowance wasn't used, so please try again in a few minutes."
FAILED = "This one didn't work. Your allowance wasn't used, so please try again."


@dataclass
class ImageRef:
    data: bytes
    mime: str


class Gateway(Protocol):
    def generate_image(self, model_key: str, prompt: str, refs: list[ImageRef], aspect: str, resolution: str) -> tuple[bytes, str]: ...
    def generate_video(self, prompt: str, refs: list[ImageRef], aspect: str, resolution: str, duration: int) -> bytes: ...
    def write_copy(self, prompt: str) -> dict: ...


def _classify(exc: Exception) -> GenerationError:
    text = str(exc).lower()
    code = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    # Log only redacted detail, server side.
    log.warning("model call failed: %s %s", type(exc).__name__, redact.redact(exc)[:500])
    if "safety" in text or "content_blocked" in text or "blocked" in text:
        return GenerationError(BLOCKED)
    if code in (429, 503) or "resource_exhausted" in text or "unavailable" in text:
        return GenerationError(BUSY)
    return GenerationError(FAILED)


class RealGateway:
    def __init__(self, settings: Settings):
        self._s = settings
        self._client = None
        self._lock = threading.Lock()

    def __repr__(self) -> str:
        return "<RealGateway>"

    def _get_client(self):
        with self._lock:
            if self._client is None:
                from google import genai

                self._client = genai.Client(api_key=secrets_store.load_gemini_key(self._s))
            return self._client

    def _image_model_id(self, key: str) -> str:
        return {"nano-banana-pro": self._s.image_model_pro, "nano-banana-2.1": self._s.image_model_21}[key]

    def generate_image(self, model_key, prompt, refs, aspect, resolution):
        from google.genai import types

        contents = [types.Part.from_bytes(data=r.data, mime_type=r.mime) for r in refs] + [prompt]
        cfg = {}
        if aspect in MODEL_ASPECTS:
            cfg["aspect_ratio"] = aspect
        if resolution in IMAGE_RESOLUTIONS:
            cfg["image_size"] = resolution
        config = types.GenerateContentConfig(image_config=types.ImageConfig(**cfg)) if cfg else None
        client = self._get_client()
        try:
            response = client.models.generate_content(
                model=self._image_model_id(model_key), contents=contents, config=config
            )
        except Exception as exc:
            raise _classify(exc) from None

        for cand in response.candidates or []:
            reason = str(cand.finish_reason or "").upper()
            if "SAFETY" in reason or "PROHIBITED" in reason:
                raise GenerationError(BLOCKED)
            for part in (cand.content.parts if cand.content else None) or []:
                if part.inline_data and part.inline_data.data:
                    data = part.inline_data.data
                    data = base64.b64decode(data) if isinstance(data, str) else data
                    return data, (part.inline_data.mime_type or "image/png")
        raise GenerationError(FAILED)

    def generate_video(self, prompt, refs, aspect, resolution, duration):
        def b64(b: bytes) -> str:
            return base64.b64encode(b).decode("ascii")

        inputs = [{"type": "image", "data": b64(r.data), "mime_type": r.mime} for r in refs]
        inputs.append({"type": "text", "text": f"{prompt}\n\nThe video is {duration} seconds long."})
        fmt: dict = {"type": "video"}
        if aspect in VIDEO_ASPECTS:
            fmt["aspect_ratio"] = aspect
        if resolution == "1080p":
            fmt["resolution"] = resolution
        request = {"model": self._s.video_model, "input": inputs, "response_format": fmt}
        client = self._get_client()
        try:
            try:
                interaction = client.interactions.create(**request)
            except Exception as fmt_err:
                # An unsupported option is rejected before any render, so one retry can't double-bill.
                if "resolution" in str(fmt_err).lower():
                    fmt.pop("resolution", None)
                    interaction = client.interactions.create(**request)
                else:
                    raise
        except Exception as exc:
            raise _classify(exc) from None

        data = self._extract_video(interaction)
        if not data:
            raise GenerationError(FAILED)
        return base64.b64decode(data) if isinstance(data, str) else data

    @staticmethod
    def _extract_video(interaction):
        out = getattr(interaction, "output_video", None)
        if out:
            return out.data
        for step in getattr(interaction, "steps", None) or []:
            if isinstance(step, dict) and step.get("type") == "model_output":
                for part in step.get("content") or []:
                    if part.get("type") == "video":
                        return part.get("data")
        return None

    def write_copy(self, prompt):
        from google.genai import types

        client = self._get_client()
        try:
            resp = client.models.generate_content(
                model=self._s.text_model,
                contents=prompt,
                config=types.GenerateContentConfig(response_mime_type="application/json", temperature=0.8),
            )
            return json.loads(resp.text)
        except Exception as exc:
            raise _classify(exc) from None


class FakeGateway:
    """No network and no cost. Used for tests and for working on the app without a key."""

    def __init__(self):
        self.fail_with: Optional[Exception] = None
        self.calls: list[dict] = []

    def _maybe_fail(self):
        if self.fail_with:
            raise self.fail_with

    def generate_image(self, model_key, prompt, refs, aspect, resolution):
        self.calls.append({"kind": "image", "model": model_key, "prompt": prompt, "refs": len(refs), "aspect": aspect})
        self._maybe_fail()
        w, h = {"1:1": (1000, 1000), "4:5": (960, 1200), "3:4": (960, 1280), "2:3": (960, 1440),
                "9:16": (810, 1440), "16:9": (1440, 810)}.get(aspect, (960, 1200))
        img = Image.new("RGB", (w, h), (30, 58, 52))
        if refs:
            try:
                product = Image.open(io.BytesIO(refs[0].data)).convert("RGB")
                product.thumbnail((int(w * 0.7), int(h * 0.55)))
                img.paste(product, ((w - product.width) // 2, int(h * 0.35)))
            except Exception:
                pass
        ImageDraw.Draw(img).text((40, 40), f"TEST RENDER\n{model_key}\n{aspect}", fill=(240, 240, 230))
        buf = io.BytesIO()
        img.save(buf, "PNG")
        return buf.getvalue(), "image/png"

    def generate_video(self, prompt, refs, aspect, resolution, duration):
        self.calls.append({"kind": "video", "prompt": prompt, "refs": len(refs), "aspect": aspect, "duration": duration})
        self._maybe_fail()
        return b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64

    def write_copy(self, prompt):
        self.calls.append({"kind": "copy", "prompt": prompt})
        self._maybe_fail()
        return {"headline": "Made for right now", "subhead": "A test line written without calling any model.", "cta": "Shop now"}


def build_gateway(settings: Settings) -> Gateway:
    return RealGateway(settings) if settings.gemini_mode == "real" else FakeGateway()
