from datetime import timedelta

import pytest
from sqlalchemy import select

from shared.db import session
from shared.models import Artifact, MovieJob, Task, now
from tests.test_job_cleanup import file
from worker.output_backups import cleanup_replaced_videos


@pytest.mark.parametrize("status", ["RUNNING", "FAILED", "CANCELLED", "SUCCEEDED"])
def test_encoded_video_expires_only_after_committed_success(client, new_job, environment, status):
    job_id = new_job["id"]
    old = file(environment.workspace_root / job_id / "encode/old/video.mkv", b"old video")
    new = file(environment.workspace_root / job_id / "encode/new/video.mkv", b"replacement")
    with session() as db:
        task = db.scalar(select(Task).where(Task.job_id == job_id))
        task.status = status
        job = db.get(MovieJob, job_id)
        job.analysis = {**job.analysis, "encoded_path": "encode/new/video.mkv"}
        for path in (old, new):
            db.add(
                Artifact(
                    job_id=job_id,
                    task_id=task.id,
                    artifact_type="ENCODED_VIDEO",
                    storage="workspace",
                    path=str(path.relative_to(environment.workspace_root / job_id)),
                    size=path.stat().st_size,
                )
            )
        db.commit()
    cleanup_replaced_videos()
    assert old.exists() == (status != "SUCCEEDED")
    assert new.read_bytes() == b"replacement"
    assert (environment.source_root / "Movie.mkv").exists()


def test_remux_removes_exported_video_links_while_next_stage_is_queued(client, new_job, environment):
    job_id = new_job["id"]
    old = file(environment.completed_root / job_id / "old/movie.mkv")
    new = file(environment.completed_root / job_id / "new/movie.mkv", b"new mux")
    encoded = file(environment.workspace_root / job_id / "encode/current/video.mkv", b"remux input")
    export = environment.artifacts_root / "20260101-000000 [ART] Movie-WiKi/Movie-WiKi/movie.mkv"
    export.parent.mkdir(parents=True)
    export.hardlink_to(old)
    staging = environment.completed_root / job_id / "releases/old/movie.mkv"
    staging.parent.mkdir(parents=True)
    staging.hardlink_to(old)
    with session() as db:
        task = db.scalar(select(Task).where(Task.job_id == job_id))
        task.status = "SUCCEEDED"
        older = Task(
            job_id=job_id,
            type="release",
            stage="RELEASING",
            status="SUCCEEDED",
            created_at=now() - timedelta(days=1),
        )
        db.add(older)
        db.flush()
        job = db.get(MovieJob, job_id)
        job.analysis = {
            **job.analysis,
            "final_path": str(new.relative_to(environment.completed_root)),
            "encoded_path": "encode/current/video.mkv",
        }
        db.add(Task(job_id=job_id, type="screenshots", stage="EXTRACTING_SCREENSHOTS", status="QUEUED"))
        for path, kind, storage in [
            (old, "FINAL_MKV", "completed"),
            (new, "FINAL_MKV", "completed"),
            (export, "RELEASE_MEDIA", "artifacts"),
        ]:
            root = environment.artifacts_root if storage == "artifacts" else environment.completed_root
            db.add(
                Artifact(
                    job_id=job_id,
                    task_id=task.id if path == new else older.id,
                    artifact_type=kind,
                    storage=storage,
                    path=str(path.relative_to(root)),
                    size=path.stat().st_size,
                    info={
                        "staging": {
                            "storage": "completed",
                            "path": str(staging.relative_to(environment.completed_root)),
                        }
                    }
                    if path == export
                    else {},
                )
            )
        db.commit()
    cleanup_replaced_videos()
    assert not old.exists() and not export.exists() and not staging.exists()
    assert new.read_bytes() == b"new mux" and encoded.read_bytes() == b"remux input"
