"""Explicit encoding revisions retain old outputs until replacements succeed."""

from uuid import uuid4

from sqlalchemy import delete, select

from backend.app import queue
from shared.config import get_settings
from shared.encoding import is_smoke_test
from shared.models import EncodeConfig, Screenshot, Task, now
from shared.paths import contained
from shared.state import STAGES, Stage


def status(db, job):
    task = db.scalar(
        select(Task)
        .where(Task.job_id == job.id, Task.type == "encode")
        .order_by(Task.created_at.desc(), Task.id.desc())
    )
    if not task or task.status != "SUCCEEDED" or is_smoke_test(job):
        return {"available": False, "reason": "A completed full encode is required."}
    if job.state not in STAGES or STAGES.index(job.state) < STAGES.index(Stage.VALIDATING_ENCODE):
        return {"available": False, "reason": "Finish encoding before starting a replacement."}
    if db.scalar(select(Task.id).where(Task.job_id == job.id, Task.status == "RUNNING")):
        return {
            "available": False,
            "reason": "Wait for active work to finish, or cancel it and wait for it to stop.",
        }
    if not db.scalar(select(EncodeConfig.id).where(EncodeConfig.job_id == job.id)):
        return {"available": False, "reason": "The saved encoding configuration is missing."}
    try:
        source = contained(get_settings().source_root, job.source_path, exists=True)
        stat = source.stat()
        if stat.st_size != job.source_size or str(stat.st_mtime_ns) != job.source_mtime_ns:
            raise ValueError("Source changed")
    except (ValueError, OSError):
        return {"available": False, "reason": "The unchanged original source must be available."}
    return {"available": True, "reason": None}


def request(db, job_id, target):
    from backend.app.services import enqueue, event, get_job

    queue.settings(db, lock=True)
    job = get_job(db, job_id, lock=True)
    available = status(db, job)
    if not available["available"]:
        raise ValueError(available["reason"])
    config = db.scalar(select(EncodeConfig).where(EncodeConfig.job_id == job.id))
    profile = config.data["profile_snapshot"]
    if target.rate_control == "crf" and not profile["crf_min"] <= target.crf <= profile["crf_max"]:
        raise ValueError("CRF is outside profile limits")
    revision = str(uuid4())
    previous = {
        "encode_config": config.data,
        "validation": job.validation,
        **{key: job.analysis.get(key) for key in ("encoded_path", "final_path", "release_result")},
    }
    for task in db.scalars(
        select(Task)
        .where(Task.job_id == job.id, Task.lane == "pipeline", Task.status == "QUEUED")
        .with_for_update()
    ):
        task.status, task.cancel_requested, task.finished_at = "CANCELLED", True, now()
        event(db, job.id, "task_cancelled", task_id=task.id, reason="Superseded by re-encode")
    config.data = {
        **{k: v for k, v in config.data.items() if k not in ("rate_control", "crf", "bitrate_kbps")},
        **target.model_dump(exclude_none=True),
        "selected_by": "user",
        "selected_at": now().isoformat(),
    }
    stale = {
        "encoded_path",
        "encoded_video",
        "encoder_average_qp",
        "candidate_index",
        "review_shortlisted_ids",
        "screenshot_selection",
        "screenshot_scan_decoder",
        "release_result",
        "encoding_skipped",
    }
    job.analysis = {
        **{k: v for k, v in job.analysis.items() if k not in stale},
        "encode_revision": {"id": revision, "previous": previous, "requested_at": now().isoformat()},
        "remux_revision": {"id": revision, "reuse_screenshots": False},
    }
    # Encoded picture types and comparison images must be verified again.
    db.execute(delete(Screenshot).where(Screenshot.job_id == job.id))
    job.validation = {}
    job.state, job.completed_at = Stage.ENCODING.value, None
    enqueue(db, job)
    event(db, job.id, "reencode_requested", revision=revision, target=target.model_dump(exclude_none=True))
    db.commit()
    return job
