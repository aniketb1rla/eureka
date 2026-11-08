"""Upload handling. Every image is decoded and re-encoded, so only clean pixels are kept:
no metadata, no location data, no polyglot files, no SVG scripts."""
from __future__ import annotations

import io
import warnings

from PIL import Image, ImageOps

MAX_UPLOAD_BYTES = 12 * 1024 * 1024
MAX_PIXELS = 50_000_000
MAX_SIDE = 2048
ALLOWED_FORMATS = {"JPEG", "PNG", "WEBP"}


class BadUpload(ValueError):
    pass


def clean_image(raw: bytes, keep_alpha: bool = False) -> tuple[bytes, str]:
    if not raw:
        raise BadUpload("That file is empty.")
    if len(raw) > MAX_UPLOAD_BYTES:
        raise BadUpload("Images must be 12 MB or smaller.")
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        try:
            probe = Image.open(io.BytesIO(raw))
            fmt = probe.format
            w, h = probe.size
        except Exception:
            raise BadUpload("Use a JPG, PNG or WebP image.") from None
        if fmt not in ALLOWED_FORMATS:
            raise BadUpload("Use a JPG, PNG or WebP image.")
        if w * h > MAX_PIXELS or w < 64 or h < 64:
            raise BadUpload("That image's dimensions are too large or too small.")
        try:
            img = Image.open(io.BytesIO(raw))
            img = ImageOps.exif_transpose(img)
            img.load()
        except Exception:
            raise BadUpload("That image couldn't be read.") from None
    img.thumbnail((MAX_SIDE, MAX_SIDE))
    has_alpha = img.mode in ("RGBA", "LA") or "transparency" in img.info
    buf = io.BytesIO()
    if keep_alpha and has_alpha:
        img.convert("RGBA").save(buf, "PNG", optimize=True)
        return buf.getvalue(), "image/png"
    img.convert("RGB").save(buf, "JPEG", quality=92)
    return buf.getvalue(), "image/jpeg"


def sniff_image_mime(data: bytes) -> str:
    """Trust the bytes, not the model's label, before serving anything to a browser."""
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    raise BadUpload("Model returned an unknown image format")
