"""Stops secrets and private files before they get into git. Runs as the pre-commit hook.

    python -m scripts.secret_scan            every file git would track
    python -m scripts.secret_scan --staged   only what is about to be committed

It reports a file, a line and a rule, and never prints the matching text.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from fnmatch import fnmatch
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

RULES = [
    ("Google API key", re.compile(r"AIza[0-9A-Za-z_\-]{30,}")),
    ("Google OAuth token", re.compile(r"ya29\.[0-9A-Za-z_\-]{20,}")),
    ("Supabase secret key", re.compile(r"sb_secret_[0-9A-Za-z_\-]{10,}")),
    ("JSON web token", re.compile(r"eyJ[0-9A-Za-z_\-]{10,}\.eyJ[0-9A-Za-z_\-]{10,}\.[0-9A-Za-z_\-]{10,}")),
    ("Private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("Database password", re.compile(r"postgres(?:ql)?(?:\+\w+)?://[^:/\s\"']+:(?!p@|password@|PASSWORD@|\[)[^@\s\"']+@")),
    ("Key assigned in a config line", re.compile(r"(?im)^\s*(?:GEMINI_API_KEY|GOOGLE_API_KEY|SUPABASE_SERVICE_ROLE_KEY)\s*=\s*\S+")),
]
# Deliberately fake values the test suite uses to prove redaction works.
TEST_VALUE = "TESTSENTINEL"

FORBIDDEN_PATHS = [".env", ".env.*", "*.pem", "*.key", "secrets/*", ".local/*", "references/*", "credentials*.json", "*service-account*.json"]
ALLOWED_PATHS = {".env.example", "references/.gitkeep"}


def forbidden_path(path: str) -> bool:
    if path in ALLOWED_PATHS:
        return False
    name = path.rsplit("/", 1)[-1]
    return any(fnmatch(path, pat) or fnmatch(name, pat) for pat in FORBIDDEN_PATHS)


def stored_key() -> str:
    """The real key, held in memory only, so even an unusual-looking key is caught exactly."""
    try:
        import keyring

        from studio.secrets_store import KEYRING_GEMINI, KEYRING_SERVICE

        return keyring.get_password(KEYRING_SERVICE, KEYRING_GEMINI) or ""
    except Exception:
        return ""


def scan_text(text: str, exact: str = "") -> list[tuple[int, str]]:
    """Returns (line number, rule name). The matching text is deliberately left out."""
    found: list[tuple[int, str]] = []
    if exact and exact in text:
        found.append((text[:text.index(exact)].count("\n") + 1, "Your stored Gemini key"))
    for name, pattern in RULES:
        for match in pattern.finditer(text):
            if TEST_VALUE in match.group(0):
                continue
            found.append((text[:match.start()].count("\n") + 1, name))
    return found


def _git(*args: str) -> bytes:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, check=True).stdout


def _paths(staged: bool) -> list[str]:
    if staged:
        raw = _git("diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z")
    else:
        raw = _git("ls-files", "-co", "--exclude-standard", "-z")
    return [p for p in raw.decode("utf-8").split("\0") if p]


def _read(path: str, staged: bool) -> bytes:
    if staged:
        return _git("show", f":{path}")
    file = ROOT / path
    return file.read_bytes() if file.is_file() else b""


def scan(staged: bool) -> list[str]:
    exact = stored_key()
    problems = []
    for path in _paths(staged):
        if forbidden_path(path):
            problems.append(f"{path}: this file must never be committed")
            continue
        # latin-1 never fails, so a key inside a binary or oddly encoded file is still seen.
        text = _read(path, staged).decode("latin-1")
        problems.extend(f"{path}:{line}: {rule}" for line, rule in scan_text(text, exact))
    return problems


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--staged", action="store_true")
    args = parser.parse_args()
    problems = scan(args.staged)
    if problems:
        print("Secret scan failed, so nothing was committed:", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        return 1
    print("Secret scan passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
