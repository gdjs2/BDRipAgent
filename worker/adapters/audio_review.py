"""Collect bounded, locally decoded audio samples for the track-review agent."""

import json
import math
import wave
from pathlib import Path

import numpy as np

from shared.config import behavior
from shared.paths import write_json
from worker.runtime import Interrupted, ToolError


def sample_starts(duration, count, seconds):
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("Audio review requires a positive finite movie duration")
    count = min(count, max(1, int(duration // seconds)))
    span = min(seconds, duration)
    return [
        round(max(0, min(duration - span, duration * (i + 1) / (count + 1) - span / 2)), 3)
        for i in range(count)
    ]


def signal_summary(path):
    with wave.open(str(path), "rb") as stream:
        if stream.getnchannels() != 1 or stream.getsampwidth() != 2 or stream.getframerate() != 16000:
            raise ValueError("Unexpected audio review sample format")
        values = np.frombuffer(stream.readframes(stream.getnframes()), dtype="<i2").astype(float) / 32768
    if not len(values):
        raise ValueError("Audio sample decoded no frames")
    rms = float(np.sqrt(np.mean(values * values)))
    peak = float(np.max(np.abs(values)))
    return {
        "duration_seconds": round(len(values) / 16000, 3),
        "rms_dbfs": round(20 * math.log10(rms), 2) if rms else None,
        "peak_dbfs": round(20 * math.log10(peak), 2) if peak else None,
        "silent": peak < 0.001,
    }


def analyze_audio(ctx, tracks, duration):
    config = behavior()["integrations"].get("audio_review", {})
    count = max(1, min(8, int(config.get("sample_count", 3))))
    seconds = max(5, min(60, float(config.get("sample_seconds", 30))))
    starts = sample_starts(float(duration), count, seconds)
    transcription = config.get("transcription", True)
    result = {}
    source = ctx.source()
    for track in tracks:
        track_id = track["track_id"]
        stream_index = track.get("ffprobe_index")
        data = {
            "method": "local decoding and speech transcription"
            if transcription
            else "local metadata and signal analysis",
            "sample_seconds": seconds,
            "samples": [],
            "limitations": [],
        }
        if stream_index is None:
            data["limitations"].append("Audio stream could not be mapped safely; metadata only.")
            result[track_id] = data
            continue
        for number, start in enumerate(starts, 1):
            ctx.check()
            ctx.progress(99, phase=f"Sampling audio track {track_id}: {number}/{len(starts)}")
            path = ctx.output("audio-review", f"track-{track_id}-sample-{number}.wav")
            try:
                ctx.run(
                    [
                        ctx.settings.ffmpeg_bin,
                        "-v",
                        "error",
                        "-nostdin",
                        "-y",
                        "-ss",
                        str(start),
                        "-i",
                        source,
                        "-map",
                        f"0:{stream_index}",
                        "-t",
                        str(min(seconds, duration - start)),
                        "-vn",
                        "-sn",
                        "-dn",
                        "-ac",
                        "1",
                        "-ar",
                        "16000",
                        "-c:a",
                        "pcm_s16le",
                        path,
                    ]
                )
                sample = {"id": number, "start_seconds": start, **signal_summary(path)}
                sample["path"] = ctx.artifact(
                    path, "AUDIO_SAMPLE", info={"track_id": track_id, "sample_id": number}
                )
                data["samples"].append(sample)
            except Interrupted:
                raise
            except (ToolError, ValueError, wave.Error) as error:
                data["limitations"].append(f"Sample {number} could not be decoded: {error}")
        result[track_id] = data
    if transcription and any(data["samples"] for data in result.values()):
        inventory = ctx.output("audio-review", "samples.json")
        write_json(inventory, {"tracks": result, "workspace": str(ctx.workspace)})
        output = ctx.output("audio-review", "transcripts.json")
        ctx.progress(99, phase="Transcribing local audio samples (first run may download the speech model)")
        try:
            ctx.run(
                [
                    ctx.settings.audio_review_python,
                    Path(__file__).with_name("audio_transcribe.py"),
                    inventory,
                    output,
                    "--model",
                    str(config.get("model", "small")),
                    "--cache",
                    ctx.settings.cache_root / "speech-models",
                    "--threads",
                    str(max(1, min(8, int(config.get("cpu_threads", 2))))),
                ],
                progress_parser=lambda chunk: (
                    {"percentage": 99, "phase": chunk.strip()[-250:]} if "AUDIO_TRANSCRIBE" in chunk else None
                ),
            )
            transcripts = json.loads(output.read_text())
            for track_id, data in result.items():
                for sample in data["samples"]:
                    sample.update(transcripts[str(track_id)][str(sample["id"])])
            ctx.artifact(output, "AUDIO_TRANSCRIPTS")
        except Interrupted:
            raise
        except (ToolError, OSError, ValueError, KeyError) as error:
            ctx.log(f"Local speech transcription unavailable: {error}")
            for data in result.values():
                data["limitations"].append(
                    "Speech transcription unavailable; content roles require manual review."
                )
                data["method"] = "local metadata and signal analysis"
    for track_id, data in result.items():
        data["sampled_seconds"] = round(sum(s["duration_seconds"] for s in data["samples"]), 3)
        data["source_duration_seconds"] = duration
        data["transcription_available"] = any(s.get("segments") for s in data["samples"])
        if not transcription:
            data["limitations"].append("Speech transcription disabled; no spoken content was inspected.")
        path = ctx.output("audio-review", f"track-{track_id}.json")
        write_json(path, data)
        ctx.artifact(path, "AUDIO_ANALYSIS", info={"track_id": track_id})
    return result
