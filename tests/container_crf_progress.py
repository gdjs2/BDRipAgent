"""Run in the worker image with an isolated SQLite database and read-only source.

Uses both actual encoder profiles; only sample count/duration are reduced.
Never point DATABASE_URL or WORKSPACE_ROOT at production storage.
"""

import copy
import json
import os
from pathlib import Path

from sqlalchemy import select

from shared.config import behavior, get_settings, profiles
from shared.db import Base, engine, session
from shared.models import Event, MovieJob, Task, now
from worker.adapters import integrations
from worker.adapters.handbrake import Crop
from worker.runtime import TaskContext


def main():
    settings = get_settings()
    assert settings.database_url.startswith("sqlite:////tmp/"), "Use a disposable test database"
    assert str(settings.workspace_root).startswith("/tmp/"), "Use a disposable workspace"
    settings.workspace_root.mkdir(parents=True, exist_ok=True)
    Base.metadata.create_all(engine())
    config = copy.deepcopy(behavior())
    config["integrations"]["crf_studio"].update(count=2, seconds=1)
    integrations.behavior = lambda: config
    source = settings.source_root / os.environ["TEST_SOURCE"]
    stat = source.stat()
    for codec in ("x264", "x265"):
        profile = profiles()[f"{codec}-live"]
        with session() as db:
            job = MovieJob(
                source_path=source.name,
                source_size=stat.st_size,
                source_mtime_ns=str(stat.st_mtime_ns),
                title="CRF progress smoke test",
                release_name="CRF.Progress.Smoke.Test",
                analysis_profile=f"{codec}-live",
                state="RUNNING_CRF_ANALYSIS",
                analysis={"video": {"width": 1920, "height": 1080}},
            )
            db.add(job)
            db.flush()
            task = Task(
                job_id=job.id,
                type="crf_analysis",
                stage=job.state,
                status="RUNNING",
                run_token="native-progress-test",
                started_at=now(),
                log_path=f"logs/{codec}.log",
            )
            db.add(task)
            db.commit()
            task_id, job_id = task.id, job.id
        ctx = TaskContext(task_id, "native-progress-test")
        try:
            result = integrations.CRFStudioAdapter().run_analysis(
                ctx, profile, Crop(top=104, bottom=104, left=0, right=0)
            )
            assert len(result["samples"]) == 2
            with session() as db:
                task = db.get(Task, task_id)
                assert task.progress == 99  # Success is committed by worker.execute, after result validation.
                assert task.progress_detail["completed"] == task.progress_detail["total"] == 4
                assert task.progress_detail["state"] == "complete"
                updates = [
                    e.data
                    for e in db.scalars(
                        select(Event)
                        .where(Event.job_id == job_id, Event.type == "task_progress")
                        .order_by(Event.id)
                    )
                ]
            assert any(0 < p["progress"] < 99 for p in updates), updates
            assert any(p.get("frames", 0) > 0 for p in updates), updates
            assert all(p["progress"] < 100 for p in updates)
            print(
                json.dumps(
                    {
                        "codec": codec,
                        "updates": len(updates),
                        "with_frames": sum(p.get("frames", 0) > 0 for p in updates),
                        "progress": [p["progress"] for p in updates],
                        "encodes_complete": 4,
                    }
                ),
                flush=True,
            )
            Path(f"/tmp/crf-progress-{codec}.json").write_text(json.dumps(updates, indent=2))
        finally:
            ctx.close()


if __name__ == "__main__":
    main()
