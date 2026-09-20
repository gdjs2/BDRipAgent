import json

import pytest
from sqlalchemy import select

from shared.db import session
from shared.models import Artifact, MovieJob, Task
from shared.paths import contained
from tests.conftest import gate
from worker.pipeline.release_migration import migrate


def legacy(environment, new_job):
    gate(new_job["id"], "COMPLETE")
    torrents = environment.artifacts_root.parent / "torrents"
    torrents.mkdir()
    with session() as db:
        job = db.get(MovieJob, new_job["id"])
        items, ids = [], []
        for suffix, kind in [
            (".mkv", "MEDIA"),
            (".nfo", "NFO"),
            (".md5", "MD5"),
            (".torrent", "TORRENT"),
            (".bbcode.txt", "BBCODE"),
            (".encoder.txt", "ENCODER_INFO"),
        ]:
            storage = "torrents" if kind == "TORRENT" else "artifacts"
            path = (
                (job.release_name + "/" if kind in ("MEDIA", "NFO", "MD5") else "")
                + job.release_name
                + suffix
            )
            target = contained(torrents if kind == "TORRENT" else environment.artifacts_root, path)
            target.parent.mkdir(exist_ok=True)
            target.write_text(kind)
            item = {"path": path, "kind": "RELEASE_" + kind, "storage": storage}
            row = Artifact(
                job_id=job.id,
                task_id=new_job["tasks"][0]["id"],
                artifact_type=item["kind"],
                path=path,
                storage=storage,
                size=len(kind),
            )
            db.add(row)
            db.flush()
            ids.append(row.id)
            items.append(item)
        job.analysis = {
            "release_result": {
                "package_storage": "artifacts",
                "package_path": job.release_name,
                "artifacts": items,
                "infohash": "unchanged",
            }
        }
        db.commit()
    return torrents, ids


def test_migration_preserves_artifact_ids_payload_and_downloads(client, environment, new_job):
    torrents, ids = legacy(environment, new_job)
    with session() as db:
        job = db.get(MovieJob, new_job["id"])
        bundle = migrate(db, job, environment, torrents)
        assert migrate(db, job, environment, torrents) == bundle
        assert job.analysis["release_result"]["infohash"] == "unchanged"
        assert job.analysis["release_result"]["torrent_storage"] == "artifacts"
        assert {a.id for a in db.scalars(select(Artifact).where(Artifact.job_id == job.id))} == set(ids)
    assert not torrents.exists()
    assert not (environment.workspace_root / new_job["id"] / "release-layout-migration").exists()
    for item in ids:
        response = client.get(f"/api/artifacts/{item}")
        assert response.status_code == 200 and response.content
    assert len([p for p in environment.artifacts_root.iterdir() if not p.name.startswith(".")]) == 1
    owner = environment.artifacts_root / ".owners" / f"{new_job['id']}.json"
    assert "legacy_paths" not in json.loads(owner.read_text())


def test_migration_does_not_touch_active_jobs_or_unrelated_torrents(client, environment, new_job):
    torrents, _ = legacy(environment, new_job)
    unrelated = torrents / "user.torrent"
    unrelated.write_text("leave alone")
    with session() as db:
        task = db.get(Task, new_job["tasks"][0]["id"])
        task.status = "RUNNING"
        db.commit()
        job = db.get(MovieJob, new_job["id"])
        with pytest.raises(ValueError, match="active work"):
            migrate(db, job, environment, torrents)
        task.status = "SUCCEEDED"
        db.commit()
        migrate(db, job, environment, torrents)
    assert unrelated.read_text() == "leave alone"
    assert list(torrents.iterdir()) == [unrelated]
