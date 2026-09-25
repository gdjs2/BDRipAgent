"""Refresh sibling screenshot galleries after their source pool grows."""

import json

from sqlalchemy import select

from backend.app.services import enqueue
from backend.app.track_choices import source_key
from shared.config import get_settings
from shared.models import MovieJob, Task
from shared.state import SCREENSHOT_SYNC_STATES, Stage


def ensure_shared_screenshots(db):
    root = get_settings().cache_root / "screenshots"
    for job in db.scalars(
        select(MovieJob)
        .where(MovieJob.deleted_at.is_(None), MovieJob.state.in_(SCREENSHOT_SYNC_STATES))
        .with_for_update(skip_locked=True)
    ):
        if not job.analysis.get("candidate_index"):
            continue  # The initial sampling stage imports the complete shared pool later.
        if db.scalar(select(Task.id).where(Task.job_id == job.id, Task.status.in_(["QUEUED", "RUNNING"]))):
            continue
        path = root / source_key(job) / "pool.json"
        if not path.exists():
            continue
        revision = json.loads(path.read_text())["revision"]
        if revision <= job.analysis.get("screenshot_shared_revision", 0):
            continue
        latest = db.scalar(
            select(Task)
            .where(Task.job_id == job.id, Task.type == "sync_screenshots")
            .order_by(Task.created_at.desc())
            .limit(1)
        )
        if latest and latest.status in ("FAILED", "CANCELLED"):
            continue  # Keep failure visible and allow explicit retry; never loop on a broken output.
        enqueue(db, job, stage=Stage.SYNCING_SCREENSHOTS)
