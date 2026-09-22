"""Native HandBrake upgrade check; run in a disposable worker container.

Exercise every saved profile in CRF and two-pass bitrate mode, using the real
command builder, scan/progress parsers and full-frame validation. No live data,
network, agent, database or production credentials are needed.
"""

import json
import subprocess
import tempfile
from pathlib import Path

from shared.config import profiles
from worker.adapters.handbrake import encode_command, parse_progress, parse_scan
from worker.adapters.media import video_metadata
from worker.pipeline.validation import EncodeValidator, timeline


def run(command):
    result = subprocess.run([str(arg) for arg in command], capture_output=True, text=True, timeout=180)
    assert result.returncode == 0, result.stderr[-8000:] + result.stdout[-8000:]
    return result.stdout, result.stderr


def inspect(path):
    stdout, _ = run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", path])
    return video_metadata(json.loads(stdout))


def main():
    stdout, stderr = run(["HandBrakeCLI", "--version"])
    assert "HandBrake 1.11.2" in stdout + stderr, stdout + stderr
    with tempfile.TemporaryDirectory(prefix="handbrake upgrade ") as directory:
        root = Path(directory)
        # Spaces and shell metacharacters must survive the compatibility launcher.
        source = root / "source $(literal).mkv"
        run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "testsrc2=size=320x160:rate=24:duration=3",
                "-vf",
                "pad=320:192:0:16",
                "-c:v",
                "ffv1",
                "-pix_fmt",
                "yuv420p",
                "-color_primaries",
                "bt709",
                "-color_trc",
                "bt709",
                "-colorspace",
                "bt709",
                "-color_range",
                "tv",
                "-threads",
                "2",
                source,
            ]
        )
        stdout, _ = run(["HandBrakeCLI", "-i", source, "--scan", "--previews", "3", "--json"])
        crop = parse_scan(stdout)
        assert crop.argument() == "16:16:0:0", crop
        metadata, points = inspect(source), timeline(source)
        for name, profile in profiles().items():
            for mode in ("crf", "bitrate"):
                output = root / f"{name}-{mode}.mkv"
                command = encode_command(
                    "HandBrakeCLI",
                    source,
                    output,
                    metadata,
                    crop,
                    profile,
                    18 if mode == "crf" else None,
                    rate_control=mode,
                    bitrate_kbps=750 if mode == "bitrate" else None,
                )
                stdout, stderr = run(command)
                updates = [p for line in stdout.splitlines() if (p := parse_progress(line))]
                assert updates, stdout + stderr
                if mode == "bitrate":
                    assert {p.get("pass_number") for p in updates} == {1, 2}, updates
                    assert all(p["percentage"] <= 50 for p in updates if p["pass_number"] == 1)
                    assert all(p["percentage"] >= 50 for p in updates if p["pass_number"] == 2)
                result = EncodeValidator().validate(
                    metadata,
                    inspect(output),
                    crop,
                    profile,
                    points,
                    timeline(output),
                    output.stat().st_size,
                )
                assert result["valid"], (name, mode, result)
                assert result["metrics"]["encoded_frames"] == 72, result
                run(["ffmpeg", "-v", "error", "-xerror", "-i", output, "-f", "null", "-"])
                print(
                    f"PASS: {name} {mode}, crop, progress, 72 frames, color, bit depth, PTS and decode",
                    flush=True,
                )


if __name__ == "__main__":
    main()
