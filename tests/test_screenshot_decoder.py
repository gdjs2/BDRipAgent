from types import SimpleNamespace

import pytest
import yaml
from sqlalchemy import select

from shared.db import session
from shared.models import MovieJob, Task
from worker.adapters import screenshot_decoder as decoder


@pytest.fixture
def context():
    messages = []
    return SimpleNamespace(
        job=SimpleNamespace(screenshot_policy={"decoder": "cuda"}),
        source=lambda: "fixture.mkv",
        check=lambda: None,
        log=messages.append,
        messages=messages,
    )


def test_gpu_default_and_explicit_cpu(context, monkeypatch):
    calls, closed = [], []

    def start(path, *, cuda=False):
        calls.append(cuda)
        return SimpleNamespace(close=lambda: closed.append(True)), iter(["first", "second"])

    monkeypatch.setattr(decoder, "start_decoder", start)
    for policy, expected in (({}, "cuda"), ({"decoder": "cpu"}, "cpu")):
        context.job.screenshot_policy = policy
        with decoder.candidate_frames(context) as (frames, info):
            assert list(frames) == ["first", "second"]
            assert info["decoder"] == expected and not info["decoder_fallback"]
    assert calls == [True, False] and len(closed) == 2


def test_unavailable_gpu_falls_back_before_emitting_frames(context, monkeypatch):
    calls = []

    def start(path, *, cuda=False):
        calls.append(cuda)
        if cuda:
            raise RuntimeError("No CUDA device")
        return SimpleNamespace(close=lambda: None), iter([0, 1, 2])

    monkeypatch.setattr(decoder, "start_decoder", start)
    with decoder.candidate_frames(context) as (frames, info):
        assert list(frames) == [0, 1, 2]
        assert info["decoder"] == "cpu" and info["decoder_requested"] == "cuda"
        assert info["decoder_fallback"] and "No CUDA device" in info["decoder_fallback_reason"]
    assert calls == [True, False]
    assert any("falling back to CPU" in message for message in context.messages)


def test_late_gpu_failure_is_not_restarted_with_duplicate_frame_numbers(context, monkeypatch):
    calls, closed = [], []

    def frames():
        yield 0
        raise RuntimeError("GPU lost after startup")

    def start(path, *, cuda=False):
        calls.append(cuda)
        return SimpleNamespace(close=lambda: closed.append(True)), frames()

    monkeypatch.setattr(decoder, "start_decoder", start)
    with pytest.raises(RuntimeError, match="GPU lost"):
        with decoder.candidate_frames(context) as (stream, info):
            assert next(stream) == 0
            next(stream)
    assert calls == [True] and closed == [True]


def test_probe_preserves_first_frame_and_closes_failed_container(monkeypatch):
    closed = []
    container = SimpleNamespace(
        decode=lambda **kwargs: iter([0, 1, 2]),
        close=lambda: closed.append(True),
        streams=SimpleNamespace(video=[SimpleNamespace(codec_context=SimpleNamespace(is_hwaccel=False))]),
    )
    monkeypatch.setattr(decoder.av, "open", lambda *args, **kwargs: container)
    with pytest.raises(RuntimeError, match="did not activate"):
        decoder.start_decoder("fixture.mkv", cuda=True)
    assert closed == [True]
    container.streams.video[0].codec_context.is_hwaccel = True
    opened, frames = decoder.start_decoder("fixture.mkv", cuda=True)
    assert opened is container and list(frames) == [0, 1, 2]


def test_decoder_choice_persists_and_running_scan_cannot_be_changed(client, new_job, environment):
    url = f"/api/jobs/{new_job['id']}/screenshots/decoder"
    assert new_job["screenshot_policy"]["decoder"] == "cuda"
    assert client.patch(url, json={"decoder": "bogus"}).status_code == 422
    result = client.patch(url, json={"decoder": "cuda"})
    assert result.status_code == 200, result.text
    assert result.json()["screenshot_policy"]["decoder"] == "cuda"
    saved = yaml.safe_load((environment.workspace_root / new_job["id"] / "manifest.yaml").read_text())
    assert saved["screenshots"]["decoder"] == "cuda"
    with session() as db:
        task = db.scalar(select(Task).where(Task.job_id == new_job["id"]))
        task.type, task.status = "generate_candidates", "RUNNING"
        db.commit()
    assert client.patch(url, json={"decoder": "cpu"}).status_code == 409
    with session() as db:
        assert db.get(MovieJob, new_job["id"]).screenshot_policy["decoder"] == "cuda"
        task = db.scalar(select(Task).where(Task.job_id == new_job["id"]))
        task.status = "CANCELLED"
        db.commit()
    assert client.patch(url, json={"decoder": "cpu"}).status_code == 200
    client.headers.clear()
    assert client.patch(url, json={"decoder": "cuda"}).status_code == 401


def test_new_job_accepts_gpu_choice(client, environment):
    (environment.source_root / "Movie.mkv").write_bytes(b"fixture")
    result = client.post(
        "/api/jobs",
        json={
            "source_path": "Movie.mkv",
            "title": "GPU fixture",
            "year": 2026,
            "screenshot_policy": {"decoder": "cuda"},
        },
    )
    assert result.status_code == 201, result.text
    assert result.json()["screenshot_policy"]["decoder"] == "cuda"
