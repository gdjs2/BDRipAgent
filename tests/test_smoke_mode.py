import json
from contextlib import contextmanager
from fractions import Fraction
from types import SimpleNamespace

import pytest
import yaml
from sqlalchemy import select

from shared.db import session
from shared.models import EncodeConfig, MovieJob, Screenshot, Task
from shared.screenshot_rules import other_variant_frames
from tests.conftest import gate
from tests.test_refinements import create_pair_fixture
from worker.pipeline import smoke, stages
from worker.runtime import TaskContext
from worker.tasks import execute


@pytest.fixture
def ready_job(new_job):
    gate(new_job["id"], "WAITING_FOR_ENCODE_SELECTION", profile="x265-live")
    with session() as db:
        job = db.get(MovieJob, new_job["id"])
        from shared.models import TrackSelection

        db.add(TrackSelection(job_id=job.id, audio_track_ids=[], subtitle_track_ids=[]))
        job.analysis = {
            "video": {"width": 1920, "height": 1080, "bit_depth": 8, "codec": "h264", "duration": 1000},
            "crop": {"top": 104, "bottom": 104, "left": 0, "right": 0},
            "prepared_tracks": [],
        }
        db.commit()
    return new_job


@pytest.fixture
def inspection_only(monkeypatch):
    commands = []

    def run(ctx, command, **kwargs):
        assert command[:2] == [ctx.settings.mkvmerge_bin, "-J"], "Smoke mode must not invoke an encoder"
        commands.append(command)
        result = json.dumps({"tracks": [{"type": "audio", "id": 0}, {"type": "video", "id": 3}]})
        kwargs["output"].write_text(result)
        return result

    monkeypatch.setattr(TaskContext, "run", run)
    return commands


def test_smoke_selection_marks_job_manifest_and_queue(client, ready_job, environment):
    result = client.post(f"/api/jobs/{ready_job['id']}/smoke-test", json={})
    assert result.status_code == 200, result.text
    job = result.json()
    assert job["state"] == "ENCODING" and job["analysis"]["smoke_test"] is True
    assert job["release_name"].startswith("SMOKE-TEST.")
    config = job["encode_config"]["data"]
    assert config["execution_mode"] == "smoke" and "crf" not in config and "bitrate_kbps" not in config
    queued = [t for t in job["tasks"] if t["status"] == "QUEUED"]
    assert len(queued) == 1 and queued[0]["type"] == "encode"
    assert client.get("/api/queue").json()["queued"][0]["smoke_test"] is True
    saved = yaml.safe_load((environment.workspace_root / job["id"] / "manifest.yaml").read_text())
    assert saved["encode"]["execution_mode"] == "smoke"
    assert client.post(f"/api/jobs/{job['id']}/smoke-test", json={}).status_code == 409


def test_smoke_cannot_bypass_preparation_or_authentication(client, new_job):
    url = f"/api/jobs/{new_job['id']}/smoke-test"
    assert client.post(url, json={}).status_code == 409
    gate(new_job["id"], "WAITING_FOR_ENCODE_SELECTION", profile="x265-live")
    assert client.post(url, json={}).status_code == 409
    client.headers.clear()
    assert client.post(url, json={}).status_code == 401


def test_normal_selection_cannot_enable_smoke_implicitly(client, ready_job):
    url = f"/api/jobs/{ready_job['id']}/encode-selection"
    target = {"codec": "x265", "profile": "x265-live", "crf": 17}
    assert client.post(url, json={**target, "execution_mode": "smoke"}).status_code == 422
    job = client.post(url, json=target).json()
    assert not job["analysis"].get("smoke_test")
    assert not job["encode_config"]["data"].get("execution_mode")


def test_smoke_skips_encoding_and_full_validation_without_faking_success(
    client, ready_job, inspection_only, monkeypatch
):
    job = client.post(f"/api/jobs/{ready_job['id']}/smoke-test", json={}).json()
    encode_id = next(t["id"] for t in job["tasks"] if t["type"] == "encode")
    execute(encode_id)
    job = client.get(f"/api/jobs/{job['id']}").json()
    assert job["state"] == "VALIDATING_ENCODE"
    assert job["analysis"]["encoding_skipped"] is True
    assert job["analysis"]["smoke_video_track_id"] == 3
    assert "encoded_path" not in job["analysis"]
    assert all(a["artifact_type"] != "ENCODED_VIDEO" for a in job["artifacts"])

    def decode(**kwargs):
        yield SimpleNamespace(pts=3, time_base=Fraction(1, 24))
        raise AssertionError("Smoke validation must not decode the full video")

    @contextmanager
    def open_video(*args):
        yield SimpleNamespace(decode=decode)

    monkeypatch.setattr(smoke.av, "open", open_video)
    monkeypatch.setattr(stages, "timeline", lambda *args: pytest.fail("Full encode validation was invoked"))
    execute(next(t["id"] for t in job["tasks"] if t["type"] == "validate"))
    job = client.get(f"/api/jobs/{job['id']}").json()
    assert job["state"] == "REMUXING"
    assert job["validation"]["valid"] is None
    assert job["validation"]["skipped"] is True and job["validation"]["source_readable"] is True
    assert job["validation"]["metrics"]["source_first_pts"] == 0.125
    assert len(inspection_only) == 1


def test_smoke_retries_keep_the_mode_and_source_immutability(client, ready_job, environment, inspection_only):
    job = client.post(f"/api/jobs/{ready_job['id']}/smoke-test", json={}).json()
    encode_id = next(t["id"] for t in job["tasks"] if t["type"] == "encode")
    with session() as db:
        db.get(Task, encode_id).status = "FAILED"
        db.commit()
    retry = client.post(f"/api/tasks/{encode_id}/retry", json={}).json()
    execute(retry["id"])
    assert client.get(f"/api/jobs/{job['id']}").json()["state"] == "VALIDATING_ENCODE"
    with session() as db:
        assert (
            db.scalar(select(EncodeConfig).where(EncodeConfig.job_id == job["id"])).data["execution_mode"]
            == "smoke"
        )
    (environment.source_root / "Movie.mkv").write_bytes(b"changed source")
    job = client.get(f"/api/jobs/{job['id']}").json()
    task_id = next(t["id"] for t in job["tasks"] if t["type"] == "validate")
    execute(task_id)
    assert client.get(f"/api/tasks/{task_id}").json()["status"] == "FAILED"
    assert client.get(f"/api/jobs/{job['id']}").json()["state"] == "VALIDATING_ENCODE"


@pytest.mark.parametrize("smoke_mode", [False, True])
def test_remux_cannot_claim_a_failed_or_unchecked_source_is_valid(smoke_mode):
    job = SimpleNamespace(
        analysis={"smoke_test": smoke_mode}, validation={"valid": False, "skipped": True, "smoke_test": True}
    )
    with pytest.raises(ValueError):
        stages.mux(SimpleNamespace(job=job))


def test_smoke_frame_choices_do_not_reserve_frames_for_real_jobs(client, environment):
    jobs = create_pair_fixture(client, environment)
    with session() as db:
        first, second = (db.get(MovieJob, j["id"]) for j in jobs)
        db.add(
            Screenshot(
                job_id=first.id,
                candidate_id=1,
                selected=True,
                info={"source_frame_number": 2400, "timeline_seconds": 100, "scene_id": 1},
            )
        )
        db.flush()
        assert len(other_variant_frames(db, second)) == 1
        first.analysis = {"smoke_test": True}
        assert other_variant_frames(db, second) == []
        first.analysis = {}
        second.analysis = {"smoke_test": True}
        assert other_variant_frames(db, second) == []
        first.analysis = {"smoke_test": True}
        assert len(other_variant_frames(db, second)) == 1
