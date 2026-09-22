"""Real pinned BDRip_Scripts release test; network disabled, upload responses stubbed."""

import hashlib
import json
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

from bdrip.release import screenshots
from bdrip.release.pipeline import source_description as upstream_source_description
from bdrip.release.torrent import verify_torrent
from PIL import Image
from torf import Torrent

from worker.adapters.release_runner import run


def main():
    # Load the small formatting module without the application's language-label dependency.
    import runpy
    import sys
    from types import SimpleNamespace

    with patch.dict(sys.modules, langcodes=SimpleNamespace(Language=object)):
        formatter = runpy.run_path("/app/shared/naming.py")["source_description"]
    names = [
        "Movie.2026.1080p.Blu-ray.AVC.DTS-HD.MA.5.1@GROUP",
        "Movie.2026.2160p.UHD.Blu-ray.HEVC.TrueHD.7.1@GROUP",
        "1080p Blu-ray AVC DTS-HD MA 5.1-GROUP",
        "Disc.2.0-GROUP",
    ]
    for name in names:
        assert formatter(name) == upstream_source_description(Path(name))
    with tempfile.TemporaryDirectory(prefix="release-test-") as directory:
        root = Path(directory)
        movie = root / "SMOKE-TEST.Movie.2026.x264-WiKi.mkv"
        encoded = subprocess.run(
            [
                "ffmpeg",
                "-v",
                "info",
                "-f",
                "lavfi",
                "-i",
                "testsrc2=size=320x180:rate=24:duration=1",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:duration=1",
                "-c:v",
                "libx264",
                "-c:a",
                "ac3",
                str(movie),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        original = movie.stat()
        encode_log = root / "encode.log"
        encode_log.write_text(encoded.stderr)
        pairs = []
        for index in range(2):
            pair = {"candidate_id": index + 1, "frame_number": index * 10 + 3}
            for kind in ("src", "encode"):
                image = root / f"{index}.{kind}.png"
                Image.new("RGB", (320, 180), (index * 80, 50, 100)).save(image)
                pair[kind] = str(image)
            pairs.append(pair)
        Image.new("RGB", (320, 180)).save(root / "not-selected.png")
        request = {
            "details": {
                "source": formatter(names[0]),
                "chinese_name": "测试电影",
                "extra_description": "双语中字",
                "tracker": "https://tracker.example/announce?passkey=fixture",
            },
            "title": "Movie",
            "year": 2026,
            "imdb_id": None,
            "smoke_test": True,
            "codec": "x264",
            "movie_path": str(movie),
            "package_dir": str(root / "package" / movie.stem),
            "pairs": pairs,
            "cache_dir": str(root / "cache"),
            "output_dir": str(root / "output"),
            "metadata_cache": str(root / "metadata.json"),
            "remote_suffix": "fixture",
        }
        calls = []
        interrupted = False

        def interrupted_upload(path, folder, token):
            nonlocal interrupted
            calls.append(path.name)
            if "000000003.1.encode" in path.name:
                raise RuntimeError("temporary fixture failure")
            if "000000013.0.src" in path.name and not interrupted:
                interrupted = True
                raise KeyboardInterrupt("fixture process interruption")
            return f"https://images.example/{path.name}", f"https://images.example/thumb-{path.name}"

        with (
            patch.object(screenshots, "upload_image", interrupted_upload),
            patch.object(screenshots.time, "sleep", lambda _: None),
        ):
            try:
                run(request, "fixture-token")
            except KeyboardInterrupt:
                pass
            else:
                raise AssertionError("Expected an interrupted upload")
        cache = json.loads((root / "cache/uploads.json").read_text())
        assert len(cache["screenshot_uploads"]["Comparison"]) == 1
        assert len(cache["failed_screenshot_uploads"]["Comparison"]) == 1
        assert not (root / "output/result.json").exists()
        calls.clear()

        def upload(path, folder, token):
            calls.append(path.name)
            assert folder.endswith("fixture/Comparison")
            return f"https://images.example/{path.name}", f"https://images.example/thumb-{path.name}"

        with patch.object(screenshots, "upload_image", upload):
            result = run(request, "fixture-token")
        assert len(calls) == 3, "Retry must upload failed AND previously unvisited images only"
        assert not any("000000003.0.src" in name for name in calls)
        assert result["uploaded_images"] == 4
        package = Path(request["package_dir"])
        expected = {movie.name, movie.stem + ".nfo", movie.stem + ".md5"}
        assert {p.name for p in package.iterdir()} == expected
        torrent_path = root / "output" / (movie.stem + ".torrent")
        torrent = Torrent.read(torrent_path)
        assert torrent.private and torrent.name == movie.stem
        assert {Path(str(p)).name for p in torrent.files} == expected
        assert verify_torrent(torrent_path, package)
        assert result["infohash"] == torrent.infohash
        assert result["md5"] == hashlib.md5(movie.read_bytes()).hexdigest()
        assert (package / (movie.stem + ".md5")).read_text().strip() == f"{result['md5']}  {movie.name}"
        assert (package / movie.name).stat().st_ino == original.st_ino
        assert (movie.stat().st_size, movie.stat().st_mtime_ns) == (original.st_size, original.st_mtime_ns)
        post = (root / "output" / (movie.stem + ".bbcode.txt")).read_text()
        assert "测试电影" in post and "双语中字" in post and "SMOKE TEST" in post
        assert post.count("[URL=") == 4 and "not-selected" not in post
        assert post.index("000000003.0.src") < post.index("000000003.1.encode")
        nfo = (package / (movie.stem + ".nfo")).read_bytes().decode("cp437")
        assert "SMOKE TEST - Source reused" in nfo and request["details"]["source"] in nfo
        assert request["details"]["source"] in post
        assert "Movie.2026.1080p" not in nfo
        assert "320x180" in nfo
        with patch.object(
            screenshots, "upload_image", side_effect=AssertionError("Cached files must not upload again")
        ):
            run(request, "fixture-token")
        # Disabling uploads must ignore even successfully cached URLs and work
        # without a token, while retaining the upstream empty section heading.
        offline = {**request, "details": {**request["details"], "upload_screenshots": False}}
        with patch.object(
            screenshots, "upload_screenshots_cached", side_effect=AssertionError("No uploads requested")
        ):
            offline_result = run(offline, "")
        offline_post = (root / "output" / (movie.stem + ".bbcode.txt")).read_text()
        assert ".Comparisons" in offline_post and "[URL=" not in offline_post
        assert "images.example" not in offline_post
        assert offline_result["uploaded_images"] == 0 and offline_result["screenshots"] == []
        assert offline_result["upload_screenshots"] is False
        assert verify_torrent(torrent_path, package)
        normal_movie = root / "Movie.2026.x264-WiKi.mkv"
        normal_movie.hardlink_to(movie)
        normal_request = {
            **request,
            "smoke_test": False,
            "movie_path": str(normal_movie),
            "encoder_log": str(encode_log),
            "package_dir": str(root / "normal-package" / normal_movie.stem),
            "output_dir": str(root / "normal-output"),
            "cache_dir": str(root / "normal-cache"),
        }
        custom_description = (
            "[b]自定义电影介绍[/b]\nFirst paragraph.\n\nSecond paragraph with [i]formatting[/i]."
        )
        normal_request["details"] = {**normal_request["details"], "movie_description": custom_description}
        with patch.object(screenshots, "upload_image", upload):
            normal_result = run(normal_request, "fixture-token")
        normal_post = (root / "normal-output" / f"{normal_movie.stem}.bbcode.txt").read_text()
        assert custom_description in normal_post
        assert normal_post.count(custom_description) == 1
        assert not normal_result["smoke_test"] and "frame I:" in normal_post
        assert "SMOKE TEST" not in normal_post and ".x264.Info" in normal_post
        from uuid import uuid4

        from worker.pipeline.release_exports import publish

        ctx = SimpleNamespace(
            check=lambda: None,
            progress=lambda *args, **kwargs: None,
            job=SimpleNamespace(id=str(uuid4()), release_name=normal_movie.stem),
            settings=SimpleNamespace(
                workspace_root=root / "jobs",
                completed_root=root,
                artifacts_root=root / "artifacts",
            ),
        )
        exported = publish(ctx, [{**item, "storage": "completed"} for item in normal_result["artifacts"]])
        public_torrent = next(Path(item["path"]) for item in exported if item["kind"] == "RELEASE_TORRENT")
        public_package = public_torrent.parent / normal_movie.stem
        assert public_torrent.parent.parent == root / "artifacts"
        assert " [ART] " in public_torrent.parent.name
        assert public_torrent.name == normal_movie.stem + ".torrent"
        assert verify_torrent(public_torrent, public_package)
        assert Torrent.read(public_torrent).name == public_package.name
        print(
            "PASS: real BDRip_Scripts BBCode/NFO/MD5/private torrent, piece verification, source-left pairs, resumable uploads, selected-only images, hard-linked media, and unchanged original MKV."
        )


if __name__ == "__main__":
    main()
