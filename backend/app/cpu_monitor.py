"""Sample Linux host CPU counters once, independently of browser polling."""

import asyncio
import math
import threading
import time
from datetime import UTC, datetime
from pathlib import Path


def read_counters(text):
    cpus = {}
    for line in text.splitlines():
        fields = line.split()
        if not fields or not (fields[0] == "cpu" or fields[0].startswith("cpu") and fields[0][3:].isdigit()):
            continue
        # guest/guest_nice are already counted in user/nice; never add them twice.
        counters = tuple(int(value) for value in fields[1:9])
        if len(counters) < 4 or any(value < 0 for value in counters):
            raise ValueError("Invalid CPU counters")
        cpus[fields[0]] = counters + (0,) * (8 - len(counters))
    if "cpu" not in cpus:
        raise ValueError("Missing CPU counters")
    return cpus


def utilization(previous, current):
    if previous is None:
        return None
    delta = [new - old for old, new in zip(previous, current, strict=True)]
    if any(value < 0 for value in delta):
        return None  # A reset/hotplug needs a fresh baseline, not a false spike.
    total = sum(delta)
    if not total:
        return None
    busy = total - delta[3] - delta[4]  # Idle and I/O wait are not CPU execution.
    return round(max(0, min(100, busy * 100 / total)), 1)


def read_load_average(text):
    values = [float(value) for value in text.split()[:3]]
    if len(values) != 3 or any(not math.isfinite(value) or value < 0 for value in values):
        raise ValueError("Invalid system load averages")
    return dict(zip(("one_minute", "five_minutes", "fifteen_minutes"), values, strict=True))


def read_frequency_mhz(text):
    values = []
    for line in text.splitlines():
        key, separator, value = line.partition(":")
        if separator and key.strip().lower() == "cpu mhz":
            mhz = float(value.strip())
            if not math.isfinite(mhz) or mhz <= 0:
                raise ValueError("Invalid CPU frequency")
            values.append(mhz)
    return round(sum(values) / len(values), 1) if values else None


class CpuMonitor:
    def __init__(self, path: Path, loadavg_path: Path | None = None, cpuinfo_path: Path | None = None):
        self.path = path
        self.loadavg_path = loadavg_path or path.with_name("loadavg")
        self.cpuinfo_path = cpuinfo_path or path.with_name("cpuinfo")
        self._previous = {}
        self._clock = None
        self._lock = threading.Lock()
        self._snapshot = {
            "available": False,
            "percent": None,
            "cores": [],
            "sampled_at": None,
            "interval_seconds": None,
            "logical_cores": 0,
            "load_average": None,
            "frequency_mhz": None,
        }

    def sample(self):
        sampled_at = datetime.now(UTC).isoformat()
        try:
            load_average = read_load_average(self.loadavg_path.read_text())
        except (OSError, ValueError):
            load_average = None
        try:
            frequency_mhz = read_frequency_mhz(self.cpuinfo_path.read_text())
        except (OSError, ValueError):
            frequency_mhz = None
        try:
            counters = read_counters(self.path.read_text())
        except (OSError, ValueError):
            self._previous = {}
            self._clock = None
            with self._lock:
                self._snapshot = {
                    **self._snapshot,
                    "available": False,
                    "percent": None,
                    "cores": [],
                    "load_average": load_average,
                    "sampled_at": sampled_at,
                    "frequency_mhz": frequency_mhz,
                }
            return
        current = time.monotonic()
        cores = [
            {"id": int(key[3:]), "percent": utilization(self._previous.get(key), value)}
            for key, value in counters.items()
            if key != "cpu"
        ]
        value = {
            "available": True,
            "percent": utilization(self._previous.get("cpu"), counters["cpu"]),
            "cores": sorted(cores, key=lambda core: core["id"]),
            "logical_cores": len(cores),
            "sampled_at": sampled_at,
            "load_average": load_average,
            "frequency_mhz": frequency_mhz,
            "interval_seconds": round(current - self._clock, 3) if self._clock is not None else None,
        }
        self._previous, self._clock = counters, current
        with self._lock:
            self._snapshot = value

    def snapshot(self):
        with self._lock:
            return {**self._snapshot, "scope": "host"}

    async def run(self):
        while True:
            await asyncio.sleep(1)
            await asyncio.to_thread(self.sample)
