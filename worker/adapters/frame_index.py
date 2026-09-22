"""Exact presentation-order frame numbers without decoding the source again."""

import json

import numpy as np

from shared.paths import contained
from worker.adapters.mkvtoolnix import MKVToolNixProgress


def frame_number(points, pts, tolerance=0.003):
    index = int(np.searchsorted(points, pts))
    nearest = min((i for i in (index - 1, index) if 0 <= i < len(points)), key=lambda i: abs(points[i] - pts))
    if abs(points[nearest] - pts) > tolerance:
        raise ValueError(f"No indexed source frame at PTS {pts:.6f}")
    return nearest


def source_frame_index(ctx):
    cached = ctx.job.analysis.get("source_frame_index")
    if cached:
        points = np.load(contained(ctx.workspace, cached, exists=True), allow_pickle=False)
    else:
        timestamps = ctx.job.analysis.get("source_video_timestamps")
        if timestamps:
            path = contained(ctx.workspace, timestamps, exists=True)
            ctx.log("Reusing source video timestamps from track extraction.")
        else:
            ctx.log("Indexing Matroska frame timestamps without decoding video.")
            ctx.progress(0, phase="Indexing frame timestamps", tool="mkvextract")
            source = ctx.source()
            metadata = json.loads(ctx.run([ctx.settings.mkvmerge_bin, "-J", source]))
            videos = [track for track in metadata["tracks"] if track["type"] == "video"]
            if len(videos) != 1:
                raise ValueError("Screenshot indexing requires exactly one video track")
            path = ctx.output("screenshots", "source-timestamps.txt")
            ctx.run(
                [
                    ctx.settings.mkvextract_bin,
                    source,
                    "timestamps_v2",
                    f"{videos[0]['id']}:{path}",
                    "--gui-mode",
                ],
                progress_parser=MKVToolNixProgress(
                    "mkvextract", "Indexing frame timestamps", progress_span=5
                ),
            )
        # MKVToolNix appends the last frame's end time. It is a duration
        # boundary, not another decoded frame. Keep the raw file for remuxing.
        points = np.loadtxt(path, comments="#", ndmin=1)[:-1] / 1000
        # Matroska stores B-frames in decode order; the exported timecodes are
        # presentation ordered by mkvextract. Check instead of guessing from FPS.
        path = ctx.output("screenshots", "source-frame-pts.npy")
        np.save(path, points)
        cached = ctx.artifact(path, "SOURCE_FRAME_INDEX")
    metrics = ctx.job.validation["metrics"]
    if points.ndim != 1 or len(points) < 2 or not np.isfinite(points).all() or np.any(np.diff(points) <= 0):
        raise ValueError("Source frame timestamps must be finite and strictly increasing")
    if metrics.get("source_frames") and len(points) != metrics["source_frames"]:
        raise ValueError("Source timestamp index disagrees with the validated frame count")
    if abs(points[0] - metrics["source_first_pts"]) > 0.003:
        raise ValueError("Source timestamp index disagrees with the validated first frame")
    return points, cached
