"""Compare real batched track preparation with separate MKVToolNix extraction.

Run in an isolated worker image; all fixtures and outputs live in /tmp.
Track selection and content classification are fixtures; extraction and PGS cropping are real.
Content detection has separate real OCR fixtures in test_subtitle_ocr.py.
"""

import json
import runpy
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from shared.config import get_settings
from worker.adapters.integrations import pgs_dimensions
from worker.pipeline import stages


def run(args):
    return subprocess.run([str(x) for x in args], check=True, capture_output=True, text=True).stdout


class Context:
    settings = get_settings()

    def __init__(self, root, source, tracks):
        self.workspace, self.input = root, source
        self.commands = []
        self.job = SimpleNamespace(
            id="fixture",
            title="Track extraction fixture",
            year=2026,
            analysis_profile="x264-live",
            analysis={
                "tracks": tracks,
                "video": {"width": 1920, "height": 1080},
                "crop": {"left": 4, "top": 120, "right": 8, "bottom": 156},
            },
        )

    def source(self):
        return self.input

    def output(self, category, filename):
        path = self.workspace / category / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def progress(self, value, **detail):
        pass

    def run(self, args, **kwargs):
        self.commands.append([str(x) for x in args])
        return run(args)

    def artifact(self, path, kind, **kwargs):
        assert path.is_file() and path.stat().st_size
        return str(path.relative_to(self.workspace))


def main():
    with tempfile.TemporaryDirectory(prefix="batch tracks ") as temporary:
        root = Path(temporary)
        audio = []
        for frequency in (440, 880):
            path = root / f"audio {frequency}.ac3"
            run(
                [
                    "ffmpeg",
                    "-v",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    f"sine=frequency={frequency}:sample_rate=48000:duration=3",
                    "-c:a",
                    "ac3",
                    path,
                ]
            )
            audio.append(path)
        subtitle = root / "original.sup"
        subtitle.write_bytes(runpy.run_path("/opt/sup2sup/tests/fixtures.py")["simple"]())
        source = root / "Multi track fixture.mkv"
        run(["mkvmerge", "-o", source, audio[0], "--sync", "0:125", audio[1], subtitle, subtitle])
        original_stat = (source.stat().st_size, source.stat().st_mtime_ns)
        identified = json.loads(run(["mkvmerge", "-J", source]))["tracks"]
        tracks = [
            {"track_id": t["id"], "kind": t["type"], "codec_id": t["properties"]["codec_id"]}
            for t in identified
        ]
        selection = SimpleNamespace(
            audio_track_ids=[t["track_id"] for t in tracks if t["kind"] == "audio"][::-1],
            subtitle_track_ids=[t["track_id"] for t in tracks if t["kind"] == "subtitles"][::-1],
        )

        @contextmanager
        def selected_tracks():
            yield SimpleNamespace(scalar=lambda query: selection)

        ctx = Context(root, source, tracks)
        with (
            patch.object(stages, "session", selected_tracks),
            patch.object(stages, "classify_subtitle", lambda ctx, track, path: track),
        ):
            save = stages.prepare_tracks(ctx)
        save(SimpleNamespace(scalars=lambda query: []), ctx.job)
        extraction = [cmd for cmd in ctx.commands if cmd[0] == ctx.settings.mkvextract_bin]
        assert len(extraction) == 1 and "timestamps_v2" in extraction[0]
        prepared = ctx.job.analysis["prepared_tracks"]
        assert [t["track_id"] for t in prepared] == selection.audio_track_ids + selection.subtitle_track_ids
        for track in prepared:
            track_id = track["track_id"]
            is_audio = track["kind"] == "audio"
            ext = "ac3" if is_audio else "sup"
            baseline = root / f"separate-{track_id}.{ext}"
            run(["mkvextract", source, "tracks", f"{track_id}:{baseline}"])
            extracted = root / ("audio" if is_audio else "subtitles") / f"track-{track_id}.{ext}"
            assert extracted.read_bytes() == baseline.read_bytes()
            if is_audio:
                timestamps = root / f"separate-{track_id}.timestamps.txt"
                run(["mkvextract", source, "timestamps_v2", f"{track_id}:{timestamps}"])
                assert (root / track["timestamps"]).read_bytes() == timestamps.read_bytes()
            else:
                assert pgs_dimensions(root / track["path"]) == (1908, 804)
                report = json.loads((root / track["path"]).with_suffix(".report.json").read_text())
                assert report["timestamps_identical"] and report["bitmap_data_identical"]
                assert report["palette_data_identical"]
        assert (source.stat().st_size, source.stat().st_mtime_ns) == original_stat
        print("PASS: one extraction for two audio tracks, two PGS tracks and audio timestamps.")
        print("Outputs match separate extraction byte-for-byte; real PGS cropping preserves cues.")


if __name__ == "__main__":
    main()
