"""Durable source-wide release information and per-job generation snapshots."""

from copy import deepcopy
from datetime import UTC, datetime

from sqlalchemy import select

from backend.app.schemas import ReleaseDetails
from backend.app.track_choices import source_key
from shared.models import MovieJob, SourceReleaseDetails, now


def legacy_details(job):
    try:
        details = ReleaseDetails.model_validate(job.analysis.get("release_details")).model_dump()
    except ValueError:
        return None
    try:
        saved_at = datetime.fromisoformat(job.analysis["release_details_saved_at"])
    except (KeyError, TypeError, ValueError):
        saved_at = job.created_at
    if saved_at.tzinfo is None:
        saved_at = saved_at.replace(tzinfo=UTC)
    return saved_at, job.id, {"details": details, "source_job_id": job.id, "title": job.title}


def seed_existing_release_details(db):
    """Backfill the newest valid draft once per source. Caller holds the queue mutex."""
    existing = set(db.scalars(select(SourceReleaseDetails.source_key)))
    newest = {}
    # Include removed jobs: source information must outlive its original encoding job.
    for job in db.scalars(select(MovieJob)):
        key = source_key(job)
        if key in existing:
            continue
        entry = legacy_details(job)
        if entry and (key not in newest or entry[:2] > newest[key][:2]):
            newest[key] = entry
    for key, (saved_at, _, data) in newest.items():
        db.add(SourceReleaseDetails(source_key=key, revision=1, data=data, updated_at=saved_at))
    db.flush()
    return len(newest)


def analysis_with_release_defaults(db, job):
    """Return current shared form values; keep stored generation snapshots untouched."""
    analysis = dict(job.analysis)
    analysis.pop("shared_release_details", None)
    record = db.get(SourceReleaseDetails, source_key(job))
    if record:
        data, revision, updated_at = record.data, record.revision, record.updated_at
    else:
        # Read-only fallback for legacy imports after startup. The next save persists it.
        entries = [
            entry
            for peer in db.scalars(
                select(MovieJob).where(
                    MovieJob.source_path == job.source_path,
                    MovieJob.source_size == job.source_size,
                    MovieJob.source_mtime_ns == job.source_mtime_ns,
                )
            )
            if (entry := legacy_details(peer))
        ]
        if not entries:
            return analysis
        updated_at, _, data = max(entries, key=lambda entry: entry[:2])
        revision = 0
    donor = db.get(MovieJob, data["source_job_id"])
    analysis.update(
        release_details=deepcopy(data["details"]),
        shared_release_details={
            "revision": revision,
            "updated_at": updated_at.isoformat(),
            "source_job_id": data["source_job_id"],
            "source_job_available": bool(donor and donor.deleted_at is None),
            "title": data["title"],
            "snapshot_differs": bool(
                analysis.get("release_details") and analysis["release_details"] != data["details"]
            ),
        },
    )
    return analysis


def store_release_details(db, job, body):
    """Serialize saves with the queue mutex; generation reads only the job snapshot."""
    record = db.get(SourceReleaseDetails, source_key(job))
    revision = record.revision if record else 0
    if body.shared_revision is not None and body.shared_revision != revision:
        raise ValueError(
            "Release information changed in another encoding. Load the shared information before saving."
        )
    details = ReleaseDetails.model_validate(body.model_dump(exclude={"shared_revision"})).model_dump()
    saved_at = now()
    if record is None:
        record = SourceReleaseDetails(source_key=source_key(job), revision=0, data={})
        db.add(record)
    if record.data.get("details") != details:
        record.data = {"details": deepcopy(details), "source_job_id": job.id, "title": job.title}
        record.revision, record.updated_at = revision + 1, saved_at
    analysis = dict(job.analysis)
    if analysis.get("release_details") != details:
        analysis.pop("release_result", None)
    job.analysis = {
        **analysis,
        "release_details": deepcopy(details),
        "release_details_saved_at": saved_at.isoformat(),
        "release_details_revision": record.revision,
    }
    db.flush()
    return record
