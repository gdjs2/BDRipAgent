import json
from pathlib import Path

import pytest
from pydantic import SecretStr
from sqlalchemy import select

from backend.app.services import advance
from shared.db import session
from shared.models import MovieJob, Screenshot, Task
from tests.conftest import gate
from worker.adapters import release_runner
from worker.adapters.release_progress import ReleaseProgressReader

DETAILS = {
    "source": "1080p Blu-ray AVC DTS-HD MA 5.1-GROUP",
    "chinese_name": "电影中文名",
    "extra_description": "双语 · 内封字幕",
    "tracker": "https://tracker.example/announce?passkey=private-key",
}


@pytest.fixture
def ready_release(client, new_job, environment):
    gate(new_job["id"], "WAITING_FOR_RELEASE_DETAILS")
    environment.tu_ttg_token = SecretStr("private-upload-token")
    workspace = environment.workspace_root / new_job["id"]
    final = environment.completed_root / new_job["id"] / "Movie.mkv"
    final.parent.mkdir()
    final.write_bytes(b"final media")
    for name in ("1.src.png", "1.encode.png", "unselected.png"):
        (workspace / name).write_bytes(b"screenshot")
    with session() as db:
        job = db.get(MovieJob, new_job["id"])
        job.analysis = {
            "final_path": f"{job.id}/Movie.mkv",
            "screenshot_selection": {"candidate_ids": [1], "count": 1},
        }
        job.validation = {"metrics": {"source_duration": 1000}}
        db.add(
            Screenshot(
                job_id=job.id,
                candidate_id=1,
                selected=True,
                shortlisted=True,
                info={
                    "source_frame_number": 100,
                    "timeline_seconds": 100,
                    "scene_id": 1,
                    "b_frames_verified": True,
                    "picture_type": "B",
                    "encoded_picture_type": "B",
                    "recommendation_rank": 1,
                    "comparisons": {"src": "1.src.png", "encode": "1.encode.png"},
                },
            )
        )
        db.add(
            Screenshot(
                job_id=job.id,
                candidate_id=2,
                selected=False,
                shortlisted=True,
                info={"path": "unselected.png"},
            )
        )
        db.commit()
    return new_job["id"]


def test_rendering_pauses_for_release_details(client, new_job):
    gate(new_job["id"], "SCREENSHOT_RENDERING")
    with session() as db:
        job = db.get(MovieJob, new_job["id"])
        advance(db, job)
        db.commit()
        assert job.state == "WAITING_FOR_RELEASE_DETAILS"
        assert not db.scalar(select(Task.id).where(Task.job_id == job.id, Task.status == "QUEUED"))


def test_release_details_persist_without_upload_or_queue(client, ready_release, environment):
    environment.tu_ttg_token = SecretStr("")
    response = client.patch(f"/api/jobs/{ready_release}/release", json=DETAILS)
    assert response.status_code == 200, response.text
    assert response.json()["analysis"]["release_details"] == DETAILS
    assert response.json()["state"] == "WAITING_FOR_RELEASE_DETAILS"
    assert not any(t["status"] == "QUEUED" for t in response.json()["tasks"])
    assert "private-upload-token" not in response.text


def test_release_accepts_fifteen_rendered_pairs(client, ready_release):
    with session() as db:
        template = db.scalar(
            select(Screenshot).where(Screenshot.job_id == ready_release, Screenshot.selected.is_(True))
        )
        for candidate_id in range(3, 17):
            db.add(
                Screenshot(
                    job_id=ready_release,
                    candidate_id=candidate_id,
                    selected=True,
                    info={**template.info, "source_frame_number": candidate_id * 100},
                )
            )
        job = db.get(MovieJob, ready_release)
        job.analysis = {**job.analysis, "screenshot_selection": {"count": 15}}
        db.commit()
    response = client.post(f"/api/jobs/{ready_release}/release", json=DETAILS)
    assert response.status_code == 202, response.text
    assert response.json()["state"] == "GENERATING_RELEASE"


@pytest.mark.parametrize("state", ["WAITING_FOR_RELEASE_DETAILS", "COMPLETE"])
def test_release_submission_queues_one_task_including_existing_completed_jobs(client, ready_release, state):
    gate(ready_release, state)
    response = client.post(f"/api/jobs/{ready_release}/release", json=DETAILS)
    assert response.status_code == 202, response.text
    job = response.json()
    assert job["state"] == "GENERATING_RELEASE" and job["completed_at"] is None
    assert [t["type"] for t in job["tasks"] if t["status"] == "QUEUED"] == ["generate_release"]
    assert client.post(f"/api/jobs/{ready_release}/release", json=DETAILS).status_code == 409
    assert client.patch(f"/api/jobs/{ready_release}/release", json=DETAILS).status_code == 409
    assert (
        client.post(
            f"/api/jobs/{ready_release}/screenshots/selection", json={"candidate_ids": [1]}
        ).status_code
        == 409
    )


@pytest.mark.parametrize(
    "change",
    [
        {"source": " "},
        {"source": "Disc\nOther"},
        {"source": "Disc\tOther"},
        {"source": "Disc\x7f"},
        {"source": "x" * 1001},
        {"chinese_name": " "},
        {"chinese_name": "name\x00"},
        {"tracker": "file:///tmp/a"},
        {"tracker": "https://tracker.example/announce\nother"},
        {"tracker": "http://user:pass@tracker.example/"},
        {"tracker": "udp://tracker.example:0/announce"},
        {"tracker": "https://tracker.example/#fragment"},
        {"unexpected": True},
    ],
)
def test_release_validates_manual_fields(client, ready_release, change):
    assert client.post(f"/api/jobs/{ready_release}/release", json={**DETAILS, **change}).status_code == 422


def test_missing_upload_token_fails_before_queueing_and_config_never_returns_it(
    client, ready_release, environment
):
    config = client.get("/api/config")
    assert config.json()["release"]["upload_configured"] is True
    assert "private-upload-token" not in config.text
    environment.tu_ttg_token = SecretStr("")
    response = client.post(f"/api/jobs/{ready_release}/release", json=DETAILS)
    assert response.status_code == 409 and "TU_TTG_TOKEN" in response.text
    assert client.get(f"/api/jobs/{ready_release}").json()["state"] == "WAITING_FOR_RELEASE_DETAILS"


@pytest.mark.parametrize("fault", ["missing_image", "missing_pair", "wrong_count", "unverified"])
def test_release_requires_the_entire_confirmed_rendered_selection(client, ready_release, environment, fault):
    with session() as db:
        shot = db.scalar(
            select(Screenshot).where(Screenshot.job_id == ready_release, Screenshot.selected.is_(True))
        )
        if fault == "missing_image":
            (environment.workspace_root / ready_release / "1.src.png").unlink()
        elif fault == "missing_pair":
            shot.info = {**shot.info, "comparisons": {}}
        elif fault == "unverified":
            shot.info = {**shot.info, "encoded_picture_type": "P"}
        else:
            job = db.get(MovieJob, ready_release)
            job.analysis = {**job.analysis, "screenshot_selection": {"count": 2}}
        db.commit()
    assert client.post(f"/api/jobs/{ready_release}/release", json=DETAILS).status_code == 409


def test_release_gate_cannot_be_skipped(client, new_job):
    for method in (client.post, client.patch):
        assert method(f"/api/jobs/{new_job['id']}/release", json=DETAILS).status_code == 409


def test_changed_details_clear_stale_release_results(client, ready_release):
    gate(ready_release, "COMPLETE")
    with session() as db:
        job = db.get(MovieJob, ready_release)
        job.analysis = {**job.analysis, "release_details": DETAILS, "release_result": {"infohash": "old"}}
        db.commit()
    response = client.patch(
        f"/api/jobs/{ready_release}/release", json={**DETAILS, "extra_description": "Changed"}
    )
    assert response.status_code == 200
    assert "release_result" not in response.json()["analysis"]


def test_worker_uses_only_selected_pairs_and_registers_outputs(
    client, ready_release, environment, monkeypatch
):
    from worker.runtime import TaskContext
    from worker.tasks import execute

    job = client.post(f"/api/jobs/{ready_release}/release", json=DETAILS).json()
    task_id = next(t["id"] for t in job["tasks"] if t["type"] == "generate_release")
    requests = []

    def run(ctx, command, **kwargs):
        assert kwargs["env"] == {"TU_TTG_TOKEN": "private-upload-token"}
        request = json.loads(Path(command[-1]).read_text())
        requests.append(request)
        assert request["details"]["source"] == DETAILS["source"]
        assert request["details"]["source"] != Path(ctx.job.source_path).name
        assert "source" not in request  # One authoritative user-entered source.
        assert [p["candidate_id"] for p in request["pairs"]] == [1]
        assert "private-upload-token" not in Path(command[-1]).read_text()
        out = Path(request["output_dir"])
        artifact = out / "Movie.bbcode.txt"
        artifact.write_text("fixture")
        (out / "result.json").write_text(
            json.dumps(
                {
                    "artifacts": [{"path": str(artifact), "kind": "RELEASE_BBCODE", "storage": "workspace"}],
                    "uploaded_images": 2,
                }
            )
        )

    monkeypatch.setattr(TaskContext, "run", run)
    execute(task_id)
    job = client.get(f"/api/jobs/{ready_release}").json()
    assert job["state"] == "COMPLETE"
    assert job["analysis"]["release_result"]["candidate_ids"] == [1]
    assert any(a["artifact_type"] == "RELEASE_BBCODE" for a in job["artifacts"])
    assert len(requests) == 1


def test_runner_redacts_upload_credentials_and_tracker_errors(tmp_path, monkeypatch, capsys):
    request = tmp_path / "input.json"
    request.write_text(json.dumps({"details": DETAILS}))
    monkeypatch.setenv("TU_TTG_TOKEN", "secret token")
    monkeypatch.setattr(release_runner.sys, "argv", ["release_runner.py", str(request)])

    def fail(*args):
        raise RuntimeError(f"secret token secret%20token secret+token {DETAILS['tracker']}")

    monkeypatch.setattr(release_runner, "run", fail)
    with pytest.raises(SystemExit):
        release_runner.main()
    output = capsys.readouterr().err
    assert "secret" not in output and "private-key" not in output
    assert "[redacted]" in output


def test_release_progress_ignores_incomplete_and_duplicate_updates(tmp_path):
    path = tmp_path / "progress.json"
    reader = ReleaseProgressReader(path)
    assert reader() is None
    progress = release_runner.Progress(path)
    progress.report(10, "Uploading selected screenshot pairs", uploaded=1, total_images=2)
    assert reader()["uploaded"] == 1
    assert reader() is None
    path.write_text("{")
    assert reader() is None


@pytest.mark.parametrize("method", ["patch", "post"])
def test_source_is_required_even_for_existing_release_details(client, ready_release, method):
    legacy = {k: v for k, v in DETAILS.items() if k != "source"}
    with session() as db:
        job = db.get(MovieJob, ready_release)
        job.analysis = {**job.analysis, "release_details": legacy}
        db.commit()
    response = getattr(client, method)(f"/api/jobs/{ready_release}/release", json=legacy)
    assert response.status_code == 422
    assert "source" in response.text
    assert client.get(f"/api/jobs/{ready_release}").json()["state"] == "WAITING_FOR_RELEASE_DETAILS"


@pytest.mark.parametrize(
    "name,expected",
    [
        ("Movie.Name.2026.1080p.Blu-ray.AVC.DTS-HD.MA.5.1@GROUP", "1080p Blu-ray AVC DTS-HD MA 5.1-GROUP"),
        ("Movie.2026.2160p.UHD.Blu-ray.HEVC.TrueHD.7.1@GROUP", "2160p UHD Blu-ray HEVC TrueHD 7.1-GROUP"),
        ("1080p Blu-ray AVC DTS-HD MA 5.1-GROUP", "1080p Blu-ray AVC DTS-HD MA 5.1-GROUP"),
        ("Disc.2.0-GROUP", "Disc 2.0-GROUP"),
        ("  Custom USA/UK Blu-ray   source  ", "Custom USA/UK Blu-ray source"),
    ],
)
def test_convert_source_uses_upstream_source_description_rules(client, name, expected):
    response = client.post("/api/release/source-description", json={"source": name})
    assert response.status_code == 200, response.text
    assert response.json() == {"source": expected}


def test_conversion_requires_auth_and_rejects_empty_result(client):
    assert client.post("/api/release/source-description", json={"source": "..."}).status_code == 409
    client.headers.clear()
    assert client.post("/api/release/source-description", json={"source": "Movie"}).status_code == 401


def test_custom_source_is_preserved_unless_conversion_requested(client, ready_release):
    entered = "  Movie.Name.2026.Blu-ray / My disc  "
    response = client.patch(f"/api/jobs/{ready_release}/release", json={**DETAILS, "source": entered})
    assert response.status_code == 200, response.text
    saved = client.get(f"/api/jobs/{ready_release}").json()["analysis"]["release_details"]
    assert saved["source"] == entered.strip()


def test_source_change_invalidates_previous_release_outputs(client, ready_release):
    with session() as db:
        job = db.get(MovieJob, ready_release)
        job.analysis = {**job.analysis, "release_details": DETAILS, "release_result": {"infohash": "old"}}
        db.commit()
    response = client.patch(
        f"/api/jobs/{ready_release}/release", json={**DETAILS, "source": "My alternate disc"}
    )
    assert response.status_code == 200, response.text
    assert "release_result" not in response.json()["analysis"]


def test_legacy_retry_requires_source_before_worker_or_uploads(client, ready_release, monkeypatch):
    from types import SimpleNamespace

    from worker.pipeline.release import generate

    ctx = SimpleNamespace(
        job=SimpleNamespace(analysis={"release_details": {k: v for k, v in DETAILS.items() if k != "source"}})
    )
    with pytest.raises(ValueError, match="source"):
        generate(ctx)
