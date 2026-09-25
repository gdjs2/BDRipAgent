from types import SimpleNamespace

import pytest

from backend.app.encode_summary import encode_sizes
from shared.db import session
from shared.models import MovieJob, Task


def test_current_encoded_file_ratio_and_no_source(environment):
    job = SimpleNamespace(id="job", source_size=1000, analysis={"encoded_path": "encode/current.mkv"})
    task = SimpleNamespace(id="current")
    root = environment.workspace_root / job.id / "encode"
    root.mkdir(parents=True)
    (root / "current.mkv").write_bytes(b"x" * 250)
    result = encode_sizes(job, task, [])
    assert result == {
        "encoded_bytes": 250,
        "source_bytes": 1000,
        "percent_of_source": 25.0,
        "encoding_skipped": False,
    }
    job.source_size = 0
    assert encode_sizes(job, task, [])["percent_of_source"] is None


def test_old_artifacts_not_used_for_new_encode(environment):
    job = SimpleNamespace(id="job", source_size=1000, analysis={})
    old = SimpleNamespace(task_id="old", artifact_type="ENCODED_VIDEO", path="old.mkv", size=500, info={})
    assert encode_sizes(job, SimpleNamespace(id="new"), [old])["encoded_bytes"] is None
    job.analysis = {"encoding_skipped": True, "encoded_path": "old.mkv"}
    assert encode_sizes(job, SimpleNamespace(id="old"), [old])["percent_of_source"] is None
    job.analysis = {}
    assert encode_sizes(job, SimpleNamespace(id="old"), [old])["encoded_bytes"] == 500


@pytest.mark.parametrize("status", ["SUCCEEDED", "RUNNING", "CANCELLED"])
def test_size_endpoint_available_without_encoder_log(client, new_job, environment, status):
    with session() as db:
        task = db.get(Task, new_job["tasks"][0]["id"])
        task.type, task.status = "encode", status
        job = db.get(MovieJob, new_job["id"])
        job.source_size = 1000
        job.analysis = {"encoded_path": "encoded.mkv"}
        (environment.workspace_root / job.id / "encoded.mkv").write_bytes(b"x" * 300)
        db.commit()
    response = client.get(f"/api/jobs/{new_job['id']}/encoder-info")
    assert response.status_code == (200 if status == "SUCCEEDED" else 409), response.text
    if status == "SUCCEEDED":
        assert response.json()["sizes"]["percent_of_source"] == 30
