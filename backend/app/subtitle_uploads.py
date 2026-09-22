"""Validated subtitle files shared by encodes of the same immutable source."""

import hashlib
from pathlib import Path
from uuid import uuid4

import av
from fastapi import HTTPException, Request
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from backend.app import queue
from backend.app.track_choices import seed_existing_choices, source_key
from shared.config import get_settings
from shared.db import session
from shared.languages import language_tag
from shared.models import SourceTrackChoices, now
from shared.naming import automatic_track_name
from shared.paths import contained
from shared.state import TRACK_EDIT_STAGES, Stage
from shared.tracks import FLAG_NAMES

MAX_UPLOAD_BYTES = 128 * 1024 * 1024
MAX_TEXT_BYTES = 16 * 1024 * 1024
FORMATS = {
    ".srt": ("srt", "S_TEXT/UTF8", "SRT"),
    ".ass": ("ass", "S_TEXT/ASS", "ASS"),
    ".ssa": ("ass", "S_TEXT/SSA", "SSA"),
    ".sup": ("sup", "S_HDMV/PGS", "PGS"),
}


def upload_allowed(db, job, *, discovery_task_id=None):
    from backend.app.remux import remux_status

    if (
        job.state not in TRACK_EDIT_STAGES | {Stage.ANALYZING_SOURCE}
        and not remux_status(db, job, ignore_task_id=discovery_task_id)["available"]
    ):
        raise ValueError("Subtitles can be uploaded during track review or when editing tracks for remux")


def validate_subtitle(path, suffix):
    """Normalize text encoding, then parse the entire bounded file as subtitles."""
    if suffix != ".sup":
        raw = path.read_bytes()
        try:
            text = raw.decode("utf-16" if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig")
        except UnicodeError as error:
            raise ValueError("Save text subtitles as UTF-8 or UTF-16 before uploading") from error
        if "\x00" in text:
            raise ValueError("Text subtitle contains invalid null characters")
        path.write_text(text, encoding="utf-8")
    else:
        # PGS consists of bounded PG segments. Reject truncated streams even if
        # a tolerant demuxer would otherwise accept their first complete cues.
        with path.open("rb") as stream:
            segments = set()
            while header := stream.read(13):
                if len(header) != 13 or header[:2] != b"PG":
                    raise ValueError("Invalid or truncated PGS subtitle")
                segments.add(header[10])
                length = int.from_bytes(header[11:13], "big")
                if len(stream.read(length)) != length:
                    raise ValueError("Truncated PGS subtitle segment")
            if not {0x14, 0x15, 0x16, 0x80}.issubset(segments):
                raise ValueError("PGS subtitle contains no complete display set")
    fmt, codec_id, codec = FORMATS[suffix]
    cues = 0
    try:
        with av.open(str(path), format=fmt) as media:
            if len(media.streams) != 1 or media.streams[0].type != "subtitle":
                raise ValueError("Upload must contain exactly one subtitle track")
            for packet in media.demux():
                if packet.size and packet.pts is not None:
                    if suffix != ".sup" and packet.duration <= 0:
                        raise ValueError("Subtitle cues must have an end time after their start time")
                    cues += 1
    except av.FFmpegError as error:
        raise ValueError("The file could not be read as a valid subtitle of this format") from error
    if not cues:
        raise ValueError("Subtitle file contains no readable cues")
    return {"codec_id": codec_id, "codec": codec, "uploaded_cues": cues}


def register_upload(
    job_id,
    path,
    original_filename,
    language,
    hearing_impaired,
    upload_id,
    *,
    provenance=None,
    detection=None,
    task_identity=None,
    origin="discovery",
):
    from backend.app.services import event, get_job, serialize_tracks

    metadata = validate_subtitle(path, path.suffix)
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    with session() as db:
        queue.settings(db, lock=True)
        job = get_job(db, job_id, lock=True)
        discovery_task_id = None
        if task_identity:
            from shared.models import Task
            from worker.runtime import Interrupted

            task = db.scalar(select(Task).where(Task.id == task_identity[0]).with_for_update())
            if (
                not task
                or task.job_id != job.id
                or task.type not in ("discover_subtitles", "review_uploaded_subtitle")
                or task.run_token != task_identity[1]
                or task.status != "RUNNING"
                or task.cancel_requested
            ):
                raise Interrupted("Subtitle discovery lost its task lease")
            # Only the verified discovery lease may register its own results while
            # a completed job is busy. Other tasks still block remux readiness.
            discovery_task_id = task.id
        upload_allowed(db, job, discovery_task_id=discovery_task_id)
        seed_existing_choices(db, job)
        key = source_key(job)
        record = db.get(SourceTrackChoices, key)
        if record is None:
            record = SourceTrackChoices(source_key=key, revision=0, data={})
            db.add(record)
        uploads = record.data.get("uploads", [])
        duplicate = next((t for t in uploads if t["sha256"] == digest), None)
        if duplicate:
            path.unlink(missing_ok=True)
            path.parent.rmdir()
            return {
                "track": next(t for t in serialize_tracks(db, job) if t["track_id"] == duplicate["track_id"]),
                "duplicate": True,
            }
        native_ids = [t["track_id"] for t in job.analysis.get("tracks", [])]
        track_id = (
            max(
                [
                    999_999,
                    *native_ids,
                    *(t["track_id"] for t in [*uploads, *record.data.get("removed_uploads", [])]),
                ]
            )
            + 1
        )
        flags = {**dict.fromkeys(FLAG_NAMES, False), "hearing_impaired": hearing_impaired}
        info = {
            **metadata,
            **flags,
            "track_id": track_id,
            "kind": "subtitles",
            "origin": "upload",
            "upload_id": upload_id,
            "upload_validated": True,
            "original_filename": original_filename,
            "upload_path": str(path.relative_to(get_settings().workspace_root)),
            "sha256": digest,
            "source_order": track_id,
            "extractable": True,
            "language": language,
            "language_override": language,
            "flag_overrides": flags,
            "name": original_filename,
            "track_review": {
                "schema_version": 1,
                "confidence": "user supplied",
                "flags": flags,
                "description": f"Uploaded {metadata['codec']} subtitles. Language and SDH confirmed by you. Timing and layout are used as supplied.",
                "flag_explanation": "Language and SDH are user choices; other flags start disabled. Review them before confirming tracks.",
            },
        }
        if provenance is not None:
            info.update(
                origin=origin,
                discovery=provenance,
                subtitle_detection=detection,
                track_review={
                    "schema_version": 1,
                    "confidence": "agent reviewed; alignment checked",
                    "flags": flags,
                    "description": provenance["review"]["explanation"],
                    "flag_explanation": "SDH was reviewed from content. Resolve any unknown flags before selecting this track.",
                },
            )
            info.pop("language_override", None)
            info.pop("flag_overrides", None)
        info["base_name"] = automatic_track_name({**info, "hearing_impaired": False})
        info["suggested_name"] = automatic_track_name(info)
        record.data = {
            **record.data,
            "uploads": [*uploads, info],
            **({"discovery_version": record.data.get("discovery_version", 0) + 1} if provenance else {}),
        }
        record.updated_at = now()
        event(db, job.id, "subtitle_uploaded", track_id=track_id, filename=original_filename)
        db.flush()
        result = next(t for t in serialize_tracks(db, job) if t["track_id"] == track_id)
        db.commit()
        return {"track": result, "duplicate": False}


async def receive_upload(request: Request, job_id: str, filename: str, code: str, hearing_impaired: bool):
    from backend.app.services import get_job

    language = language_tag(code.strip())
    if (
        not filename
        or len(filename) > 255
        or any(c in filename for c in ("/", "\\"))
        or any(ord(c) < 32 for c in filename)
    ):
        raise ValueError("Provide a subtitle filename without directory components")
    suffix = Path(filename).suffix.lower()
    if suffix not in FORMATS:
        raise ValueError("Supported subtitle formats: .srt, .ass, .ssa, .sup")
    limit = MAX_UPLOAD_BYTES if suffix == ".sup" else MAX_TEXT_BYTES
    declared = request.headers.get("content-length")
    if declared and (not declared.isdigit() or int(declared) > limit):
        raise HTTPException(413, f"Subtitle exceeds the {limit // (1024 * 1024)} MB size limit")
    with session() as db:
        job = get_job(db, job_id)
        upload_allowed(db, job)
        key = source_key(job)
    upload_id = str(uuid4())
    path = contained(get_settings().workspace_root, f"uploaded-subtitles/{key}/{upload_id}/subtitle{suffix}")
    path.parent.mkdir(parents=True, exist_ok=True)
    retained = False
    try:
        size = 0
        with path.open("xb") as output:
            async for chunk in request.stream():
                size += len(chunk)
                if size > limit:
                    raise HTTPException(413, f"Subtitle exceeds the {limit // (1024 * 1024)} MB size limit")
                await run_in_threadpool(output.write, chunk)
        from backend.app.subtitle_imports import queue_upload

        handler = register_upload if suffix == ".sup" else queue_upload
        result = await run_in_threadpool(
            handler, job_id, path, filename, language, hearing_impaired, upload_id
        )
        retained = True
        return result
    finally:
        if not retained:
            path.unlink(missing_ok=True)
            path.parent.rmdir()
