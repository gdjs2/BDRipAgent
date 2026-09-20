from datetime import timedelta

from sqlalchemy import select

from backend.app.services import reconcile
from shared.db import session
from shared.models import MovieJob, Task, now
from tests.conftest import gate


def test_discovery_ignores_partial_and_escaping_symlinks(client, environment, tmp_path):
    (environment.source_root / "valid.mkv").write_bytes(b"complete")
    (environment.source_root / "copy.mkv.partial").write_bytes(b"incomplete")
    external = tmp_path / "outside.mkv"
    external.write_bytes(b"external")
    (environment.source_root / "escape.mkv").symlink_to(external)
    assert client.get("/api/sources").json() == [{"path": "valid.mkv", "size": 8}]


def test_authenticated_cookie_and_unauthenticated_access(client, environment):
    client.headers.clear()
    assert client.get("/api/jobs").status_code == 401
    assert client.post("/api/session", json={"token": "wrong"}).status_code == 401
    response = client.post("/api/session", json={"token": environment.api_token})
    assert response.status_code == 200
    assert "HttpOnly" in response.headers["set-cookie"]
    assert "SameSite=strict" in response.headers["set-cookie"]
    assert client.get("/api/jobs").status_code == 200
    client.delete("/api/session")
    assert client.get("/api/jobs").status_code == 401


def test_job_creation_is_persisted_and_automatically_queued(client, new_job, environment):
    assert new_job["state"] == "ANALYZING_SOURCE"
    assert len(new_job["tasks"]) == 1
    assert new_job["tasks"][0]["status"] == "QUEUED"
    assert (environment.workspace_root / new_job["id"] / "manifest.yaml").is_file()
    assert client.get(f"/api/jobs/{new_job['id']}").json()["title"] == "Movie"


def test_paths_and_unfinished_sources_are_rejected(client, environment):
    for name in ["../outside.mkv", "/etc/passwd", "Movie.mkv.partial"]:
        response = client.post("/api/jobs", json={"source_path": name, "title": "Movie", "year": 2026})
        assert response.status_code == 409


def test_human_gates_cannot_be_skipped(client, new_job):
    prefix = f"/api/jobs/{new_job['id']}"
    assert client.post(prefix + "/encode", json={}).status_code == 409
    assert (
        client.post(
            prefix + "/encode-selection", json={"codec": "x265", "profile": "x265-live", "crf": 17}
        ).status_code
        == 409
    )
    assert (
        client.post(
            prefix + "/tracks/selection", json={"audio_track_ids": [], "subtitle_track_ids": []}
        ).status_code
        == 409
    )


def test_track_selection_checks_type_and_persists_order(client, new_job):
    tracks = [
        {"track_id": 4, "kind": "audio", "info": {"codec_id": "A_DTS", "extractable": True}},
        {"track_id": 8, "kind": "subtitles", "info": {"codec_id": "S_TEXT/UTF8"}},
        {"track_id": 9, "kind": "subtitles", "info": {"codec_id": "S_HDMV/PGS"}},
    ]
    gate(new_job["id"], "WAITING_FOR_TRACK_SELECTION", tracks)
    url = f"/api/jobs/{new_job['id']}/tracks/selection"
    assert client.post(url, json={"audio_track_ids": [9], "subtitle_track_ids": []}).status_code == 409
    assert client.post(url, json={"audio_track_ids": [4], "subtitle_track_ids": [8]}).status_code == 409
    assert client.post(url, json={"audio_track_ids": [4, 4], "subtitle_track_ids": []}).status_code == 422
    response = client.post(url, json={"audio_track_ids": [4], "subtitle_track_ids": [9]})
    assert response.status_code == 200
    assert response.json()["state"] == "PREPARING_TRACKS"
    assert response.json()["track_selection"]["subtitle_track_ids"] == [9]
    assert client.post(url, json={"audio_track_ids": [], "subtitle_track_ids": []}).status_code == 409


def test_encode_selection_requires_analyzed_profile_and_stores_snapshot(client, new_job):
    gate(new_job["id"], "WAITING_FOR_ENCODE_SELECTION", profile="x265-live")
    url = f"/api/jobs/{new_job['id']}/encode-selection"
    assert client.post(url, json={"codec": "x264", "profile": "x264-live", "crf": 17}).status_code == 409
    result = client.post(url, json={"codec": "x265", "profile": "x265-live", "crf": 17.5})
    assert result.status_code == 200, result.text
    assert result.json()["state"] == "ENCODING"
    saved = result.json()["encode_config"]["data"]
    assert saved["selected_by"] == "user"
    assert saved["profile_snapshot"]["encoder"] == "x265_10bit"


def test_repeated_enqueue_does_not_duplicate_task(client, new_job):
    for _ in range(3):
        response = client.post(f"/api/jobs/{new_job['id']}/analyze", json={})
        assert response.json()["id"] == new_job["tasks"][0]["id"]
    assert len(client.get(f"/api/jobs/{new_job['id']}/tasks").json()) == 1


def test_recovery_fences_stale_worker_and_retry_keeps_stage(client, new_job):
    task_id = new_job["tasks"][0]["id"]
    with session() as db:
        task = db.get(Task, task_id)
        task.status, task.run_token = "RUNNING", "old-worker"
        task.heartbeat_at = now() - timedelta(minutes=10)
        db.commit()
        reconcile(db)
        db.refresh(task)
        assert task.status == "FAILED"
        assert task.run_token is None
        assert db.get(MovieJob, new_job["id"]).state == "ANALYZING_SOURCE"
    result = client.post(f"/api/tasks/{task_id}/retry", json={})
    assert result.status_code == 202
    assert result.json()["attempt"] == 2
    assert result.json()["id"] != task_id
    assert client.post(f"/api/tasks/{task_id}/retry", json={}).status_code == 409


def test_live_lease_survives_api_restart(client, new_job):
    with session() as db:
        task = db.get(Task, new_job["tasks"][0]["id"])
        task.status, task.heartbeat_at = "RUNNING", now()
        db.commit()
        reconcile(db)
        assert db.get(Task, task.id).status == "RUNNING"


def test_cancel_queued_task_and_delete_never_deletes_source(client, new_job, environment):
    job_id, task_id = new_job["id"], new_job["tasks"][0]["id"]
    assert client.post(f"/api/tasks/{task_id}/cancel", json={}).json()["status"] == "CANCELLED"
    assert client.delete(f"/api/jobs/{job_id}?confirm=no").status_code == 409
    assert client.delete(f"/api/jobs/{job_id}?confirm={job_id}").status_code == 200
    assert (environment.source_root / "Movie.mkv").is_file()
    assert client.get(f"/api/jobs/{job_id}").status_code == 404


def test_worker_success_failure_and_duplicate_delivery(client, new_job, monkeypatch):
    from worker.pipeline.stages import HANDLERS
    from worker.tasks import execute

    calls = []
    monkeypatch.setitem(HANDLERS, "analyze", lambda ctx: calls.append(ctx.task_id))
    task_id = new_job["tasks"][0]["id"]
    execute(task_id)
    execute(task_id)
    assert calls == [task_id]
    job = client.get(f"/api/jobs/{new_job['id']}").json()
    assert job["state"] == "WAITING_FOR_TRACK_SELECTION"
    assert job["tasks"][0]["status"] == "SUCCEEDED"
    client.post(
        f"/api/jobs/{new_job['id']}/tracks/selection", json={"audio_track_ids": [], "subtitle_track_ids": []}
    )
    with session() as db:
        task = db.scalar(select(Task).where(Task.job_id == new_job["id"], Task.status == "QUEUED"))

    def fail(ctx):
        raise RuntimeError("expected fixture failure")

    monkeypatch.setitem(HANDLERS, "prepare_tracks", fail)
    execute(task.id)
    job = client.get(f"/api/jobs/{new_job['id']}").json()
    assert job["state"] == "PREPARING_TRACKS"
    assert any(
        t["status"] == "FAILED" and t["error_message"] == "expected fixture failure" for t in job["tasks"]
    )


def test_task_logs_are_bounded_and_tail_is_available(client, new_job, environment):
    task = new_job["tasks"][0]
    path = environment.workspace_root / new_job["id"] / task["log_path"]
    path.write_text("first\n" + "x" * 1000 + "\nlast\n")
    response = client.get(f"/api/tasks/{task['id']}/logs?tail=true&limit=20").json()
    assert response["text"].endswith("last\n")
    assert len(response["text"]) == 20


def test_running_task_cancellation_terminates_subprocess(client, new_job, monkeypatch):
    import sys
    import threading
    import time

    from worker.pipeline.stages import HANDLERS
    from worker.tasks import execute

    started = threading.Event()

    def long_running(ctx):
        started.set()
        ctx.run([sys.executable, "-c", "import time; time.sleep(60)"])

    monkeypatch.setitem(HANDLERS, "analyze", long_running)
    task_id = new_job["tasks"][0]["id"]
    thread = threading.Thread(target=execute, args=(task_id,), daemon=True)
    thread.start()
    assert started.wait(5)
    time.sleep(0.1)
    assert client.post(f"/api/tasks/{task_id}/cancel", json={}).status_code == 200
    thread.join(12)
    assert not thread.is_alive(), "Cancelled subprocess did not stop"
    result = client.get(f"/api/tasks/{task_id}").json()
    assert result["status"] == "CANCELLED"
    assert client.get(f"/api/jobs/{new_job['id']}").json()["state"] == "ANALYZING_SOURCE"


def test_artifact_download_rejects_escaping_symlink(client, new_job, environment, tmp_path):
    from shared.models import Artifact

    external = tmp_path / "private.txt"
    external.write_text("must not be served")
    workspace = environment.workspace_root / new_job["id"]
    (workspace / "escaped.txt").symlink_to(external)
    with session() as db:
        artifact = Artifact(
            job_id=new_job["id"],
            task_id=new_job["tasks"][0]["id"],
            artifact_type="LOG",
            path="escaped.txt",
            size=18,
        )
        db.add(artifact)
        db.commit()
        artifact_id = artifact.id
    assert client.get(f"/api/artifacts/{artifact_id}").status_code == 409
