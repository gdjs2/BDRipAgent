"""One fair, cancellable queue shared by every agent operation in this service."""

import threading
import time
from collections import deque
from contextlib import contextmanager


class AgentQueue:
    def __init__(self):
        self._condition = threading.Condition()
        self._waiting = deque()
        self._active = None

    def enqueue(self):
        ticket = object()
        with self._condition:
            self._waiting.append(ticket)
            self._condition.notify_all()
        return ticket

    def discard(self, ticket):
        with self._condition:
            if self._active is ticket:
                self._active = None
            if ticket in self._waiting:
                self._waiting.remove(ticket)
            self._condition.notify_all()

    def snapshot(self):
        with self._condition:
            return {"running": int(self._active is not None), "waiting": len(self._waiting)}

    @contextmanager
    def slot(self, ticket, *, timeout, check=lambda: None, on_position=lambda position: None):
        deadline = time.monotonic() + timeout
        previous = None
        try:
            while True:
                check()
                with self._condition:
                    if self._active is None and self._waiting[0] is ticket:
                        self._waiting.popleft()
                        self._active = ticket
                        break
                    if time.monotonic() >= deadline:
                        raise TimeoutError(
                            f"Agent queue wait exceeded {timeout:g} seconds. Retry this task; prepared inputs are retained."
                        )
                    position = self._waiting.index(ticket) + 1
                if position != previous:
                    on_position(position)
                    previous = position
                with self._condition:
                    self._condition.wait(timeout=min(0.2, max(0, deadline - time.monotonic())))
            check()
            on_position(0)
            yield
        finally:
            self.discard(ticket)
