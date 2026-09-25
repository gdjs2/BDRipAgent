"""Verify subtitle dialogue anchors independently of the agent runtime."""

import numpy as np


def fit_alignment(review, candidate_cues, reference_cues, duration):
    if not review.usable or not review.alignment_confident:
        raise ValueError("Agent could not establish reliable subtitle alignment: " + review.explanation)
    candidates = {cue["id"]: cue for cue in candidate_cues}
    references = {cue["id"]: cue for cue in reference_cues}
    anchors = review.anchors
    if (
        len(anchors) < 6
        or len({a.candidate_id for a in anchors}) != len(anchors)
        or len({a.reference_id for a in anchors}) != len(anchors)
    ):
        raise ValueError("Alignment needs at least six distinct dialogue matches")
    if any(a.candidate_id not in candidates or a.reference_id not in references for a in anchors):
        raise ValueError("Alignment cited a cue outside the supplied evidence")
    if len({references[a.reference_id]["track_id"] for a in anchors}) != 1:
        raise ValueError("Alignment anchors must use one consistent source subtitle track")
    pairs = sorted(
        (candidates[a.candidate_id]["seconds"], references[a.reference_id]["seconds"]) for a in anchors
    )
    x, y = np.array(pairs).T
    if (
        np.any(np.diff(y) <= 0)
        or np.ptp(y) < duration * 0.55
        or min(y) > duration * 0.25
        or max(y) < duration * 0.75
    ):
        raise ValueError("Alignment needs consistent dialogue matches across the beginning, middle and end")
    if len({min(2, int(t / duration * 3)) for t in y}) < 3:
        raise ValueError("Alignment anchors do not cover all three parts of the movie")
    fitted, offset = np.polyfit(x, y, 1)
    # Snap to conventional FPS ratios when they explain the matches accurately.
    fps = (24000 / 1001, 24, 25, 30000 / 1001, 30)
    scales = [1.0, *(a / b for a in fps for b in fps if 0.9 <= a / b <= 1.1)]
    snapped = min(scales, key=lambda value: abs(value - fitted))
    snapped_offset = float(np.median(y - x * snapped))
    if max(abs(y - (x * snapped + snapped_offset))) <= 0.6:
        fitted, offset = snapped, snapped_offset
    residuals = abs(y - (x * fitted + offset))
    if not 0.9 <= fitted <= 1.1 or abs(offset) > 600 or max(residuals) > 0.75:
        raise ValueError(
            "Cue matches show inconsistent drift or a different cut; manual alignment is required"
        )
    proposed = x * review.scale + review.offset_seconds
    if max(abs(proposed - y)) > 1.5:
        raise ValueError("The agent's proposed FPS/offset does not agree with its dialogue matches")
    # Hold out each match in turn to detect unstable fits rather than trusting a pair of endpoints.
    for index in range(len(x)):
        a, b = np.polyfit(np.delete(x, index), np.delete(y, index), 1)
        if abs(a * x[index] + b - y[index]) > 1:
            raise ValueError("Alignment does not pass independent cue checks")
    return {
        "scale": float(fitted),
        "offset_seconds": float(offset),
        "max_error_seconds": float(max(residuals)),
        "anchor_count": len(anchors),
        "reference_track_id": references[anchors[0].reference_id]["track_id"],
        "anchors": [a.model_dump() for a in anchors],
        "method": "agent dialogue matches + verified linear FPS/offset fit",
    }
