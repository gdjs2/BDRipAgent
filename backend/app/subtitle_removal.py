"""Remove discovered tracks from the shared source inventory without deleting files."""

from copy import deepcopy

from sqlalchemy import select

from backend.app import queue
from backend.app.track_choices import seed_existing_choices, source_key, source_peers, track_rows
from shared.models import MovieJob, SourceTrackChoices, Task, TrackSelection, now
from shared.state import TRACK_EDIT_STAGES
from shared.subtitle_discovery import missing_languages


def removal_status(db, job):
    busy = db.scalar(
        select(Task.id)
        .join(MovieJob, MovieJob.id == Task.job_id)
        .where(
            *source_peers(job),
            Task.type.in_(["discover_subtitles", "review_uploaded_subtitle", "review_tracks", "prepare_tracks", "mux"]),
            Task.status.in_(["QUEUED", "RUNNING"]),
        )
    )
    return {
        "allowed": not busy,
        "reason": "Wait for subtitle discovery, track review or remux preparation for this source to finish (or cancel it first)."
        if busy
        else None,
    }


def remove_discovered(db, job_id, track_id):
    from backend.app.services import event, get_job

    queue.settings(db, lock=True)
    job = get_job(db, job_id, lock=True)
    record = db.get(SourceTrackChoices, source_key(job))
    track = (
        next((t for t in record.data.get("uploads", []) if t["track_id"] == track_id), None)
        if record
        else None
    )
    if not track:
        if record and any(
            t["track_id"] == track_id and t.get("origin") == "discovery"
            for t in record.data.get("removed_uploads", [])
        ):
            return job
        raise LookupError("Agent-found subtitle track not found for this source")
    if track.get("origin") != "discovery":
        raise ValueError("Only subtitles found by the agent can be removed with this action")
    status = removal_status(db, job)
    if not status["allowed"]:
        raise ValueError(status["reason"])
    seed_existing_choices(db, job)
    data = deepcopy(record.data)
    data["uploads"] = [t for t in data.get("uploads", []) if t["track_id"] != track_id]
    data["removed_uploads"] = [
        *data.get("removed_uploads", []),
        {**track, "removed_at": now().isoformat(), "removed_by_job_id": job.id},
    ]
    if "audio_track_ids" in data:
        data["source_job_id"] = job.id
    if "subtitle_track_ids" in data:
        data["subtitle_track_ids"] = [i for i in data["subtitle_track_ids"] if i != track_id]
    for key in ("track_names", "track_flags", "track_languages"):
        if key in data:
            data[key].pop(str(track_id), None)
    data["discovery_version"] = data.get("discovery_version", 0) + 1
    report = deepcopy(data.get("subtitle_discovery") or job.analysis.get("subtitle_discovery"))
    if report:
        report["added_tracks"] = [t for t in report.get("added_tracks", []) if t.get("track_id") != track_id]
        for candidate in report.get("candidates", []):
            if candidate.get("track_id") == track_id:
                candidate["status"] = "removed"
        data["subtitle_discovery"] = report
    record.data, record.revision, record.updated_at = data, record.revision + 1, now()
    db.flush()
    if report:
        report["missing"] = missing_languages(
            [t.info for t in track_rows(db, job).values()], report.get("original_languages", [])
        )
        record.data = {**record.data, "subtitle_discovery": report}
    for peer in db.scalars(
        select(MovieJob).where(*source_peers(job)).order_by(MovieJob.id).with_for_update()
    ):
        if peer.state in TRACK_EDIT_STAGES:
            selection = db.scalar(select(TrackSelection).where(TrackSelection.job_id == peer.id))
            if selection:
                selection.subtitle_track_ids = [i for i in selection.subtitle_track_ids if i != track_id]
            peer.analysis = {
                **peer.analysis,
                "uploaded_tracks": [
                    t for t in peer.analysis.get("uploaded_tracks", []) if t["track_id"] != track_id
                ],
                "prepared_track_cache": {
                    k: v
                    for k, v in peer.analysis.get("prepared_track_cache", {}).items()
                    if k != str(track_id)
                },
            }
            if "prepared_tracks" in peer.analysis:
                peer.analysis = {
                    **peer.analysis,
                    "prepared_tracks": [
                        t for t in peer.analysis["prepared_tracks"] if t["track_id"] != track_id
                    ],
                }
        event(
            db,
            peer.id,
            "discovered_subtitle_removed",
            track_id=track_id,
            source_job_id=job.id,
            files_retained=True,
        )
    db.commit()
    return job
