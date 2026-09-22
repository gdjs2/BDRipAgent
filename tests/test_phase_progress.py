from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from shared.db import session
from shared.models import Task
from worker.progress import plan
from worker.runtime import TaskContext


def recording_context():
    updates = []
    ctx = SimpleNamespace(progress=lambda value, **detail: updates.append((value, detail)))
    ctx.branch = lambda: ctx
    return ctx, updates


def test_extraction_completion_leaves_room_for_content_analysis_and_agent():
    ctx, updates = recording_context()
    steps = plan(ctx, extraction=20, local=45, agent=35)
    steps["extraction"].progress(100, tool_percentage=100)
    assert updates[-1][0] == 20
    assert updates[-1][1]["tool_percentage"] == 100
    local = plan(steps["local"], ocr=1, audio=1)
    local["ocr"].progress(50, completed_cues=5, total_cues=10)
    assert updates[-1][0] == pytest.approx(31.25)
    steps["agent"].progress(None, phase="Waiting for agent")
    assert updates[-1][0] == pytest.approx(31.25)
    assert updates[-1][1]["indeterminate"] is True
    steps["local"].done()
    steps["agent"].done()
    assert updates[-1][0] == 99.9


def test_concurrent_nested_reports_are_monotonic_and_keep_both_lanes():
    ctx, updates = recording_context()
    steps = plan(ctx, local=60, agent=40)
    local = plan(steps["local"].branch(), first=1, second=1)

    def report(step):
        for percentage in range(101):
            step.progress(percentage)

    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(report, [local["first"], local["second"], steps["agent"]]))
    values = [value for value, _ in updates]
    assert values == sorted(values)
    assert values[-1] == 99.9
    steps["local"].progress(5)
    assert updates[-1][0] == 99.9


def test_tool_callbacks_publish_inside_their_own_substep_and_preserve_metadata():
    ctx, updates = recording_context()

    def run(command, **options):
        assert options["progress_parser"]("tool output") is None
        assert options["progress_reader"]() is None
        return "finished"

    ctx.run = run
    steps = plan(ctx, tool=75, files=25)
    assert (
        steps["tool"].run(
            [],
            progress_parser=lambda _: {"percentage": 50, "tool_percentage": 50},
            progress_reader=lambda: {"percentage": 100},
        )
        == "finished"
    )
    assert [value for value, _ in updates] == [37.5, 75]
    assert updates[0][1]["tool_percentage"] == 50
    steps["files"].progress(50, completed_bytes=500, total_bytes=1000)
    assert updates[-1][0] == 87.5


def test_runtime_preserves_completed_work_during_activity_and_reserves_100(new_job):
    task_id = new_job["tasks"][0]["id"]
    with session() as db:
        task = db.get(Task, task_id)
        task.status, task.run_token = "RUNNING", "progress-test"
        db.commit()
    ctx = TaskContext(task_id, "progress-test")
    try:
        ctx.progress(50)
        ctx.progress(None, phase="Waiting for agent")
        with session() as db:
            task = db.get(Task, task_id)
            assert task.progress == 50
            assert task.progress_detail["indeterminate"] is True
        ctx.progress(100)
        with session() as db:
            assert db.get(Task, task_id).progress == 99.9
    finally:
        ctx.close()


def test_validation_reports_decoded_frames_and_relative_timeline(monkeypatch):
    from worker.pipeline import validation

    frames = [SimpleNamespace(pts=1000 + i, time_base=1 / 24) for i in range(481)]

    class Container:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def decode(self, **kwargs):
            return iter(frames)

    monkeypatch.setattr(validation.av, "open", lambda _: Container())
    updates = []
    points = validation.timeline("video.mkv", on_progress=lambda *args: updates.append(args))
    assert len(points) == 481
    assert updates[0] == (1, 0)
    assert updates[-1] == pytest.approx((481, 20))
    assert len(updates) >= 3


def test_release_publication_reports_actual_bytes_for_copy_fallback(environment, monkeypatch):
    from tests.test_release_exports import context, outputs
    from worker.pipeline import release_exports

    ctx = context(environment)
    updates = []
    ctx.progress = lambda value, **detail: updates.append((value, detail))
    items = outputs(ctx)

    def no_links(*args):
        raise OSError("Cross-device link")

    monkeypatch.setattr(release_exports.os, "link", no_links)
    release_exports.publish(ctx, items)
    assert updates[0][0] == 0
    assert updates[-1][0] == 100
    assert updates[-1][1]["completed_bytes"] == updates[-1][1]["total_bytes"] > 0
    assert [value for value, _ in updates] == sorted(value for value, _ in updates)


def test_transcription_reports_finished_samples_including_silent_and_reused_audio(tmp_path):
    from worker.adapters.audio_transcribe import transcribe

    (tmp_path / "sample.wav").touch()
    reports = []

    def infer(*args, **kwargs):
        assert not any("PROGRESS 1/" in message for message in reports)
        return iter([]), SimpleNamespace(language="en", language_probability=0.99)

    samples = [{"id": i, "path": "sample.wav", "pcm_sha256": "same", "silent": i == 3} for i in (1, 2, 3)]
    transcribe(
        {"workspace": str(tmp_path), "tracks": {"1": {"samples": samples}}},
        SimpleNamespace(transcribe=infer),
        lambda message, **kwargs: reports.append(message),
    )
    assert [message for message in reports if "PROGRESS" in message] == [
        "AUDIO_TRANSCRIBE_PROGRESS 1/3",
        "AUDIO_TRANSCRIBE_PROGRESS 2/3",
        "AUDIO_TRANSCRIBE_PROGRESS 3/3",
    ]


def test_crf_flush_and_result_processing_are_activity_not_near_complete_estimates():
    from worker.adapters.crf_progress import normalize_progress

    for value in [
        {"state": "running", "total": 4, "completed": 3, "flushing": True, "sample_fraction": 1},
        {"state": "complete", "total": 4, "completed": 4},
    ]:
        assert normalize_progress(value)["indeterminate"] is True


def test_handbrake_pass_completion_is_not_whole_encode_completion():
    from worker.adapters.handbrake import parse_progress

    first = parse_progress("Encoding: task 1 of 2, 100.00 %")
    assert first["percentage"] == 50
    assert not first.get("indeterminate")
    final = parse_progress("Encoding: task 2 of 2, 100.00 %")
    assert final["percentage"] == 100
    assert final["indeterminate"] is True
    assert final["phase"] == "Finalizing encoded file"
