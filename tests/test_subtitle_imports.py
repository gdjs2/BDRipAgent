from sqlalchemy import select

from shared.db import session
from tests.test_source_choices import CHOICES, read, ready, save
from tests.test_source_sharing import peer
from tests.test_subtitle_uploads import SRT, upload
from worker.tasks import execute


def test_upload_quarantine_sharing_failure_retry_and_auth(client, new_job, environment):
    job_id = new_job["id"]
    ready(job_id)
    sibling = peer(client)["id"]
    ready(sibling)
    before = read(client, job_id)
    response = upload(client, job_id)
    assert response.status_code == 201 and response.json()["queued"]
    task_id = response.json()["task_id"]
    upload_id = response.json()["upload_id"]
    for ident in (job_id, sibling):
        job = read(client, ident)
        assert all(t["track_id"] < 1000000 for t in job["tracks"])
        assert job["subtitle_discovery"]["active_task"]["type"] == "review_uploaded_subtitle"
        assert job["subtitle_uploads"][0]["status"] == "QUEUED"
        assert save(client, ident, CHOICES).status_code == 409
        assert client.get(f"/api/jobs/{ident}/subtitles/uploads/{upload_id}/download").content == SRT
    assert upload(client, sibling).status_code == 409
    assert task_id in {t["id"] for t in client.get("/api/queue").json()["queued"]}
    execute(task_id)  # No reference cues: fail before contacting the agent.
    current = read(client, job_id)
    assert current["subtitle_uploads"][0]["status"] == "FAILED"
    assert "source subtitle cues" in current["subtitle_uploads"][0]["error"]
    assert current["tracks"] == before["tracks"]
    retry = client.post(f"/api/tasks/{task_id}/retry", json={})
    assert retry.status_code == 202, retry.text
    execute(retry.json()["id"])
    assert read(client, sibling)["subtitle_uploads"][0]["status"] == "FAILED"
    assert (
        "original subtitle upload is not available"
        not in read(client, sibling)["subtitle_uploads"][0]["error"]
    )
    client.headers.clear()
    assert client.get(f"/api/jobs/{job_id}/subtitles/uploads/{upload_id}/download").status_code == 401


def test_native_download_reuses_retained_audio_and_subtitles_and_contains_paths(client, new_job, environment):
    ready(new_job["id"])
    from shared.models import MovieTrack

    with session() as db:
        for tid, suffix in [(4, "ac3"), (9, "sup")]:
            path = environment.workspace_root / new_job["id"] / f"track-{tid}.{suffix}"
            path.write_bytes(b"retained track " + str(tid).encode())
            row = db.scalar(
                select(MovieTrack).where(MovieTrack.job_id == new_job["id"], MovieTrack.track_id == tid)
            )
            row.info = {**row.info, "source_track_path": path.name}
        db.commit()
    for tid in (4, 9):
        result = client.get(f"/api/jobs/{new_job['id']}/tracks/{tid}/download")
        assert result.status_code == 200 and result.content == b"retained track " + str(tid).encode()
        assert "attachment" in result.headers["content-disposition"]
    assert client.get(f"/api/jobs/{new_job['id']}/tracks/999/download").status_code == 404
    assert client.get(f"/api/jobs/{new_job['id']}/tracks/9/download?variant=../../secret").status_code == 409
    with session() as db:
        row = db.scalar(
            select(MovieTrack).where(MovieTrack.job_id == new_job["id"], MovieTrack.track_id == 9)
        )
        row.info = {**row.info, "source_track_path": "../outside.sup"}
        db.commit()
    assert client.get(f"/api/jobs/{new_job['id']}/tracks/9/download").status_code == 409
    client.headers.clear()
    assert client.get(f"/api/jobs/{new_job['id']}/tracks/4/download").status_code == 401


def test_uncached_audio_and_text_tracks_download_without_reencoding(client, environment, monkeypatch):
    import json
    import subprocess

    from backend.app import track_downloads
    from shared.models import MovieTrack

    subtitle = environment.source_root / "input.srt"
    subtitle.write_bytes(SRT)
    source = environment.source_root / "Movie.mkv"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=size=64x64:rate=1:duration=3",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=3",
            "-i",
            str(subtitle),
            "-map",
            "0:v",
            "-map",
            "1:a",
            "-map",
            "2:s",
            "-c:v",
            "ffv1",
            "-c:a",
            "pcm_s16le",
            "-c:s",
            "srt",
            str(source),
        ],
        check=True,
    )
    job = client.post(
        "/api/jobs", json={"source_path": source.name, "title": "Download fixture", "year": 2026}
    ).json()
    with session() as db:
        for tid, kind, index in [(10, "audio", 1), (20, "subtitles", 2)]:
            db.add(
                MovieTrack(
                    job_id=job["id"],
                    track_id=tid,
                    kind=kind,
                    info={
                        "track_id": tid,
                        "kind": kind,
                        "language": "en",
                        "ffprobe_index": index,
                        "source_order": index,
                    },
                )
            )
        db.commit()
    for tid, codec in [(10, "pcm_s16le"), (20, "subrip")]:
        result = client.get(f"/api/jobs/{job['id']}/tracks/{tid}/download")
        assert result.status_code == 200, result.text
        path = environment.workspace_root / f"download-{tid}.mkv"
        path.write_bytes(result.content)
        streams = json.loads(
            subprocess.check_output(["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(path)])
        )["streams"]
        assert len(streams) == 1 and streams[0]["codec_name"] == codec

    def no_extract(*args, **kwargs):
        raise AssertionError("A second download must reuse the retained export")

    monkeypatch.setattr(track_downloads.subprocess, "run", no_extract)
    assert client.get(f"/api/jobs/{job['id']}/tracks/10/download").status_code == 200


def test_upload_retry_cannot_overlap_another_review_for_the_source(client, new_job):
    ready(new_job["id"])
    other = peer(client)["id"]
    ready(other)
    response = upload(client, new_job["id"])
    task_id = response.json()["task_id"]
    execute(task_id)
    assert upload(client, other).status_code == 201
    retry = client.post(f"/api/tasks/{task_id}/retry", json={})
    assert retry.status_code == 409
