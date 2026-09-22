"""Weighted work units shared by sequential and concurrent task steps.

Percentages describe completed work, never guessed elapsed time. Unknown-duration
operations explicitly request an activity indicator while retaining completed work.
"""

import threading
import time
from dataclasses import dataclass, field


@dataclass
class _Step:
    weight: float = 1
    value: float = 0
    children: list = field(default_factory=list)

    def fraction(self):
        if self.value == 1 or not self.children:
            return self.value
        return sum(child.weight * child.fraction() for child in self.children) / sum(
            child.weight for child in self.children
        )


class ProgressScope:
    def __init__(self, ctx, step=None, root=None, lock=None):
        self._ctx = ctx
        self._step = step or _Step()
        self._root = root or self._step
        self._lock = lock or threading.RLock()

    def __getattr__(self, name):
        return getattr(self._ctx, name)

    def split(self, **weights):
        children = {key: _Step(weight=value) for key, value in weights.items() if value > 0}
        if not children:
            raise ValueError("Progress plan needs at least one positive weight")
        with self._lock:
            self._step.children = list(children.values())
        return {
            key: ProgressScope(self._ctx, child, self._root, self._lock) for key, child in children.items()
        }

    def progress(self, value, **detail):
        with self._lock:
            if value is not None:
                self._step.value = max(self._step.value, min(1, max(0, value / 100)))
            detail.setdefault("indeterminate", value is None)
            detail.setdefault("phase_percentage", value)
            detail["progress_basis"] = "work_units"
            # Serialize parallel reports through publication, not just calculation.
            self._ctx.progress(min(99.9, self._root.fraction() * 100), **detail)

    def done(self, phase="Step complete", **detail):
        self.progress(100, phase=phase, **detail)

    def branch(self):
        return ProgressScope(self._ctx.branch(), self._step, self._root, self._lock)

    def run(self, command, *, progress_parser=None, progress_reader=None, **kwargs):
        started = time.monotonic()

        def wrap(callback):
            def report(*args):
                update = callback(*args)
                if update:
                    detail = dict(update)
                    value = detail.pop("percentage")
                    detail.setdefault("elapsed_seconds", time.monotonic() - started)
                    self.progress(value, **detail)
                # Publication is already done under the shared lock.
                return None

            return report

        return self._ctx.run(
            command,
            **kwargs,
            **({"progress_parser": wrap(progress_parser)} if progress_parser else {}),
            **({"progress_reader": wrap(progress_reader)} if progress_reader else {}),
        )


def plan(ctx, **weights):
    scope = ctx if isinstance(ctx, ProgressScope) else ProgressScope(ctx)
    return scope.split(**weights)
