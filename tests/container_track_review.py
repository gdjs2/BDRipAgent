"""Verify manual track names and all five flags with real MKVToolNix."""

import json
import runpy
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace

from shared.config import get_settings
from worker.pipeline.stages import mux_command


def run(args):
    return subprocess.check_output([str(arg) for arg in args], text=True)


def main():
    with tempfile.TemporaryDirectory(prefix="track review mux ") as directory:
        root = Path(directory)
        video = root / "encoded.mkv"
        run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "color=size=192x108:rate=24:duration=2",
                "-c:v",
                "libx264",
                video,
            ]
        )
        audio = root / "audio.ac3"
        run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:duration=2",
                "-c:a",
                "ac3",
                audio,
            ]
        )
        sub = root / "subtitle.sup"
        sub.write_bytes(runpy.run_path("/opt/sup2sup/tests/fixtures.py")["simple"]())
        source = root / "source.mkv"
        run(["mkvmerge", "-o", source, video, audio, sub])
        flags = {
            "default": True,
            "forced": True,
            "hearing_impaired": True,
            "visual_impaired": True,
            "commentary": True,
        }
        tracks = [
            {
                "kind": "audio",
                "path": audio.name,
                "language": "en",
                "name_override": "User audio label",
                **flags,
            },
            {
                "kind": "subtitles",
                "path": sub.name,
                "language": "zh-Hant",
                "name_override": "繁體中文 — Custom $(literal) label",
                **{key: False for key in flags},
            },
        ]
        ctx = SimpleNamespace(
            settings=get_settings(),
            workspace=root,
            source=lambda: source,
            job=SimpleNamespace(
                title="Amélie: A Film",
                year=2001,
                release_name="Amelie.A.Film.2001.1080p.BluRay.x264-WiKi",
                analysis={
                    "prepared_tracks": tracks,
                    "encoded_path": video.name,
                    "original_languages": ["en"],
                },
                validation={"metrics": {"source_first_pts": 0, "encoded_first_pts": 0}},
            ),
        )
        final = root / "final.mkv"
        run(mux_command(ctx, final))
        inspection = json.loads(run(["mkvmerge", "-J", final]))
        assert inspection["container"]["properties"]["title"] == "Amélie: A Film (2001)"
        assert inspection["tracks"][0]["properties"]["flag_original"] is True
        actual = inspection["tracks"][1:]
        assert actual[0]["properties"]["flag_original"] is True
        assert actual[1]["properties"]["flag_original"] is False
        mapping = {
            "default": "default_track",
            "forced": "forced_track",
            "hearing_impaired": "flag_hearing_impaired",
            "visual_impaired": "flag_visual_impaired",
            "commentary": "flag_commentary",
        }
        for output, expected in zip(actual, tracks, strict=True):
            assert output["properties"]["track_name"] == expected["name_override"]
            for field, property_name in mapping.items():
                assert bool(output["properties"].get(property_name, False)) is expected[field]
        assert actual[1]["properties"]["language_ietf"] == "zh-Hant"
        print("PASS: real MKV preserves custom Unicode names and every explicit true/false flag.")


if __name__ == "__main__":
    main()
