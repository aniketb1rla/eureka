"""Copies a secret into Google Secret Manager without it appearing on screen or in shell history.

    python -m scripts.push_secret gemini-api-key --from-credential-manager
    python -m scripts.push_secret studio-database-url
    python -m scripts.push_secret supabase-service-key

Run it yourself. The value goes straight into gcloud's standard input and is never written to disk.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys

from scripts.set_gemini_key import ask_hidden
from studio.secrets_store import KEYRING_GEMINI, KEYRING_SERVICE


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("name", choices=["gemini-api-key", "studio-database-url", "supabase-service-key"])
    parser.add_argument("--from-credential-manager", action="store_true", help="use the Gemini key already stored on this computer")
    parser.add_argument("--project", default="", help="Google Cloud project id, if it isn't your gcloud default")
    args = parser.parse_args()

    if args.from_credential_manager:
        if args.name != "gemini-api-key":
            raise SystemExit("Only the Gemini key is kept in Credential Manager.")
        import keyring

        value = (keyring.get_password(KEYRING_SERVICE, KEYRING_GEMINI) or "").strip()
    else:
        value = ask_hidden()
    if not value:
        raise SystemExit("There's no value to store, so nothing was sent.")

    gcloud = shutil.which("gcloud") or shutil.which("gcloud.cmd")
    if not gcloud:
        raise SystemExit("gcloud isn't installed or isn't on your PATH.")
    scope = [f"--project={args.project}"] if args.project else []
    exists = subprocess.run([gcloud, "secrets", "describe", args.name, *scope], capture_output=True).returncode == 0
    if not exists:
        created = subprocess.run([gcloud, "secrets", "create", args.name, "--replication-policy=automatic", *scope], capture_output=True)
        if created.returncode != 0:
            print(f"Couldn't create the secret {args.name}. Check you're signed in to gcloud with access to the project.", file=sys.stderr)
            return 1
    # Output is captured and dropped: gcloud has no reason to print the value, but nothing here relies on that.
    added = subprocess.run([gcloud, "secrets", "versions", "add", args.name, "--data-file=-", *scope],
                           input=value.encode("utf-8"), capture_output=True)
    if added.returncode != 0:
        print(f"Couldn't store a new version of {args.name}. Check you have access to the project.", file=sys.stderr)
        return 1
    print(f"Stored a new version of {args.name}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
