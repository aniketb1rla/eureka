# Eureka Studio

Make ads from stored creative templates with Nano Banana Pro and Nano Banana 2.1, animate them with Omni Flash 1.1, or
generate freely from a prompt. "Eureka Studio" is a working name.

## Run it on this computer

Test mode needs no key and costs nothing. Results are placeholders.

```bash
python -m venv .venv
```

```bash
.venv\Scripts\pip install -r requirements-dev.txt
```

```bash
.venv\Scripts\python -m uvicorn studio.main:app_factory --factory --host 127.0.0.1 --port 8010
```

## Use real models

Run this yourself in PowerShell. The key goes into Windows Credential Manager through a hidden prompt:

```bash
.venv\Scripts\python -m scripts.set_gemini_key
```

```bash
.venv\Scripts\python -m scripts.set_gemini_key --verify
```

To use real models in the app, create a `.env` file containing `GEMINI_MODE=real`. Each generation then costs money.
Never put the key in `.env`: the app refuses to start if it finds the key in its environment.

## Choose the model for each template

| Command | What it does | Cost |
| --- | --- | --- |
| `python -m scripts.bakeoff plan` | Lists what would be made | Free |
| `python -m scripts.bakeoff run --only big-bite --yes` | Tests one template | 3 images |
| `python -m scripts.bakeoff run --yes` | Makes everything still missing | Up to 15 images |
| `python -m scripts.bakeoff pick big-bite=nano-banana-pro` | Stores your winner and updates the gallery sample | Free |

Side-by-side comparisons are saved in `.local/bakeoff/`.

## Other commands

- `python -m pytest -q` runs all the tests. No key is needed, and nothing is billed.
- `python -m scripts.secret_scan` checks for secrets. It also runs before every commit.
- `python -m scripts.analyze_pins --yes` runs the Pinterest app's analyzer over the source pins, using 5 text-model calls.

To go live, follow [deploy/README.md](deploy/README.md).

## Where things live

| Folder | What's in it |
| --- | --- |
| `studio/` | The server. Only `gemini.py` talks to the models, and only `secrets_store.py` reads secrets. |
| `web/` | The browser app. It holds no secrets and talks only to this server. |
| `templates/` | The stored templates. Prompts stay on the server. |
| `references/` | The original Pinterest pins. They're never served or committed, and go only to the model as layout references. |
| `web/samples/` | Gallery samples, made only from our own renders of made-up products. |
