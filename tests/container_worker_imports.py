"""Exercise late screenshot imports in a real Celery prefork task, without services.

Mount this file at /app/container_worker_imports.py in the worker image and run it
with Python. --console-script reproduces the previous broken startup command.
"""

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from celery.signals import worker_ready

from worker.tasks import celery, ready

# This fixture must never dispatch application jobs or connect to real services.
worker_ready.disconnect(ready)
celery.conf.update(
    broker_url="memory://",
    worker_enable_remote_control=False,
    worker_hijack_root_logger=False,
)


@celery.task(name="fixture.late_screenshot_imports")
def late_imports():
    result = {"ok": False, "sys_path": sys.path, "pid": os.getpid()}
    try:
        # Deliberately import only after Celery's temporary startup path is gone.
        from agent.schemas import Selection
        from worker.pipeline.screenshots import generate, render, select_frames
        from worker.pipeline.stages import HANDLERS

        assert callable(generate) and callable(render) and callable(select_frames)
        assert {"generate_candidates", "select_screenshots", "render_screenshots"} <= HANDLERS.keys()
        assert Selection(selected=[]).selected == []
        result["ok"] = True
    except Exception as error:
        result["error"] = f"{type(error).__name__}: {error}"
    destination = Path(os.environ["IMPORT_TEST_RESULT"])
    pending = destination.with_suffix(".tmp")
    pending.write_text(json.dumps(result))
    pending.replace(destination)


@worker_ready.connect
def enqueue_fixture(**kwargs):
    late_imports.delay()


def main():
    with tempfile.TemporaryDirectory(prefix="worker-imports-") as temporary:
        root = Path(temporary)
        result_path = root / "result.json"
        command = (
            [str(Path(sys.executable).with_name("celery"))]
            if "--console-script" in sys.argv
            else [sys.executable, "-m", "celery"]
        )
        command += [
            "-A",
            "container_worker_imports:celery",
            "worker",
            "--pool=prefork",
            "--concurrency=1",
            "--loglevel=WARNING",
            "--without-gossip",
            "--without-mingle",
            "--without-heartbeat",
        ]
        env = {**os.environ, "IMPORT_TEST_RESULT": str(result_path)}
        env.pop("PYTHONPATH", None)  # Do not let a caller mask the startup bug.
        with (root / "worker.log").open("w+") as log:
            process = subprocess.Popen(command, cwd="/app", env=env, stdout=log, stderr=log)
            try:
                deadline = time.monotonic() + 25
                while not result_path.exists() and process.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.2)
                if not result_path.exists():
                    log.seek(0)
                    raise AssertionError("Import task did not finish:\n" + log.read()[-5000:])
                result = json.loads(result_path.read_text())
                assert result["pid"] != process.pid, "The test must run in a prefork child"
                assert result["ok"], result
                print(
                    "PASS: real Celery prefork task imports agent schema and screenshot stages after startup."
                )
            finally:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


if __name__ == "__main__":
    main()
