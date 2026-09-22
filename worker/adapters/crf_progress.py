"""Consume the same atomic progress file as CRF Studio's desktop queue."""

import json
import math
from pathlib import Path


def normalize_progress(data):
    if not isinstance(data, dict):
        raise ValueError("CRF progress must be an object")
    detail = {}
    for key in ("state", "stage", "message", "codec"):
        if isinstance(data.get(key), str):
            detail[key] = data[key][:500]
    for key in ("completed", "total", "sample_index", "samples_per_endpoint", "frames"):
        value = data.get(key, 0)
        if type(value) is not int or value < 0:
            raise ValueError(f"Invalid CRF progress {key}")
        detail[key] = value
    for key in ("sample_fraction", "crf"):
        value = data.get(key, 0)
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            raise ValueError(f"Invalid CRF progress {key}")
        if key in data:
            detail[key] = value
    detail["sample_fraction"] = min(0.99, detail.get("sample_fraction", 0))
    detail["flushing"] = data.get("flushing") is True
    total = detail["total"]
    detail["completed"] = min(detail["completed"], total)
    # Match the desktop GUI: include submitted frames within the current sample,
    # but reserve 100% for successful process exit AND accepted analysis results.
    fraction = detail["sample_fraction"] if data.get("state") == "running" else 0
    percentage = min(99, int(100 * (detail["completed"] + fraction) / total)) if total else 0
    return {
        "percentage": percentage,
        **detail,
        "indeterminate": not total or detail["flushing"] or data.get("state") == "complete",
    }


class CRFProgressReader:
    def __init__(self, path: Path):
        self.path = path
        self.previous = None

    def __call__(self):
        try:
            with self.path.open() as stream:
                text = stream.read(65537)
            if len(text) > 65536 or text == self.previous:
                return None
            update = normalize_progress(json.loads(text))
        except (OSError, ValueError, OverflowError):
            # Missing/partial reports must not terminate an expensive analysis.
            return None
        self.previous = text
        return update
