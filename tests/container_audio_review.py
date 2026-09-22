"""Real local speech and audio sampling smoke test; never invokes the remote agent.

Run in a worker test image with espeak and the audio-review venv installed.
The first run downloads the configured speech model into /speech-cache.
"""

import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from worker.adapters.audio_review import analyze_audio

from shared.config import get_settings


def main():
    with tempfile.TemporaryDirectory(prefix="audio-review-") as directory:
        root = Path(directory)
        speech = root / "speech.wav"
        text = (
            "This is the director's commentary. When we filmed this scene, we used a camera near the old house. "
            "The actor spent three days rehearsing. "
        ) * 4
        subprocess.run(["espeak", "-s", "145", "-w", str(speech), text], check=True)
        source = root / "source.mkv"
        subprocess.run(["ffmpeg", "-v", "error", "-i", str(speech), "-c:a", "flac", str(source)], check=True)
        duration = float(
            subprocess.check_output(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "default=nw=1:nk=1",
                    str(source),
                ],
                text=True,
            )
        )
        settings = get_settings().model_copy(update={"cache_root": Path("/speech-cache")})

        def output(category, name):
            path = root / category / name
            path.parent.mkdir(exist_ok=True)
            return path

        def run(command, **kwargs):
            subprocess.run([str(x) for x in command], check=True)

        ctx = SimpleNamespace(
            settings=settings,
            workspace=root,
            source=lambda: source,
            output=output,
            check=lambda: None,
            run=run,
            progress=lambda *a, **kw: None,
            log=print,
            artifact=lambda path, *a, **kw: str(path.relative_to(root)),
        )
        with patch(
            "worker.adapters.audio_review.behavior",
            return_value={
                "integrations": {
                    "audio_review": {
                        "transcription": True,
                        "model": "small",
                        "sample_count": 2,
                        "sample_seconds": 20,
                    }
                }
            },
        ):
            evidence = analyze_audio(ctx, [{"track_id": 11, "ffprobe_index": 0}], duration)[11]
        assert not evidence["limitations"], evidence["limitations"]
        assert evidence["transcription_available"]
        transcript = " ".join(
            segment["text"] for sample in evidence["samples"] for segment in sample["segments"]
        ).lower()
        assert "commentary" in transcript and "scene" in transcript, transcript
        assert all(sample["detected_language"] == "en" for sample in evidence["samples"])
        print(
            "PASS: real FFmpeg sampling and local small-model speech recognition; commentary transcript recovered."
        )


if __name__ == "__main__":
    main()
