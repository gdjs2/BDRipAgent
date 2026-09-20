"""Incremental MKVToolNix console progress for the shared task event stream."""

import re


class MKVToolNixProgress:
    def __init__(self, tool, phase, *, progress_span=100):
        self.tool = tool
        self.phase = phase
        self.progress_span = progress_span
        self.pending = ""
        self.last = None

    def __call__(self, chunk):
        # Log reads can split a progress record anywhere, including within a
        # percentage. Accept GUI records and the older carriage-return format.
        self.pending += chunk
        latest = None
        consumed = 0
        for match in re.finditer(
            r"(?:^|[\r\n])[ \t\ufeff]*(?:#GUI#progress[ \t]+|Progress:[ \t]*)"
            r"([0-9]+(?:\.[0-9]+)?)[ \t]*%",
            self.pending,
        ):
            consumed = match.end()
            value = float(match[1])
            if 0 <= value <= 100:
                latest = value
        # Keep only the unfinished record; bound memory for unrelated tool logs.
        self.pending = self.pending[consumed:][-256:]
        if latest is None or latest == self.last:
            return None
        self.last = latest
        return {
            # Tool completion still needs artifact and output validation.
            "percentage": min(99, latest * self.progress_span / 100),
            "tool": self.tool,
            "tool_percentage": latest,
            "phase": self.phase,
        }
