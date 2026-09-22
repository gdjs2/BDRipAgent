"""Collect bounded, locally decoded audio samples for the track-review agent."""

import hashlib
import json
import math
import re
import wave
from pathlib import Path

import numpy as np

from shared.config import behavior
from shared.paths import contained, write_json
from worker.progress import plan
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
        "pcm_sha256": hashlib.sha256(values.tobytes()).hexdigest(),
        "duration_seconds": round(len(values) / 16000, 3),
        "rms_dbfs": round(20 * math.log10(rms), 2) if rms else None,
        "peak_dbfs": round(20 * math.log10(peak), 2) if peak else None,
        "silent": peak < 0.001,
    }


def diffusion_starts(duration, seconds, used, count=2):
    """Bisect the largest untouched intervals, without repeating sampled windows."""
    intervals = [(0.0, float(duration))]
    for start, length in sorted(used):
        next_intervals = []
        for left, right in intervals:
            if start >= right or start + length <= left:
                next_intervals.append((left, right))
            else:
                if start > left:
                    next_intervals.append((left, start))
                if start + length < right:
                    next_intervals.append((start + length, right))
        intervals = next_intervals
    starts = []
    for _ in range(count):
        if not intervals:
            break
        left, right = max(intervals, key=lambda item: (item[1] - item[0], -item[0]))
        intervals.remove((left, right))
        if right - left < min(seconds, duration):
            break
        start = (left + right - min(seconds, duration)) / 2
        starts.append(round(start, 3))
        intervals.extend([(left, start), (start + min(seconds, duration), right)])
    return sorted(starts)


def native_start_seconds(ctx, track):
    """Extracted streams lose their container offset; restore it for aligned sampling."""
    timestamps = track.get("source_timestamps_path")
    if timestamps:
        with contained(ctx.workspace, timestamps, exists=True).open() as stream:
            for line in stream:
                if line.strip() and not line.startswith("#"):
                    value = float(line) / 1000
                    if not math.isfinite(value):
                        raise ValueError("Invalid audio timestamp")
                    return value
        raise ValueError("Audio timestamp file contains no timestamps")
    return float(track.get("start_time", 0))


def analyze_audio(ctx, tracks, duration, *, starts=None):
    config = behavior()["integrations"].get("audio_review", {})
    count = max(1, min(8, int(config.get("sample_count", 3))))
    seconds = max(5, min(60, float(config.get("sample_seconds", 30))))
    starts = sample_starts(float(duration), count, seconds) if starts is None else starts
    transcription = config.get("transcription", True)
    steps = plan(ctx, sampling=35, transcription=60 if transcription else 0, reports=5)
    sampling = steps["sampling"]
    total_samples = len(tracks) * len(starts)
    completed_samples = 0
    result = {}
    source = ctx.source()
    video_start = getattr(getattr(ctx, "job", None), "analysis", {}).get("video", {}).get("start_time", 0)
    for track in tracks:
        track_id = track["track_id"]
        stream_index = track.get("ffprobe_index")
        native = track.get("source_track_path")
        input_path = contained(ctx.workspace, native, exists=True) if native else source
        stream_map = "0:a:0" if native else f"0:{stream_index}"
        native_start = native_start_seconds(ctx, track) if native else 0
        previous = track.get("audio_analysis", {})
        data = {
            "method": "local decoding and speech transcription"
            if transcription
            else "local metadata and signal analysis",
            "sample_seconds": seconds,
            "samples": list(previous.get("samples", [])),
            "limitations": list(previous.get("limitations", [])),
            "attempted_starts": list(previous.get("attempted_starts", [])),
        }
        if stream_index is None and not native:
            data["limitations"].append("Audio stream could not be mapped safely; metadata only.")
            result[track_id] = data
            completed_samples += len(starts)
            continue
        for start in starts:
            sampling.progress(
                100 * completed_samples / max(1, total_samples),
                phase=f"Sampling audio track {track_id}",
                completed_samples=completed_samples,
                total_samples=total_samples,
            )
            completed_samples += 1
            if start in data["attempted_starts"] or any(
                abs(s["start_seconds"] - start) < 0.01 for s in data["samples"]
            ):
                continue
            data["attempted_starts"].append(start)
            number = max([s["id"] for s in data["samples"]] + [0]) + 1
            ctx.check()
            path = ctx.output("audio-review", f"track-{track_id}-sample-{number}.wav")
            try:
                seek = video_start + start - native_start
                sample_input, sample_map = input_path, stream_map
                if native and seek < 0:
                    if stream_index is None:
                        raise ValueError("Track begins after the requested interval and cannot be aligned")
                    sample_input, sample_map = source, f"0:{stream_index}"
                    seek = video_start + start
                ctx.run(
                    [
                        ctx.settings.ffmpeg_bin,
                        "-v",
                        "error",
                        "-nostdin",
                        "-y",
                        "-ss",
                        str(seek),
                        *(["-seek_timestamp", "1"] if sample_input == source else []),
                        "-i",
                        sample_input,
                        "-map",
                        sample_map,
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
    sampling.done("Audio samples prepared", completed_samples=completed_samples, total_samples=total_samples)
    if transcription and any(
        "segments" not in s and not s.get("transcription_error")
        for data in result.values()
        for s in data["samples"]
    ):
        inventory = ctx.output("audio-review", "samples.json")
        write_json(
            inventory,
            {
                "tracks": {
                    track_id: {
                        **data,
                        "samples": [
                            s
                            for s in data["samples"]
                            if "segments" not in s and not s.get("transcription_error")
                        ],
                    }
                    for track_id, data in result.items()
                },
                "workspace": str(ctx.workspace),
            },
        )
        output = ctx.output("audio-review", "transcripts.json")
        speech = steps["transcription"]
        speech.progress(None, phase="Loading local speech model (first run may download it)")
        pending_text = ""

        def transcript_progress(chunk):
            nonlocal pending_text
            pending_text += chunk
            lines = pending_text.split("\n")
            pending_text = lines.pop()
            update = None
            for line in lines:
                match = re.search(r"AUDIO_TRANSCRIBE_PROGRESS (\d+)/(\d+)", line)
                if match:
                    done, total = map(int, match.groups())
                    update = {
                        "percentage": 100 * done / max(1, total),
                        "phase": "Transcribing local audio samples",
                        "completed_samples": done,
                        "total_samples": total,
                    }
                elif "AUDIO_TRANSCRIBE" in line and update is None:
                    update = {"percentage": None, "phase": line.strip()[-250:]}
            return update

        try:
            speech.run(
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
                progress_parser=transcript_progress,
            )
            transcripts = json.loads(output.read_text())
            for track_id, data in result.items():
                for sample in data["samples"]:
                    sample.update(transcripts.get(str(track_id), {}).get(str(sample["id"]), {}))
            ctx.artifact(output, "AUDIO_TRANSCRIPTS")
        except Interrupted:
            raise
        except (ToolError, OSError, ValueError, KeyError) as error:
            ctx.log(f"Local speech transcription unavailable: {error}")
            for data in result.values():
                for sample in data["samples"]:
                    if "segments" not in sample:
                        sample["transcription_error"] = True
                data["limitations"].append(
                    "Speech transcription unavailable; content roles require manual review."
                )
                data["method"] = "local metadata and signal analysis"
    if transcription:
        steps["transcription"].done("Audio transcription complete")
    steps["reports"].progress(None, phase="Saving audio evidence")
    for track_id, data in result.items():
        data["sampled_seconds"] = round(sum(s["duration_seconds"] for s in data["samples"]), 3)
        data["source_duration_seconds"] = duration
        data["transcription_available"] = any(s.get("segments") for s in data["samples"])
        if not transcription:
            data["limitations"].append("Speech transcription disabled; no spoken content was inspected.")
        path = ctx.output("audio-review", f"track-{track_id}.json")
        write_json(path, data)
        ctx.artifact(path, "AUDIO_ANALYSIS", info={"track_id": track_id})
    steps["reports"].done("Audio evidence ready")
    return result
