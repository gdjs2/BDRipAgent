import pytest
from fastapi.testclient import TestClient

from shared.config import get_settings
from shared.db import Base, engine, session


@pytest.fixture
def environment(tmp_path, monkeypatch):
    for key, folder in [
        ("SOURCE_ROOT", "incoming"),
        ("WORKSPACE_ROOT", "jobs"),
        ("COMPLETED_ROOT", "completed"),
        ("CACHE_ROOT", "cache"),
    ]:
        path = tmp_path / folder
        path.mkdir()
        monkeypatch.setenv(key, str(path))
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/test.sqlite")
    monkeypatch.setenv("API_TOKEN", "test-token-with-more-than-24-characters")
    monkeypatch.setenv("AGENT_TOKEN", "test-agent-token")
    get_settings.cache_clear()
    engine.cache_clear()
    import shared.models  # noqa: F401

    Base.metadata.create_all(engine())
    yield get_settings()
    engine().dispose()
    engine.cache_clear()
    get_settings.cache_clear()


@pytest.fixture
def client(environment):
    from backend.app.main import app

    with TestClient(app) as client:
        client.headers["Authorization"] = f"Bearer {environment.api_token}"
        yield client


@pytest.fixture
def new_job(client, environment):
    (environment.source_root / "Movie.mkv").write_bytes(b"fixture source")
    response = client.post(
        "/api/jobs",
        json={
            "source_path": "Movie.mkv",
            "title": "Movie",
            "year": 2026,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def gate(job_id, stage, tracks=None, profile=None):
    from sqlalchemy import select

    from shared.config import profiles
    from shared.models import CRFResult, MovieJob, MovieTrack, Task

    with session() as db:
        job = db.get(MovieJob, job_id)
        job.state = stage
        for task in db.scalars(select(Task).where(Task.job_id == job_id)):
            task.status = "SUCCEEDED"
        for track in tracks or []:
            db.add(MovieTrack(job_id=job_id, **track))
        if profile:
            db.add(CRFResult(job_id=job_id, data={"profile_snapshot": profiles()[profile]}))
        db.commit()
