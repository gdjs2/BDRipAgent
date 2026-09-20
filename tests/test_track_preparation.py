from pathlib import Path

import pytest
from sqlalchemy import select

from shared.db import session
from shared.models import MovieJob, Task
from tests.conftest import gate
from worker.adapters.integrations import Sup2supAdapter
from worker.runtime import TaskContext, ToolError
from worker.tasks import execute


@pytest.fixture
def extraction_job(client, new_job, monkeypatch):
    tracks = [
        {"track_id": i, "kind": "audio", "codec_id": "A_AC3", "channels": 2, "extractable": True}
        for i in (4, 8)
    ] + [{"track_id": i, "kind": "subtitles", "codec_id": "S_HDMV/PGS"} for i in (9, 12)]
    gate(
        new_job["id"],
        "WAITING_FOR_TRACK_SELECTION",
        tracks=[{"track_id": t["track_id"], "kind": t["kind"], "info": t} for t in tracks],
    )
    with session() as db:
        db.get(MovieJob, new_job["id"]).analysis = {
            "tracks": tracks,
            "crop": {"left": 0, "top": 104, "right": 0, "bottom": 104},
            "video": {"width": 1920, "height": 1080},
        }
        db.commit()
    calls, cropped = [], []
    fault = {}

    def run(ctx, command, **kwargs):
        calls.append([str(x) for x in command])
        if fault.get("exit"):
            raise ToolError(command, 2)
        mode = None
        for arg in command[2:]:
            if arg in ("tracks", "timestamps_v2"):
                mode = arg
                continue
            track_id, filename = arg.split(":", 1)
            path = Path(filename)
            if fault.get("target") == (mode, int(track_id)):
                if fault.get("empty"):
                    path.touch()
                continue
            path.write_text("# timestamp format v2\n0\n32\n" if mode == "timestamps_v2" else "track data")

    def crop(adapter, ctx, original, output, *args):
        assert original.is_file()
        cropped.append(original.name)
        output.write_bytes(original.read_bytes())

    monkeypatch.setattr(TaskContext, "run", run)
    monkeypatch.setattr(Sup2supAdapter, "crop", crop)
    return new_job["id"], calls, cropped, fault


def prepare(client, job_id, audio, subtitles):
    response = client.post(
        f"/api/jobs/{job_id}/tracks/selection",
        json={"audio_track_ids": audio, "subtitle_track_ids": subtitles},
    )
    assert response.status_code == 200, response.text
    with session() as db:
        task_id = db.scalar(select(Task.id).where(Task.job_id == job_id, Task.type == "prepare_tracks"))
    execute(task_id)
    return client.get(f"/api/jobs/{job_id}").json()


@pytest.mark.parametrize("audio,subtitles", [([8, 4], [12, 9]), ([4, 8], []), ([], [9, 12]), ([], [])])
def test_selected_tracks_and_audio_timestamps_share_one_extraction(client, extraction_job, audio, subtitles):
    job_id, calls, cropped, _ = extraction_job
    job = prepare(client, job_id, audio, subtitles)
    assert job["state"] == "RUNNING_CRF_ANALYSIS"
    prepared = job["analysis"]["prepared_tracks"]
    assert [t["track_id"] for t in prepared] == audio + subtitles
    assert len(calls) == int(bool(audio or subtitles))
    if calls:
        command = calls[0]
        split = command.index("timestamps_v2") if audio else len(command)
        assert command[2] == "tracks"
        assert [int(arg.split(":", 1)[0]) for arg in command[3:split]] == audio + subtitles
        assert [int(arg.split(":", 1)[0]) for arg in command[split + 1 :]] == audio
    assert cropped == [f"track-{i}.sup" for i in subtitles]
    for track in prepared:
        assert ("timestamps" in track) == (track["kind"] == "audio")
        assert track["path"].endswith(".ac3" if track["kind"] == "audio" else ".cropped.sup")
    kinds = [a["artifact_type"] for a in job["artifacts"]]
    assert kinds.count("AUDIO") == kinds.count("AUDIO_TIMESTAMPS") == len(audio)
    assert kinds.count("SUBTITLE_ORIGINAL") == kinds.count("SUBTITLE_CROPPED") == len(subtitles)


@pytest.mark.parametrize("mode,track_id", [("tracks", 8), ("tracks", 12), ("timestamps_v2", 8)])
@pytest.mark.parametrize("empty", [False, True])
def test_incomplete_batch_does_not_publish_tracks_or_crop_subtitles(
    client, extraction_job, mode, track_id, empty
):
    job_id, calls, cropped, fault = extraction_job
    fault.update(target=(mode, track_id), empty=empty)
    job = prepare(client, job_id, [4, 8], [9, 12])
    assert len(calls) == 1 and not cropped
    assert not job["analysis"].get("prepared_tracks")
    assert all(a["artifact_type"] == "LOG" for a in job["artifacts"])
    task = next(t for t in job["tasks"] if t["type"] == "prepare_tracks")
    assert task["status"] == "FAILED" and f"Track {track_id}" in task["error_message"]


def test_extraction_command_failure_stops_preparation(client, extraction_job):
    job_id, calls, cropped, fault = extraction_job
    fault["exit"] = True
    job = prepare(client, job_id, [4, 8], [9, 12])
    assert len(calls) == 1 and not cropped
    assert all(a["artifact_type"] == "LOG" for a in job["artifacts"])
    assert next(t for t in job["tasks"] if t["type"] == "prepare_tracks")["status"] == "FAILED"
