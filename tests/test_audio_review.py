import subprocess
from types import SimpleNamespace
from uuid import uuid4

import pytest

from worker.adapters import audio_review
from worker.adapters.audio_transcribe import transcribe
from worker.runtime import Interrupted, ToolError


def test_samples_are_bounded_unique_and_span_timeline():
    assert audio_review.sample_starts(20, 3, 30) == [0]
    starts = audio_review.sample_starts(600, 3, 30)
    assert starts == [135, 285, 435]
    with pytest.raises(ValueError):
        audio_review.sample_starts(float("nan"), 3, 30)


@pytest.mark.parametrize("speech_failure", [False, True])
def test_local_sampling_maps_ffprobe_index_and_records_transcription_limits(
    tmp_path, environment, monkeypatch, speech_failure
):
    source = tmp_path / "two audio tracks.mkv"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=6",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=48000:cl=stereo:d=6",
            "-map",
            "0:a",
            "-map",
            "1:a",
            "-c:a",
            "pcm_s16le",
            str(source),
        ],
        check=True,
    )
    commands = []

    def output(category, name):
        path = tmp_path / category / name
        path.parent.mkdir(exist_ok=True)
        return path

    def run(command, **kwargs):
        commands.append(command)
        if command[0] != environment.ffmpeg_bin:
            raise ToolError(command, 1)
        subprocess.run([str(x) for x in command], check=True, capture_output=True)

    monkeypatch.setattr(
        audio_review,
        "behavior",
        lambda: {
            "integrations": {
                "audio_review": {"transcription": speech_failure, "sample_count": 1, "sample_seconds": 5}
            }
        },
    )
    ctx = SimpleNamespace(
        settings=environment,
        workspace=tmp_path,
        job=SimpleNamespace(id=str(uuid4())),
        source=lambda: source,
        check=lambda: None,
        progress=lambda *a, **kw: None,
        log=lambda *a: None,
        output=output,
        run=run,
        artifact=lambda path, *a, **kw: str(path.relative_to(tmp_path)),
    )
    evidence = audio_review.analyze_audio(
        ctx, [{"track_id": 9, "ffprobe_index": 1}, {"track_id": 4, "ffprobe_index": 0}], 6
    )
    assert evidence[9]["samples"][0]["silent"] is True
    assert evidence[4]["samples"][0]["silent"] is False
    assert evidence[4]["sampled_seconds"] == 5
    assert evidence[9]["limitations"] and evidence[4]["transcription_available"] is False
    assert all(cmd[cmd.index("-map") + 1] in ("0:0", "0:1") for cmd in commands if "-map" in cmd)
    monkeypatch.setattr(ctx, "check", lambda: (_ for _ in ()).throw(Interrupted("cancelled")))
    with pytest.raises(Interrupted):
        audio_review.analyze_audio(ctx, [{"track_id": 1, "ffprobe_index": 0}], 6)


def test_local_transcriber_preserves_timestamps_confidence_and_containment(tmp_path):
    path = tmp_path / "sample.wav"
    path.touch()
    calls = []

    def infer(path, **kwargs):
        calls.append((path, kwargs))
        return iter(
            [SimpleNamespace(start=1.2, end=2.8, text="Dialogue", avg_logprob=-0.2, no_speech_prob=0.01)]
        ), SimpleNamespace(language="en", language_probability=0.98)

    inventory = {"workspace": str(tmp_path), "tracks": {"7": {"samples": [{"id": 2, "path": "sample.wav"}]}}}
    result = transcribe(inventory, SimpleNamespace(transcribe=infer), lambda *a, **kw: None)
    assert result["7"]["2"]["segments"][0]["text"] == "Dialogue"
    assert result["7"]["2"]["language_probability"] == 0.98
    assert calls[0][1]["vad_filter"] and calls[0][1]["condition_on_previous_text"] is False
    inventory["tracks"]["7"]["samples"][0]["path"] = "../outside.wav"
    (tmp_path.parent / "outside.wav").touch()
    with pytest.raises(ValueError, match="outside"):
        transcribe(inventory, SimpleNamespace(transcribe=infer), lambda *a, **kw: None)
