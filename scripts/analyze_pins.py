"""Runs the Pinterest app's Gemini vision analyzer over each template's source pin.

    python -m scripts.analyze_pins          says what it would do; makes no calls
    python -m scripts.analyze_pins --yes    makes one text-model call per pin

Results go to .local/analysis/ for comparing against templates/*.json. The analysis is only a check:
nothing in the templates changes automatically. The pins go only to Gemini, never anywhere public.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys

from studio import redact
from studio.config import ROOT, load_settings
from studio.secrets_store import MissingSecret, load_gemini_key
from studio.templates_store import REFERENCES_DIR, load_templates

PINTEREST_APP = ROOT.parent / "pinterest-ref-collector"
OUT = ROOT / ".local" / "analysis"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--yes", action="store_true", help="make the paid calls")
    args = parser.parse_args()
    settings = load_settings()
    templates = [t for t in load_templates().values() if t.reference]
    if not args.yes:
        print(f"This would send {len(templates)} pins to {settings.text_model}, one call each. Add --yes to run it.")
        return 0
    try:
        key = load_gemini_key(settings)
    except MissingSecret:
        print("No key is stored yet. Run: python -m scripts.set_gemini_key")
        return 1

    sys.path.insert(0, str(PINTEREST_APP))
    from src.design_analyzer import gemini_analyze  # the Pinterest app's analyzer, unchanged

    OUT.mkdir(parents=True, exist_ok=True)
    failed = 0
    for t in templates:
        path = REFERENCES_DIR / t.reference
        pin = {"id": t.source_pin_id, "industry": t.industry}
        captured = io.StringIO()
        try:
            # The analyzer prints exception text, so its output is captured and redacted first.
            with contextlib.redirect_stdout(captured):
                result = gemini_analyze(pin, path.read_bytes(), api_key=key, model_name=settings.text_model)
        except Exception as exc:
            failed += 1
            print(f"{t.id}: failed ({type(exc).__name__}: {redact.redact(exc)[:300]})")
            continue
        finally:
            if captured.getvalue().strip():
                print(redact.redact(captured.getvalue()).strip())
        (OUT / f"{t.id}.json").write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"{t.id}: saved")
    print(f"Results are in {OUT}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
