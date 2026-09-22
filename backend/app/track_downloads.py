"""Download retained tracks, or stream-copy an uncached source track on demand."""

import fcntl
import json
import subprocess
import tempfile
from pathlib import Path

from fastapi.responses import FileResponse

from backend.app.track_choices import source_key, track_rows
from shared.config import get_settings
from shared.models import SourceTrackChoices
from shared.paths import contained, job_dir


def response(path, track_id, *, prefix="track"):
    return FileResponse(
        path, filename=f"{prefix}-{track_id}{path.suffix}", media_type="application/octet-stream"
    )


def uploaded_original(db, job, upload_id):
    record = db.get(SourceTrackChoices, source_key(job))
    entry = (
        next((e for e in record.data.get("subtitle_imports", []) if e["id"] == upload_id), None)
        if record
        else None
    )
    if not entry:
        raise LookupError("Subtitle upload not found")
    return FileResponse(
        contained(get_settings().workspace_root, entry["path"], exists=True),
        filename=entry["filename"],
        media_type="application/octet-stream",
    )


def track_download(db, job, track_id, variant="track"):
    settings = get_settings()
    rows = track_rows(db, job)
    row = rows.get(track_id)
    if not row or row.kind not in ("audio", "subtitles"):
        raise LookupError("Track not found")
    info = row.info
    workspace = job_dir(settings.workspace_root, job.id)
    if info.get("origin") in ("upload", "discovery"):
        original = contained(settings.workspace_root, info["upload_path"], exists=True)
        if variant == "cleaned":
            path = original.with_name("cleaned.srt")
        elif variant == "original":
            files = [
                p
                for p in original.parent.glob("original.*")
                if p.suffix.lower() in (".srt", ".ass", ".ssa", ".sup")
            ]
            path = files[0] if files else original
        elif info.get("discovery", {}).get("crop_checked"):
            path = original.with_name("cropped.sup")
        else:
            path = original
        return response(
            contained(settings.workspace_root, str(path.relative_to(settings.workspace_root)), exists=True),
            track_id,
        )
    if variant != "track":
        raise ValueError("This version is available only for reviewed uploaded/discovered subtitles")
    scanned = next((t for t in job.analysis.get("tracks", []) if t["track_id"] == track_id), {})
    for key in ("source_track_path", "subtitle_source_path"):
        relative = info.get(key) or scanned.get(key)
        if relative:
            path = contained(workspace, relative)
            if path.is_file() and path.stat().st_size:
                return response(path, track_id)
    # Most tracks already exist in the shared extraction cache. Older or
    # unsupported native codecs can still be downloaded in a Matroska container.
    source = contained(settings.source_root, job.source_path, exists=True)

    def check_source():
        stat = source.stat()
        if stat.st_size != job.source_size or str(stat.st_mtime_ns) != job.source_mtime_ns:
            raise ValueError("Source video changed; its track identity can no longer be verified")

    check_source()
    folder = workspace / "track-downloads"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"track-{track_id}{'.mka' if row.kind == 'audio' else '.mks'}"
    kind = "subtitle" if row.kind == "subtitles" else "audio"
    peers = sorted(
        [
            t
            for t in rows.values()
            if t.kind == row.kind and t.info.get("origin") not in ("upload", "discovery")
        ],
        key=lambda t: t.info.get("source_order", t.track_id),
    )
    peer_ids = [t.track_id for t in peers]
    db.rollback()  # Do not hold a DB transaction during a potentially large stream copy.
    with path.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not path.is_file():
            probe = subprocess.run(
                [settings.ffprobe_bin, "-v", "error", "-show_streams", "-of", "json", str(source)],
                check=True,
                capture_output=True,
                text=True,
                timeout=60,
            )
            streams = [s for s in json.loads(probe.stdout)["streams"] if s["codec_type"] == kind]
            index = info.get("ffprobe_index", scanned.get("ffprobe_index"))
            if index is None:
                if len(peer_ids) != len(streams):
                    raise ValueError("Cannot identify the requested source track reliably")
                index = streams[peer_ids.index(track_id)]["index"]
            if not any(s["index"] == index for s in streams):
                raise ValueError("Source stream does not match the requested track type")
            with tempfile.TemporaryDirectory(prefix="track-", dir=folder) as staging:
                output = Path(staging) / path.name
                result = subprocess.run(
                    [
                        settings.ffmpeg_bin,
                        "-v",
                        "error",
                        "-nostdin",
                        "-i",
                        str(source),
                        "-map",
                        f"0:{index}",
                        "-c",
                        "copy",
                        "-map_chapters",
                        "-1",
                        "-f",
                        "matroska",
                        str(output),
                    ],
                    capture_output=True,
                    timeout=1800,
                )
                if result.returncode or not output.is_file() or not output.stat().st_size:
                    raise ValueError("Track export failed; check the source file and codec support")
                check_source()
                output.replace(path)
    return response(path, track_id)
