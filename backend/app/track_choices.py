"""Source-wide confirmed choices, with immutable per-encode remux snapshots."""

import hashlib
import json
from copy import deepcopy
from types import SimpleNamespace

from sqlalchemy import func, select

from shared.languages import language_tag
from shared.models import Event, MovieJob, MovieTrack, SourceTrackChoices, Task, TrackSelection, now
from shared.naming import automatic_track_name, track_name
from shared.state import TRACK_EDIT_STAGES
from shared.tracks import FLAG_NAMES, track_analysis_complete


def source_key(job):
    identity = [job.source_path, job.source_size, job.source_mtime_ns]
    return hashlib.sha256(json.dumps(identity).encode()).hexdigest()


def source_peers(job):
    return (
        MovieJob.source_path == job.source_path,
        MovieJob.source_size == job.source_size,
        MovieJob.source_mtime_ns == job.source_mtime_ns,
        MovieJob.deleted_at.is_(None),
    )


def track_rows(db, job):
    tracks = {t.track_id: t for t in db.scalars(select(MovieTrack).where(MovieTrack.job_id == job.id))}
    record = db.get(SourceTrackChoices, source_key(job))
    snapshots = {t["track_id"]: t for t in job.analysis.get("uploaded_tracks", [])}
    for uploaded in record.data.get("uploads", []) if record else []:
        info = {**uploaded, **snapshots.get(uploaded["track_id"], {})}
        tracks[uploaded["track_id"]] = SimpleNamespace(
            track_id=uploaded["track_id"], kind="subtitles", info=info
        )
    return tracks


def inventory(tracks):
    return sorted(
        (t.track_id, t.kind, t.info.get("codec_id"))
        for t in tracks.values()
        if t.info.get("origin") not in ("upload", "discovery")
    )


def checked_tracks(tracks, audio, subtitles, names, flags, languages=None):
    languages = languages or {}
    for track_id in audio:
        if track_id not in tracks or tracks[track_id].kind != "audio":
            raise ValueError(f"Invalid audio track {track_id}")
        if not tracks[track_id].info.get("extractable"):
            raise ValueError(f"Audio codec on track {track_id} is not supported for native extraction")
    for track_id in subtitles:
        if (
            track_id not in tracks
            or tracks[track_id].kind != "subtitles"
            or (
                tracks[track_id].info.get("codec_id") != "S_HDMV/PGS"
                and not (
                    tracks[track_id].info.get("origin") in ("upload", "discovery")
                    and tracks[track_id].info.get("upload_validated")
                )
            )
        ):
            raise ValueError(f"Track {track_id} is not a supported subtitle")
    updated = {}
    for track_id in [*audio, *subtitles]:
        track = tracks[track_id]
        info = deepcopy(track.info)
        if track_id in languages:
            info["language"] = info["language_override"] = language_tag(languages[track_id])
            info["base_name"] = automatic_track_name(
                {**info, "kind": track.kind, "hearing_impaired": False, "forced": False}
            )
            info["suggested_name"] = automatic_track_name({**info, "kind": track.kind})
        if (
            track.kind == "subtitles"
            and info.get("subtitle_detection", {}).get("language_confident") is False
            and not info.get("language_override")
        ):
            raise ValueError(f"Track {track_id}: verify a language code before continuing")
        if track_id in names:
            info["name_override"] = names[track_id]
        if track_id in flags:
            overrides = {**info.get("flag_overrides", {}), **flags[track_id]}
            info.update(overrides)
            info["flag_overrides"] = overrides
        if info.get("track_review", {}).get("schema_version") == 1:
            unresolved = [flag for flag in FLAG_NAMES if not isinstance(info.get(flag), bool)]
            if unresolved:
                raise ValueError(
                    f"Track {track_id}: choose unresolved flags before continuing: {', '.join(unresolved)}"
                )
        info["mux_name"] = track_name({**info, "kind": track.kind})
        updated[track_id] = info
    return updated


def choices_data(job, tracks, audio, subtitles, updated):
    # Confirmed automatic suggestions are choices too: other analyses must not
    # silently change the accepted names or flags for the same source track.
    return {
        "source_job_id": job.id,
        "inventory": inventory(tracks),
        "audio_track_ids": list(audio),
        "subtitle_track_ids": list(subtitles),
        "track_names": {str(i): t["mux_name"] for i, t in updated.items()},
        "track_languages": {
            str(i): t["language_override"] for i, t in updated.items() if t.get("language_override")
        },
        "track_flags": {str(i): {f: bool(t.get(f)) for f in FLAG_NAMES} for i, t in updated.items()},
    }


def store_choices(db, job, body):
    """Caller holds the queue mutex before any job locks; saves are serialized."""
    from backend.app.subtitle_discovery import active_discovery

    if active_discovery(db, job):
        raise ValueError(
            "Wait for subtitle discovery or uploaded-subtitle review to finish or cancel it before confirming tracks"
        )
    record = db.get(SourceTrackChoices, source_key(job))
    revision = record.revision if record else 0
    if body.shared_revision is not None and body.shared_revision != revision:
        raise ValueError("Track choices changed in another encoding. Load the shared choices before saving.")
    tracks = track_rows(db, job)
    updated = checked_tracks(
        tracks,
        body.audio_track_ids,
        body.subtitle_track_ids,
        body.track_names,
        body.track_flags,
        body.track_languages,
    )
    data = choices_data(job, tracks, body.audio_track_ids, body.subtitle_track_ids, updated)
    data["confirmed_discovery_version"] = record.data.get("discovery_version", 0) if record else 0
    if record is None:
        record = SourceTrackChoices(source_key=source_key(job), revision=0, data={})
        db.add(record)
    record.data, record.revision, record.updated_at = {**record.data, **data}, revision + 1, now()
    db.flush()
    write_snapshot(db, job, record, tracks, updated)
    return record


def write_snapshot(db, job, record, tracks, updated):
    for track_id, info in updated.items():
        tracks[track_id].info = info
    job.analysis = {
        **{
            k: v
            for k, v in job.analysis.items()
            if k not in ("prepared_tracks", "shared_track_selection_error")
        },
        "tracks": [
            {**track, **updated.get(track["track_id"], {})} for track in job.analysis.get("tracks", [])
        ],
        "uploaded_tracks": [
            deepcopy(t.info) for t in tracks.values() if t.info.get("origin") in ("upload", "discovery")
        ],
        "shared_track_selection": {
            "revision": record.revision,
            "source_job_id": record.data["source_job_id"],
        },
    }
    selection = db.scalar(select(TrackSelection).where(TrackSelection.job_id == job.id))
    if selection is None:
        selection = TrackSelection(job_id=job.id)
        db.add(selection)
    selection.audio_track_ids = list(record.data["audio_track_ids"])
    selection.subtitle_track_ids = list(record.data["subtitle_track_ids"])
    selection.selected_by = "user" if record.data["source_job_id"] == job.id else "shared source"
    from backend.app.services import event

    event(
        db,
        job.id,
        "tracks_selected",
        shared_revision=record.revision,
        source_job_id=record.data["source_job_id"],
    )


def apply_shared_choices(db, job, record=None):
    """Apply only after review is finished and before preparing the final tracks."""
    if job.state not in TRACK_EDIT_STAGES:
        return False
    record = record or db.get(SourceTrackChoices, source_key(job))
    if (
        record is None
        or "audio_track_ids" not in record.data
        or record.data.get("discovery_version", 0) != record.data.get("confirmed_discovery_version", 0)
        or job.analysis.get("shared_track_selection", {}).get("revision") == record.revision
    ):
        return False
    reviewed_or_confirmed = track_analysis_complete(job.analysis) or db.scalar(
        select(TrackSelection.id).where(TrackSelection.job_id == job.id)
    )
    if not reviewed_or_confirmed or db.scalar(
        select(Task.id).where(
            Task.job_id == job.id, Task.lane == "tracks", Task.status.in_(["QUEUED", "RUNNING"])
        )
    ):
        return False
    tracks = track_rows(db, job)
    data = record.data
    try:
        if inventory(tracks) != [tuple(t) for t in data["inventory"]]:
            raise ValueError("The source track inventory differs; review and confirm this job's tracks.")
        updated = checked_tracks(
            tracks,
            data["audio_track_ids"],
            data["subtitle_track_ids"],
            {int(k): v for k, v in data["track_names"].items()},
            {int(k): v for k, v in data["track_flags"].items()},
            {int(k): v for k, v in data.get("track_languages", {}).items()},
        )
    except ValueError as error:
        job.analysis = {**job.analysis, "shared_track_selection_error": str(error)}
        return False
    write_snapshot(db, job, record, tracks, updated)
    return True


def choices_current(db, job):
    from backend.app.subtitle_discovery import active_discovery

    if active_discovery(db, job):
        return False
    record = db.get(SourceTrackChoices, source_key(job))
    if record and record.data.get("discovery_version", 0) != record.data.get(
        "confirmed_discovery_version", 0
    ):
        return False
    return (
        record is None
        or "audio_track_ids" not in record.data
        or job.analysis.get("shared_track_selection", {}).get("revision") == record.revision
    )


def seed_existing_choices(db, job):
    """Adopt the newest legacy confirmation once, under the queue mutex."""
    key = source_key(job)
    existing = db.get(SourceTrackChoices, key)
    if existing and "audio_track_ids" in existing.data:
        return
    last_saved = (
        select(func.max(Event.created_at))
        .where(Event.job_id == MovieJob.id, Event.type == "tracks_selected")
        .correlate(MovieJob)
        .scalar_subquery()
    )
    candidates = db.execute(
        select(MovieJob, TrackSelection)
        .join(TrackSelection, TrackSelection.job_id == MovieJob.id)
        .where(*source_peers(job))
        .order_by(func.coalesce(last_saved, TrackSelection.created_at).desc(), MovieJob.id)
    )
    for donor, selection in candidates:
        tracks = track_rows(db, donor)
        try:
            updated = checked_tracks(tracks, selection.audio_track_ids, selection.subtitle_track_ids, {}, {})
        except ValueError:
            continue
        record = existing or SourceTrackChoices(source_key=key, revision=0, data={})
        record.data = {
            **record.data,
            **choices_data(donor, tracks, selection.audio_track_ids, selection.subtitle_track_ids, updated),
        }
        record.revision, record.updated_at = 1, now()
        db.add(record)
        db.flush()
        return


def shared_choices_status(db, job):
    record = db.get(SourceTrackChoices, source_key(job))
    if record is None or "audio_track_ids" not in record.data:
        return None
    applied = job.analysis.get("shared_track_selection", {}).get("revision", 0)
    return {
        "revision": record.revision,
        "applied_revision": applied,
        "source_job_id": record.data["source_job_id"],
        "updated_at": record.updated_at,
        "pending": job.state in TRACK_EDIT_STAGES and applied != record.revision,
        "error": job.analysis.get("shared_track_selection_error"),
    }
