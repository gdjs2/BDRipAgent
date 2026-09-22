"""One source pass for native tracks and timestamps, shared by jobs and retries."""

import errno
import fcntl
import hashlib
import json
import os
import shutil
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from shared.paths import contained, write_json
from worker.adapters.media import AUDIO_EXTENSIONS
from worker.adapters.mkvtoolnix import MKVToolNixProgress


def source_key(ctx):
    source = ctx.source()
    stat = source.stat()
    identity = [str(source.resolve()), stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns]
    return hashlib.sha256(json.dumps(identity).encode()).hexdigest()


@contextmanager
def source_lock(ctx, root, *, phase="Waiting for the shared source extraction"):
    root.mkdir(parents=True, exist_ok=True)
    with (root / ".lock").open("a") as lock:
        waiting = False
        while True:
            ctx.check()
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if not waiting:
                    ctx.progress(None, phase=phase)
                    waiting = True
                time.sleep(0.2)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def link_file(source, destination):
    """Keep immutable extraction files reusable without duplicating bytes on one filesystem."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and os.path.samefile(source, destination):
        return
    temporary = destination.with_name(f".{destination.name}.{uuid4()}.tmp")
    try:
        try:
            os.link(source, temporary)
        except OSError as error:
            if error.errno not in (errno.EXDEV, errno.EPERM, errno.EOPNOTSUPP, errno.EMLINK):
                raise
            shutil.copy2(source, temporary)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def extract_tracks(ctx, tracks, *, video_track_id=None):
    """Extract only missing files in one MKVToolNix invocation; publish complete batches."""
    root = ctx.settings.cache_root / "source-tracks" / source_key(ctx)
    specifications = []
    for track in tracks:
        track_id = track["track_id"]
        audio = track["kind"] == "audio"
        extension = AUDIO_EXTENSIONS[track["codec_id"]] if audio else "sup"
        specifications.append(("tracks", track_id, f"track-{track_id}.{extension}"))
        if audio:
            specifications.append(("timestamps_v2", track_id, f"track-{track_id}.timestamps.txt"))
    if video_track_id is not None:
        specifications.append(("timestamps_v2", video_track_id, f"track-{video_track_id}.timestamps.txt"))
    paths = {}
    with source_lock(ctx, root):
        manifest_path = root / "files.json"
        manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
        # Older jobs may already own valid native files. Seed the common cache
        # from those originals instead of extracting them again during upgrade.
        for track in tracks:
            track_id = track["track_id"]
            audio = track["kind"] == "audio"
            extension = AUDIO_EXTENSIONS[track["codec_id"]] if audio else "sup"
            originals = [
                (
                    track.get("source_track_path") or track.get("subtitle_source_path"),
                    f"track-{track_id}.{extension}",
                )
            ]
            if audio:
                originals.append((track.get("source_timestamps_path"), f"track-{track_id}.timestamps.txt"))
            for relative, filename in originals:
                if relative and filename not in manifest:
                    original = contained(ctx.workspace, relative)
                    if original.is_file() and original.stat().st_size:
                        link_file(original, root / filename)
                        manifest[filename] = original.stat().st_size
        missing = []
        for mode, track_id, filename in specifications:
            path = contained(root, filename)
            if not path.is_file() or not path.stat().st_size or path.stat().st_size != manifest.get(filename):
                missing.append((mode, track_id, filename))
        if missing:
            with tempfile.TemporaryDirectory(prefix="extract-", dir=root) as directory:
                staging = Path(directory)
                command = [ctx.settings.mkvextract_bin, ctx.source()]
                for mode in ("tracks", "timestamps_v2"):
                    batch = [(track_id, filename) for kind, track_id, filename in missing if kind == mode]
                    if batch:
                        command += [
                            mode,
                            *(f"{track_id}:{staging / filename}" for track_id, filename in batch),
                        ]
                phase = "Extracting source tracks and timestamps"
                ctx.progress(0, phase=phase, tool="mkvextract")
                ctx.run(
                    [*command, "--gui-mode"],
                    progress_parser=MKVToolNixProgress("mkvextract", phase, progress_span=85),
                )
                for mode, track_id, filename in missing:
                    path = staging / filename
                    if not path.is_file() or not path.stat().st_size:
                        kind = "timestamp " if mode == "timestamps_v2" else ""
                        raise ValueError(f"Track {track_id} {kind}extraction produced no data")
                ctx.check()
                ctx.source()  # Never publish files if the immutable input changed during extraction.
                for _, _, filename in missing:
                    path = staging / filename
                    manifest[filename] = path.stat().st_size
                    path.replace(root / filename)
                write_json(manifest_path, manifest)
        else:
            ctx.log("Reusing source tracks and timestamps from the extraction cache.")
        write_json(manifest_path, manifest)
        for index, (_, _, filename) in enumerate(specifications):
            ctx.progress(
                85 + 14 * index / max(1, len(specifications)),
                phase="Making extracted tracks available",
                indeterminate=True,
                completed_files=index,
                total_files=len(specifications),
            )
            path = contained(ctx.workspace, f"source-tracks/{filename}")
            link_file(root / filename, path)
            paths[filename] = path
    extracted = {}
    for track in tracks:
        track_id = track["track_id"]
        audio = track["kind"] == "audio"
        extension = AUDIO_EXTENSIONS[track["codec_id"]] if audio else "sup"
        extracted[track_id] = {
            "path": paths[f"track-{track_id}.{extension}"],
            "timestamps": paths.get(f"track-{track_id}.timestamps.txt") if audio else None,
        }
    video_timestamps = (
        paths.get(f"track-{video_track_id}.timestamps.txt") if video_track_id is not None else None
    )
    ctx.progress(100, phase="Extracted tracks and timestamps ready")
    return extracted, video_timestamps
