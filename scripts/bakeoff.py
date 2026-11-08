"""Tests both Nano Banana models on each template, so you can choose the winner for each one.

    python -m scripts.bakeoff plan                          what it would make; no calls, no cost
    python -m scripts.bakeoff run --only big-bite --yes     one template first (3 images)
    python -m scripts.bakeoff run --yes                     everything still missing
    python -m scripts.bakeoff pick big-bite=nano-banana-pro quiet-editorial=nano-banana-2.1

Every product and brand below is made up, so the public gallery samples show no real company.
Finished images are kept and never remade, so rerunning after a failure can't bill for them twice.
"""
from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from typing import Iterable, Optional

from PIL import Image, ImageDraw

from studio import media, redact
from studio.config import ROOT, load_settings
from studio.gemini import IMAGE_MODELS, Gateway, GenerationError, ImageRef, RealGateway
from studio.secrets_store import MissingSecret
from studio.templates_store import SAMPLES_DIR, TEMPLATES_DIR, Template, build_image_prompt, load_templates

OUT = ROOT / ".local" / "bakeoff"
PRODUCT_SHOT_MODEL = "nano-banana-2.1"
SAMPLE_LONG_SIDE = 1400

SAMPLES = {
    "big-bite": {
        "shot": "a juicy smash burger with melted cheddar in a plain sesame bun",
        "BRAND": "Ollo Burger Co", "PRODUCT": "smash burger with melted cheddar", "CATEGORY": "Burgers",
        "HEADLINE": "Bite first. Talk later.", "SUBHEAD": "Smashed to order", "CTA": "Order now",
    },
    "quiet-editorial": {
        "shot": "a pair of hammered gold hoop earrings with no hallmark or logo",
        "BRAND": "Maren Atelier", "PRODUCT": "hammered gold hoop earrings", "CATEGORY": "Fine jewelry",
        "HEADLINE": "Worn quietly. Noticed anyway.", "SUBHEAD": "Hammered gold hoops", "CTA": "Discover",
    },
    "hand-held-spotlight": {
        "shot": "a flat screenshot of a fictional budgeting app home screen with a simple chart, large balance and no real bank names or logos",
        "BRAND": "Tilt", "PRODUCT": "budgeting app", "CATEGORY": "Personal finance",
        "HEADLINE": "Your money, at a glance.", "SUBHEAD": "Every account in one calm view", "CTA": "Get the app",
    },
    "giant-type": {
        "shot": "a black cast-iron kettlebell with no markings or logo",
        "BRAND": "Forge Athletic", "PRODUCT": "black cast-iron kettlebell", "CATEGORY": "Strength gear",
        "HEADLINE": "HEAVY ON PURPOSE", "SUBHEAD": "Cast iron kettlebells", "CTA": "Shop gear",
    },
    "on-the-move": {
        "shot": "a sage green hard-shell cabin suitcase with no logo",
        "BRAND": "Waylark", "PRODUCT": "sage green hard-shell cabin suitcase", "CATEGORY": "Luggage",
        "HEADLINE": "Pack light. Go far.", "SUBHEAD": "Hard-shell cabin suitcase", "CTA": "See the range",
    },
}


def shot_prompt(description: str) -> str:
    return (f"A clean, realistic studio product photograph of {description}, centred on a plain light grey background "
            "with soft even light. No text, no people and no props.")


def stem(path_dir: Path, name: str) -> Optional[Path]:
    found = sorted(path_dir.glob(f"{name}.*"))
    return found[0] if found else None


def steps_for(template: Template, out: Path) -> list[tuple[str, str]]:
    """(file stem, model key) for everything this template still needs."""
    folder = out / template.id
    wanted = [("product", PRODUCT_SHOT_MODEL)] + [(key, key) for key in IMAGE_MODELS]
    return [(name, model) for name, model in wanted if not stem(folder, name)]


def save(folder: Path, name: str, data: bytes) -> Path:
    # Sniffing rejects anything that isn't really an image before it is written.
    mime = media.sniff_image_mime(data)
    ext = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}[mime]
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{name}.{ext}"
    path.write_bytes(data)
    return path


def as_ref(path: Path) -> ImageRef:
    data = path.read_bytes()
    return ImageRef(data, media.sniff_image_mime(data))


def make_template(template: Template, gateway: Gateway, out: Path) -> list[str]:
    """Makes what's missing, product shot first. Returns the failures as public messages."""
    sample = SAMPLES[template.id]
    folder = out / template.id
    failures = []
    if not stem(folder, "product"):
        try:
            data, _ = gateway.generate_image(PRODUCT_SHOT_MODEL, shot_prompt(sample["shot"]), [], "1:1", "1K")
            save(folder, "product", data)
            print(f"  {template.id}: product shot done")
        except GenerationError as exc:
            # No product shot means nothing else can be made, so nothing more is spent.
            return [f"{template.id} product shot: {exc.public_message}"]
    refs = [as_ref(stem(folder, "product"))]
    reference = template.reference_image()
    if reference:
        refs.append(reference)
    fields = {k: v for k, v in sample.items() if k != "shot"}
    fields["COLORS"] = template.default_colors
    prompt = build_image_prompt(template, fields, has_logo=False, has_reference=bool(reference))
    for model in IMAGE_MODELS:
        if stem(folder, model):
            continue
        try:
            data, _ = gateway.generate_image(model, prompt, refs, template.aspect, "1K")
            save(folder, model, data)
            print(f"  {template.id}: {IMAGE_MODELS[model]['label']} done")
        except GenerationError as exc:
            failures.append(f"{template.id} {IMAGE_MODELS[model]['label']}: {exc.public_message}")
    make_comparison(template, out)
    return failures


def make_comparison(template: Template, out: Path) -> Optional[Path]:
    """Side by side: the made-up product, then each model's render."""
    folder = out / template.id
    panels = [("Made-up product", stem(folder, "product"))] + [(IMAGE_MODELS[k]["label"], stem(folder, k)) for k in IMAGE_MODELS]
    panels = [(label, path) for label, path in panels if path]
    if not panels:
        return None
    height, gap, header = 900, 20, 44
    images = []
    for _, path in panels:
        with Image.open(path) as im:
            rgb = im.convert("RGB")
        images.append(rgb.resize((round(rgb.width * height / rgb.height), height)))
    sheet = Image.new("RGB", (sum(i.width for i in images) + gap * (len(images) + 1), height + header + gap), (18, 18, 18))
    draw = ImageDraw.Draw(sheet)
    x = gap
    for (label, _), im in zip(panels, images):
        draw.text((x, 14), label, fill=(240, 240, 240), font_size=22)
        sheet.paste(im, (x, header))
        x += im.width + gap
    path = out / f"{template.id}-compare.jpg"
    sheet.save(path, "JPEG", quality=88)
    return path


def pick(templates: dict[str, Template], choices: Iterable[str], out: Path = OUT, templates_dir: Path = TEMPLATES_DIR,
         samples_dir: Path = SAMPLES_DIR) -> None:
    """Stores the winning model on the template and publishes its render as the gallery sample."""
    parsed = []
    for choice in choices:
        template_id, _, model = choice.partition("=")
        if template_id not in templates:
            raise SystemExit(f"Unknown template: {template_id}")
        if model not in IMAGE_MODELS:
            raise SystemExit(f"Unknown model for {template_id}. Use one of: {', '.join(IMAGE_MODELS)}")
        render = stem(out / template_id, model)
        if not render:
            raise SystemExit(f"There's no {model} render for {template_id} yet.")
        parsed.append((template_id, model, render))
    samples_dir.mkdir(parents=True, exist_ok=True)
    for template_id, model, render in parsed:
        # Re-encoding drops any metadata. Only our own renders ever reach this public folder.
        with Image.open(render) as im:
            sample = im.convert("RGB")
        sample.thumbnail((SAMPLE_LONG_SIDE, SAMPLE_LONG_SIDE))
        sample.save(samples_dir / f"{template_id}.jpg", "JPEG", quality=86, optimize=True)
        path = templates_dir / f"{template_id}.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["model"], data["model_status"] = model, "tested"
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"{template_id}: {IMAGE_MODELS[model]['label']} is the winner; gallery sample updated")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("plan")
    run = sub.add_parser("run")
    run.add_argument("--only", default="", help="comma-separated template ids")
    run.add_argument("--yes", action="store_true", help="make the paid calls")
    chooser = sub.add_parser("pick")
    chooser.add_argument("choices", nargs="+", metavar="template=model")
    args = parser.parse_args()

    redact.install()
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    templates = load_templates()
    if args.command == "pick":
        pick(templates, args.choices)
        return 0

    only = {x for x in getattr(args, "only", "").split(",") if x}
    if only - set(templates):
        raise SystemExit(f"Unknown template: {', '.join(sorted(only - set(templates)))}")
    chosen = [t for t in templates.values() if not only or t.id in only]
    total = 0
    for t in chosen:
        steps = steps_for(t, OUT)
        total += len(steps)
        print(f"{t.id}: {', '.join(name for name, _ in steps) or 'nothing left to make'}")
    print(f"That's {total} paid image{'' if total == 1 else 's'}.")
    if args.command == "plan" or not total:
        return 0
    if not args.yes:
        print("Nothing was made. Add --yes to run it.")
        return 0

    settings = load_settings()
    gateway = RealGateway(settings)
    failures: list[str] = []
    try:
        for t in chosen:
            failures += make_template(t, gateway, OUT)
    except MissingSecret:
        print("No key is stored yet. Run: python -m scripts.set_gemini_key")
        return 1
    for failure in failures:
        print(f"FAILED {failure}")
    print(f"Comparisons are in {OUT}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
