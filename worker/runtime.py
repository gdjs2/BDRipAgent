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
from shared.paths import contained, job_dir


class Interrupted(RuntimeError):
    pass


class ToolError(RuntimeError):
    def __init__(self, command, exit_code):
        super().__init__(f"{command[0]} exited with code {exit_code}; see persistent task log")
        self.exit_code = exit_code


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

    def run(
        self,
        command,
        *,
        output: Path | None = None,
        allowed=(0,),
        progress_parser=None,
        progress_reader=None,
        env: dict[str, str] | None = None,
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
                cursor = begin

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
                        detail.setdefault("elapsed_seconds", time.monotonic() - start)
                        self.progress(percentage, **detail)

                while process.poll() is None:
                    self.check()
                    if time.monotonic() - start > behavior()["command_timeout_seconds"]:
                        raise TimeoutError(f"{argv[0]} exceeded configured timeout")
                    publish_progress()
                    time.sleep(1)
                self.check()
                publish_progress()
                if process.returncode not in allowed:
                    raise ToolError(argv, process.returncode)
                if process.returncode:
                    self.log(f"Tool completed with warnings (exit {process.returncode})")
            finally:
                if process and process.poll() is None:
                    os.killpg(process.pid, signal.SIGTERM)
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait()
                if output:
                    stdout.close()
        if output:
            return output.read_text(errors="replace")
        with self.log_path.open("rb") as reader:
            reader.seek(max(begin, self.log_path.stat().st_size - 16 * 1024 * 1024))
            return reader.read().decode(errors="replace")

    def artifact(self, path, kind, *, info=None, storage="workspace"):
        self.check()
        root = self.settings.completed_root if storage == "completed" else self.workspace
        relative = str(path.relative_to(root))
        safe = contained(root, relative, exists=True)
        with session() as db:
            row = db.scalar(select(Artifact).where(Artifact.job_id == self.job.id, Artifact.path == relative))
            if not row:
                row = Artifact(job_id=self.job.id, path=relative, task_id=self.task_id)
                db.add(row)
            row.artifact_type, row.storage = kind, storage
            row.size, row.info = safe.stat().st_size, info or {}
            db.flush()
            event(db, self.job.id, "artifact_created", artifact_id=row.id, artifact_type=kind)
            db.commit()
        return relative
