import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from backend.app.track_choices import source_key
from shared.db import session
from shared.models import Artifact, MovieJob, SourceTrackChoices, Task
from worker.adapters.source_tracks import source_key as extraction_key
from worker.job_cleanup import cleanup_deleted_jobs, remove_owned


def file(path, data=b"generated"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def remove(client, job_id):
    response = client.delete(f"/api/jobs/{job_id}?confirm={job_id}")
    assert response.status_code == 200, response.text
    assert response.json()["file_cleanup"] == "pending"


def test_delete_cleans_all_owned_storage_and_preserves_source(client, new_job, environment):
    job_id = new_job["id"]
    source = environment.source_root / "Movie.mkv"
    original = source.read_bytes()
    workspace = environment.workspace_root / job_id
    file(workspace / "encode/video.mkv")
    (workspace / "source-link.mkv").symlink_to(source)
    file(environment.completed_root / job_id / "movie.mkv")
    file(environment.cache_root / "agent" / job_id / "frames/test.png")
    bundle = "20260101-000000 [ART] Movie-WiKi"
    partial = "20260101-000001 [ART] Movie-WiKi"
    file(environment.artifacts_root / bundle / "Movie-WiKi/movie.mkv")
    file(environment.artifacts_root / partial / "partial.tmp")
    file(
        environment.artifacts_root / ".owners" / f"{job_id}.json",
        json.dumps(
            {
                "job_id": job_id,
                "bundle": partial,
                "bundles": [bundle, partial],
            }
        ).encode(),
    )
    untouched = file(environment.artifacts_root / "unrelated/user-file.txt")
    model = file(environment.cache_root / "speech-models/model.bin")
    with session() as db:
        task = db.scalar(select(Task).where(Task.job_id == job_id))
        db.add(
            Artifact(
                job_id=job_id,
                task_id=task.id,
                artifact_type="RELEASE_MEDIA",
                storage="artifacts",
                path=f"{bundle}/Movie-WiKi/movie.mkv",
                size=9,
            )
        )
        db.commit()
    remove(client, job_id)
    cleanup_deleted_jobs()
    cleanup_deleted_jobs()  # Recovery is idempotent.
    assert source.read_bytes() == original
    assert untouched.exists() and model.exists()
    assert not workspace.exists()
    assert not (environment.completed_root / job_id).exists()
    assert not (environment.cache_root / "agent" / job_id).exists()
    assert not (environment.artifacts_root / bundle).exists()
    assert not (environment.artifacts_root / partial).exists()
    with session() as db:
        assert db.get(MovieJob, job_id).analysis["file_cleanup"]["status"] == "complete"
        assert not db.scalar(select(Artifact).where(Artifact.job_id == job_id))


def test_shared_files_are_removed_with_last_source_job(client, new_job, environment):
    sibling = client.post(
        "/api/jobs",
        json={"source_path": "Movie.mkv", "title": "Movie", "year": 2026, "analysis_profile": "x264-live"},
    ).json()
    with session() as db:
        job = db.get(MovieJob, new_job["id"])
        logical = source_key(job)
        db.add(SourceTrackChoices(source_key=logical, revision=1, data={"uploads": []}))
        db.commit()
    physical = extraction_key(
        SimpleNamespace(
            source=lambda: environment.source_root / "Movie.mkv",
            workspace=environment.workspace_root / new_job["id"],
        )
    )
    shared = [
        file(environment.workspace_root / "uploaded-subtitles" / logical / "upload/subtitle.sup"),
        file(environment.cache_root / "subtitle-discovery" / logical / ".lock"),
        file(environment.cache_root / "source-tracks" / physical / "track.sup"),
        file(environment.cache_root / "track-analysis" / physical / "analysis.json"),
    ]
    remove(client, new_job["id"])
    cleanup_deleted_jobs()
    assert all(path.exists() for path in shared)
    assert (environment.workspace_root / sibling["id"]).exists()
    remove(client, sibling["id"])
    # Saved identity from the first deleted sibling still cleans the cache if the source was moved.
    (environment.source_root / "Movie.mkv").rename(environment.source_root / "Moved.mkv")
    cleanup_deleted_jobs()
    assert not any(path.exists() for path in shared)
    assert (environment.source_root / "Moved.mkv").exists()
    with session() as db:
        assert db.get(SourceTrackChoices, logical) is None


def test_cleanup_failure_is_persisted_and_retried(client, new_job, environment, monkeypatch):
    from worker import job_cleanup

    original = job_cleanup.remove_owned
    monkeypatch.setattr(
        job_cleanup, "remove_owned", lambda *a, **kw: (_ for _ in ()).throw(PermissionError("denied"))
    )
    remove(client, new_job["id"])
    cleanup_deleted_jobs()
    with session() as db:
        status = db.get(MovieJob, new_job["id"]).analysis["file_cleanup"]
        assert status["status"] == "pending" and "denied" in status["error"]
    monkeypatch.setattr(job_cleanup, "remove_owned", original)
    cleanup_deleted_jobs()
    assert not (environment.workspace_root / new_job["id"]).exists()


def test_cleanup_never_follows_symlink_or_traversal(tmp_path):
    source = file(tmp_path / "source/movie.mkv")
    root = tmp_path / "work"
    root.mkdir()
    (root / "link").symlink_to(source.parent, target_is_directory=True)
    with pytest.raises(ValueError):
        remove_owned(root, "link/movie.mkv", {source.parent})
    with pytest.raises(ValueError):
        remove_owned(root, "../source/movie.mkv", {source.parent})
    with pytest.raises(ValueError):
        remove_owned(tmp_path, "source", {source.parent})
    remove_owned(root, "link", {source.parent})
    assert source.exists() and not (root / "link").is_symlink()


def test_cleanup_respects_live_artifact_reference(client, new_job, environment):
    sibling = client.post(
        "/api/jobs", json={"source_path": "Movie.mkv", "title": "Movie", "year": 2026}
    ).json()
    relative = "20260101-000000 [ART] Shared-WiKi/video.mkv"
    media = file(environment.artifacts_root / relative)
    with session() as db:
        for job_id in (new_job["id"], sibling["id"]):
            task = db.scalar(select(Task).where(Task.job_id == job_id))
            db.add(
                Artifact(
                    job_id=job_id,
                    task_id=task.id,
                    artifact_type="RELEASE_MEDIA",
                    storage="artifacts",
                    path=relative,
                    size=9,
                )
            )
        db.commit()
    remove(client, new_job["id"])
    cleanup_deleted_jobs()
    assert media.exists()
    remove(client, sibling["id"])
    cleanup_deleted_jobs()
    assert not media.exists()
