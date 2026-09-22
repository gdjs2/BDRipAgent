"""Isolated local speech inference; raw audio stays on the worker."""

import argparse
import fcntl
import json
from pathlib import Path


def transcribe(inventory, model, check_progress=print):
    result = {}
    by_pcm = {}
    total = sum(len(track["samples"]) for track in inventory["tracks"].values())
    completed = 0

    def finished():
        nonlocal completed
        completed += 1
        check_progress(f"AUDIO_TRANSCRIBE_PROGRESS {completed}/{total}", flush=True)

    root = Path(inventory["workspace"]).resolve()
    for track_id, track in inventory["tracks"].items():
        result[str(track_id)] = {}
        for sample in track["samples"]:
            path = (root / sample["path"]).resolve(strict=True)
            if not path.is_relative_to(root):
                raise ValueError("Audio sample is outside the workspace")
            check_progress(f"AUDIO_TRANSCRIBE track {track_id}, sample {sample['id']}", flush=True)
            if sample.get("silent"):
                result[str(track_id)][str(sample["id"])] = {"segments": [], "speech_skipped": "silent"}
                finished()
                continue
            fingerprint = sample.get("pcm_sha256")
            if fingerprint and fingerprint in by_pcm:
                result[str(track_id)][str(sample["id"])] = by_pcm[fingerprint]
                finished()
                continue
            segments, info = model.transcribe(
                str(path), beam_size=3, vad_filter=True, condition_on_previous_text=False
            )
            result[str(track_id)][str(sample["id"])] = {
                "detected_language": info.language,
                "language_probability": round(info.language_probability, 4),
                "segments": [
                    {
                        "start": round(s.start, 3),
                        "end": round(s.end, 3),
                        "text": s.text[:2000],
                        "avg_logprob": round(s.avg_logprob, 4),
                        "no_speech_prob": round(s.no_speech_prob, 4),
                    }
                    for s in segments
                ][:100],
            }
            if fingerprint:
                by_pcm[fingerprint] = result[str(track_id)][str(sample["id"])]
            finished()
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("inventory", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--model", required=True)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--threads", type=int, default=2)
    args = parser.parse_args()
    from faster_whisper import WhisperModel

    args.cache.mkdir(parents=True, exist_ok=True)
    # Share model weights, and bound CPU/RAM use when both codec jobs are analyzing.
    with (args.cache / "transcription.lock").open("a") as lock:
        print("AUDIO_TRANSCRIBE waiting for the local speech engine", flush=True)
        fcntl.flock(lock, fcntl.LOCK_EX)
        model = WhisperModel(
            args.model,
            device="cpu",
            compute_type="int8",
            cpu_threads=args.threads,
            download_root=str(args.cache),
        )
        result = transcribe(json.loads(args.inventory.read_text()), model)
        args.output.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
