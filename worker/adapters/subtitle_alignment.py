"""Inspect text subtitles and verify agent cue matches before changing timestamps."""

import re

import numpy as np
import pysubs2

from backend.app.subtitle_uploads import validate_subtitle


def read_text(path, *, allow_corrupt_text=False):
    raw = path.read_bytes()
    if len(raw) > 16 * 1024 * 1024:
        raise ValueError("Text subtitles exceed 16 MB")
    try:
        text = raw.decode("utf-16" if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig")
    except UnicodeError:
        raise ValueError("Subtitle encoding is ambiguous; a UTF-8/UTF-16 copy is required") from None
    if "\x00" in text or ("\ufffd" in text and not allow_corrupt_text):
        raise ValueError("Subtitles contain corrupt or undecodable characters")
    path.write_text(text, encoding="utf-8")
    subtitles = pysubs2.load(path)
    cues, warnings = [], []
    for index, event in enumerate(subtitles, 1):
        if event.is_comment:
            continue
        if event.start < 0 or event.end <= event.start:
            raise ValueError(f"Cue {index} has invalid start/end times")
        plain = event.plaintext.strip()
        if "\ufffd" in plain:
            warnings.append(
                f"Cue {index} contains damaged replacement characters; reference-based repair required"
            )
        if not plain:
            warnings.append(f"Cue {index} has no readable text")
        cues.append(
            {"id": index, "seconds": event.start / 1000, "end": event.end / 1000, "text": plain[:500]}
        )
        if len(plain) / ((event.end - event.start) / 1000) > 30:
            warnings.append(f"Cue {index} has a reading speed above 30 characters/second")
        if max(map(len, plain.splitlines()), default=0) > 60:
            warnings.append(f"Cue {index} has a line longer than 60 characters")
        if re.search(r"\\(?:pos|move|t|k|K|clip|p[1-9])(?:\(|\d)", event.text):
            warnings.append(
                f"Cue {index} uses ASS positioning, animation or drawings; inspect the PGS rendering"
            )
    if not cues or len(cues) > 10000:
        raise ValueError("Subtitles must contain 1–10,000 dialogue cues")
    ordered = sorted(cues, key=lambda cue: cue["seconds"])
    overlaps = sum(b["seconds"] < a["end"] for a, b in zip(ordered, ordered[1:]))
    if overlaps:
        warnings.append(f"{overlaps} overlapping cue pairs")
    # Bound the model input while keeping dialogue across the entire timeline.
    indexes = np.linspace(0, len(cues) - 1, min(len(cues), 1800), dtype=int)
    samples = [cues[index] for index in indexes]
    budget = 240000 // len(samples)
    samples = [{**cue, "text": cue["text"][:budget]} for cue in samples]
    return subtitles, cues, {"cues": len(cues), "warnings": warnings[:60], "sampled_cues": samples}


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


def retime_text(subtitles, output, alignment, duration):
    for cue in subtitles:
        cue.start = round((cue.start / 1000 * alignment["scale"] + alignment["offset_seconds"]) * 1000)
        cue.end = round((cue.end / 1000 * alignment["scale"] + alignment["offset_seconds"]) * 1000)
        if not cue.is_comment and (cue.start < 0 or cue.end <= cue.start or cue.end / 1000 > duration + 5):
            raise ValueError("Aligned subtitles extend outside the movie; check the release/cut")
    subtitles.save(output)
    validate_subtitle(output, output.suffix)


def retime_pgs(source, output, alignment, duration):
    validate_subtitle(source, ".sup")
    data = bytearray(source.read_bytes())
    position = 0
    while position < len(data):
        size = int.from_bytes(data[position + 11 : position + 13], "big")
        for field in (2, 6):
            value = int.from_bytes(data[position + field : position + field + 4], "big")
            if field == 6 and value == 0:
                continue  # Missing decoding timestamps stay absent.
            transformed = round(value * alignment["scale"] + alignment["offset_seconds"] * 90000)
            if not 0 <= transformed <= min(0xFFFFFFFF, (duration + 5) * 90000):
                raise ValueError("Aligned PGS timestamps extend outside the movie")
            data[position + field : position + field + 4] = transformed.to_bytes(4, "big")
        position += 13 + size
    output.write_bytes(data)
    validate_subtitle(output, ".sup")
