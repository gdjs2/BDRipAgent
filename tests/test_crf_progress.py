import json
import sys

import pytest
from sqlalchemy import select

from shared.config import profiles
from shared.db import session
from shared.models import Event, MovieJob, Task
from worker.adapters.crf_progress import CRFProgressReader, normalize_progress
from worker.adapters.handbrake import Crop
from worker.adapters.integrations import CRFStudioAdapter
from worker.runtime import TaskContext, ToolError


def test_progress_counts_sample_frames_like_studio_gui():
    data = {
        "state": "running",
        "stage": "encoding",
        "codec": "x265",
        "crf": 20,
        "message": "x265 · CRF 20 · sample 4/10 (10s)",
        "completed": 13,
        "total": 20,
        "sample_index": 4,
        "samples_per_endpoint": 10,
        "sample_fraction": 0.5,
        "frames": 120,
        "flushing": False,
        "encoder_pid": 42,
        "log_path": "/private/log",
        "elapsed_seconds": 12,
    }
    update = normalize_progress(data)
    assert update["percentage"] == 67
    assert update["message"] == data["message"]
    assert update["frames"] == 120 and update["crf"] == 20
    assert "log_path" not in update and "encoder_pid" not in update
    assert "elapsed_seconds" not in update  # Runtime supplies elapsed wall time.
    data.update(completed=19, sample_fraction=1, flushing=True)
    assert normalize_progress(data)["percentage"] == 99
    assert normalize_progress(data)["sample_fraction"] == 0.99
    data.update(completed=20, state="complete", stage="complete")
    assert normalize_progress(data)["percentage"] == 99  # Not yet accepted by the application.
    data.update(completed=5, state="interrupted")
    assert normalize_progress(data)["percentage"] == 25
    assert normalize_progress({"stage": "inspecting", "total": 0})["percentage"] == 0


@pytest.mark.parametrize(
    "data",
    [
        [],
        None,
        {"total": "20"},
        {"total": True},
        {"completed": -1},
        {"frames": 1.5},
        {"sample_fraction": float("nan")},
        {"crf": float("inf")},
    ],
)
def test_invalid_progress_is_ignored_until_a_valid_report_arrives(tmp_path, data):
    path = tmp_path / "progress.json"
    read = CRFProgressReader(path)
    assert read() is None
    path.write_text(json.dumps(data))
    assert read() is None
    path.write_text('{"total":')
    assert read() is None
    path.write_text(json.dumps({"total": 20, "completed": 2, "state": "running"}))
    assert read()["percentage"] == 10
    assert read() is None  # Do not duplicate events when the file hasn't changed.


@pytest.mark.parametrize("exit_code", [0, 1])
def test_crf_subprocess_publishes_live_progress_and_final_report(
    client, new_job, environment, tmp_path, monkeypatch, exit_code
):
    # Exercise the adapter, process polling, persisted task fields and API together.
    # A silent CLI writes only atomic progress files, like the native encoder.
    script = tmp_path / "studio.py"
    script.write_text("""
import json, sys, time
from pathlib import Path
args = sys.argv
path = Path(args[args.index('--progress-file') + 1])
output = Path(args[args.index('--output-dir') + 1])
codec = args[args.index('--codec') + 1]
def write(data):
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(data))
    temp.replace(path)
write(dict(state='running', stage='encoding', message='Encoding sample', total=4,
           completed=1, sample_fraction=0.5, crf=13, codec=codec, sample_index=2,
           samples_per_endpoint=2, frames=12, flushing=True))
time.sleep(1.5)
code = int(args[-1])
write(dict(state='complete' if code == 0 else 'failed', stage='complete' if code == 0 else 'failed',
           total=4, completed=4 if code == 0 else 1, sample_fraction=0,
           message='Completed' if code == 0 else 'Encoder failed'))
(output/'results.json').write_text(json.dumps(dict(schema_version=5, state='complete',
    codecs={codec: dict(rows=[dict(crf=c, average_bitrate_mbps=10-c/3, average_qp=c+3,
                                  complete=True) for c in (13, 20)])})))
sys.exit(code)
""")
    with session() as db:
        task = db.get(Task, new_job["tasks"][0]["id"])
        task.status, task.type, task.run_token = "RUNNING", "crf_analysis", "progress-test"
        db.get(MovieJob, new_job["id"]).analysis = {"video": {"width": 1920, "height": 1080}}
        task_id = task.id
        db.commit()
    ctx = TaskContext(task_id, "progress-test")
    run, progress = ctx.run, ctx.progress
    observed = []

    def capture(value, **detail):
        progress(value, **detail)
        row = client.get(f"/api/tasks/{task_id}").json()
        assert row["status"] == "RUNNING"
        observed.append(row)

    def fake_cli(command, **kwargs):
        assert command[0] == environment.crf_studio_bin
        return run([sys.executable, script, *command[1:], str(exit_code)], **kwargs)

    monkeypatch.setattr(ctx, "progress", capture)
    monkeypatch.setattr(ctx, "run", fake_cli)
    try:
        if exit_code:
            with pytest.raises(ToolError):
                CRFStudioAdapter().run_analysis(
                    ctx, profiles()["x264-live"], Crop(top=104, bottom=104, left=0, right=0)
                )
        else:
            result = CRFStudioAdapter().run_analysis(
                ctx, profiles()["x264-live"], Crop(top=104, bottom=104, left=0, right=0)
            )
            assert [p["crf"] for p in result["samples"]] == [13, 20]
        assert any(
            row["progress"] == pytest.approx(37 * 0.95) and row["progress_detail"]["frames"] == 12
            for row in observed
        )
        assert observed[-1]["progress_detail"]["stage"] == ("failed" if exit_code else "saving")
        assert observed[-1]["progress"] == pytest.approx(37 * 0.95 if exit_code else 95)
        assert any(row["progress_detail"].get("elapsed_seconds", 0) > 0 for row in observed)
        assert all(row["progress"] < 100 for row in observed)
        with session() as db:
            events = db.scalars(
                select(Event).where(Event.job_id == new_job["id"], Event.type == "task_progress")
            ).all()
            assert len(events) == len(observed)
    finally:
        ctx.close()


def test_progress_written_at_process_exit_is_not_missed(new_job, tmp_path):
    path = tmp_path / "progress.json"
    with session() as db:
        task = db.get(Task, new_job["tasks"][0]["id"])
        task.status, task.run_token = "RUNNING", "progress-test"
        task_id = task.id
        db.commit()
    ctx = TaskContext(task_id, "progress-test")
    try:
        ctx.run(
            [
                sys.executable,
                "-c",
                "import pathlib,sys; pathlib.Path(sys.argv[1]).write_text(sys.argv[2])",
                path,
                json.dumps({"state": "complete", "stage": "complete", "total": 2, "completed": 2}),
            ],
            progress_reader=CRFProgressReader(path),
        )
        with session() as db:
            task = db.get(Task, task_id)
            assert task.progress == 99
            assert task.progress_detail["stage"] == "complete"
    finally:
        ctx.close()
