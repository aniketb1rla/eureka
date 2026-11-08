"""Tables and the atomic allowance logic. Works on SQLite (local) and Postgres (Supabase)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import (JSON, Boolean, Column, DateTime, Index, Integer, MetaData, String, Table, Text, create_engine,
                        event, func, insert, select, text, update)
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from .config import Settings

metadata = MetaData()

users = Table(
    "studio_users", metadata,
    Column("id", String(64), primary_key=True),
    Column("email", String(320), nullable=False),
    Column("images_used", Integer, nullable=False, default=0),
    Column("videos_used", Integer, nullable=False, default=0),
    Column("image_allowance", Integer, nullable=False),
    Column("video_allowance", Integer, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

daily_usage = Table(
    "studio_daily_usage", metadata,
    Column("day", String(10), primary_key=True),
    Column("images", Integer, nullable=False, default=0),
    Column("videos", Integer, nullable=False, default=0),
)

generations = Table(
    "studio_generations", metadata,
    Column("id", String(32), primary_key=True),
    Column("user_id", String(64), nullable=False),
    Column("kind", String(8), nullable=False),      # image | video
    Column("source", String(12), nullable=False),   # template | free | animate
    Column("template_id", String(64)),
    Column("parent_id", String(32)),
    Column("model", String(40), nullable=False),
    Column("status", String(12), nullable=False),   # queued | running | succeeded | failed
    Column("params", JSON, nullable=False),
    Column("input_keys", JSON, nullable=False),
    Column("output_key", String(255)),
    Column("output_mime", String(40)),
    Column("error_public", Text),
    Column("refunded", Boolean, nullable=False, default=False),
    Column("deleted", Boolean, nullable=False, default=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("started_at", DateTime(timezone=True)),
    Column("finished_at", DateTime(timezone=True)),
    Column("lease_until", DateTime(timezone=True)),
    Index("ix_gen_user_created", "user_id", "created_at"),
    Index("ix_gen_status_created", "status", "created_at"),
)

rate_events = Table(
    "studio_rate_events", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("bucket", String(120), nullable=False),
    Column("at", DateTime(timezone=True), nullable=False),
    Index("ix_rate_bucket_at", "bucket", "at"),
)


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime) -> str:
    """Every stored time is UTC, but SQLite drops the timezone, so browsers would read it as local time."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def build_engine(s: Settings) -> Engine:
    url = s.database_url
    if url:
        # Supabase hands out postgresql:// URLs; SQLAlchemy needs the psycopg 3 driver named.
        if url.startswith("postgresql://"):
            url = "postgresql+psycopg://" + url[len("postgresql://"):]
    else:
        s.data_dir.mkdir(parents=True, exist_ok=True)
        url = f"sqlite:///{(s.data_dir / 'studio.db').as_posix()}"
    if url.startswith("sqlite"):
        engine = create_engine(url, connect_args={"check_same_thread": False, "timeout": 20})

        @event.listens_for(engine, "connect")
        def _on_connect(conn, _):
            conn.isolation_level = None  # hand transaction control to SQLAlchemy's BEGIN below
            cur = conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.close()

        @event.listens_for(engine, "begin")
        def _begin_immediate(conn):
            # Take the write lock up front so concurrent allowance updates serialize.
            conn.exec_driver_sql("BEGIN IMMEDIATE")
    else:
        engine = create_engine(url, pool_size=5, max_overflow=5, pool_pre_ping=True, hide_parameters=True)
    if not s.is_production:
        metadata.create_all(engine)
    return engine


def ensure_user(engine: Engine, s: Settings, user_id: str, email: str) -> None:
    with engine.begin() as c:
        if c.execute(select(users.c.id).where(users.c.id == user_id)).first():
            return
        try:
            with c.begin_nested():
                c.execute(insert(users).values(
                    id=user_id, email=email, images_used=0, videos_used=0,
                    image_allowance=s.free_image_allowance, video_allowance=s.free_video_allowance, created_at=now(),
                ))
        except IntegrityError:
            pass  # a parallel first request created the row


class LimitReached(Exception):
    def __init__(self, code: str, public_message: str):
        super().__init__(public_message)
        self.code = code
        self.public_message = public_message


def reserve_and_create(engine: Engine, s: Settings, row: dict) -> None:
    """Atomically: take from the allowance, check concurrency, count the daily cap, create the job.
    Any failed check rolls the whole transaction back, so racing requests can't overrun a limit."""
    kind = row["kind"]
    if kind == "image":
        used, allowance, day_col, cap = users.c.images_used, users.c.image_allowance, daily_usage.c.images, s.global_daily_image_cap
    else:
        used, allowance, day_col, cap = users.c.videos_used, users.c.video_allowance, daily_usage.c.videos, s.global_daily_video_cap
    day = now().strftime("%Y-%m-%d")
    hour_ago = now() - timedelta(hours=1)
    with engine.begin() as c:
        # This UPDATE also locks the user's row, so one user's requests run one at a time.
        got = c.execute(
            update(users).where(users.c.id == row["user_id"], used < allowance).values({used.name: used + 1})
        ).rowcount
        if got != 1:
            raise LimitReached("allowance", f"You've used your free {kind} allowance.")
        active = c.execute(select(func.count()).select_from(generations).where(
            generations.c.user_id == row["user_id"], generations.c.status.in_(("queued", "running")))).scalar_one()
        if active >= s.max_active_jobs:
            raise LimitReached("busy", f"You already have {active} generations running. Wait for one to finish, then try again.")
        recent = c.execute(select(func.count()).select_from(generations).where(
            generations.c.user_id == row["user_id"], generations.c.created_at >= hour_ago)).scalar_one()
        if recent >= s.generations_per_hour:
            raise LimitReached("rate", "You've made a lot in the last hour. Please try again a little later.")
        if not c.execute(select(daily_usage.c.day).where(daily_usage.c.day == day)).first():
            try:
                with c.begin_nested():
                    c.execute(insert(daily_usage).values(day=day, images=0, videos=0))
            except IntegrityError:
                pass  # another request created today's row first
        got = c.execute(
            update(daily_usage).where(daily_usage.c.day == day, day_col < cap).values({day_col.name: day_col + 1})
        ).rowcount
        if got != 1:
            raise LimitReached("daily_cap", "Generation is paused for today because the studio hit its daily limit. It resets at midnight UTC.")
        c.execute(insert(generations).values(**row))


def refund(engine: Engine, gen_id: str) -> None:
    """Return the user's allowance once for a failed job. The daily cap stays counted on purpose:
    it is a spend ceiling, and a failed call may still have cost money."""
    with engine.begin() as c:
        got = c.execute(update(generations).where(generations.c.id == gen_id, generations.c.refunded.is_(False))
                        .values(refunded=True)).rowcount
        if got != 1:
            return
        g = c.execute(select(generations.c.user_id, generations.c.kind).where(generations.c.id == gen_id)).first()
        col = users.c.images_used if g.kind == "image" else users.c.videos_used
        c.execute(update(users).where(users.c.id == g.user_id, col > 0).values({col.name: col - 1}))


def hit_rate_limit(engine: Engine, bucket: str, limit: int, window: timedelta) -> bool:
    """Record an event; True if over the limit. Stored in the database so it holds across instances."""
    cutoff = now() - window
    with engine.begin() as c:
        count = c.execute(select(func.count()).select_from(rate_events).where(
            rate_events.c.bucket == bucket, rate_events.c.at >= cutoff)).scalar_one()
        if count >= limit:
            return True
        c.execute(insert(rate_events).values(bucket=bucket, at=now()))
        return False


def purge_rate_events(engine: Engine) -> None:
    with engine.begin() as c:
        c.execute(rate_events.delete().where(rate_events.c.at < now() - timedelta(days=2)))


def health(engine: Engine) -> bool:
    with engine.connect() as c:
        return c.execute(text("select 1")).scalar_one() == 1
