"""Authenticated, bounded, on-demand HLS preview of registered movie artifacts."""

import asyncio
import hashlib
import json
import math
import tempfile
from collections import OrderedDict
from contextlib import suppress
from pathlib import Path
from typing import Annotated
from urllib.parse import urlencode
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy.orm import Session

from backend.app.services import get_job
from shared.config import get_settings
from shared.db import get_db
from shared.media_details import positive_number
from shared.models import Artifact
from shared.naming import language_name
from shared.paths import artifact_root, contained

router = APIRouter()
SEGMENT_SECONDS = 6
MOVIE_TYPES = {"FINAL_MKV", "SMOKE_TEST_MKV", "RELEASE_MEDIA", "ENCODED_VIDEO"}
BITMAP_SUBTITLES = {"hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle"}
TEXT_SUBTITLES = {"subrip", "ass", "ssa", "webvtt", "mov_text", "text"}
DB = Annotated[Session, Depends(get_db)]


def movie_path(db, artifact_id):
    item = db.get(Artifact, str(artifact_id))
    if not item:
        raise HTTPException(404, "Artifact not found")
    get_job(db, item.job_id)
    if item.artifact_type not in MOVIE_TYPES:
        raise HTTPException(409, "Choose a video artifact for playback")
    return contained(artifact_root(get_settings(), item.job_id, item.storage), item.path, exists=True)


def version(path):
    stat = path.stat()
    return hashlib.sha256(f"{path}:{stat.st_ino}:{stat.st_size}:{stat.st_mtime_ns}".encode()).hexdigest()[:24]


class MediaPreview:
    def __init__(self):
        self.slots = asyncio.Semaphore(2)
        self.processes = set()
        self.pending = 0
        self.cache = OrderedDict()
        self.cache_size = 0

    async def close(self):
        for process in list(self.processes):
            with suppress(ProcessLookupError):
                process.kill()
        await asyncio.gather(*(p.wait() for p in list(self.processes)), return_exceptions=True)
        self.cache.clear()

    async def run(self, command, *, cwd=None, timeout=45):
        if self.pending >= 8:
            raise HTTPException(429, "Preview is busy. Try again shortly.", headers={"Retry-After": "2"})
        self.pending += 1
        try:
            async with self.slots:
                process = await asyncio.create_subprocess_exec(
                    *map(str, command),
                    cwd=cwd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                self.processes.add(process)
                try:
                    stdout, stderr = await asyncio.wait_for(process.communicate(), timeout)
                    if process.returncode:
                        raise HTTPException(
                            422,
                            "Cannot prepare this media preview: " + stderr.decode(errors="replace")[-1200:],
                        )
                    return stdout
                except TimeoutError:
                    raise HTTPException(
                        504, "Media preview took too long. Retry or choose another track."
                    ) from None
                finally:
                    if process.returncode is None:
                        with suppress(ProcessLookupError):
                            process.kill()
                        await process.wait()
                    self.processes.discard(process)
        finally:
            self.pending -= 1

    def cached(self, key):
        if key in self.cache:
            self.cache.move_to_end(key)
            return self.cache[key]
        return None

    def remember(self, key, value):
        if key in self.cache:
            self.cache_size -= len(self.cache.pop(key))
        self.cache[key] = value
        self.cache_size += len(value)
        while self.cache_size > 64 * 1024 * 1024 or len(self.cache) > 128:
            _, old = self.cache.popitem(last=False)
            self.cache_size -= len(old)
        return value

    async def info(self, path):
        fingerprint = version(path)
        key = ("info", fingerprint)
        if cached := self.cached(key):
            return json.loads(cached)
        settings = get_settings()
        probe = json.loads(
            await self.run(
                [
                    settings.ffprobe_bin,
                    "-v",
                    "error",
                    "-show_streams",
                    "-show_format",
                    "-show_chapters",
                    "-of",
                    "json",
                    path,
                ]
            )
        )
        duration = positive_number(probe.get("format", {}).get("duration"))
        if not duration or duration > 86400:
            raise HTTPException(422, "Video duration is unavailable or exceeds the preview limit of 24 hours")
        tracks = []
        subtitle_index = 0
        for stream in probe.get("streams", []):
            kind, codec = stream.get("codec_type"), stream.get("codec_name", "")
            if kind not in ("video", "audio", "subtitle") or stream.get("disposition", {}).get(
                "attached_pic"
            ):
                continue
            tags = stream.get("tags", {})
            language = tags.get("language", "und")
            tracks.append(
                {
                    "index": stream["index"],
                    "kind": kind,
                    "codec": codec,
                    "name": tags.get("title") or f"{language_name(language)} {codec.upper()}",
                    "language": language,
                    "default": bool(stream.get("disposition", {}).get("default")),
                    "width": stream.get("width"),
                    "height": stream.get("height"),
                    "channels": stream.get("channels"),
                    "supported": kind != "subtitle" or codec in BITMAP_SUBTITLES | TEXT_SUBTITLES,
                    "subtitle_index": subtitle_index if kind == "subtitle" else None,
                }
            )
            if kind == "subtitle":
                subtitle_index += 1
        if not any(t["kind"] == "video" for t in tracks):
            raise HTTPException(422, "This artifact has no playable video track")
        info = (await self.run([settings.mediainfo_bin, path])).decode("utf-8", errors="replace")
        result = {
            "filename": path.name,
            "version": fingerprint,
            "duration": duration,
            "tracks": tracks,
            "text": info[: 1024 * 1024],
            "truncated": len(info) > 1024 * 1024,
            "chapters": [
                {"start": float(c["start_time"]), "title": c.get("tags", {}).get("title", f"Chapter {i + 1}")}
                for i, c in enumerate(probe.get("chapters", []))
            ],
        }
        if version(path) != fingerprint:
            raise HTTPException(409, "The video was replaced. Reload its preview.")
        self.remember(key, json.dumps(result).encode())
        return result

    def selection(self, info, video, audio, subtitle):
        def find(index, kind, optional=False):
            if optional and index == -1:
                return None
            track = next((t for t in info["tracks"] if t["index"] == index and t["kind"] == kind), None)
            if not track or not track["supported"]:
                raise HTTPException(422, f"Invalid or unsupported {kind} track")
            return track

        return find(video, "video"), find(audio, "audio", True), find(subtitle, "subtitle", True)

    async def segment(self, path, info, index, video, audio, subtitle):
        v, a, sub = self.selection(info, video, audio, subtitle)
        start = index * SEGMENT_SECONDS
        if start >= info["duration"]:
            raise HTTPException(404, "Preview segment is outside the video")
        key = ("segment", info["version"], index, video, audio, subtitle)
        if cached := self.cached(key):
            return cached
        length = min(SEGMENT_SECONDS, info["duration"] - start)
        settings = get_settings()
        with tempfile.TemporaryDirectory(prefix="bdrip-preview-") as folder:
            root = Path(folder)
            (root / "movie.mkv").symlink_to(path)
            command = [
                settings.ffmpeg_bin,
                "-v",
                "error",
                "-nostdin",
                "-y",
                "-threads",
                "2",
                "-filter_complex_threads",
                "1",
                "-ss",
                str(start),
                "-t",
                str(length + 0.25),
                "-i",
                "movie.mkv",
            ]
            source = f"[0:{video}]"
            filters = []
            if sub and sub["codec"] in BITMAP_SUBTITLES:
                filters.append(f"{source}[0:{subtitle}]overlay=eof_action=pass:repeatlast=0[captioned]")
                source = "[captioned]"
            elif sub:
                # Input seeking makes timestamps relative; libass needs original movie times.
                filters.append(
                    f"{source}setpts=PTS+{start}/TB,subtitles=movie.mkv:si={sub['subtitle_index']},setpts=PTS-{start}/TB[captioned]"
                )
                source = "[captioned]"
            filters.append(
                source
                + "scale=w='min(1280,iw)':h='min(720,ih)':force_original_aspect_ratio=decrease:force_divisible_by=2,setsar=1,fps=24,format=yuv420p[v]"
            )
            command += ["-filter_complex", ";".join(filters), "-map", "[v]"]
            if a:
                command += ["-map", f"0:{audio}", "-c:a", "aac", "-b:a", "128k", "-ac", "2", "-ar", "48000"]
            else:
                command += ["-an"]
            command += [
                "-c:v",
                "libx264",
                "-threads",
                "2",
                "-preset",
                "veryfast",
                "-crf",
                "24",
                "-maxrate",
                "2500k",
                "-bufsize",
                "5000k",
                "-g",
                "48",
                "-bf",
                "0",
                "-sc_threshold",
                "0",
                "-t",
                str(length),
                "-output_ts_offset",
                str(start),
                "-muxdelay",
                "0",
                "-f",
                "mpegts",
                "segment.ts",
            ]
            await self.run(command, cwd=folder, timeout=90)
            output = root / "segment.ts"
            if not output.is_file() or not 0 < output.stat().st_size <= 16 * 1024 * 1024:
                raise HTTPException(422, "The selected tracks did not produce a playable preview")
            data = output.read_bytes()
        if version(path) != info["version"]:
            raise HTTPException(409, "The video was replaced. Reload its preview.")
        return self.remember(key, data)


async def connected(request, work):
    task = asyncio.create_task(work)
    try:
        while not task.done():
            await asyncio.wait({task}, timeout=0.25)
            if await request.is_disconnected():
                raise HTTPException(499, "Preview request closed")
        return await task
    finally:
        if not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


@router.get("/artifacts/{artifact_id}/media-info")
async def media_info(artifact_id: UUID, request: Request, db: DB):
    path = movie_path(db, artifact_id)
    db.close()
    return await connected(request, request.app.state.media_preview.info(path))


@router.get("/artifacts/{artifact_id}/playback/{part}")
async def playback(
    artifact_id: UUID,
    part: str,
    request: Request,
    db: DB,
    video: int = Query(ge=0),
    audio: int = Query(default=-1, ge=-1),
    subtitle: int = Query(default=-1, ge=-1),
    v: str = Query(min_length=24, max_length=24),
):
    path = movie_path(db, artifact_id)
    db.close()
    service = request.app.state.media_preview
    info = await connected(request, service.info(path))
    if info["version"] != v:
        raise HTTPException(409, "The video was replaced. Reload its preview.")
    service.selection(info, video, audio, subtitle)
    if part == "index.m3u8":
        query = urlencode({"video": video, "audio": audio, "subtitle": subtitle, "v": v})
        lines = [
            "#EXTM3U",
            "#EXT-X-VERSION:3",
            "#EXT-X-PLAYLIST-TYPE:VOD",
            "#EXT-X-TARGETDURATION:6",
            "#EXT-X-MEDIA-SEQUENCE:0",
            "#EXT-X-INDEPENDENT-SEGMENTS",
        ]
        for index in range(math.ceil(info["duration"] / SEGMENT_SECONDS)):
            length = min(SEGMENT_SECONDS, info["duration"] - index * SEGMENT_SECONDS)
            lines += [f"#EXTINF:{length:.6f},", f"{index}.ts?{query}"]
        return Response(
            "\n".join([*lines, "#EXT-X-ENDLIST", ""]),
            media_type="application/vnd.apple.mpegurl",
            headers={"Cache-Control": "private, no-store"},
        )
    if not part.endswith(".ts") or not part[:-3].isdigit() or len(part) > 12:
        raise HTTPException(404, "Unknown preview segment")
    data = await connected(request, service.segment(path, info, int(part[:-3]), video, audio, subtitle))
    return Response(data, media_type="video/mp2t", headers={"Cache-Control": "private, max-age=300"})
