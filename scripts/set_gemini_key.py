"""Stores the Gemini key in Windows Credential Manager. Run this yourself; never paste the key into a chat.

    python -m scripts.set_gemini_key            ask for the key (typing is hidden) and store it
    python -m scripts.set_gemini_key --status   say whether a key is stored
    python -m scripts.set_gemini_key --verify   check the key works and the models exist (free, makes nothing)
    python -m scripts.set_gemini_key --delete   remove the stored key

Nothing here ever prints any part of the key, including its length.
"""
from __future__ import annotations

import argparse
import getpass
import sys
import warnings

import keyring

from studio import redact
from studio.config import load_settings
from studio.secrets_store import KEYRING_GEMINI, KEYRING_SERVICE, MissingSecret, load_gemini_key


def stored() -> bool:
    return bool(keyring.get_password(KEYRING_SERVICE, KEYRING_GEMINI))


def ask_hidden() -> str:
    # getpass falls back to visible typing on a terminal it can't control. Treat that as a failure.
    warnings.simplefilter("error", getpass.GetPassWarning)
    if sys.stdin is not sys.__stdin__ or not sys.stdin.isatty():
        raise SystemExit("Run this in a normal PowerShell window so the key stays hidden while you paste it.")
    try:
        return getpass.getpass("Paste your Gemini API key (nothing will appear), then press Enter: ").strip()
    except getpass.GetPassWarning:
        raise SystemExit("This terminal can't hide typing. Run it in a normal PowerShell window.") from None


def set_key() -> int:
    key = ask_hidden()
    if not key or any(c.isspace() for c in key):
        print("That doesn't look like a key, so nothing was stored.")
        return 1
    keyring.set_password(KEYRING_SERVICE, KEYRING_GEMINI, key)
    print("Key stored in Windows Credential Manager.")
    print("Next: python -m scripts.set_gemini_key --verify")
    return 0


def verify() -> int:
    from google import genai

    settings = load_settings()
    try:
        client = genai.Client(api_key=load_gemini_key(settings))
        available = {m.name.removeprefix("models/") for m in client.models.list()}
    except MissingSecret:
        print("No key is stored yet.")
        return 1
    except Exception as exc:
        print(f"The key didn't work: {type(exc).__name__}: {redact.redact(exc)[:300]}")
        return 1
    print("The key works.")
    wanted = {
        "Nano Banana Pro": settings.image_model_pro,
        "Nano Banana 2.1": settings.image_model_21,
        "Omni Flash 1.1": settings.video_model,
        "Text helper (AI copy, pin analysis)": settings.text_model,
    }
    missing = 0
    for label, model_id in wanted.items():
        ok = model_id in available
        missing += not ok
        print(f"  {'available' if ok else 'NOT FOUND'}  {label} ({model_id})")
    return 1 if missing else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--delete", action="store_true")
    args = parser.parse_args()
    if args.status:
        print("A key is stored." if stored() else "No key is stored yet.")
        return 0
    if args.verify:
        return verify()
    if args.delete:
        if stored():
            keyring.delete_password(KEYRING_SERVICE, KEYRING_GEMINI)
        print("No key is stored now.")
        return 0
    return set_key()


if __name__ == "__main__":
    raise SystemExit(main())
