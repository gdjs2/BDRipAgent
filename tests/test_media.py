import json
import subprocess
from pathlib import Path

import av
import numpy as np
import pytest
from PIL import Image

from shared.config import behavior, profiles
from worker.adapters.handbrake import Crop, encode_command, parse_progress, parse_scan
from worker.adapters.integrations import normalize_crf, studio_config, validate_pgs_dimensions
from worker.adapters.media import video_metadata
from worker.pipeline.screenshots import acceptable, cropped_image, extract_at, overlay, technical_metrics
from worker.pipeline.validation import EncodeValidator, timeline


def test_handbrake_scan_uses_json_crop_and_does_not_guess():
    text = "scan: other log output\nJSON Title Set: " + json.dumps(
        {"TitleList": [{"Index": 1, "Crop": [138, 138, 0, 0]}]}
    )
    crop = parse_scan(text)
    assert crop.dimensions(1920, 1080) == (1920, 804)
    assert crop.argument() == "138:138:0:0"
    with pytest.raises(ValueError):
        parse_scan("scan complete with no crop")
    with pytest.raises(ValueError):
        Crop(top=1080, bottom=0, left=0, right=0).dimensions(1920, 1080)


@pytest.mark.parametrize("separator", [":", " = "])
def test_handbrake_scan_accepts_both_text_crop_formats(separator):
    text = f"scan: 20 previews, 1920x1080, autocrop{separator}104/104/0/0, aspect 16:9\n"
    crop = parse_scan(text * 2)
    assert crop.argument() == "104:104:0:0"
    assert crop.dimensions(1920, 1080) == (1920, 872)
    assert parse_scan("+ autocrop = 0/0/0/0").argument() == "0:0:0:0"


def test_handbrake_scan_recovers_crop_from_interleaved_legacy_log():
    # HandBrake's exit diagnostic landed inside a subtitle attribute in the
    # affected movie's saved scan. The preview summary still has a valid crop.
    text = (
        "[07:45:00] scan: 20 previews, 1920x1080, autocrop = 104/104/0/0, aspect 16:9\n"
        'JSON Title Set: {"TitleList": [{"Index": 1, "Crop": [104,104,0,0], '
        '"SubtitleList": [{"ClosHandBrake has exited.\nedCaption": false}]}]}'
    )
    assert parse_scan(text).argument() == "104:104:0:0"
    with pytest.raises(ValueError, match="unambiguous crop"):
        parse_scan(text + "\n+ autocrop: 100/100/0/0\n")


def test_encode_command_does_not_interpret_shell_characters():
    cmd = encode_command(
        "HandBrakeCLI",
        Path("/source/Movie; $(touch evil).mkv"),
        Path("/workspace/video.mkv"),
        {"width": 1920, "height": 1080},
        Crop(top=138, bottom=138, left=0, right=0),
        profiles()["x264-live"],
        17,
    )
    assert cmd[cmd.index("-i") + 1] == "/source/Movie; $(touch evil).mkv"
    assert cmd[cmd.index("--crop") + 1] == "138:138:0:0"
    assert cmd[cmd.index("--height") + 1] == "804"
    assert cmd[cmd.index("--audio") + 1] == "none"


def test_progress_parses_carriage_return_output():
    data = parse_progress("\rEncoding: task 1 of 1, 68.30 % (13.80 fps, avg 12.80 fps, ETA 02h31m22s)\r")
    assert data == {"percentage": 68.3, "fps": 13.8, "eta_seconds": 9082}


def test_crf_adapter_rejects_partial_nonfinite_and_wrong_codec_reports():
    rows = [
        {"crf": 13, "average_bitrate_mbps": 9.5, "average_qp": 19, "complete": True},
        {"crf": 20, "average_bitrate_mbps": 6, "average_qp": 22, "complete": True},
    ]
    raw = {"schema_version": 5, "state": "complete", "codecs": {"x264": {"rows": rows}}}
    result = normalize_crf(raw, "x264")
    assert result["samples"][0]["bitrate_kbps"] == 9500
    assert result["predicted"][-1]["average_qp"] == 22
    assert len(result["predicted"]) == 8
    with pytest.raises(ValueError):
        normalize_crf(raw, "x265")
    rows[0]["complete"] = False
    with pytest.raises(ValueError):
        normalize_crf(raw, "x264")
    rows[0]["complete"] = True
    rows[0]["average_bitrate_mbps"] = float("nan")
    with pytest.raises(ValueError):
        normalize_crf(raw, "x264")
    rows[0]["average_bitrate_mbps"] = 9.5
    rows[0]["average_qp"] = None
    assert all(p["average_qp"] is None for p in normalize_crf(raw, "x264")["predicted"])


@pytest.mark.parametrize("level,expected", [(4.1, "4.1"), (4, "4"), ("4.1", "4.1"), (None, None)])
def test_crf_profile_level_matches_handbrake(level, expected):
    profile = {**profiles()["x264-live"], "level": level}
    config = json.loads(json.dumps(studio_config(profile)))["codecs"]["x264"]
    assert config["level"] == expected
    assert profile["level"] == level  # Keep the saved profile unchanged.
    command = encode_command(
        "HandBrakeCLI",
        "source.mkv",
        "video.mkv",
        {"width": 1920, "height": 1080},
        Crop(top=104, bottom=104, left=0, right=0),
        profile,
        18,
    )
    if expected is None:
        assert "--encoder-level" not in command
    else:
        assert command[command.index("--encoder-level") + 1] == config["level"]


@pytest.fixture(scope="module")
def videos(tmp_path_factory):
    directory = tmp_path_factory.mktemp("media")
    source, encoded = directory / "source.mkv", directory / "encoded.mkv"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x192:rate=24:duration=3",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-crf",
            "12",
            "-threads",
            "1",
            str(source),
        ],
        check=True,
    )
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(source),
            "-vf",
            "crop=320:160:0:16",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-crf",
            "18",
            "-threads",
            "1",
            str(encoded),
        ],
        check=True,
    )

    def metadata(path):
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
            capture_output=True,
            text=True,
            check=True,
        )
        return video_metadata(json.loads(result.stdout))

    return source, encoded, metadata(source), metadata(encoded)


def test_real_video_validation_checks_every_frame_and_crop(videos):
    source, encoded, src, enc = videos
    source_pts, encoded_pts = timeline(source), timeline(encoded)
    validator = EncodeValidator()
    result = validator.validate(
        src,
        enc,
        Crop(top=16, bottom=16, left=0, right=0),
        profiles()["x264-live"],
        source_pts,
        encoded_pts,
        encoded.stat().st_size,
    )
    assert result["valid"], result
    assert result["metrics"]["source_frames"] == 72
    bad = validator.validate(
        src,
        enc,
        Crop(top=0, bottom=0, left=0, right=0),
        profiles()["x264-live"],
        source_pts,
        encoded_pts[:-1],
        encoded.stat().st_size,
    )
    assert not bad["valid"]
    assert any("resolution" in s for s in bad["errors"])
    assert any("frame count" in s for s in bad["errors"])


def test_real_timestamp_matching_and_picture_types(videos):
    source, encoded, *_ = videos
    with av.open(str(source)) as container:
        expected = list(container.decode(video=0))[37]
    pts = float(expected.pts * expected.time_base)
    extracted = extract_at(source, pts, 0.003)
    pair = extract_at(encoded, pts, 0.003)
    assert extracted.pts == expected.pts
    assert np.array_equal(extracted.to_ndarray(format="rgb24"), expected.to_ndarray(format="rgb24"))
    assert int(extracted.pict_type) in [1, 2, 3]
    assert cropped_image(extracted, Crop(top=16, bottom=16, left=0, right=0)).size == pair.to_image().size


def test_overlay_calibration_fixture_preserves_resolution_and_pixels(tmp_path):
    image = Image.new("RGB", (1920, 804), "#555555")
    result = overlay(image, 43631, 200484, "B", "Source")
    result.save(tmp_path / "overlay-calibration.png")
    rgb = np.asarray(result)
    assert result.size == (1920, 804)
    changed = np.any(rgb != 85, axis=2)
    ys, xs = np.where(changed)
    assert 7 <= xs.min() <= 9
    assert 2 <= ys.min() <= 5
    assert ys.max() < 58
    assert not changed[60:].any()
    assert np.any(np.all(rgb == [255, 255, 0], axis=2))


def test_candidate_filter_rejects_black_white_and_blank_frames():
    for color in ["black", "white", "#777777"]:
        metrics, _ = technical_metrics(Image.new("RGB", (320, 180), color))
        assert not acceptable(metrics, behavior())


def test_pgs_adapter_checks_composition_dimensions(tmp_path):
    output = tmp_path / "cropped.sup"
    payload = (1920).to_bytes(2, "big") + (804).to_bytes(2, "big")
    header = b"PG" + bytes(8) + b"\x16" + len(payload).to_bytes(2, "big")
    output.write_bytes(header + payload)
    validate_pgs_dimensions(output, 1920, 804)
    with pytest.raises(ValueError, match="dimensions"):
        validate_pgs_dimensions(output, 1920, 1080)
    output.write_bytes(header + payload[:2])
    with pytest.raises(ValueError, match="truncated"):
        validate_pgs_dimensions(output, 1920, 804)


@pytest.mark.parametrize(
    "filename,label",
    [
        ("43631.src.png", "Source"),
        ("43631.wiki.png", "No.Other.Choice.2025.1080p.BluRay.x264-WiKi"),
    ],
)
def test_overlay_matches_supplied_reference_geometry(filename, label):
    from scripts.calibrate_overlay import compare

    reference = Image.open(Path(__file__).resolve().parents[1] / "docs" / filename)
    rendered = overlay(Image.new("RGB", reference.size, "#555555"), 43631, 200484, "B", label)
    report = compare(reference, rendered)
    # A metrically similar open font: glyph rasterization differs from the reference.
    assert report["yellow_mask_iou"] > 0.45, report


@pytest.mark.parametrize("smoke", [False, True])
def test_comparison_last_row_uses_actual_final_movie_filename(tmp_path, monkeypatch, smoke):
    from fractions import Fraction
    from types import SimpleNamespace

    from worker.pipeline import screenshots

    frame = SimpleNamespace(pict_type=3, pts=1000, time_base=Fraction(1, 24))
    monkeypatch.setattr(screenshots, "extract_at", lambda *a: frame)
    monkeypatch.setattr(screenshots, "cropped_image", lambda *a: Image.new("RGB", (1920, 1080), "gray"))
    monkeypatch.setattr(screenshots, "rgb_image", lambda *a: Image.new("RGB", (1920, 1080), "gray"))
    (tmp_path / "video.mkv").touch()
    artifacts = []

    def artifact(path, kind, **kwargs):
        artifacts.append((kind, kwargs["info"]))
        assert Image.open(path).size == (1920, 1080)
        return path.name

    ctx = SimpleNamespace(
        workspace=tmp_path,
        check=lambda: None,
        source=lambda: tmp_path / "source.mkv",
        progress=lambda *a, **kw: None,
        output=lambda category, name: tmp_path / name,
        artifact=artifact,
        job=SimpleNamespace(
            release_name="Stale.Title-WiKi",
            analysis={
                "smoke_test": smoke,
                "crop": {"left": 0, "top": 0, "right": 0, "bottom": 0},
                "video": {},
                "encoded_path": "video.mkv",
                "final_path": "job/Actual.Movie.x265-WiKi.mkv",
            },
            validation={"metrics": {"source_first_pts": 0, "encoded_first_pts": 0}},
        ),
    )
    shot = SimpleNamespace(
        id="shot",
        candidate_id=1,
        info={
            "source_pts_seconds": 1000 / 24,
            "source_frame_number": 1000,
            "source_total_frames": 5000,
        },
    )
    screenshots.render_pairs(ctx, [shot])
    assert artifacts[0][1]["overlay_label"] == "Source"
    expected = "Actual.Movie.x265-WiKi.mkv"
    assert artifacts[1][1]["overlay_label"] == (
        "SMOKE TEST - Source reused | " + expected if smoke else expected
    )
