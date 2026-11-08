"""Background generation. Jobs live in the database, so any instance can pick them up.

A job is claimed atomically and never retried automatically: a retry could bill twice. If an instance
dies mid-job, its lease runs out, the job is marked failed and the user's allowance is returned.
"""
from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from sqlalchemy import select, update

from . import db, media, redact
from .config import Settings
from .gemini import FAILED, Gateway, GenerationError, ImageRef
from .secrets_store import MissingSecret
from .storage import Storage
from .templates_store import Template

log = logging.getLogger("studio.jobs")

IMAGE_LEASE = timedelta(minutes=6)
VIDEO_LEASE = timedelta(minutes=25)
NOT_SET_UP = "The studio isn't fully set up yet, so nothing was made. Your allowance wasn't used."
LOST = "This one was interrupted before it finished. Your allowance wasn't used, so please try again."
MP4_MARKER = b"ftyp"


class Worker:
    def __init__(self, engine, settings: Settings, gateway: Gateway, storage: Storage, templates: dict[str, Template]):
        self.engine = engine
        self.s = settings
        self.gateway = gateway
        self.storage = storage
        self.templates = templates
        self._stop = threading.Event()
        self._pool = ThreadPoolExecutor(max_workers=settings.worker_threads, thread_name_prefix="studio-job")
        self._slots = threading.Semaphore(settings.worker_threads)
        self._thread = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="studio-worker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        self._pool.shutdown(wait=False)

    def _loop(self) -> None:
        last_sweep = 0.0
        while not self._stop.is_set():
            try:
                if time.time() - last_sweep > 30:
                    self.sweep_expired()
                    db.purge_rate_events(self.engine)
                    last_sweep = time.time()
                worked = self.run_available()
            except Exception as exc:
                log.warning("worker loop error: %s", redact.redact(exc)[:300])
                worked = False
            self._stop.wait(0.2 if worked else 1.0)

    def run_available(self) -> bool:
        if not self._slots.acquire(blocking=False):
            return False
        gen_id = self.claim_next()
        if not gen_id:
            self._slots.release()
            return False

        def task():
            try:
                self.process(gen_id)
            finally:
                self._slots.release()

        self._pool.submit(task)
        return True

    def claim_next(self):
        with self.engine.begin() as c:
            row = c.execute(select(db.generations.c.id, db.generations.c.kind)
                            .where(db.generations.c.status == "queued")
                            .order_by(db.generations.c.created_at).limit(1)).first()
            if not row:
                return None
            lease = VIDEO_LEASE if row.kind == "video" else IMAGE_LEASE
            got = c.execute(update(db.generations)
                            .where(db.generations.c.id == row.id, db.generations.c.status == "queued")
                            .values(status="running", started_at=db.now(), lease_until=db.now() + lease)).rowcount
            return row.id if got == 1 else None

    def sweep_expired(self) -> None:
        with self.engine.begin() as c:
            expired = c.execute(select(db.generations.c.id).where(
                db.generations.c.status == "running", db.generations.c.lease_until < db.now())).fetchall()
        for row in expired:
            self._fail(row.id, LOST)

    def _fail(self, gen_id: str, public_message: str) -> None:
        with self.engine.begin() as c:
            c.execute(update(db.generations).where(db.generations.c.id == gen_id).values(
                status="failed", error_public=public_message, finished_at=db.now()))
        db.refund(self.engine, gen_id)

    def process(self, gen_id: str) -> None:
        try:
            self._run(gen_id)
        except GenerationError as exc:
            self._fail(gen_id, exc.public_message)
        except MissingSecret:
            log.error("generation %s failed: Gemini key is not configured", gen_id)
            self._fail(gen_id, NOT_SET_UP)
        except Exception as exc:
            log.warning("generation %s failed: %s %s", gen_id, type(exc).__name__, redact.redact(exc)[:300])
            self._fail(gen_id, FAILED)

    def _run(self, gen_id: str) -> None:
        with self.engine.connect() as c:
            g = c.execute(select(db.generations).where(db.generations.c.id == gen_id)).first()
        refs = [ImageRef(self.storage.get(item["key"]), item["mime"]) for item in g.input_keys]
        p = g.params
        if g.kind == "image":
            if g.template_id and p.get("use_reference"):
                t = self.templates.get(g.template_id)
                ref = t.reference_image() if t else None
                if ref:
                    refs.append(ref)
            data, _ = self.gateway.generate_image(g.model, p["prompt"], refs, p["aspect"], p.get("resolution", "1K"))
            mime = media.sniff_image_mime(data)
            ext = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}[mime]
        else:
            data = self.gateway.generate_video(p["prompt"], refs, p["aspect"], p.get("resolution", "720p"), int(p.get("duration", 8)))
            if MP4_MARKER not in data[:32]:
                raise GenerationError(FAILED)
            mime, ext = "video/mp4", "mp4"
        key = f"users/{g.user_id}/outputs/{gen_id}.{ext}"
        self.storage.put(key, data, mime)
        with self.engine.begin() as c:
            c.execute(update(db.generations).where(db.generations.c.id == gen_id).values(
                status="succeeded", output_key=key, output_mime=mime, finished_at=db.now()))
