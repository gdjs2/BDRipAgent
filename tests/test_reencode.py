import pytest
from sqlalchemy import select

from shared.db import session
from shared.models import EncodeConfig, MovieJob, Screenshot, Task
from tests.test_encode_target_edit import queued_encode as pending_encode

queued_encode = pending_encode


@pytest.fixture
def finished_encode(queued_encode, environment):
    job, task = queued_encode
    old = environment.workspace_root / job["id"] / "encode/video.mkv"
    old.parent.mkdir(exist_ok=True)
    old.write_bytes(b"old encoded video")
    with session() as db:
        row = db.get(MovieJob, job["id"])
        row.state = "COMPLETE"
        row.validation = {"valid": True}
        row.analysis = {
            "encoded_path": "encode/video.mkv",
            "encoded_video": {"codec": "hevc"},
            "encoder_average_qp": {"I": 20},
            "video": {"codec": "h264"},
            "release_details": {"chinese_name": "Saved"},
            "prepared_tracks": [],
            "release_result": {"task_id": "old-release"},
            "candidate_index": "old.json",
            "review_shortlisted_ids": [1],
            "screenshot_selection": {"count": 7},
        }
        db.get(Task, task["id"]).status = "SUCCEEDED"
        db.add(Screenshot(job_id=row.id, candidate_id=1, selected=True, info={}))
        db.commit()
    return job, task, old


@pytest.mark.parametrize(
    "target", [{"rate_control": "bitrate", "bitrate_kbps": 12000}, {"rate_control": "crf", "crf": 18}]
)
def test_reencode_retains_old_outputs_and_source_work_but_invalidates_derived_state(
    client, finished_encode, target
):
    job, old_task, path = finished_encode
    result = client.post(f"/api/jobs/{job['id']}/reencode", json=target)
    assert result.status_code == 202, result.text
    value = result.json()
    assert value["state"] == "ENCODING" and value["validation"] == {}
    assert value["analysis"]["release_details"]["chinese_name"] == "Saved"
    assert value["analysis"]["prepared_tracks"] == []
    assert path.read_bytes() == b"old encoded video"
    assert value["analysis"]["encode_revision"]["previous"]["encoded_path"] == "encode/video.mkv"
    for field in (
        "encoded_path",
        "encoded_video",
        "encoder_average_qp",
        "candidate_index",
        "review_shortlisted_ids",
        "release_result",
        "screenshot_selection",
    ):
        assert field not in value["analysis"]
    assert not value["analysis"]["remux_revision"]["reuse_screenshots"]
    assert next(t for t in value["tasks"] if t["id"] == old_task["id"])["status"] == "SUCCEEDED"
    queued = [t for t in value["tasks"] if t["status"] == "QUEUED"]
    assert len(queued) == 1 and queued[0]["type"] == "encode"
    assert all(value["encode_config"]["data"][k] == v for k, v in target.items())
    assert client.get(f"/api/jobs/{job['id']}/screenshots").json()["items"] == []
    assert client.post(f"/api/jobs/{job['id']}/reencode", json=target).status_code == 409


def test_reencode_cancels_queued_followup_but_never_running_work(client, finished_encode):
    job, _, _ = finished_encode
    with session() as db:
        row = Task(job_id=job["id"], type="validate", stage="VALIDATING_ENCODE", status="RUNNING")
        db.add(row)
        db.commit()
        ident = row.id
    url = f"/api/jobs/{job['id']}/reencode"
    assert client.post(url, json={"crf": 18}).status_code == 409
    with session() as db:
        db.get(Task, ident).status = "QUEUED"
        db.commit()
    response = client.post(url, json={"crf": 18})
    assert response.status_code == 202, response.text
    assert next(t for t in response.json()["tasks"] if t["id"] == ident)["status"] == "CANCELLED"


def test_reencode_rejects_changed_source_and_invalid_crf(client, finished_encode, environment):
    job, _, _ = finished_encode
    url = f"/api/jobs/{job['id']}/reencode"
    assert client.post(url, json={"crf": 50}).status_code == 409
    (environment.source_root / "Movie.mkv").write_bytes(b"changed source")
    assert client.post(url, json={"crf": 18}).status_code == 409
    with session() as db:
        assert db.get(MovieJob, job["id"]).state == "COMPLETE"
        assert (
            db.scalar(select(EncodeConfig).where(EncodeConfig.job_id == job["id"])).data["bitrate_kbps"]
            == 8000
        )
