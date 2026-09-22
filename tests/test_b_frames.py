from pathlib import Path
from types import SimpleNamespace

import av
import pytest

from agent.schemas import Selection, validate_selection
from shared.config import ScreenshotPolicy
from shared.db import session
from shared.models import MovieJob, Screenshot
from shared.screenshot_rules import is_b_frame_pair
from tests.test_agent import fixture_selection
from tests.test_media import videos  # noqa: F401
from worker.pipeline import screenshots


@pytest.mark.parametrize("source,encoded,verified", [("I", "B", True), ("B", "P", True), ("B", "B", False)])
def test_agent_rejects_any_pair_without_both_verified_b_frames(source, encoded, verified):
    candidates, selected = fixture_selection()
    candidates[0].update(picture_type=source, encoded_picture_type=encoded, b_frames_verified=verified)
    errors = validate_selection(
        Selection(selected=selected),
        candidates,
        ScreenshotPolicy(count=4, representative=2, encode_challenging=2),
        1000,
    )
    assert any("B-frames" in error for error in errors)


@pytest.mark.parametrize("eligible", [False, True])
def test_manual_replacement_requires_verified_b_frame_pair(client, new_job, eligible):
    with session() as db:
        job = db.get(MovieJob, new_job["id"])
        job.state = "COMPLETE"
        job.validation = {"metrics": {"source_duration": 500}}
        job.screenshot_policy = {**job.screenshot_policy, "min_timeline_bins": 1}
        old = Screenshot(
            job_id=job.id,
            candidate_id=1,
            selected=True,
            info={
                "source_frame_number": 100,
                "timeline_seconds": 100,
                "scene_id": 1,
            },
        )
        new = Screenshot(
            job_id=job.id,
            candidate_id=2,
            info={
                "source_frame_number": 200,
                "timeline_seconds": 200,
                "scene_id": 2,
                "picture_type": "B",
                "encoded_picture_type": "B" if eligible else "P",
                "b_frames_verified": True,
            },
        )
        db.add_all([old, new])
        db.flush()
        old_id = old.id
        db.commit()
    response = client.post(
        f"/api/jobs/{new_job['id']}/screenshots/{old_id}/replace", json={"candidate_id": 2}
    )
    assert response.status_code == (200 if eligible else 409), response.text


def test_real_pair_refinement_preserves_exact_frame_numbers_pts_and_b_types(videos):  # noqa: F811
    source, encoded, video, _ = videos
    workspace = source.parent
    with av.open(str(source)) as container:
        source_frames = list(container.decode(video=0))
    with av.open(str(encoded)) as container:
        encoded_frames = list(container.decode(video=0))

    def output(category, name):
        path = workspace / category / name
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    ctx = SimpleNamespace(
        workspace=workspace,
        source=lambda: source,
        log=lambda message: None,
        check=lambda: None,
        progress=lambda *args, **kwargs: None,
        output=output,
        job=SimpleNamespace(
            analysis={
                "video": video,
                "crop": {"left": 0, "right": 0, "top": 16, "bottom": 16},
                "encoded_path": encoded.name,
            },
            validation={"metrics": {"source_first_pts": 0, "encoded_first_pts": 0}},
        ),
    )
    candidates = [
        {
            "candidate_id": i + 1,
            "source_frame_number": frame_number,
            "scene_id": i,
            "source_pts_seconds": float(
                source_frames[frame_number].pts * source_frames[frame_number].time_base
            ),
            "picture_type": "unknown",  # CUDA does not expose this metadata.
        }
        for i, frame_number in enumerate((0, 12, 24, 36, 48))
    ]
    pairs = screenshots.verify_b_frame_candidates(ctx, candidates)
    assert pairs
    assert any(
        p["source_frame_number"] != candidates[p["candidate_id"] - 1]["source_frame_number"] for p in pairs
    )
    for pair in pairs:
        assert is_b_frame_pair(pair)
        frame = source_frames[pair["source_frame_number"]]
        assert float(frame.pts * frame.time_base) == pair["source_pts_seconds"]
        assert int(frame.pict_type) == 3
        matched = [
            f for f in encoded_frames if abs(float(f.pts * f.time_base) - pair["source_pts_seconds"]) < 0.003
        ]
        assert len(matched) == 1 and int(matched[0].pict_type) == 3
        assert (workspace / pair["path"]).is_file()


@pytest.mark.parametrize("source_type,encoded_type", [(1, 3), (3, 2)])
def test_render_rechecks_actual_decoded_types_before_writing(
    client, new_job, environment, monkeypatch, source_type, encoded_type
):
    workspace = environment.workspace_root / new_job["id"]
    encoded = workspace / "encoded.mkv"
    encoded.touch()
    with session() as db:
        db.add(
            Screenshot(
                job_id=new_job["id"],
                candidate_id=1,
                selected=True,
                info={
                    "source_pts_seconds": 1.0,
                    "picture_type": "B",
                    "encoded_picture_type": "B",
                    "b_frames_verified": True,
                },
            )
        )
        db.commit()
    ctx = SimpleNamespace(
        workspace=workspace,
        source=lambda: Path("source.mkv"),
        check=lambda: None,
        output=lambda *args: pytest.fail("A non-B pair must never be written"),
        progress=lambda *args, **kwargs: None,
        job=SimpleNamespace(
            id=new_job["id"],
            screenshot_policy={"count": 1},
            analysis={"encoded_path": encoded.name, "crop": {"left": 0, "right": 0, "top": 0, "bottom": 0}},
            validation={"metrics": {"source_first_pts": 0, "encoded_first_pts": 0}},
        ),
    )
    monkeypatch.setattr(
        screenshots,
        "extract_at",
        lambda path, *args: SimpleNamespace(
            pict_type=encoded_type if path == encoded else source_type,
        ),
    )
    with pytest.raises(ValueError, match="not a B-frame in both"):
        screenshots.render(ctx)
