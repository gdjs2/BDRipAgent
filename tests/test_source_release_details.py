"""Release information belongs to the source, while generation uses a job snapshot."""

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, select

from backend.app import queue
from backend.app.release_defaults import seed_existing_release_details
from backend.app.track_choices import source_key
from shared.config import get_settings
from shared.db import engine, session
from shared.models import MovieJob, SourceReleaseDetails, Task, now
from tests.test_source_sharing import DETAILS, peer


def save(client, job, details=DETAILS, revision=None):
    return client.patch(
        f"/api/jobs/{job['id']}/release",
        json={**details, **({"shared_revision": revision} if revision is not None else {})},
    )


def test_source_information_survives_removing_every_job_and_new_sessions(client, new_job):
    other = peer(client)
    saved = save(client, new_job, revision=0)
    assert saved.status_code == 200, saved.text
    assert saved.json()["analysis"]["shared_release_details"]["revision"] == 1
    with session() as db:
        key = source_key(db.get(MovieJob, new_job["id"]))
        record = db.get(SourceReleaseDetails, key)
        assert record.data["details"] == DETAILS
    for job in (new_job, other):
        response = client.delete(f"/api/jobs/{job['id']}?confirm={job['id']}")
        assert response.status_code == 200, response.text
    engine().dispose()
    future = peer(client)
    assert future["analysis"]["release_details"] == DETAILS
    assert future["analysis"]["shared_release_details"]["source_job_available"] is False
    with session() as db:
        assert db.get(SourceReleaseDetails, key).data["details"] == DETAILS
        assert "release_details" not in db.get(MovieJob, future["id"]).analysis


def test_revisions_prevent_stale_tab_overwrites_and_noop_saves_keep_revision(client, new_job):
    other = peer(client)
    assert save(client, new_job, revision=0).status_code == 200
    changed = {**DETAILS, "chinese_name": "新片名", "movie_description": "[b]New shared synopsis[/b]"}
    second = save(client, other, changed, revision=1)
    assert second.status_code == 200
    assert second.json()["analysis"]["shared_release_details"]["revision"] == 2
    stale = save(client, new_job, revision=1)
    assert stale.status_code == 409 and "another encoding" in stale.text
    current = client.get(f"/api/jobs/{new_job['id']}").json()
    assert current["analysis"]["release_details"] == changed
    with session() as db:
        # A sibling save does not rewrite this job's prior snapshot.
        assert db.get(MovieJob, new_job["id"]).analysis["release_details"] == DETAILS
    same = save(client, new_job, changed, revision=2)
    assert same.status_code == 200
    assert same.json()["analysis"]["shared_release_details"]["revision"] == 2
    with session() as db:
        assert "shared_revision" not in db.get(MovieJob, new_job["id"]).analysis["release_details"]


def test_sibling_save_preserves_active_generation_and_existing_release_files(client, new_job):
    other = peer(client)
    assert save(client, new_job).status_code == 200
    with session() as db:
        job = db.get(MovieJob, new_job["id"])
        job.state = "GENERATING_RELEASE"
        job.analysis = {**job.analysis, "release_result": {"infohash": "existing-output"}}
        for task in db.scalars(select(Task).where(Task.job_id == job.id)):
            task.status = "SUCCEEDED"
        db.add(Task(job_id=job.id, type="generate_release", stage=job.state, status="RUNNING"))
        db.commit()
    changed = {**DETAILS, "extra_description": "Updated for the source"}
    assert save(client, other, changed, revision=1).status_code == 200
    assert save(client, new_job, changed, revision=2).status_code == 409
    value = client.get(f"/api/jobs/{new_job['id']}").json()["analysis"]
    assert value["release_details"] == changed
    assert value["shared_release_details"]["snapshot_differs"] is True
    assert value["release_result"] == {"infohash": "existing-output"}
    with session() as db:
        assert db.get(MovieJob, new_job["id"]).analysis["release_details"] == DETAILS
        assert db.scalar(select(Task).where(Task.type == "generate_release")).status == "RUNNING"


def test_startup_backfills_newest_valid_legacy_release_and_never_overwrites_shared_record(client, new_job):
    older, newer, invalid = new_job, peer(client), peer(client)
    latest = {**DETAILS, "movie_description": "Persist even after the original job was removed."}
    with session() as db:
        for job, timestamp, details in (
            (older, "2026-09-20T12:00:00+00:00", DETAILS),
            (newer, "2026-09-21T12:00:00+00:00", latest),
            (invalid, "2026-09-22T12:00:00+00:00", {"chinese_name": "Incomplete draft"}),
        ):
            row = db.get(MovieJob, job["id"])
            row.analysis = {**row.analysis, "release_details": details, "release_details_saved_at": timestamp}
        db.get(MovieJob, newer["id"]).deleted_at = now()
        db.commit()
        queue.settings(db, lock=True)
        assert seed_existing_release_details(db) == 1
        db.commit()
        assert seed_existing_release_details(db) == 0
        record = db.get(SourceReleaseDetails, source_key(db.get(MovieJob, older["id"])))
        assert record.revision == 1 and record.data["details"] == latest
        # Backfills on later starts cannot supersede an existing source record.
        row = db.get(MovieJob, older["id"])
        row.analysis = {**row.analysis, "release_details_saved_at": "2030-01-01T00:00:00+00:00"}
        db.commit()
        assert seed_existing_release_details(db) == 0
        assert record.data["details"] == latest
    assert client.get(f"/api/jobs/{older['id']}/analysis").json()["release_details"] == latest


@pytest.mark.parametrize("revision", [-1, "1", True])
def test_release_revision_is_validated(client, new_job, revision):
    assert save(client, new_job, revision=revision).status_code == 422


def test_source_release_table_migration_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/release-upgrade.sqlite")
    get_settings.cache_clear()
    engine.cache_clear()
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    try:
        command.upgrade(config, "0008")
        assert "source_release_details" not in inspect(engine()).get_table_names()
        command.upgrade(config, "head")
        assert "source_release_details" in inspect(engine()).get_table_names()
        assert inspect(engine()).get_foreign_keys("source_release_details") == []
        command.check(config)
        command.downgrade(config, "0008")
        assert "source_release_details" not in inspect(engine()).get_table_names()
        command.upgrade(config, "head")
        command.check(config)
    finally:
        engine().dispose()
        engine.cache_clear()
        get_settings.cache_clear()
