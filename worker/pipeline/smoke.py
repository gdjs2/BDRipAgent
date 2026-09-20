"""Source reuse for an explicitly selected downstream smoke test. No encoder runs."""

import av


def validation_report(ctx):
    source = ctx.source()  # Retain the source identity/path checks.
    video = ctx.job.analysis["video"]
    with av.open(str(source)) as container:
        first = next(container.decode(video=0), None)
        if first is None or first.pts is None:
            raise ValueError("Smoke test requires a readable source video with timestamps")
        first_pts = float(first.pts * first.time_base)
    if video["duration"] <= 0:
        raise ValueError("Smoke test requires a positive source duration")
    return {
        "valid": None,
        "skipped": True,
        "smoke_test": True,
        "source_readable": True,
        "errors": [],
        "warnings": [
            "Smoke test: no video was encoded and encode validation was skipped.",
            "Remux uses the uncropped source video; comparison images reuse cropped source frames.",
        ],
        "metrics": {
            "source_duration": video["duration"],
            "source_first_pts": first_pts,
            "encoded_first_pts": first_pts,
            "source_frames": video.get("frame_count"),
            "duration_basis": "source container metadata",
            "width": video["width"],
            "height": video["height"],
            "bit_depth": video["bit_depth"],
            "codec": video["codec"],
        },
    }
