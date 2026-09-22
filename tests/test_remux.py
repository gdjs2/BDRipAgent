from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from backend.app.services import advance
from shared.db import session
from shared.models import Artifact, MovieJob, Task, now
from shared.subtitles import subtitle_is_resolved
from tests.test_source_choices import CHOICES, read, ready
from worker.output_backups import cleanup_expired_outputs, retire_outputs
from worker.pipeline.release_exports import publish


@pytest.fixture
def completed_job(client, new_job, environment):
    job_id = new_job["id"]
    ready(job_id)
    assert client.post(f"/api/jobs/{job_id}/tracks/selection", json=CHOICES).status_code == 200
    workspace = environment.workspace_root / job_id
    (workspace / "encoded.mkv").write_bytes(b"retained video")
    final = environment.completed_root / job_id / "old-WiKi.mkv"
    final.parent.mkdir()
    final.write_bytes(b"previous mux")
    with session() as db:
        job = db.get(MovieJob, job_id)
        job.state = "COMPLETE"
        job.validation = {"valid": True}
        job.analysis = {
            **job.analysis,
            "final_path": f"{job_id}/old-WiKi.mkv",
            "encoded_path": "encoded.mkv",
            "prepared_tracks": [],
        }
        task = db.scalar(select(Task).where(Task.job_id == job_id))
        db.add(
            Artifact(
                job_id=job_id,
                task_id=task.id,
                artifact_type="FINAL_MKV",
                storage="completed",
                path=f"{job_id}/old-WiKi.mkv",
                size=final.stat().st_size,
            )
        )
        db.commit()
    return job_id


def test_remux_queues_preparation_once_and_retains_video(client, completed_job, environment):
    before = read(client, completed_job)
    assert before["remux"]["available"] and before["remux"]["backup_days"] == 3
    response = client.post(
        f"/api/jobs/{completed_job}/remux", json={**CHOICES, "shared_revision": 1, "audio_track_ids": [4, 8]}
    )
    assert response.status_code == 202, response.text
    job = response.json()
    assert job["state"] == "PREPARING_TRACKS"
    assert job["track_selection"]["audio_track_ids"] == [4, 8]
    assert [t["type"] for t in job["tasks"] if t["status"] == "QUEUED"] == ["prepare_tracks"]
    assert job["analysis"]["encoded_path"] == before["analysis"]["encoded_path"]
    assert (environment.completed_root / before["analysis"]["final_path"]).read_bytes() == b"previous mux"
    assert client.post(f"/api/jobs/{completed_job}/remux", json=CHOICES).status_code == 409
    with session() as db:
        stored = db.get(MovieJob, completed_job)
        for task in db.scalars(select(Task).where(Task.job_id == completed_job)):
            task.status = "SUCCEEDED"
        db.flush()
        advance(db, stored)
        assert stored.state == "REMUXING"


def test_remux_requires_retained_inputs(client, completed_job, environment):
    (environment.workspace_root / completed_job / "encoded.mkv").unlink()
    assert not read(client, completed_job)["remux"]["available"]
    assert (
        client.post(f"/api/jobs/{completed_job}/remux", json={**CHOICES, "shared_revision": 1}).status_code
        == 409
    )


def test_remux_refreshes_chosen_screenshots_without_agent_rescan(client, completed_job):
    with session() as db:
        job = db.get(MovieJob, completed_job)
        job.state = "REMUXING"
        job.analysis = {**job.analysis, "remux_revision": {"reuse_screenshots": True}}
        advance(db, job)
        assert job.state == "SCREENSHOT_RENDERING"
        assert (
            db.scalar(select(Task).where(Task.job_id == job.id, Task.status == "QUEUED")).type
            == "render_screenshots"
        )


def test_language_code_verification_and_shared_override(client, new_job):
    ready(new_job["id"])
    from tests.test_source_sharing import peer

    sibling = peer(client)
    ready(sibling["id"])
    url = f"/api/jobs/{new_job['id']}/tracks/9/language"
    for entered, code, name in [
        ("fre", "fr", "French"),
        ("zh-hant", "zh-Hant", "Chinese"),
        ("jpn", "ja", "Japanese"),
    ]:
        response = client.get(url, params={"code": entered})
        assert response.status_code == 200, response.text
        assert response.json()["code"] == code and name in response.json()["language_name"]
    for code in ("und", "zzzz", "en-x-private", "en--US"):
        assert client.get(url, params={"code": code}).status_code in (409, 422)
    body = {**CHOICES, "track_languages": {"9": "jpn", "8": "fre"}, "track_names": {}}
    response = client.post(f"/api/jobs/{new_job['id']}/tracks/selection", json=body)
    assert response.status_code == 200, response.text
    tracks = {t["track_id"]: t["info"] for t in response.json()["tracks"]}
    assert tracks[9]["language"] == "ja" and tracks[9]["language_name"] == "Japanese"
    assert tracks[9]["subtitle_detection"]["language_code"] == "en", "Keep original evidence intact"
    assert subtitle_is_resolved(tracks[9])
    assert tracks[8]["language"] == "fr"
    shared = {t["track_id"]: t["info"] for t in read(client, sibling["id"])["tracks"]}
    assert shared[9]["language_override"] == "ja" and shared[8]["language_override"] == "fr"
    assert (
        client.post(
            f"/api/jobs/{new_job['id']}/tracks/selection",
            json={**body, "shared_revision": 1, "track_languages": {"9": "und"}},
        ).status_code
        == 422
    )


def test_uncertain_subtitle_can_be_confirmed_manually(client, new_job):
    from shared.models import MovieTrack

    ready(new_job["id"])
    with session() as db:
        row = db.scalar(
            select(MovieTrack).where(MovieTrack.job_id == new_job["id"], MovieTrack.track_id == 9)
        )
        row.info = {
            **row.info,
            "subtitle_detection": {
                **row.info["subtitle_detection"],
                "language_confident": False,
                "language_code": "und",
            },
        }
        db.commit()
    response = client.post(
        f"/api/jobs/{new_job['id']}/tracks/selection", json={**CHOICES, "track_languages": {"9": "deu"}}
    )
    assert response.status_code == 200, response.text
    assert subtitle_is_resolved(next(t["info"] for t in response.json()["tracks"] if t["track_id"] == 9))


def retire(client, job_id):
    with session() as db:
        job = db.get(MovieJob, job_id)
        job.analysis = {**job.analysis, "final_path": f"{job_id}/new-WiKi.mkv"}
        retire_outputs(db, job, kinds={"FINAL_MKV"})
        db.commit()
        return db.scalar(select(Artifact).where(Artifact.job_id == job_id)).info["backup"]


def expire(job_id):
    with session() as db:
        row = db.scalar(select(Artifact).where(Artifact.job_id == job_id))
        row.info = {
            **row.info,
            "backup": {**row.info["backup"], "expires_at": (now() - timedelta(seconds=1)).isoformat()},
        }
        db.commit()


def test_backups_expire_only_after_three_days_and_never_touch_inputs(client, completed_job, environment):
    backup = retire(client, completed_job)
    from datetime import datetime

    assert datetime.fromisoformat(backup["expires_at"]) - datetime.fromisoformat(
        backup["replaced_at"]
    ) == timedelta(days=3)
    old = environment.completed_root / completed_job / "old-WiKi.mkv"
    cleanup_expired_outputs()
    assert old.exists()
    expire(completed_job)
    cleanup_expired_outputs()
    assert not old.exists()
    assert (environment.workspace_root / completed_job / "encoded.mkv").read_bytes() == b"retained video"
    assert (environment.source_root / "Movie.mkv").exists()
    assert read(client, completed_job)["artifacts"] == []


@pytest.mark.parametrize("changed", ["current", "modified", "symlink", "active"])
def test_cleanup_preserves_current_changed_or_busy_outputs(client, completed_job, environment, changed):
    retire(client, completed_job)
    expire(completed_job)
    old = environment.completed_root / completed_job / "old-WiKi.mkv"
    if changed == "modified":
        old.write_bytes(b"User replacement")
    elif changed == "symlink":
        original = old.with_suffix(".saved")
        old.rename(original)
        old.symlink_to(original)
    else:
        with session() as db:
            if changed == "current":
                job = db.get(MovieJob, completed_job)
                job.analysis = {**job.analysis, "final_path": f"{completed_job}/old-WiKi.mkv"}
            else:
                db.scalar(select(Task).where(Task.job_id == completed_job)).status = "RUNNING"
            db.commit()
    cleanup_expired_outputs()
    assert old.is_file()


def test_release_regeneration_has_separate_bundle_and_allows_new_name(environment):
    from tests.test_release_exports import context, outputs

    ctx = context(environment)
    ctx.task_id = "first"
    items = outputs(ctx)
    first = publish(ctx, items)
    ctx.task_id = "second"
    ctx.job.release_name = "Renamed.2026.1080p.BluRay.x264-WiKi"
    second = publish(ctx, items)
    assert first != second
    assert all(Path(item["path"]).is_file() for item in first + second)
    assert publish(ctx, items) == second


@pytest.mark.parametrize("external_subtitles", [False, True])
def test_native_remux_and_release_regeneration(client, environment, monkeypatch, external_subtitles):
    """Use real MKVToolNix, screenshot rendering, MD5 and pinned torrent generation."""
    import hashlib
    import json
    import shutil
    import subprocess

    from shared.models import EncodeConfig, MovieTrack, Screenshot
    from worker.tasks import execute

    if not shutil.which("mkvmerge") or not Path(environment.bdrip_python).exists():
        pytest.skip("Requires the worker's native media tools and pinned BDRip environment")

    def run(*args):
        return subprocess.run([str(a) for a in args], check=True, capture_output=True, text=True)

    source = environment.source_root / "Native.mkv"
    encoded_log = run(
        "ffmpeg",
        "-v",
        "info",
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=320x180:rate=24:duration=3",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:sample_rate=48000:duration=3",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=880:sample_rate=48000:duration=3",
        "-map",
        "0:v",
        "-map",
        "1:a",
        "-map",
        "2:a",
        "-c:v",
        "libx264",
        "-bf",
        "3",
        "-c:a",
        "ac3",
        source,
    ).stderr
    created = client.post(
        "/api/jobs",
        json={
            "source_path": source.name,
            "title": "Native Remux",
            "year": 2026,
            "analysis_profile": "x264-live",
        },
    ).json()
    job_id = created["id"]
    workspace = environment.workspace_root / job_id
    encoded = workspace / "retained-video.mkv"
    run("mkvmerge", "-o", encoded, "--no-audio", source)
    old = environment.completed_root / job_id / (created["release_name"] + ".mkv")
    old.parent.mkdir()
    shutil.copy2(source, old)
    encoded_hash = hashlib.sha256(encoded.read_bytes()).hexdigest()
    tracks = [
        {
            "track_id": i,
            "kind": "audio",
            "codec": "AC-3",
            "codec_id": "A_AC3",
            "channels": 1,
            "language": "en",
            "extractable": True,
            "default": False,
            "forced": False,
            "hearing_impaired": False,
            "visual_impaired": False,
            "commentary": False,
        }
        for i in (1, 2)
    ]

    def frames(path):
        return json.loads(
            run(
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_frames",
                "-show_entries",
                "frame=best_effort_timestamp_time,pict_type",
                "-of",
                "json",
                path,
            ).stdout
        )["frames"]

    original_frames, encoded_frames = frames(source), frames(encoded)
    index, frame = next((i, f) for i, f in enumerate(original_frames) if f["pict_type"] == "B")
    log = workspace / "original-encode.log"
    log.write_text(encoded_log)
    from PIL import Image

    for name in ("before.src.png", "before.encode.png"):
        Image.new("RGB", (320, 180)).save(workspace / name)
    with session() as db:
        job = db.get(MovieJob, job_id)
        job.state = "WAITING_FOR_RELEASE_DETAILS"
        job.analysis = {
            "tracks": tracks,
            "track_review_version": 1,
            "encoded_path": encoded.name,
            "final_path": str(old.relative_to(environment.completed_root)),
            "video": {"width": 320, "height": 180, "duration": 3},
            "crop": {"top": 0, "bottom": 0, "left": 0, "right": 0},
            "screenshot_selection": {"count": 1, "candidate_ids": [1]},
        }
        job.validation = {
            "valid": True,
            "metrics": {
                "source_first_pts": float(original_frames[0]["best_effort_timestamp_time"]),
                "encoded_first_pts": float(encoded_frames[0]["best_effort_timestamp_time"]),
                "source_duration": 3,
            },
        }
        task = db.scalar(select(Task).where(Task.job_id == job_id))
        task.type, task.status, task.log_path = "encode", "SUCCEEDED", log.name
        db.add(
            Artifact(
                job_id=job_id,
                task_id=task.id,
                artifact_type="FINAL_MKV",
                storage="completed",
                path=job.analysis["final_path"],
                size=old.stat().st_size,
            )
        )
        for track in tracks:
            db.add(MovieTrack(job_id=job_id, track_id=track["track_id"], kind="audio", info=track))
        db.add(EncodeConfig(job_id=job_id, data={"codec": "x264", "execution_mode": "normal"}))
        db.add(
            Screenshot(
                job_id=job_id,
                candidate_id=1,
                selected=True,
                info={
                    "source_frame_number": index,
                    "source_total_frames": len(original_frames),
                    "source_pts_seconds": float(frame["best_effort_timestamp_time"]),
                    "b_frames_verified": True,
                    "picture_type": "B",
                    "encoded_picture_type": "B",
                    "comparisons": {"src": "before.src.png", "encode": "before.encode.png"},
                },
            )
        )
        db.commit()
    details = {
        "chinese_name": "测试",
        "source": "Fixture Blu-ray",
        "tracker": "https://example.com/announce",
        "upload_screenshots": False,
    }

    def execute_next(kind):
        job = read(client, job_id)
        task = next(t for t in job["tasks"] if t["status"] == "QUEUED")
        assert task["type"] == kind
        execute(task["id"])
        result = read(client, job_id)
        finished = next(t for t in result["tasks"] if t["id"] == task["id"])
        assert finished["status"] == "SUCCEEDED", finished.get("error_message")
        return result

    assert client.post(f"/api/jobs/{job_id}/release", json=details).status_code == 202
    first = execute_next("generate_release")["analysis"]["release_result"]
    first_media = environment.artifacts_root / next(
        a["path"] for a in first["artifacts"] if a["kind"] == "RELEASE_MEDIA"
    )
    first_bytes = first_media.read_bytes()
    uploads = []
    if external_subtitles:
        import runpy

        from tests.test_subtitle_uploads import ASS, SRT
        from tests.test_subtitle_uploads import registered_upload as upload

        pgs = runpy.run_path("/opt/sup2sup/tests/fixtures.py")["simple"]()
        for filename, content in [("English.srt", SRT), ("French.ass", ASS), ("English.sup", pgs)]:
            response = upload(client, job_id, content, filename)
            assert response.status_code == 201, response.text
            uploads.append(response.json()["track"])
    subtitle_ids = [t["track_id"] for t in reversed(uploads)]
    response = client.post(
        f"/api/jobs/{job_id}/remux",
        json={
            "audio_track_ids": [2, 1],
            "subtitle_track_ids": subtitle_ids,
            "track_languages": {"2": "jpn"},
            "track_names": {"2": "Japanese commentary"},
            "track_flags": {"2": {"commentary": True}},
        },
    )
    assert response.status_code == 202, response.text
    execute_next("prepare_tracks")
    revised = execute_next("mux")
    assert revised["state"] == "SCREENSHOT_RENDERING"
    movie = environment.completed_root / revised["analysis"]["final_path"]
    assert movie != old and old.exists()
    inspection = json.loads(run("mkvmerge", "-J", movie).stdout)
    assert inspection["container"]["properties"]["title"] == "Native Remux (2026)"
    assert movie.name == revised["release_name"] + ".mkv"
    actual = inspection["tracks"]
    assert [t["type"] for t in actual] == ["video", "audio", "audio"] + ["subtitles"] * len(uploads)
    if uploads:
        assert [t["properties"]["codec_id"] for t in actual[3:]] == [
            "S_HDMV/PGS",
            "S_TEXT/ASS",
            "S_TEXT/UTF8",
        ]
        assert all(
            t["properties"]["language_ietf"] == "en" and t["properties"]["flag_hearing_impaired"]
            for t in actual[3:]
        )
        assert [t["track_id"] for t in revised["analysis"]["prepared_tracks"]] == [2, 1, *subtitle_ids]
    assert actual[1]["properties"]["track_name"] == "Japanese commentary"
    assert actual[1]["properties"]["language_ietf"] == "ja"
    assert actual[1]["properties"]["flag_commentary"]
    execute_next("render_screenshots")
    assert client.post(f"/api/jobs/{job_id}/release", json=details).status_code == 202
    final = execute_next("generate_release")
    second = final["analysis"]["release_result"]
    assert final["state"] == "COMPLETE" and second["bundle_path"] != first["bundle_path"]
    assert second["md5"] == hashlib.md5(movie.read_bytes()).hexdigest()
    assert second["infohash"] != first["infohash"]
    assert first_media.read_bytes() == first_bytes
    assert hashlib.sha256(encoded.read_bytes()).hexdigest() == encoded_hash
    assert sum(t["type"] == "encode" for t in final["tasks"]) == 1
    assert any(a["info"].get("backup") for a in final["artifacts"] if a["artifact_type"] == "RELEASE_TORRENT")
    assert all((workspace / f"source-tracks/track-{i}.ac3").is_file() for i in (1, 2))
    # A second revision selecting fewer tracks reuses the native originals.
    response = client.post(
        f"/api/jobs/{job_id}/remux", json={"audio_track_ids": [1], "subtitle_track_ids": []}
    )
    assert response.status_code == 202
    prepared = execute_next("prepare_tracks")
    last_prepare = sorted(
        (t for t in prepared["tasks"] if t["type"] == "prepare_tracks"), key=lambda t: t["created_at"]
    )[-1]
    assert not last_prepare["command_json"], "A repeat remux must reuse cached native tracks and timestamps"

    from worker.runtime import TaskContext

    pending = next(t for t in prepared["tasks"] if t["status"] == "QUEUED")

    def failed_mux(*args, **kwargs):
        raise RuntimeError("Fixture interrupted merge")

    monkeypatch.setattr(TaskContext, "run", failed_mux)
    execute(pending["id"])
    failed = read(client, job_id)
    assert next(t for t in failed["tasks"] if t["id"] == pending["id"])["status"] == "FAILED"
    assert failed["analysis"]["final_path"] == str(movie.relative_to(environment.completed_root))
    assert movie.is_file() and first_media.is_file()
    with session() as db:
        for row in db.scalars(select(Artifact).where(Artifact.job_id == job_id)):
            if row.info.get("backup"):
                row.info = {
                    **row.info,
                    "backup": {
                        **row.info["backup"],
                        "expires_at": (now() - timedelta(seconds=1)).isoformat(),
                    },
                }
        db.commit()
    cleanup_expired_outputs()
    assert not first_media.exists() and not old.exists()
    assert not (environment.completed_root / job_id / "releases" / first["task_id"]).exists()
    assert movie.exists() and encoded.exists()
    for track in uploads:
        assert (environment.workspace_root / track["info"]["upload_path"]).is_file()
        prepared_track = next(
            t for t in revised["analysis"]["prepared_tracks"] if t["track_id"] == track["track_id"]
        )
        assert (workspace / prepared_track["path"]).is_file()
