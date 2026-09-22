"""Pinned BDRip_Scripts CRF CLI and Sup2Sup CLI contracts."""

import json
import math
from fractions import Fraction

from shared.config import behavior
from worker.adapters.crf_progress import CRFProgressReader
from worker.progress import plan


def pgs_dimensions(path):
    dimensions = set()
    with path.open("rb") as stream:
        while header := stream.read(13):
            if len(header) != 13 or header[:2] != b"PG":
                raise ValueError("Invalid PGS segment stream")
            size = int.from_bytes(header[11:13], "big")
            payload = stream.read(size)
            if len(payload) != size:
                raise ValueError("PGS stream contains a truncated segment")
            if header[10] == 0x16:
                if len(payload) < 4:
                    raise ValueError("PGS composition contains truncated dimensions")
                dimensions.add((int.from_bytes(payload[:2], "big"), int.from_bytes(payload[2:4], "big")))
    if len(dimensions) != 1:
        raise ValueError("PGS must contain one consistent composition canvas")
    return dimensions.pop()


def validate_pgs_dimensions(path, width, height):
    if pgs_dimensions(path) != (width, height):
        raise ValueError("PGS composition dimensions do not match the HandBrake crop")


def subtitle_dimensions(source, crop, video):
    width, height = pgs_dimensions(source)
    scale = Fraction(width, video["width"])
    if scale != Fraction(height, video["height"]):
        raise ValueError("PGS canvas and source video have different aspect ratios")
    margins = [scale * v for v in (crop.left, crop.top, crop.right, crop.bottom)]
    if any(v.denominator != 1 for v in margins):
        raise ValueError("HandBrake crop does not map to whole subtitle pixels")
    left, top, right, bottom = map(int, margins)
    return width - left - right, height - top - bottom


class Sup2supAdapter:
    def crop(self, ctx, source, output, crop, video):
        crop.dimensions(video["width"], video["height"])
        dimensions = subtitle_dimensions(source, crop, video)
        report = output.with_suffix(".report.json")
        config = behavior()["integrations"]["sup2sup"]
        ctx.run(
            [
                ctx.settings.sup2sup_bin,
                "crop",
                source,
                output,
                "--crop",
                str(crop.left),
                str(crop.top),
                str(crop.right),
                str(crop.bottom),
                "--video-source",
                str(video["width"]),
                str(video["height"]),
                "--fit",
                config["fit"],
                "--margin",
                str(config["margin"]),
                "--report",
                report,
            ]
        )
        validate_pgs_dimensions(output, *dimensions)
        data = json.loads(report.read_text())
        if not data.get("presentation_timestamps_identical") or not data.get("palette_data_identical"):
            raise ValueError("Sup2Sup did not preserve presentation timestamps and palettes")
        if not data.get("bitmap_data_identical") and not data.get("cropped_fullscreen_cues"):
            raise ValueError("Sup2Sup changed ordinary subtitle bitmap data")
        ctx.artifact(report, "SUBTITLE_CROP_REPORT")


def normalize_crf(raw, codec):
    if raw.get("schema_version") != 5 or raw.get("state") != "complete":
        raise ValueError("CRF Studio requires a complete schema-version-5 report")
    if codec not in raw.get("codecs", {}):
        raise ValueError("CRF Studio did not analyze the selected codec")
    analysis = raw["codecs"][codec]
    samples = []
    for point in analysis["rows"]:
        if not point.get("complete"):
            raise ValueError("CRF Studio returned an incomplete endpoint")
        normalized = {
            "crf": float(point["crf"]),
            "bitrate_kbps": float(point["average_bitrate_mbps"]) * 1000,
            "average_qp": float(point["average_qp"]) if point.get("average_qp") is not None else None,
        }
        if any(v is not None and (not math.isfinite(v) or v < 0) for v in normalized.values()):
            raise ValueError("Invalid CRF statistic")
        if normalized["bitrate_kbps"] <= 0:
            raise ValueError("CRF bitrate must be positive")
        samples.append({**point, **normalized})
    samples.sort(key=lambda p: p["crf"])
    if [p["crf"] for p in samples] != [13, 20]:
        raise ValueError("CRF Studio must supply exactly the measured CRF 13 and 20 endpoints")
    # Same two-point model as upstream. Interpolate only inside the measured range.
    low, high = samples
    predicted = []
    for crf in range(13, 21):
        fraction = (crf - 13) / 7
        qp = (
            None
            if low["average_qp"] is None or high["average_qp"] is None
            else (low["average_qp"] + fraction * (high["average_qp"] - low["average_qp"]))
        )
        predicted.append(
            {
                "crf": crf,
                "average_qp": qp,
                "bitrate_kbps": math.exp(
                    math.log(low["bitrate_kbps"]) * (1 - fraction) + math.log(high["bitrate_kbps"]) * fraction
                ),
            }
        )
    return {
        "samples": samples,
        "predicted": predicted,
        "statistics": {
            "qp_frame_type": raw.get("qp_frame_type"),
            "models": analysis.get("models", {}),
            "sample_plan": raw.get("sample_plan", []),
            "versions": raw.get("versions", {}),
            "notes": raw.get("notes", []),
            "method": raw.get("method"),
        },
    }


def studio_config(profile):
    return {
        "sampling": behavior()["integrations"]["crf_studio"],
        "codecs": {
            profile["codec"]: {
                "preset": profile["preset"],
                "tune": profile.get("tune"),
                "pixel_format": "yuv420p10le" if profile["bit_depth"] == 10 else "yuv420p",
                "profile": profile.get("video_profile", "high" if profile["codec"] == "x264" else "main10"),
                # YAML parses an unquoted level such as 4.1 as a number.
                # CRF Studio requires a string, just like HandBrake's CLI argument.
                "level": str(profile["level"]) if profile.get("level") is not None else None,
                # Replace upstream defaults so sampling uses the same configured encoder settings.
                "params": profile.get("extra_options") or {},
                "options": {},
            }
        },
    }


class CRFStudioAdapter:
    def run_analysis(self, ctx, profile, crop):
        output = ctx.output("crf", "results.json")
        config = ctx.output("crf", "config.json")
        config.write_text(json.dumps(studio_config(profile), indent=2))
        ctx.artifact(config, "CRF_PROFILE")
        video = ctx.job.analysis["video"]
        width, height = crop.dimensions(video["width"], video["height"])
        progress = ctx.output("crf", "progress.json")
        steps = plan(ctx, encoding=95, reports=5)
        steps["encoding"].progress(None, stage="starting", message="Starting CRF Studio", elapsed_seconds=0)
        steps["encoding"].run(
            [
                ctx.settings.crf_studio_bin,
                "crf",
                ctx.source(),
                "--codec",
                profile["codec"],
                "--config",
                config,
                "--output-dir",
                output.parent,
                "--progress-file",
                progress,
                "--crop",
                f"{width}:{height}:{crop.left}:{crop.top}",
            ],
            progress_reader=CRFProgressReader(progress),
        )
        steps["encoding"].done("CRF samples encoded")
        steps["reports"].progress(None, stage="saving", message="Validating and saving CRF results")
        raw = json.loads(output.read_text())
        result = normalize_crf(raw, profile["codec"])
        for path in sorted(output.parent.iterdir()):
            if path.is_file() and path != config:
                ctx.artifact(path, "CRF_RESULTS_RAW" if path == output else "CRF_REPORT")
        return {
            **result,
            "profile_snapshot": profile,
            "raw_path": str(output.relative_to(ctx.workspace)),
            "sampling_backend": "PyAV native encoders; full encode uses HandBrakeCLI",
        }
