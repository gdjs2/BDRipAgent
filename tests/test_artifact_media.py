import json
import subprocess

import numpy as np
import pytest

from shared.db import session
from shared.media_details import encoder_summary, video_bitrate
from shared.models import Artifact, MovieJob, Task


def run(command):
    return subprocess.run(list(map(str, command)), check=True, capture_output=True).stdout


@pytest.fixture
def media_fixture(client, new_job, environment):
    root = environment.artifacts_root
    srt = root / "captions.srt"
    srt.write_text(
        "1\n00:00:01,000 --> 00:00:03,000\nFIRST CAPTION\n\n2\n00:00:05,000 --> 00:00:09,000\nSECOND CAPTION\n\n3\n00:00:13,000 --> 00:00:15,000\nTHIRD CAPTION\n"
    )
    base = root / "base.mkv"
    run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=red:size=320x180:rate=24:duration=18",
            "-f",
            "lavfi",
            "-i",
            "color=blue:size=320x180:rate=24:duration=18",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=18",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=880:duration=18",
            "-i",
            srt,
            "-map",
            "0:v",
            "-map",
            "1:v",
            "-map",
            "2:a",
            "-map",
            "3:a",
            "-map",
            "4:s",
            "-c:v",
            "libx264",
            "-threads",
            "1",
            "-preset",
            "ultrafast",
            "-c:a",
            "flac",
            "-c:s",
            "srt",
            "-metadata:s:a:0",
            "title=Main audio",
            "-metadata:s:a:1",
            "title=Commentary",
            base,
        ]
    )
    run(
        [
            environment.subtitleedit_bin,
            srt,
            "bluraysup",
            "--resolution:320x180",
            "--fps:24",
            "--font-size:18",
            f"--output-folder:{root}",
            "--output-filename:captions.sup",
        ]
    )
    movie = root / "Movie [a:b]'s.mkv"
    run(["mkvmerge", "-o", movie, base, root / "captions.sup"])
    with session() as db:
        row = Artifact(
            job_id=new_job["id"],
            task_id=new_job["tasks"][0]["id"],
            artifact_type="FINAL_MKV",
            storage="artifacts",
            path=movie.name,
            size=movie.stat().st_size,
            info={},
        )
        db.add(row)
        db.commit()
        ident = row.id
    return ident, movie


def info_and_url(client, ident):
    response = client.get(f"/api/artifacts/{ident}/media-info")
    assert response.status_code == 200, response.text
    info = response.json()
    base = f"/api/artifacts/{ident}/playback"
    query = f"video=0&audio=2&subtitle=-1&v={info['version']}"
    return info, base, query


def frame(path, seconds):
    data = run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            path,
            "-ss",
            str(seconds),
            "-frames:v",
            "1",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "-",
        ]
    )
    return np.frombuffer(data, np.uint8).reshape(180, 320, 3)


def test_native_preview_tracks_mediainfo_timing_subtitles_and_switching(client, media_fixture, tmp_path):
    ident, _ = media_fixture
    info, base, query = info_and_url(client, ident)
    assert "General" in info["text"] and "Video" in info["text"] and "Audio" in info["text"]
    assert [t["kind"] for t in info["tracks"]] == ["video", "video", "audio", "audio", "subtitle", "subtitle"]
    manifest = client.get(base + "/index.m3u8?" + query)
    assert manifest.status_code == 200 and "#EXT-X-ENDLIST" in manifest.text
    assert len([line for line in manifest.text.splitlines() if line.endswith(query)]) == 3
    paths = {}
    for name, segment, changes in [
        ("plain", 1, {}),
        ("text", 1, {"subtitle": 4}),
        ("pgs", 1, {"subtitle": 5}),
        ("blue", 1, {"video": 1, "audio": 3}),
        ("silent", 2, {"audio": -1}),
    ]:
        params = {"video": 0, "audio": 2, "subtitle": -1, "v": info["version"], **changes}
        response = client.get(base + f"/{segment}.ts", params=params)
        assert response.status_code == 200, (name, response.text[:1600])
        path = tmp_path / (name + ".ts")
        path.write_bytes(response.content)
        paths[name] = path
        probe = json.loads(
            run(["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", path])
        )
        video = next(t for t in probe["streams"] if t["codec_type"] == "video")
        assert video["codec_name"] == "h264" and video["pix_fmt"] == "yuv420p"
        assert abs(float(probe["format"]["duration"]) - 6) < 0.15
        assert abs(float(video["start_time"]) - segment * 6) < 0.1
        if name == "silent":
            assert len(probe["streams"]) == 1
    red = frame(paths["plain"], 1.5)
    blue = frame(paths["blue"], 1.5)
    assert red[:, :, 0].mean() > 200 and blue[:, :, 2].mean() > 200
    for name in ("text", "pgs"):
        caption = frame(paths[name], 1.5)
        assert np.count_nonzero(np.max(abs(caption.astype(int) - red.astype(int)), axis=2) > 30) > 100, name
    assert frame(paths["pgs"], 4.5)[:, :, 0].mean() > 200  # Empty subtitle intervals still produce video.
    assert client.get(base + "/100.ts?" + query).status_code == 404
    assert client.get(base + "/0.ts", params={"video": 2, "v": info["version"]}).status_code == 422
    assert client.get(base + "/0.ts", params={"video": 0, "v": "a" * 24}).status_code == 409
    client.headers.clear()
    assert client.get(base + "/0.ts?" + query).status_code == 401
    assert client.get(f"/api/artifacts/{ident}/media-info").status_code == 401


def test_media_endpoints_reject_nonvideo_and_outside_paths(client, new_job, environment):
    with session() as db:
        row = Artifact(
            job_id=new_job["id"],
            task_id=new_job["tasks"][0]["id"],
            artifact_type="LOG",
            storage="workspace",
            path="test.log",
            size=1,
            info={},
        )
        db.add(row)
        db.commit()
        ident = row.id
    assert client.get(f"/api/artifacts/{ident}/media-info").status_code == 409
    with session() as db:
        row = db.get(Artifact, ident)
        row.artifact_type = "FINAL_MKV"
        row.path = "../escape.mkv"
        db.commit()
    assert client.get(f"/api/artifacts/{ident}/media-info").status_code == 409


def test_video_bitrate_never_uses_container_rate():
    probe = {"streams": [{"codec_type": "video"}], "format": {"bit_rate": "99999999"}}
    assert video_bitrate(probe) is None
    probe["streams"][0]["tags"] = {"BPS-eng": "12345678"}
    assert video_bitrate(probe) == 12345678
    assert (
        video_bitrate(
            media_info={
                "media": {
                    "track": [
                        {"@type": "General", "OverallBitRate": "99999999"},
                        {"@type": "Video", "BitRate": "12000000"},
                    ]
                }
            }
        )
        == 12000000
    )


def test_legacy_encoder_summary_and_cached_source_video_bitrate(client, new_job, environment):
    ident = new_job["tasks"][0]["id"]
    text = "\n".join(
        "x265 [info]: " + line
        for line in [
            "Main 10 profile, Level-4 (Main tier)",
            "frame I: 20, Avg QP:15",
            "frame P: 100, Avg QP:18",
            "frame B: 500, Avg QP:22",
            "Weighted P-Frames: Y:12%",
            "Weighted B-Frames: Y:11%",
            "consecutive B-frames: 5% 95%",
        ]
    )
    workspace = environment.workspace_root / new_job["id"]
    log = workspace / "logs" / f"{ident}.log"
    log.write_text("earlier pass\n" + text + "\n")
    metadata = workspace / "metadata" / "ffprobe.json"
    metadata.write_text(
        json.dumps(
            {
                "streams": [{"codec_type": "video", "tags": {"BPS": "12345678"}}],
                "format": {"bit_rate": "99000000"},
            }
        )
    )
    with session() as db:
        task = db.get(Task, ident)
        task.type = "encode"
        task.status = "SUCCEEDED"
        job = db.get(MovieJob, new_job["id"])
        job.analysis = {"video": {"bit_rate": 99000000}}
        db.add(
            Artifact(
                job_id=job.id,
                task_id=ident,
                artifact_type="SOURCE_METADATA",
                storage="workspace",
                path="metadata/ffprobe.json",
                size=metadata.stat().st_size,
                info={},
            )
        )
        db.commit()
    response = client.get(f"/api/jobs/{new_job['id']}/encoder-info")
    assert response.status_code == 200, response.text
    assert response.json()["text"].strip() == text
    assert client.get(f"/api/jobs/{new_job['id']}").json()["analysis"]["video"]["bit_rate"] == 12345678
    assert encoder_summary(
        "x264 [info]: profile High\nx264 [info]: frame I: 1\nx264 [info]: frame I: 2", "x264"
    ).endswith("frame I: 2")


def test_preview_limits_and_cancellation():
    import asyncio
    import sys

    from fastapi import HTTPException

    from backend.app.media_preview import MediaPreview, connected

    async def check():
        preview = MediaPreview()
        command = [sys.executable, "-c", "import time; time.sleep(30)"]
        work = [asyncio.create_task(preview.run(command)) for _ in range(3)]
        try:
            for _ in range(100):
                if len(preview.processes) == 2 and preview.pending == 3:
                    break
                await asyncio.sleep(0.01)
            assert len(preview.processes) == 2 and preview.pending == 3
        finally:
            for task in work:
                task.cancel()
            await asyncio.gather(*work, return_exceptions=True)
        assert not preview.processes and preview.pending == 0
        with pytest.raises(HTTPException) as timed:
            await preview.run(command, timeout=0.01)
        assert timed.value.status_code == 504 and not preview.processes

        class Disconnected:
            async def is_disconnected(self):
                return True

        with pytest.raises(HTTPException) as closed:
            await connected(Disconnected(), preview.run(command))
        assert closed.value.status_code == 499
        assert not preview.processes and preview.pending == 0
        preview.pending = 8
        with pytest.raises(HTTPException) as busy:
            await preview.run(command)
        assert busy.value.status_code == 429
        preview.pending = 0
        await preview.close()

    asyncio.run(check())
