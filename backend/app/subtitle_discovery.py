"""Human-triggered and opt-in automatic subtitle discovery in the track task lane."""

from sqlalchemy import select

from backend.app.track_choices import source_key, source_peers, track_rows
from shared.models import MovieJob, SourceTrackChoices, Task
from shared.state import TRACK_EDIT_STAGES, Stage
from shared.subtitle_discovery import DiscoveryPolicy, missing_languages
from shared.tracks import track_analysis_complete


def active_discovery(db, job):
    return db.scalar(
        select(Task)
        .join(MovieJob, MovieJob.id == Task.job_id)
        .where(
            *source_peers(job),
            Task.type.in_(["discover_subtitles", "review_uploaded_subtitle"]),
            Task.status.in_(["QUEUED", "RUNNING"]),
        )
        .order_by(Task.created_at)
    )


def status(db, job):
    from backend.app.remux import remux_status
    from backend.app.subtitle_removal import removal_status

    policy = DiscoveryPolicy.model_validate(job.analysis.get("subtitle_discovery_policy", {}))
    record = db.get(SourceTrackChoices, source_key(job))
    report = (record.data.get("subtitle_discovery") if record else None) or job.analysis.get(
        "subtitle_discovery"
    )
    originals = policy.original_languages or (report or {}).get("original_languages", [])
    active = active_discovery(db, job)
    return {
        "removal": removal_status(db, job),
        "active_task": {
            "id": active.id,
            "type": active.type,
            "job_id": active.job_id,
            "status": active.status,
            "progress": active.progress,
            "detail": active.progress_detail,
        }
        if active
        else None,
        "allowed": (job.state in TRACK_EDIT_STAGES and track_analysis_complete(job.analysis))
        or remux_status(db, job)["available"],
        "review_required": bool(
            record
            and record.data.get("discovery_version", 0) != record.data.get("confirmed_discovery_version", 0)
        ),
        "policy": policy.model_dump(),
        "missing": missing_languages([t.info for t in track_rows(db, job).values()], originals),
        "original_language_unknown": not originals,
        "report": report,
    }


def request_discovery(db, job, policy=None, *, manual=True):
    from backend.app.services import enqueue

    active = active_discovery(db, job) or db.scalar(
        select(Task).where(
            Task.job_id == job.id, Task.lane == "tracks", Task.status.in_(["QUEUED", "RUNNING"])
        )
    )
    if active:
        if active.type == "discover_subtitles":
            return active
        raise ValueError("Wait for the current track review to finish")
    if not status(db, job)["allowed"]:
        raise ValueError("Wait for track analysis or for the completed video to be ready for remux")
    if policy is not None:
        job.analysis = {**job.analysis, "subtitle_discovery_policy": policy.model_dump()}
    job.analysis = {**job.analysis, "subtitle_discovery_refresh": manual}
    return enqueue(db, job, stage=Stage.FINDING_SUBTITLES)


def ensure_discovery(db, job):
    policy = DiscoveryPolicy.model_validate(job.analysis.get("subtitle_discovery_policy", {}))
    if not policy.enabled or job.state not in TRACK_EDIT_STAGES or not status(db, job)["allowed"]:
        return False
    if db.scalar(select(Task.id).where(Task.job_id == job.id, Task.type == "discover_subtitles")):
        return False  # One automatic attempt; retry failures only at the user's request.
    if db.scalar(
        select(Task.id).where(
            Task.job_id == job.id, Task.lane == "tracks", Task.status.in_(["QUEUED", "RUNNING"])
        )
    ):
        return False
    request_discovery(db, job, manual=False)
    return True
