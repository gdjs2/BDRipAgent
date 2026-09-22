import shutil
import sys
import wave

import pytest
from sqlalchemy import select
from worker.adapters.mkvtoolnix import MKVToolNixProgress

from shared.db import session
from shared.models import Event, Task
from worker.runtime import TaskContext, ToolError


@pytest.mark.parametrize("record", ["#GUI#progress 37%", "Progress: 37%", "Progress: 37.0 %"])
def test_progress_survives_every_log_chunk_boundary(record):
    for split in range(1, len(record)):
        parse = MKVToolNixProgress("mkvextract", "Extracting selected tracks")
        assert parse(record[:split]) is None
        update = parse(record[split:])
        assert update == {
            "percentage": 37,
            "tool_percentage": 37,
            "tool": "mkvextract",
            "phase": "Extracting selected tracks",
        }
        assert parse("") is None
        assert parse("\r" + record + "\n") is None


def test_progress_uses_latest_report_and_does_not_treat_tool_completion_as_task_success():
    parse = MKVToolNixProgress("mkvmerge", "Merging final MKV")
    assert parse("Header\rProgress: 12%\rProgress: 47%\n")["percentage"] == 47
    assert parse("#GUI#warning message 82%\n") is None
    assert parse("#GUI#progress 101%\n#GUI#progress -1%\nProgress: NaN%\n") is None
    result = parse("#GUI#progress 100%")
    assert result["percentage"] == 99
    assert result["tool_percentage"] == 100


def test_timestamp_index_progress_fits_the_existing_screenshot_stage_allocation():
    parse = MKVToolNixProgress("mkvextract", "Indexing frame timestamps", progress_span=5)
    assert parse("#GUI#progress 40%\n")["percentage"] == 2
    update = parse("#GUI#progress 100%\n")
    assert update["percentage"] == 5
    assert update["tool_percentage"] == 100


def test_unrelated_output_does_not_grow_the_parser_buffer():
    parse = MKVToolNixProgress("mkvmerge", "Merging final MKV")
    assert parse("Other log output " * 10000) is None
    assert len(parse.pending) <= 256
    assert parse("\n#GUI#progress 0%\n")["percentage"] == 0


@pytest.fixture
def running_context(new_job):
    with session() as db:
        task = db.get(Task, new_job["tasks"][0]["id"])
        task.status, task.run_token = "RUNNING", "mkv-progress-test"
        task_id = task.id
        db.commit()
    ctx = TaskContext(task_id, "mkv-progress-test")
    try:
        yield ctx
    finally:
        ctx.close()


@pytest.mark.parametrize(
    "tool,exit_code", [("mkvextract", 0), ("mkvmerge", 0), ("mkvmerge", 1), ("mkvextract", 2)]
)
def test_subprocess_progress_reaches_task_api_and_events_while_running(
    client, running_context, tmp_path, monkeypatch, tool, exit_code
):
    ctx = running_context
    script = tmp_path / "tool.py"
    script.write_text("""
import sys, time
sys.stdout.write('#GUI#progress 37%\\n')
sys.stdout.flush()
time.sleep(1.5)
sys.stdout.write('#GUI#progress 41%' if int(sys.argv[1]) == 2 else '#GUI#progress 100%')
sys.stdout.flush()
sys.exit(int(sys.argv[1]))
""")
    observed = []
    progress = ctx.progress

    def capture(value, **detail):
        progress(value, **detail)
        row = client.get(f"/api/tasks/{ctx.task_id}").json()
        assert row["status"] == "RUNNING"
        observed.append(row)

    monkeypatch.setattr(ctx, "progress", capture)
    # A prior command in this same task log must not supply the current progress.
    ctx.log("#GUI#progress 91%")
    kwargs = {
        "allowed": (0, 1) if tool == "mkvmerge" else (0,),
        "progress_parser": MKVToolNixProgress(tool, "Processing MKV"),
    }
    if exit_code == 2:
        with pytest.raises(ToolError):
            ctx.run([sys.executable, script, exit_code], **kwargs)
    else:
        ctx.run([sys.executable, script, exit_code], **kwargs)
    assert [row["progress"] for row in observed] == [37, 41 if exit_code == 2 else 99]
    assert observed[-1]["progress_detail"]["tool_percentage"] == (41 if exit_code == 2 else 100)
    assert all(row["progress_detail"]["tool"] == tool for row in observed)
    assert observed[-1]["progress_detail"]["elapsed_seconds"] > 0
    with session() as db:
        events = db.scalars(
            select(Event).where(Event.job_id == ctx.job.id, Event.type == "task_progress").order_by(Event.id)
        ).all()
        assert [event.data["progress"] for event in events] == [row["progress"] for row in observed]
    assert "#GUI#progress 37%" in ctx.log_path.read_text()


@pytest.mark.skipif(
    not shutil.which("mkvmerge") or not shutil.which("mkvextract"), reason="MKVToolNix required"
)
def test_real_mkvtoolnix_redirected_output_is_parsed(running_context, tmp_path):
    ctx = running_context
    audio = tmp_path / "source audio.wav"
    with wave.open(str(audio), "wb") as output:
        output.setparams((1, 2, 48000, 0, "NONE", "not compressed"))
        output.writeframes(b"\0\0" * 48000)
    source = tmp_path / "source movie.mkv"
    extracted = tmp_path / "extracted audio.wav"
    timestamps = tmp_path / "audio timestamps.txt"
    commands = [
        ("mkvmerge", [ctx.settings.mkvmerge_bin, "--gui-mode", "-o", source, audio]),
        (
            "mkvextract",
            [
                ctx.settings.mkvextract_bin,
                source,
                "tracks",
                f"0:{extracted}",
                "timestamps_v2",
                f"0:{timestamps}",
                "--gui-mode",
            ],
        ),
    ]
    for tool, command in commands:
        ctx.run(command, progress_parser=MKVToolNixProgress(tool, "Processing MKV"))
        with session() as db:
            row = db.get(Task, ctx.task_id)
            assert row.progress == 99
            assert row.status == "RUNNING"
            assert row.progress_detail["tool"] == tool
            assert row.progress_detail["tool_percentage"] == 100
    assert extracted.stat().st_size > 0
    assert timestamps.stat().st_size > 0
