"""Disposable PostgreSQL/Redis/Celery check for separate encoder lifetimes.

The harness replaces media work with a short measured wait. It must only run
against worker_isolation_test; it does not read or encode a user's movie.
"""

import sys
import time
from pathlib import Path

from sqlalchemy import select

import worker.tasks as task_worker
from backend.app.services import enqueue
from shared.config import get_settings
from shared.db import Base, engine, session
from shared.models import MovieJob, QueueSettings, Task, now
from worker.pipeline.stages import HANDLERS
from worker.tasks import celery as celery


def fixture_work(ctx):
    with session() as db:
        seconds = 35 if db.get(Task, ctx.task_id).type == "encode" else 1
    for index in range(seconds):
        ctx.check()
        ctx.progress(index * 100 / seconds, phase="Container isolation fixture")
        time.sleep(1)


def advance_fixture(db, job):
    job.state = "WAITING_FOR_ENCODE_SELECTION"


for kind in ("encode", "crf_analysis", "analyze"):
    HANDLERS[kind] = fixture_work
task_worker.advance = advance_fixture


def add_job(kind):
    settings = get_settings()
    source = settings.source_root / "Fixture.mkv"
    stages = {"encode": "ENCODING", "crf_analysis": "RUNNING_CRF_ANALYSIS", "analyze": "ANALYZING_SOURCE"}
    with session() as db:
        job = MovieJob(
            source_path=source.name,
            source_size=source.stat().st_size,
            source_mtime_ns=str(source.stat().st_mtime_ns),
            title="Isolation fixture",
            year=2026,
            release_name="Isolation.fixture",
            state=stages[kind],
            analysis={"tracks": [], "track_review_version": 1},
        )
        db.add(job)
        db.flush()
        enqueue(db, job)
        db.commit()


def main():
    if not get_settings().database_url.endswith("/worker_isolation_test"):
        raise RuntimeError("Use only the dedicated worker_isolation_test database")
    action = sys.argv[1]
    if action == "setup":
        Base.metadata.create_all(engine())
        get_settings().source_root.mkdir(parents=True, exist_ok=True)
        (get_settings().source_root / "Fixture.mkv").write_bytes(b"fixture")
        with session() as db:
            db.add(QueueSettings(id=1, paused=False, max_encoding_tasks=1, max_other_tasks=1))
            db.commit()
        for kind in ("encode", "analyze", "crf_analysis"):
            add_job(kind)
        return
    if action == "add-other":
        add_job("analyze")
        return
    deadline = time.monotonic() + 55
    while time.monotonic() < deadline:
        with session() as db:
            tasks = db.scalars(select(Task).order_by(Task.created_at)).all()
            assert not any(t.status == "FAILED" for t in tasks), [(t.type, t.error_message) for t in tasks]
            encoded = next(t for t in tasks if t.type == "encode")
            if (
                action == "ready"
                and encoded.status == "RUNNING"
                and all(t.status == "SUCCEEDED" for t in tasks if t.type != "encode")
            ):
                Path("/evidence/encoder-owner").write_text(encoded.worker_id)
                print("Encoder running; source scan and CRF completed in their own workers.", flush=True)
                return
            if action == "alive":
                assert encoded.status == "RUNNING"
                assert encoded.worker_id == Path("/evidence/encoder-owner").read_text()
                assert (now() - encoded.heartbeat_at).total_seconds() < 10
                print("Encoder retains its lease while the general container is stopped.", flush=True)
                return
            if action == "finished" and all(t.status == "SUCCEEDED" for t in tasks):
                owner = Path("/evidence/encoder-owner").read_text()
                assert all(t.worker_id == owner for t in tasks if t.type == "encode")
                assert all(t.worker_id != owner for t in tasks if t.type in ("analyze", "crf_analysis"))
                general_owners = {t.worker_id for t in tasks if t.type == "analyze"}
                assert len(general_owners) == 2
                assert all(
                    t.worker_id not in general_owners | {owner} for t in tasks if t.type == "crf_analysis"
                )
                print(
                    "PASS: encoder survived general-container replacement; encoding, CRF, and other jobs used separate workers.",
                    flush=True,
                )
                return
        time.sleep(0.5)
    raise AssertionError("Timed out waiting for the isolation fixture")


if __name__ == "__main__":
    main()
