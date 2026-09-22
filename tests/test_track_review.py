import copy
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from agent import main as service
from agent.track_agent import CodexTrackReviewer
from shared.db import session
from shared.models import TrackSelection
from shared.naming import track_name
from shared.tracks import FLAG_NAMES, TrackReviewResult, validate_review
from tests.conftest import gate
from worker.adapters import track_review
from worker.pipeline import stages
from worker.runtime import TaskContext
from worker.tasks import execute


def fixture_tracks():
    return [
        {
            "track_id": 4,
            "kind": "audio",
            "codec": "AC-3",
            "codec_id": "A_AC3",
            "language": "eng",
            "channels": 2,
            "ffprobe_index": 1,
            "extractable": True,
            "default": True,
            "forced": False,
            "hearing_impaired": False,
            "visual_impaired": False,
            "commentary": False,
            "name": "Source label",
        },
        *[
            {
                "track_id": i,
                "kind": "subtitles",
                "codec": "PGS",
                "codec_id": "S_HDMV/PGS",
                "language": "chi",
                "extractable": True,
                "default": False,
                "forced": False,
                "hearing_impaired": None,
                "visual_impaired": False,
                "commentary": False,
                "name": "Unverified SDH",
            }
            for i in (9, 12)
        ],
        {
            "track_id": 16,
            "kind": "subtitles",
            "codec": "SRT",
            "codec_id": "S_TEXT/UTF8",
            "language": "eng",
            "extractable": False,
        },
    ]


def agent_answer(tracks):
    return {
        "audio_comparison": {
            "summary": "Audio tracks compared using local evidence.",
            "distinctions": [
                {
                    "track_id": t["track_id"],
                    "compared_with": [p["track_id"] for p in tracks if p["kind"] == "audio" and p != t],
                    "difference": "Main dialogue with the supplied technical characteristics.",
                    "evidence_sample_ids": [1],
                    "resolved": True,
                }
                for t in tracks
                if t["kind"] == "audio"
            ],
            "needs_more": False,
            "next_track_ids": [],
            "question": "",
        },
        "tracks": [
            {
                "track_id": t["track_id"],
                "description": f"Description for track {t['track_id']} from sampled content.",
                "flags": {
                    **{flag: False for flag in FLAG_NAMES},
                    "hearing_impaired": t.get("subtitle_detection", {}).get("hearing_impaired")
                    if t["kind"] == "subtitles"
                    else False,
                },
                "flag_explanation": "Reviewed the supplied samples; source flags are unverified.",
                "evidence_sample_ids": [1] if t["kind"] == "audio" else [],
                "confidence": "medium",
            }
            for t in tracks
        ],
    }


@pytest.fixture
def analyzed_job(client, new_job, monkeypatch):
    tracks = fixture_tracks()
    commands, classified = [], []
    monkeypatch.setattr(
        stages,
        "analyze",
        lambda ctx: {
            "tracks": copy.deepcopy(tracks),
            "video": {"width": 1920, "height": 1080, "duration": 300},
            "crop": {"left": 0, "right": 0, "top": 0, "bottom": 0},
        },
    )

    def run(ctx, command, **kwargs):
        commands.append([str(x) for x in command])
        mode = "tracks"
        for arg in command[3:]:
            if arg == "--gui-mode":
                continue
            if arg == "timestamps_v2":
                mode = arg
                continue
            _, value = arg.split(":", 1)
            Path(value).write_text("track data" if mode == "tracks" else "# timestamp format v2\n0\n")

    def classify(ctx, track, path, **kwargs):
        assert kwargs == {"require_confident": False, "agent_review": True, "prepared": None}
        assert path.read_text() == "track data"
        with session() as db:
            assert not db.scalar(select(TrackSelection).where(TrackSelection.job_id == ctx.job.id))
        classified.append(track["track_id"])
        return {
            **track,
            "language": "zh-Hant",
            "hearing_impaired": False,
            "subtitle_detection": {
                "schema_version": 2,
                "language_code": "zh-Hant",
                "status": "resolved",
                "language_confident": True,
                "sdh_confident": True,
                "method": "program + agent",
                "hearing_impaired": False,
                "explanation": "Traditional Chinese dialogue, no SDH in samples.",
            },
        }

    def audio(ctx, tracks, duration):
        (ctx.workspace / "private.wav").write_bytes(b"audio evidence")
        assert [t["track_id"] for t in tracks] == [4] and duration == 300
        return {
            4: {
                "samples": [{"id": 1, "path": "private.wav", "segments": [{"text": "sample dialogue"}]}],
                "limitations": [],
            }
        }

    def review(ctx):
        inventory_path = (
            ctx.settings.cache_root / "agent" / ctx.job.id / ctx.task_id / "tracks/inventory.json"
        )
        inventory = json.loads(inventory_path.read_text())
        assert "private.wav" not in inventory_path.read_text()
        return agent_answer(inventory["tracks"])

    monkeypatch.setattr(TaskContext, "run", run)
    monkeypatch.setattr(stages, "classify_subtitle", classify)
    monkeypatch.setattr(stages, "prepare_subtitle", lambda *a: None)
    monkeypatch.setattr(track_review, "analyze_audio", audio)
    monkeypatch.setattr(track_review, "review", review)
    execute(new_job["tasks"][0]["id"])
    job = client.get(f"/api/jobs/{new_job['id']}").json()
    execute(next(t for t in job["tasks"] if t["type"] == "review_tracks")["id"])
    job = client.get(f"/api/jobs/{new_job['id']}").json()
    assert job["state"] == "RUNNING_CRF_ANALYSIS", job["tasks"]
    assert job["track_analysis_complete"], job["tasks"]
    return job, commands, classified


def test_all_pgs_and_audio_are_described_before_any_selection(analyzed_job):
    job, calls, classified = analyzed_job
    assert classified == [9, 12] and len(calls) == 1
    assert job["track_selection"] is None
    assert job["analysis"]["track_review_version"] == 1
    assert len(job["tracks"]) == 4
    for track in job["tracks"]:
        assert track["info"]["track_review"]["description"].startswith("Description")
    sub = next(t["info"] for t in job["tracks"] if t["track_id"] == 9)
    assert sub["language"] == "zh-Hant" and sub["hearing_impaired"] is False
    assert sub["mux_name"] == "Traditional Chinese PGS"
    assert sub["subtitle_source_path"].endswith("track-9.sup")


def test_user_names_and_flags_survive_cached_preparation_and_mux_plan(
    client, analyzed_job, monkeypatch, environment
):
    job, calls, classified = analyzed_job
    gate(job["id"], "WAITING_FOR_TRACK_SELECTION")
    name = "繁體中文 — Custom $(literal) label"
    response = client.post(
        f"/api/jobs/{job['id']}/tracks/selection",
        json={
            "audio_track_ids": [4],
            "subtitle_track_ids": [12, 9],
            "track_names": {"9": name, "4": "My audio"},
            "track_flags": {
                "9": {"hearing_impaired": True, "forced": True, "default": True},
                "4": {"commentary": True},
            },
        },
    )
    assert response.status_code == 200, response.text
    monkeypatch.setattr(
        stages, "classify_subtitle", lambda *a, **kw: pytest.fail("Cached detection must be reused")
    )
    monkeypatch.setattr(
        stages.Sup2supAdapter,
        "crop",
        lambda self, ctx, source, output, *a: output.write_bytes(source.read_bytes()),
    )
    task = next(t for t in response.json()["tasks"] if t["type"] == "prepare_tracks")
    execute(task["id"])
    result = client.get(f"/api/jobs/{job['id']}").json()
    assert result["state"] == "RUNNING_CRF_ANALYSIS", result["tasks"]
    assert len(calls) == 1  # Native audio, subtitles and timestamps were all extracted during analysis.
    assert "timestamps_v2" in calls[0]
    assert classified == [9, 12]
    prepared = result["analysis"]["prepared_tracks"]
    assert [t["track_id"] for t in prepared] == [4, 12, 9]
    assert track_name(prepared[-1]) == name and prepared[-1]["hearing_impaired"] is True
    assert prepared[-1]["subtitle_detection"]["hearing_impaired"] is False  # evidence preserved
    root = environment.workspace_root / job["id"]
    (root / "encoded.mkv").write_bytes(b"video")
    ctx = SimpleNamespace(
        settings=environment,
        workspace=root,
        source=lambda: environment.source_root / "Movie.mkv",
        job=SimpleNamespace(
            title=job["title"],
            year=job["year"],
            release_name=job["release_name"],
            analysis={**result["analysis"], "encoded_path": "encoded.mkv"},
            validation={"metrics": {"source_first_pts": 0, "encoded_first_pts": 0}},
        ),
    )
    command = stages.mux_command(ctx, root / "output.mkv")
    index = command.index("0:" + name)
    assert command[index - 1] == "--track-name"
    assert command[index + 1 : index + 7] == [
        "--default-track-flag",
        "0:1",
        "--forced-display-flag",
        "0:1",
        "--hearing-impaired-flag",
        "0:1",
    ]
    assert "0:My audio" in command


@pytest.mark.parametrize(
    "extra",
    [
        {"track_names": {"9": ""}},
        {"track_names": {"9": "bad\nname"}},
        {"track_names": {"9": "x" * 256}},
        {"track_names": {"12": "Not selected"}},
        {"track_flags": {"9": {"unknown": True}}},
        {"track_flags": {"9": {"forced": "false"}}},
        {"track_flags": {"9": {"forced": None}}},
        {"track_flags": {"12": {"forced": True}}},
    ],
)
def test_invalid_names_and_flags_are_rejected_before_starting_tasks(client, new_job, extra):
    gate(
        new_job["id"],
        "WAITING_FOR_TRACK_SELECTION",
        [{"track_id": 9, "kind": "subtitles", "info": {"codec_id": "S_HDMV/PGS"}}],
    )
    response = client.post(
        f"/api/jobs/{new_job['id']}/tracks/selection",
        json={"audio_track_ids": [], "subtitle_track_ids": [9], **extra},
    )
    assert response.status_code == 422
    assert client.get(f"/api/jobs/{new_job['id']}").json()["state"] == "WAITING_FOR_TRACK_SELECTION"


def test_unknown_flags_require_explicit_user_choice(client, new_job):
    track = fixture_tracks()[0]
    track.update(commentary=None, track_review={"schema_version": 1})
    gate(new_job["id"], "WAITING_FOR_TRACK_SELECTION", [{"track_id": 4, "kind": "audio", "info": track}])
    url = f"/api/jobs/{new_job['id']}/tracks/selection"
    body = {"audio_track_ids": [4], "subtitle_track_ids": []}
    response = client.post(url, json=body)
    assert response.status_code == 409 and "commentary" in response.text
    assert client.post(url, json={**body, "track_flags": {"4": {"commentary": False}}}).status_code == 200


def test_legacy_waiting_job_can_queue_initial_review_but_selected_job_cannot(client, new_job):
    url = f"/api/jobs/{new_job['id']}/tracks/analyze"
    assert client.post(url, json={}).status_code == 202
    gate(new_job["id"], "WAITING_FOR_TRACK_SELECTION")
    response = client.post(url, json={})
    assert response.status_code == 202
    assert response.json()["state"] == "WAITING_FOR_TRACK_SELECTION"
    assert client.post(url, json={}).status_code == 202
    gate(new_job["id"], "WAITING_FOR_TRACK_SELECTION")
    client.post(
        f"/api/jobs/{new_job['id']}/tracks/selection", json={"audio_track_ids": [], "subtitle_track_ids": []}
    )
    assert client.post(url, json={}).status_code == 409


def test_track_agent_stream_and_schema_validation(tmp_path, monkeypatch, environment):
    tracks = fixture_tracks()
    tracks[0]["audio_analysis"] = {"samples": [{"id": 1}]}
    (tmp_path / "inventory.json").write_text(json.dumps({"tracks": tracks}))
    events = []

    def invoke(prompt, images, schema, emit, check):
        assert "have NOT listened" in prompt and not images
        # Codex rejects the request before generation if any object property is optional.
        for object_schema in [schema, *schema["$defs"].values()]:
            assert set(object_schema["required"]) == set(object_schema["properties"])
            assert object_schema["additionalProperties"] is False
        assert {"type": "null"} in schema["properties"]["audio_comparison"]["anyOf"]
        emit({"type": "delta", "item_id": "reply", "text": "Reviewing"})
        return json.dumps(agent_answer(tracks)), "test-thread"

    monkeypatch.setattr("agent.track_agent.invoke", invoke)
    result = CodexTrackReviewer(on_event=events.append).review(tmp_path)
    assert len(result["tracks"]) == 4
    assert [e["type"] for e in events] == ["prompt", "delta", "complete"]
    invalid = TrackReviewResult.model_validate(
        {"tracks": result["tracks"][:-1], "audio_comparison": result["audio_comparison"]}
    )
    with pytest.raises(ValueError, match="exactly once"):
        validate_review(invalid, tracks)
    invalid = TrackReviewResult.model_validate(
        {"tracks": result["tracks"], "audio_comparison": result["audio_comparison"]}
    )
    invalid.tracks[0].evidence_sample_ids = [99]
    with pytest.raises(ValueError, match="not supplied"):
        validate_review(invalid, tracks)
    invalid.tracks[0].evidence_sample_ids = [1]
    invalid.tracks[1].flags.hearing_impaired = True
    with pytest.raises(ValueError, match="SDH"):
        validate_review(invalid, tracks)


def test_track_review_requires_explicit_audio_comparison():
    answer = agent_answer(fixture_tracks())
    del answer["audio_comparison"]
    with pytest.raises(ValueError, match="audio_comparison.*\n.*Field required"):
        TrackReviewResult.model_validate(answer)


@pytest.mark.parametrize("include_audio", [False, True])
def test_track_agent_null_comparison_only_without_audio(tmp_path, monkeypatch, environment, include_audio):
    tracks = fixture_tracks()
    tracks[0]["audio_analysis"] = {"samples": [{"id": 1}]}
    if not include_audio:
        tracks = [track for track in tracks if track["kind"] != "audio"]
    (tmp_path / "inventory.json").write_text(json.dumps({"tracks": tracks}))
    answer = {**agent_answer(tracks), "audio_comparison": None}
    monkeypatch.setattr("agent.track_agent.invoke", lambda *args: (json.dumps(answer), "test-thread"))
    reviewer = CodexTrackReviewer()
    if include_audio:
        with pytest.raises(ValueError, match="Include audio_comparison"):
            reviewer.review(tmp_path)
    else:
        assert reviewer.review(tmp_path)["audio_comparison"] is None


def test_track_review_endpoint_authentication_stream_and_lock(environment, monkeypatch):
    def review(self, root):
        assert root.name == "tracks"
        self.on_event({"type": "prompt", "invocation_id": "test", "text": "Inspect local evidence"})
        return {"tracks": []}

    monkeypatch.setattr(CodexTrackReviewer, "review", review)
    body = {"job_id": str(uuid4()), "task_id": str(uuid4())}
    headers = {"Authorization": f"Bearer {environment.agent_token}"}
    with TestClient(service.app) as client:
        assert client.post("/review-tracks", json=body).status_code == 401
        response = client.post("/review-tracks", json=body, headers=headers)
        assert response.status_code == 200
        messages = [json.loads(line) for line in response.text.splitlines()]
        assert messages[0]["type"] in ("prompt", "heartbeat", "status") and messages[-1]["type"] == "result"
        assert service.agent_queue.snapshot() == {"running": 0, "waiting": 0}


def test_subtitle_citations_are_validated_against_subtitle_cues():
    tracks = fixture_tracks()
    tracks[0]["audio_analysis"] = {"samples": [{"id": 1}]}
    subtitle = next(t for t in tracks if t["kind"] == "subtitles")
    subtitle["subtitle_detection"] = {"hearing_impaired": False, "evidence_cues": [{"id": 59}, {"id": 98}]}
    result = TrackReviewResult.model_validate(agent_answer(tracks))
    reviewed = next(t for t in result.tracks if t.track_id == subtitle["track_id"])
    reviewed.evidence_sample_ids = [59, 98]
    validate_review(result, tracks)
    reviewed.evidence_sample_ids = [1]  # An audio sample ID is not a subtitle cue ID.
    with pytest.raises(ValueError, match="subtitle cue IDs.*not supplied"):
        validate_review(result, tracks)
    reviewed.evidence_sample_ids = []
    result.tracks[0].evidence_sample_ids = [59]
    with pytest.raises(ValueError, match="audio sample IDs.*not supplied"):
        validate_review(result, tracks)


@pytest.mark.parametrize("recover", [True, False])
def test_track_agent_retries_invalid_citations_without_disabling_validation(
    tmp_path, monkeypatch, environment, recover
):
    tracks = fixture_tracks()
    tracks[0]["audio_analysis"] = {"samples": [{"id": 1}]}
    (tmp_path / "inventory.json").write_text(json.dumps({"tracks": tracks}))
    prompts, events = [], []

    def invoke(prompt, images, schema, emit, check):
        prompts.append(prompt)
        value = agent_answer(tracks)
        if not recover or len(prompts) == 1:
            value["tracks"][1]["evidence_sample_ids"] = [59, 98, 1101]
        return json.dumps(value), "thread"

    monkeypatch.setattr("agent.track_agent.invoke", invoke)
    reviewer = CodexTrackReviewer(on_event=events.append)
    if recover:
        assert reviewer.review(tmp_path)["tracks"][1]["evidence_sample_ids"] == []
        assert len(prompts) == 2
        assert events[-1]["type"] == "complete"
    else:
        with pytest.raises(ValueError, match="subtitle cue"):
            reviewer.review(tmp_path)
        assert len(prompts) == 3 and events[-1]["type"] == "error"
    assert "allowed IDs for this track: []" in prompts[1]
    assert len({e["invocation_id"] for e in events}) == len(prompts)
