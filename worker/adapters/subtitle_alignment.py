"""Inspect text subtitles and verify agent cue matches before changing timestamps."""

import re

import numpy as np
import pysubs2

from backend.app.subtitle_uploads import validate_subtitle
from shared.subtitle_alignment import fit_alignment  # noqa: F401


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
