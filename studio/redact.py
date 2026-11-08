"""Log and error redaction. Installed on the loggers before anything else runs."""
from __future__ import annotations

import logging
import re

# Google API keys, bearer tokens, JWTs, key=... query strings, Postgres passwords
_PATTERNS = [
    re.compile(r"AIza[0-9A-Za-z_\-]{20,}"),
    re.compile(r"(?i)bearer\s+[0-9A-Za-z._\-]{10,}"),
    re.compile(r"eyJ[0-9A-Za-z_\-]{8,}\.[0-9A-Za-z_\-]{8,}\.[0-9A-Za-z_\-]*"),
    re.compile(r"(?i)([?&](?:key|api_key|apikey|token|access_token|refresh_token)=)[^&\s\"']+"),
    re.compile(r"(?i)(x-goog-api-key['\"]?\s*[:=]\s*['\"]?)[^\s'\",}]+"),
    re.compile(r"(?i)(postgres(?:ql)?(?:\+\w+)?://[^:/\s]+:)[^@\s]+"),
]
_exact: set[str] = set()


def register_secret(value: str) -> None:
    """Redact this exact value everywhere, whatever it looks like."""
    if value and len(value) >= 8:
        _exact.add(value)


def redact(text: object) -> str:
    out = str(text)
    for secret in _exact:
        out = out.replace(secret, "[REDACTED]")
    for pat in _PATTERNS:
        if pat.groups:
            out = pat.sub(lambda m: m.group(1) + "[REDACTED]", out)
        else:
            out = pat.sub("[REDACTED]", out)
    return out


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        message = redact(record.getMessage())
        if record.exc_info and record.exc_info[1] is not None:
            # Tracebacks can carry request objects and locals; keep a redacted one-line summary.
            exc = record.exc_info[1]
            message = f"{message} | {type(exc).__name__}: {redact(exc)}"
        record.msg = message
        record.args = ()
        record.exc_info = None
        record.exc_text = None
        return True


def install() -> None:
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    flt = RedactingFilter()
    for name in (None, "uvicorn", "uvicorn.error", "uvicorn.access", "httpx", "httpcore", "google_genai"):
        logger = logging.getLogger(name)
        if not any(isinstance(f, RedactingFilter) for f in logger.filters):
            logger.addFilter(flt)
        for handler in logger.handlers:
            if not any(isinstance(f, RedactingFilter) for f in handler.filters):
                handler.addFilter(flt)
    # These log full request URLs and headers at lower levels.
    for noisy in ("httpcore", "httpx", "google_genai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
