import sys
import threading
import time
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from backend.app import queue
from backend.app.services import reconcile
from shared.db import session
from shared.models import Event, MovieJob, Task, now
from worker import runtime
from worker.pipeline.stages import HANDLERS
from worker.tasks import execute


def wait_for(function, timeout=8):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        result = function()
        if result:
            return result
        time.sleep(0.05)
    raise AssertionError("Timed out waiting for worker state")


@pytest.fixture
def encoding_task(client, new_job):
    task_id = new_job["tasks"][0]["id"]
    with session() as db:
        task = db.get(Task, task_id)
        task.type, task.stage = "encode", "ENCODING"
        db.get(MovieJob, new_job["id"]).state = "ENCODING"
        db.commit()
    return task_id


def task_state(client, task_id):
    response = client.get(f"/api/tasks/{task_id}")
    assert response.status_code == 200
    return response.json()


def test_pause_controls_require_a_live_supported_encode_and_auth(client, encoding_task):
    url = f"/api/tasks/{encoding_task}"
    assert client.post(url + "/pause", json={}).status_code == 409
    with session() as db:
        task = db.get(Task, encoding_task)
        task.status = "RUNNING"
        db.commit()
    assert client.post(url + "/pause", json={}).status_code == 409  # old worker / not started
    with session() as db:
        db.get(Task, encoding_task).can_pause = True
        db.commit()
    response = client.post(url + "/pause", json={})
    assert response.status_code == 202
    assert response.json()["pause_requested"] and response.json()["paused_at"] is None
    assert client.post(url + "/pause", json={}).status_code == 202
    with session() as db:
        events = db.scalars(select(Event).where(Event.type == "task_pause_requested")).all()
        assert len(events) == 1
    assert client.post(url + "/resume", json={}).status_code == 202
    assert not task_state(client, encoding_task)["pause_requested"]
    with session() as db:
        db.get(Task, encoding_task).cancel_requested = True
        db.commit()
    assert client.post(url + "/pause", json={}).status_code == 409
    client.headers.clear()
    assert client.post(url + "/resume", json={}).status_code == 401


@pytest.mark.parametrize(
    "kind,status",
    [
        ("analyze", "RUNNING"),
        ("crf_analysis", "RUNNING"),
        ("encode", "SUCCEEDED"),
        ("encode", "FAILED"),
        ("encode", "CANCELLED"),
    ],
)
def test_other_stages_and_terminal_tasks_cannot_be_paused(client, encoding_task, kind, status):
    with session() as db:
        task = db.get(Task, encoding_task)
        task.type, task.status, task.can_pause = kind, status, True
        db.commit()
    for action in ("pause", "resume"):
        assert client.post(f"/api/tasks/{encoding_task}/{action}", json={}).status_code == 409


@pytest.fixture
def live_encode(client, encoding_task, environment, monkeypatch):
    root = environment.workspace_root / task_state(client, encoding_task)["job_id"]
    script = root / "encoder-fixture.py"
    # One process group, containing parent and child, lets us verify the whole encoder stops.
    script.write_text("""import os, subprocess, sys, time
from pathlib import Path
role = sys.argv[1]
Path(role + '.pid').write_text(str(os.getpid()))
child = subprocess.Popen([sys.executable, __file__, 'child']) if role == 'parent' else None
while not Path('finish').exists():
    with Path(role + '.ticks').open('a') as stream:
        stream.write('tick\\n')
    if role == 'parent':
        print('encoding progress', flush=True)
    time.sleep(0.05)
if child:
    child.wait()
""")

    def handler(ctx):
        ctx.run(
            [sys.executable, script, "parent"],
            pausable=True,
            progress_parser=lambda text: {"percentage": 40, "eta_seconds": 20} if text else None,
        )

    monkeypatch.setitem(HANDLERS, "encode", handler)
    thread = threading.Thread(target=execute, args=(encoding_task,), daemon=True)
    thread.start()
    try:
        wait_for(lambda: task_state(client, encoding_task)["can_pause"] and (root / "child.ticks").exists())
        yield encoding_task, root, thread
    finally:
        (root / "finish").touch()
        state = task_state(client, encoding_task)
        if state["status"] == "RUNNING":
            client.post(f"/api/tasks/{encoding_task}/cancel", json={})
        thread.join(12)
        assert not thread.is_alive(), "Fixture encoder did not stop"


def process_state(pid):
    try:
        return Path(f"/proc/{pid}/stat").read_text().split(")", 1)[1].split()[0]
    except FileNotFoundError:
        return None


def pause_and_check(client, task_id, root):
    assert client.post(f"/api/tasks/{task_id}/pause", json={}).status_code == 202
    wait_for(lambda: task_state(client, task_id)["paused_at"])
    pids = [int((root / f"{role}.pid").read_text()) for role in ("parent", "child")]
    wait_for(lambda: all(process_state(pid) == "T" for pid in pids))
    ticks = [(root / f"{role}.ticks").read_text() for role in ("parent", "child")]
    time.sleep(0.2)
    assert ticks == [(root / f"{role}.ticks").read_text() for role in ("parent", "child")]
    return pids, ticks


def test_pause_freezes_process_group_renews_lease_and_resume_keeps_progress(client, live_encode, monkeypatch):
    task_id, root, thread = live_encode
    original_behavior = runtime.behavior
    monkeypatch.setattr(runtime, "behavior", lambda: {**original_behavior(), "command_timeout_seconds": 5})
    pids, ticks = pause_and_check(client, task_id, root)
    paused = task_state(client, task_id)
    time.sleep(6)  # Longer than the command timeout; the worker must renew its lease without timing out.
    current = task_state(client, task_id)
    assert current["status"] == "RUNNING" and current["paused_at"] == paused["paused_at"]
    assert current["heartbeat_at"] != paused["heartbeat_at"]
    assert current["progress"] == paused["progress"]
    assert current["progress_detail"] == paused["progress_detail"]
    with session() as db:
        reconcile(db)
        assert queue.running_count(db) == 1
        assert db.get(Task, task_id).status == "RUNNING"
    row = client.get("/api/queue").json()["running"][0]
    assert row["paused_at"] and row["pause_requested"] and row["can_pause"]
    assert client.post(f"/api/tasks/{task_id}/resume", json={}).status_code == 202
    wait_for(lambda: not task_state(client, task_id)["paused_at"])
    wait_for(
        lambda: (
            (root / "parent.ticks").read_text() != ticks[0] and (root / "child.ticks").read_text() != ticks[1]
        )
    )
    assert pids == [int((root / f"{role}.pid").read_text()) for role in ("parent", "child")]
    (root / "finish").touch()
    thread.join(8)
    result = task_state(client, task_id)
    assert result["status"] == "SUCCEEDED", result["error_message"]
    assert not result["can_pause"] and not result["pause_requested"] and result["paused_at"] is None
    assert len(result["command_json"]) == 1 and result["attempt"] == 1
    assert result["progress_detail"]["elapsed_seconds"] < 5
    with session() as db:
        events = [e.type for e in db.scalars(select(Event))]
        assert "task_paused" in events and "task_resumed" in events


def test_cancel_paused_encoder_terminates_it_and_its_children(client, live_encode):
    task_id, root, thread = live_encode
    pids, _ = pause_and_check(client, task_id, root)
    assert client.post(f"/api/tasks/{task_id}/cancel", json={}).status_code == 200
    thread.join(8)
    assert not thread.is_alive()
    result = task_state(client, task_id)
    assert result["status"] == "CANCELLED"
    assert not result["can_pause"] and result["paused_at"] is None
    wait_for(lambda: all(process_state(pid) in (None, "Z") for pid in pids))


def test_lost_lease_stops_paused_encoder_without_touching_replacement_state(client, live_encode):
    task_id, root, thread = live_encode
    pids, _ = pause_and_check(client, task_id, root)
    with session() as db:
        task = db.get(Task, task_id)
        task.run_token, task.status = "replacement", "FAILED"
        task.can_pause, task.pause_requested, task.paused_at = False, False, None
        db.commit()
    thread.join(8)
    assert not thread.is_alive()
    assert task_state(client, task_id)["status"] == "FAILED"
    wait_for(lambda: all(process_state(pid) in (None, "Z") for pid in pids))


def test_recovery_clears_paused_state_and_retry_starts_unpaused(client, encoding_task):
    with session() as db:
        task = db.get(Task, encoding_task)
        task.status, task.can_pause, task.pause_requested = "RUNNING", True, True
        task.paused_at = now()
        task.heartbeat_at = now() - timedelta(minutes=10)
        db.commit()
        reconcile(db)
        db.refresh(task)
        assert (
            task.status == "FAILED"
            and not task.can_pause
            and not task.pause_requested
            and task.paused_at is None
        )
    response = client.post(f"/api/tasks/{encoding_task}/retry", json={})
    assert response.status_code == 202
    result = response.json()
    assert result["status"] == "QUEUED" and not result["can_pause"] and not result["pause_requested"]


def test_real_handbrake_can_pause_resume_and_finish_valid_video(
    client, encoding_task, environment, monkeypatch
):
    import json
    import shutil
    import subprocess

    from worker.adapters.handbrake import Crop, encode_command, parse_progress

    if not shutil.which(environment.handbrake_bin):
        pytest.skip("HandBrake is required for the native pause check")
    job_id = task_state(client, encoding_task)["job_id"]
    root = environment.workspace_root / job_id
    source = root / "native-source.mkv"
    subprocess.run(
        [
            environment.ffmpeg_bin,
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=640x360:rate=24:duration=8",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            str(source),
        ],
        check=True,
    )
    output = root / "native-encoded.mkv"
    pids = []
    original_popen = runtime.subprocess.Popen

    def popen(*args, **kwargs):
        process = original_popen(*args, **kwargs)
        pids.append(process.pid)
        return process

    monkeypatch.setattr(runtime.subprocess, "Popen", popen)

    def handler(ctx):
        ctx.run(
            encode_command(
                ctx.settings.handbrake_bin,
                source,
                output,
                {"width": 640, "height": 360},
                Crop(top=0, bottom=0, left=0, right=0),
                {"encoder": "x264", "preset": "veryslow", "extra_options": "threads=2"},
                20,
            ),
            pausable=True,
            progress_parser=parse_progress,
        )

    monkeypatch.setitem(HANDLERS, "encode", handler)
    thread = threading.Thread(target=execute, args=(encoding_task,), daemon=True)
    thread.start()
    try:
        wait_for(lambda: task_state(client, encoding_task)["can_pause"])
        assert client.post(f"/api/tasks/{encoding_task}/pause", json={}).status_code == 202
        wait_for(lambda: task_state(client, encoding_task)["paused_at"])
        wait_for(lambda: process_state(pids[0]) == "T")
        time.sleep(0.3)
        assert process_state(pids[0]) == "T"
        assert client.post(f"/api/tasks/{encoding_task}/resume", json={}).status_code == 202
        thread.join(60)
        assert not thread.is_alive()
        result = task_state(client, encoding_task)
        assert result["status"] == "SUCCEEDED", result["error_message"]
        assert len(result["command_json"]) == 1 and len(pids) == 1
        inspection = json.loads(
            subprocess.check_output(
                [
                    environment.ffprobe_bin,
                    "-v",
                    "error",
                    "-count_frames",
                    "-show_streams",
                    "-of",
                    "json",
                    str(output),
                ],
                text=True,
            )
        )
        assert int(inspection["streams"][0]["nb_read_frames"]) == 192
        subprocess.run(
            [environment.ffmpeg_bin, "-v", "error", "-xerror", "-i", str(output), "-f", "null", "-"],
            check=True,
            capture_output=True,
        )
    finally:
        if thread.is_alive():
            client.post(f"/api/tasks/{encoding_task}/cancel", json={})
            thread.join(12)
