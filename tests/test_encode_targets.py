from pathlib import Path

import pytest
import yaml
from sqlalchemy import select

from shared.config import profiles
from shared.db import session
from shared.models import EncodeConfig, Task
from tests.conftest import gate
from worker.adapters.handbrake import Crop, encode_command, parse_progress


@pytest.mark.parametrize("codec", ["x264", "x265"])
def test_bitrate_selection_persists_target_profile_and_one_queued_task(client, new_job, environment, codec):
    from shared.models import MovieJob

    profile = f"{codec}-live"
    with session() as db:
        db.get(MovieJob, new_job["id"]).analysis_profile = profile
        db.commit()
    gate(new_job["id"], "WAITING_FOR_ENCODE_SELECTION", profile=profile)
    body = {"codec": codec, "profile": profile, "rate_control": "bitrate", "bitrate_kbps": 8123}
    response = client.post(f"/api/jobs/{new_job['id']}/encode-selection", json=body)
    assert response.status_code == 200, response.text
    job = response.json()
    assert job["state"] == "ENCODING"
    saved = job["encode_config"]["data"]
    assert saved["rate_control"] == "bitrate" and saved["bitrate_kbps"] == 8123
    assert "crf" not in saved
    assert saved["profile_snapshot"] == profiles()[profile]
    assert saved["selected_by"] == "user"
    queued = [t for t in job["tasks"] if t["status"] == "QUEUED"]
    assert len(queued) == 1 and queued[0]["type"] == "encode"
    with session() as db:
        assert db.scalar(select(EncodeConfig).where(EncodeConfig.job_id == job["id"])).data == saved
    manifest = yaml.safe_load((environment.workspace_root / job["id"] / "manifest.yaml").read_text())
    assert manifest["encode"] == saved
    # A second request cannot enqueue another encode or alter a confirmed target.
    assert client.post(f"/api/jobs/{job['id']}/encode-selection", json=body).status_code == 409


@pytest.mark.parametrize(
    "target",
    [
        {},
        {"rate_control": "bitrate"},
        {"rate_control": "unknown", "crf": 17},
        {"rate_control": "crf", "bitrate_kbps": 8000},
        {"rate_control": "bitrate", "crf": 17},
        {"rate_control": "bitrate", "crf": 17, "bitrate_kbps": 8000},
        {"rate_control": "crf", "crf": 17, "bitrate_kbps": 8000},
        {"rate_control": "bitrate", "bitrate_kbps": 0},
        {"rate_control": "bitrate", "bitrate_kbps": -8000},
        {"rate_control": "bitrate", "bitrate_kbps": 1000001},
        {"rate_control": "bitrate", "bitrate_kbps": 8000.5},
        {"rate_control": "bitrate", "bitrate_kbps": "8000"},
        {"rate_control": "bitrate", "bitrate_kbps": True},
        {"rate_control": "bitrate", "bitrate_kbps": 8000, "two_pass": False},
        {"rate_control": "crf", "crf": "NaN"},
    ],
)
def test_invalid_targets_do_not_queue_an_encode(client, new_job, target):
    gate(new_job["id"], "WAITING_FOR_ENCODE_SELECTION", profile="x265-live")
    response = client.post(
        f"/api/jobs/{new_job['id']}/encode-selection",
        json={
            "codec": "x265",
            "profile": "x265-live",
            **target,
        },
    )
    assert response.status_code == 422, response.text
    with session() as db:
        assert db.scalar(select(EncodeConfig).where(EncodeConfig.job_id == new_job["id"])) is None
        assert db.scalar(select(Task).where(Task.job_id == new_job["id"], Task.status == "QUEUED")) is None


def test_bitrate_still_requires_human_gate_and_analyzed_profile(client, new_job):
    url = f"/api/jobs/{new_job['id']}/encode-selection"
    body = {"codec": "x265", "profile": "x265-live", "rate_control": "bitrate", "bitrate_kbps": 8000}
    assert client.post(url, json=body).status_code == 409
    gate(new_job["id"], "WAITING_FOR_ENCODE_SELECTION", profile="x265-live")
    assert client.post(url, json={**body, "profile": "x265-animation"}).status_code == 409
    assert client.post(url, json={**body, "codec": "x264"}).status_code == 409
    assert client.post(url, json={"codec": "x265", "profile": "x265-live", "crf": 21}).status_code == 409
    # Bitrate selection is independent of the CRF bounds and sampled rates.
    assert client.post(url, json={**body, "bitrate_kbps": 1000}).status_code == 200


@pytest.mark.parametrize("codec", ["x264", "x265"])
def test_handbrake_selects_exactly_one_rate_control_and_preserves_profile(codec):
    profile = profiles()[f"{codec}-live"]
    args = (
        "HandBrakeCLI",
        Path("source.mkv"),
        Path("video.mkv"),
        {"width": 1920, "height": 1080},
        Crop(top=104, bottom=104, left=0, right=0),
        profile,
    )
    crf = encode_command(*args, 17.5)  # Old saved config and callers require no mode.
    assert crf[crf.index("-q") + 1] == "17.5"
    assert "--no-two-pass" in crf and "--vb" not in crf and "--two-pass" not in crf
    bitrate = encode_command(*args, rate_control="bitrate", bitrate_kbps=8123)
    assert bitrate[bitrate.index("--vb") + 1] == "8123"
    assert "--two-pass" in bitrate and "--no-turbo" in bitrate and "-q" not in bitrate
    for cmd in (crf, bitrate):
        for option, value in [
            ("-e", profile["encoder"]),
            ("--encoder-preset", profile["preset"]),
            ("--encopts", profile["extra_options"]),
            ("--crop", "104:104:0:0"),
            ("--height", "872"),
            ("--audio", "none"),
            ("--subtitle", "none"),
        ]:
            assert cmd[cmd.index(option) + 1] == value
        assert "--vfr" in cmd
    with pytest.raises(ValueError):
        encode_command(*args, 17.5, rate_control="bitrate", bitrate_kbps=8123)


def test_two_pass_progress_is_overall_and_eta_is_for_current_pass():
    first = parse_progress("\rEncoding: task 1 of 2, 60.00 % (13.80 fps, avg 12.80 fps, ETA 00h01m22s)\r")
    assert first == {
        "percentage": 30,
        "pass_number": 1,
        "pass_count": 2,
        "pass_percentage": 60,
        "fps": 13.8,
        "pass_eta_seconds": 82,
    }
    boundary = parse_progress("Encoding: task 1 of 2, 100.00 %\rEncoding: task 2 of 2, 0.00 %\r")
    assert boundary["percentage"] == 50 and boundary["pass_number"] == 2
    second = parse_progress("Encoding: task 2 of 2, 60.00 % (10.00 fps, avg 9.00 fps, ETA 00h02m03s)\r")
    assert second["percentage"] == 80
    assert second["pass_eta_seconds"] == 123
    assert "eta_seconds" not in second
    assert parse_progress("Encoding: task 2 of 2, 100.00 %")["percentage"] == 100
