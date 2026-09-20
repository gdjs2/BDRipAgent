"""Exercise real Sup2Sup plus MKVToolNix track names/flags in the worker image."""

import json
import runpy
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace

from shared.config import get_settings
from shared.naming import track_name
from worker.adapters.handbrake import Crop
from worker.adapters.integrations import Sup2supAdapter, pgs_dimensions
from worker.pipeline.stages import mux_command


class Context:
    settings = get_settings()

    def run(self, args):
        result = subprocess.run([str(x) for x in args], check=True, capture_output=True, text=True)
        return result.stdout

    def artifact(self, path, kind):
        assert path.is_file(), (path, kind)

    def source(self):
        return Path("/source/Fixture.mkv")


def main():
    fixture = runpy.run_path("/opt/sup2sup/tests/fixtures.py")["simple"]
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        ctx = Context()
        ctx.workspace = root
        original = root / "original.sup"
        original.write_bytes(fixture())
        for scale in (1, 2):
            output = root / f"cropped-{scale}.sup"
            Sup2supAdapter().crop(
                ctx,
                original,
                output,
                Crop(left=4 * scale, top=120 * scale, right=8 * scale, bottom=156 * scale),
                {"width": 1920 * scale, "height": 1080 * scale},
            )
            assert pgs_dimensions(output) == (1908, 804)
            report = json.loads(output.with_suffix(".report.json").read_text())
            assert report["timestamps_identical"] and report["bitmap_data_identical"]
            assert report["palette_data_identical"] and report["moved_cues"] == 1
        assert original.read_bytes() == fixture()
        probe = json.loads(ctx.run(["mkvmerge", "-J", ctx.source()]))
        audio_id = next(t["id"] for t in probe["tracks"] if t["type"] == "audio")
        ctx.run(["mkvextract", ctx.source(), "tracks", f"{audio_id}:{root / 'audio.ac3'}"])
        audio = {
            "kind": "audio",
            "codec": "AC-3",
            "codec_id": "A_AC3",
            "language": "eng",
            "name": "Arbitrary source label",
            "channels": 1,
            "channel_layout": "mono",
            "default": True,
            "forced": False,
            "commentary": True,
            "path": "audio.ac3",
        }
        subtitle = {
            "kind": "subtitles",
            "codec": "HDMV PGS",
            "codec_id": "S_HDMV/PGS",
            "language": "eng",
            "default": False,
            "forced": True,
            "hearing_impaired": True,
            "path": "cropped-1.sup",
        }
        import shutil

        shutil.copyfile(ctx.source(), root / "video.mkv")
        ctx.job = SimpleNamespace(
            title="Movie",
            release_name="Movie.2025.1080p.BluRay.x264-WiKi",
            analysis={"encoded_path": "video.mkv", "prepared_tracks": [audio, subtitle]},
            validation={"metrics": {"source_first_pts": 0, "encoded_first_pts": 0}},
        )
        output = root / "final.mkv"
        ctx.run(mux_command(ctx, output))
        result = json.loads(ctx.run(["mkvmerge", "-J", output]))
        assert result["container"]["properties"]["title"] == ctx.job.release_name
        assert len(result["tracks"]) == 3
        a, s = [t["properties"] for t in result["tracks"][1:]]
        assert a["track_name"] == track_name(audio) == "English Dolby Digital 1.0"
        assert a["default_track"] and a["flag_commentary"] and not a["forced_track"]
        assert s["track_name"] == "English PGS SDH Forced"
        assert s["flag_hearing_impaired"] and s["forced_track"] and not s["default_track"]
        print(
            "PASS: Sup2Sup asymmetric/scaled crop, preserved bitmaps/palettes/timing; real mux title, names and flags"
        )


if __name__ == "__main__":
    main()
