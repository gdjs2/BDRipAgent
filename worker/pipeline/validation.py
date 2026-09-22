from fractions import Fraction

import av

from worker.adapters.handbrake import Crop


def timeline(path, check=lambda: None, *, on_progress=None):
    points = []
    with av.open(str(path)) as container:
        for index, frame in enumerate(container.decode(video=0)):
            if index % 240 == 0:
                check()
            if frame.pts is None:
                raise ValueError("Decoded frame has no presentation timestamp")
            pts = float(frame.pts * frame.time_base)
            if points and pts <= points[-1]:
                raise ValueError("Video presentation timestamps are not strictly increasing")
            points.append(pts)
            if on_progress and index % 240 == 0:
                on_progress(len(points), pts - points[0])
    if len(points) < 2:
        raise ValueError("Video contains fewer than two frames")
    if on_progress:
        on_progress(len(points), points[-1] - points[0])
    return points


class EncodeValidator:
    def validate(self, source, encoded, crop: Crop, profile, source_pts, encoded_pts, file_size):
        errors, warnings = [], []
        width, height = crop.dimensions(source["width"], source["height"])
        if (encoded["width"], encoded["height"]) != (width, height):
            errors.append("Encoded resolution does not match the authoritative HandBrake crop")
        if encoded["sample_aspect_ratio"] not in ["1:1", "N/A"]:
            errors.append("Encoded pixel/display aspect ratio changed")
        fps = float(Fraction(source["fps"]))
        if fps <= 0 or abs(float(Fraction(encoded["fps"])) - fps) > 0.01:
            errors.append("Frame rate changed")
        if encoded["bit_depth"] != profile["bit_depth"]:
            errors.append("Encoded bit depth does not match the selected profile")
        if encoded["bit_depth"] < source["bit_depth"]:
            errors.append("Encoding reduced source bit depth")
        expected_codec = {"x264": "h264", "x265": "hevc"}[profile["codec"]]
        if encoded["codec"] != expected_codec:
            errors.append("Encoded codec does not match the selected profile")
        for key in ["color_primaries", "color_transfer", "color_space", "color_range"]:
            if source.get(key) in [None, "unknown", "unspecified"]:
                warnings.append(f"Source {key} is unspecified")
            elif source[key] != encoded.get(key):
                errors.append(f"Color metadata changed: {key}")
        hdr_types = {
            "Mastering display metadata",
            "Content light level metadata",
            "DOVI configuration record",
        }
        source_hdr = [s for s in source["side_data_list"] if s.get("side_data_type") in hdr_types]
        encoded_hdr = [s for s in encoded["side_data_list"] if s.get("side_data_type") in hdr_types]
        if source_hdr != encoded_hdr:
            errors.append("HDR metadata changed")
        if len(source_pts) != len(encoded_pts):
            errors.append("Decoded frame count changed")
        source_duration = source_pts[-1] - source_pts[0] + (source_pts[-1] - source_pts[-2])
        encoded_duration = encoded_pts[-1] - encoded_pts[0] + (encoded_pts[-1] - encoded_pts[-2])
        if abs(source_duration - encoded_duration) > max(0.005, 1 / fps):
            errors.append("Decoded video duration changed")
        drift = max(abs((s - source_pts[0]) - (e - encoded_pts[0])) for s, e in zip(source_pts, encoded_pts))
        if drift > max(0.003, 0.25 / fps):
            errors.append("Frame timestamp continuity/correspondence changed")
        return {
            "valid": not errors,
            "warnings": warnings,
            "errors": errors,
            "metrics": {
                "source_frames": len(source_pts),
                "encoded_frames": len(encoded_pts),
                "source_duration": source_duration,
                "encoded_duration": encoded_duration,
                "source_first_pts": source_pts[0],
                "encoded_first_pts": encoded_pts[0],
                "max_timestamp_drift_seconds": drift,
                "file_size": file_size,
                "bitrate_kbps": file_size * 8 / encoded_duration / 1000,
                "width": encoded["width"],
                "height": encoded["height"],
                "bit_depth": encoded["bit_depth"],
            },
        }
