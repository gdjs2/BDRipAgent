import json
import os
import signal
import subprocess
import threading
import time
from pathlib import Path

from sqlalchemy import select

from backend.app.services import event
from shared.config import behavior, get_settings
from shared.db import session
from shared.models import Artifact, MovieJob, Task, now
from shared.paths import artifact_root, contained, job_dir


class Interrupted(RuntimeError):
    pass


class ToolError(RuntimeError):
    def __init__(self, command, exit_code):
        super().__init__(f"{command[0]} exited with code {exit_code}; see persistent task log")
        self.exit_code = exit_code


class EncodingPause:
    """Worker-owned process signals; API requests never operate on persisted PIDs."""

    def __init__(self, ctx):
        self.ctx = ctx
        self.started = None
        self.total = 0.0

    @property
    def seconds(self):
        return self.total + (time.monotonic() - self.started if self.started is not None else 0)

    def checked_task(self, db):
        task = db.scalar(select(Task).where(Task.id == self.ctx.task_id).with_for_update())
        if not task or task.status != "RUNNING" or task.run_token != self.ctx.token or task.cancel_requested:
            self.ctx.lost.set()
            self.ctx.check()
        if task.type != "encode":
            raise ValueError("Only final encoding commands support pause")
        return task

    def enable(self):
        with session() as db:
            task = self.checked_task(db)
            task.can_pause = True
            event(db, task.job_id, "task_pause_available", task_id=task.id)
            db.commit()

    def disable(self):
        with session() as db:
            task = db.scalar(select(Task).where(Task.id == self.ctx.task_id).with_for_update())
            if task and task.run_token == self.ctx.token:
                task.can_pause, task.pause_requested, task.paused_at = False, False, None
                event(db, task.job_id, "task_pause_unavailable", task_id=task.id)
                db.commit()

    def sync(self, process):
        with session() as db:
            task = self.checked_task(db)
            pause = task.pause_requested
            if pause == (self.started is not None):
                return
            try:
                os.killpg(process.pid, signal.SIGSTOP if pause else signal.SIGCONT)
            except ProcessLookupError:
                return  # Encoder finished concurrently; command cleanup clears the controls.
            if pause:
                self.started = time.monotonic()
                task.paused_at = now()
            else:
                self.total += time.monotonic() - self.started
                self.started = None
                task.paused_at = None
            event(db, task.job_id, "task_paused" if pause else "task_resumed", task_id=task.id)
            db.commit()
        self.ctx.log("Encoding paused; progress is retained." if pause else "Encoding resumed.")


class TaskContext:
    def __init__(self, task_id, token):
        self.task_id, self.token = task_id, token
        self.settings = get_settings()
        self.stopped = threading.Event()
        self.lost = threading.Event()
        with session() as db:
            task = db.get(Task, task_id)
            self.job = db.get(MovieJob, task.job_id)
            self.workspace = job_dir(self.settings.workspace_root, task.job_id)
            self.log_path = contained(self.workspace, task.log_path)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_path.touch()
        self.thread = threading.Thread(target=self._heartbeat, daemon=True)
        self.thread.start()

    def _heartbeat(self):
        while not self.stopped.wait(5):
            try:
                with session() as db:
                    task = db.scalar(select(Task).where(Task.id == self.task_id).with_for_update())
                    if task.status != "RUNNING" or task.run_token != self.token or task.cancel_requested:
                        self.lost.set()
                        return
                    task.heartbeat_at = now()
                    db.commit()
            except Exception:
                # Fail closed on loss of database authority, including network partitions.
                self.lost.set()
                return

    def check(self):
        if self.lost.is_set():
            raise Interrupted("Task cancelled or worker lease lost")

    def close(self):
        self.stopped.set()
        self.thread.join(timeout=6)

    def output(self, category, filename):
        result = contained(self.workspace, f"{category}/{self.task_id}/{filename}")
        result.parent.mkdir(parents=True, exist_ok=True)
        return result

    def source(self):
        path = contained(self.settings.source_root, self.job.source_path, exists=True)
        stat = path.stat()
        if stat.st_size != self.job.source_size or str(stat.st_mtime_ns) != self.job.source_mtime_ns:
            raise RuntimeError("Immutable source changed since job creation")
        return path

    def log(self, message):
        with self.log_path.open("a") as stream:
            stream.write(message + "\n")

    def progress(self, value, **detail):
        self.check()
        with session() as db:
            task = db.scalar(select(Task).where(Task.id == self.task_id).with_for_update())
            if task.status != "RUNNING" or task.run_token != self.token or task.cancel_requested:
                self.lost.set()
                self.check()
            task.progress = max(0, min(100, value))
            task.progress_detail = detail
            task.heartbeat_at = now()
            event(db, task.job_id, "task_progress", task_id=task.id, progress=task.progress, **detail)
            db.commit()

    def agent_event(self, data):
        self.check()
        with session() as db:
            task = db.scalar(select(Task).where(Task.id == self.task_id).with_for_update())
            if task.status != "RUNNING" or task.run_token != self.token or task.cancel_requested:
                self.lost.set()
                self.check()
            event(db, task.job_id, "agent_output", task_id=task.id, **data)
            db.commit()

    def run(
        self,
        command,
        *,
        output: Path | None = None,
        allowed=(0,),
        progress_parser=None,
        progress_reader=None,
        env: dict[str, str] | None = None,
        pausable: bool = False,
    ):
        self.check()
        argv = [str(x) for x in command]
        with session() as db:
            task = db.get(Task, self.task_id)
            task.command_json = [*task.command_json, argv]
            db.commit()
        self.log("$ " + json.dumps(argv))
        start = time.monotonic()
        with self.log_path.open("ab", buffering=0) as log:
            begin = log.tell()
            stdout = output.open("wb") if output else log
            process = None
            pause = EncodingPause(self) if pausable else None
            try:
                process = subprocess.Popen(
                    argv,
                    stdout=stdout,
                    stderr=log,
                    stdin=subprocess.DEVNULL,
                    start_new_session=True,
                    cwd=self.workspace,
                    env={**os.environ, **env} if env is not None else None,
                )
                if pause:
                    pause.enable()
                cursor = begin

                def active_seconds():
                    return time.monotonic() - start - (pause.seconds if pause else 0)

                def publish_progress():
                    nonlocal cursor
                    update = None
                    if progress_parser:
                        with self.log_path.open("rb") as reader:
                            reader.seek(cursor)
                            chunk = reader.read()
                            cursor = reader.tell()
                        update = progress_parser(chunk.decode(errors="replace"))
                    if progress_reader:
                        update = progress_reader()
                    if update:
                        detail = dict(update)
                        percentage = detail.pop("percentage")
                        detail.setdefault("elapsed_seconds", active_seconds())
                        self.progress(percentage, **detail)

                while process.poll() is None:
                    self.check()
                    if pause:
                        pause.sync(process)
                    if active_seconds() > behavior()["command_timeout_seconds"]:
                        raise TimeoutError(f"{argv[0]} exceeded configured timeout")
                    if not pause or pause.started is None:
                        publish_progress()
                    time.sleep(1)
                self.check()
                publish_progress()
                if process.returncode not in allowed:
                    raise ToolError(argv, process.returncode)
                if process.returncode:
                    self.log(f"Tool completed with warnings (exit {process.returncode})")
            finally:
                try:
                    if process and process.poll() is None:
                        try:
                            # SIGTERM is pending while stopped; SIGCONT lets it take effect.
                            os.killpg(process.pid, signal.SIGTERM)
                            os.killpg(process.pid, signal.SIGCONT)
                        except ProcessLookupError:
                            pass
                        try:
                            process.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            os.killpg(process.pid, signal.SIGKILL)
                            process.wait()
                finally:
                    if output:
                        stdout.close()
                    if pause:
                        pause.disable()
        if output:
            return output.read_text(errors="replace")
        with self.log_path.open("rb") as reader:
            reader.seek(max(begin, self.log_path.stat().st_size - 16 * 1024 * 1024))
            return reader.read().decode(errors="replace")

    def artifact(self, path, kind, *, info=None, storage="workspace"):
        self.check()
        root = artifact_root(self.settings, self.job.id, storage)
        relative = str(path.relative_to(root))
        safe = contained(root, relative, exists=True)
        with session() as db:
            row = db.scalar(select(Artifact).where(Artifact.job_id == self.job.id, Artifact.path == relative))
            if not row:
                row = Artifact(job_id=self.job.id, path=relative, task_id=self.task_id)
                db.add(row)
            row.task_id = self.task_id
            row.artifact_type, row.storage = kind, storage
            row.size, row.info = safe.stat().st_size, info or {}
            db.flush()
            event(db, self.job.id, "artifact_created", artifact_id=row.id, artifact_type=kind)
            db.commit()
        return relative
