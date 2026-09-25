from types import SimpleNamespace

from PIL import Image
from sqlalchemy import select

from backend.app.screenshot_sharing import ensure_shared_screenshots
from backend.app.track_choices import source_key
from shared.db import session
from shared.models import MovieJob, Screenshot, Task, now
from shared.paths import write_json
from tests.conftest import gate
from tests.test_screenshot_sampling import make_context, sampling_source  # noqa: F401
from worker.adapters import screenshot_pool
from worker.pipeline import screenshots
from worker.pipeline.validation import timeline


def test_same_source_samples_once_and_reuses_additional_batches(tmp_path, sampling_source, monkeypatch):  # noqa: F811
    points = timeline(sampling_source)
    contexts = []
    for name in ("x264", "x265", "later"):
        ctx = make_context(tmp_path / name, sampling_source, points)
        ctx.settings = SimpleNamespace(cache_root=tmp_path / "cache")
        ctx.job.source_path = "Movie.mkv"
        ctx.job.source_size = sampling_source.stat().st_size
        ctx.job.source_mtime_ns = str(sampling_source.stat().st_mtime_ns)
        contexts.append(ctx)
    config = {**screenshots.behavior(), "candidate_count": 8, "duplicate_hash_distance": 0}
    monkeypatch.setattr(screenshots, "behavior", lambda: config)
    calls = []
    original = screenshots.scan_candidates

    def scan(*args, **kwargs):
        calls.append(kwargs)
        return original(*args, **kwargs)

    monkeypatch.setattr(screenshots, "scan_candidates", scan)
    first, _, _ = screenshots.sample_candidates(contexts[0])
    second, _, _ = screenshots.sample_candidates(contexts[1])
    assert len(calls) == 1
    assert len(first["candidates"]) == len(second["candidates"]) == 8
    assert [c["source_frame_number"] for c in first["candidates"]] == [
        c["source_frame_number"] for c in second["candidates"]
    ]
    assert all(c["b_frames_verified"] for c in second["candidates"])
    for ctx in contexts[:2]:
        ctx.job.analysis["screenshot_append"] = {"count": 3, "batch_id": "batch-one"}
    more, _, _ = screenshots.sample_candidates(
        contexts[0], existing=first["candidates"], target=3, sampling_round=1
    )
    shared, _, _ = screenshots.sample_candidates(
        contexts[1], existing=second["candidates"], target=3, sampling_round=1
    )
    assert len(calls) == 2
    assert len(more["candidates"]) == len(shared["candidates"]) == 11
    later, _, _ = screenshots.sample_candidates(contexts[2])
    assert len(calls) == 2 and len(later["candidates"]) == 11
    assert first["candidates"] == more["candidates"][:8]
    # Changing the source identity never inherits the earlier movie's pool.
    contexts[2].job.source_mtime_ns = "different-version"
    screenshots.sample_candidates(contexts[2])
    assert len(calls) == 3


def test_shared_pool_rechecks_each_encode_and_keeps_failed_pairs_out(tmp_path, monkeypatch, new_job):
    root = tmp_path / "job"
    root.mkdir()
    ctx = SimpleNamespace(
        settings=SimpleNamespace(cache_root=tmp_path / "cache"),
        workspace=root,
        job=SimpleNamespace(source_path="a.mkv", source_size=1, source_mtime_ns="1", analysis={}),
        source=lambda: None,
        check=lambda: None,
        progress=lambda *a, **k: None,
        log=lambda *a: None,
        output=lambda category, filename: output(root, category, filename),
        artifact=lambda path, *a, **k: str(path.relative_to(root)),
    )
    ctx.job.id = new_job["id"]
    with session() as db:
        db.add(Screenshot(job_id=ctx.job.id, candidate_id=500, selected=True, info={}))
        db.commit()
    pool = screenshot_pool.pool_root(ctx.settings, ctx.job)
    write_json(
        pool / "pool.json",
        {
            "revision": 1,
            "batches": {"initial": [1, 2]},
            "candidates": [
                {"candidate_id": i, "source_frame_number": i * 100, "b_frames_verified": True} for i in (1, 2)
            ],
        },
    )
    calls = []

    def verify(ctx, candidates, **kwargs):
        calls.append(kwargs)
        c = candidates[0]
        if c["shared_source_id"] == 1:
            return []
        path = ctx.output("frames", "2.png")
        Image.new("RGB", (32, 32)).save(path)
        return [{**c, "path": str(path.relative_to(root))}]

    monkeypatch.setattr(screenshots, "verify_b_frame_candidates", verify)
    monkeypatch.setattr(screenshots, "contact_sheets", lambda *a: [])
    monkeypatch.setattr("worker.adapters.frame_index.source_frame_index", lambda ctx: (None, "pts.npy"))
    result, _, _ = screenshot_pool.shared_sample(ctx, lambda *a, **k: None, sync_only=True)
    assert len(result["candidates"]) == 1
    assert result["candidates"][0]["shared_source_id"] == 2
    assert result["candidates"][0]["candidate_id"] > 500
    assert all(call["exact"] for call in calls)
    assert result["decoder"]["shared_seen"] == [1, 2]


def output(root, category, filename):
    path = root / category / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def test_sibling_refresh_is_queued_once_and_preserves_release_state(
    client, new_job, environment, monkeypatch
):
    job_id = new_job["id"]
    gate(job_id, "COMPLETE")
    with session() as db:
        job = db.get(MovieJob, job_id)
        job.analysis = {"candidate_index": "candidates.json"}
        job.completed_at = now()
        completed = job.completed_at
        original = {
            "candidate_id": 1,
            "source_frame_number": 10,
            "recommendation_rank": 1,
            "comparisons": {"a": "b"},
        }
        db.add(Screenshot(job_id=job_id, candidate_id=1, info=original, shortlisted=True, selected=True))
        write_json(environment.cache_root / "screenshots" / source_key(job) / "pool.json", {"revision": 1})
        db.commit()
    with session() as db:
        ensure_shared_screenshots(db)
        ensure_shared_screenshots(db)
        db.commit()
        tasks = list(db.scalars(select(Task).where(Task.type == "sync_screenshots")))
        assert len(tasks) == 1 and tasks[0].lane == "screenshots"
        assert db.get(MovieJob, job_id).state == "COMPLETE"
        from backend.app import queue

        assert tasks[0].id in {t.id for t in queue.pending(db)}
    new = {"candidate_id": 2, "source_frame_number": 200, "path": "new.png"}
    root = environment.workspace_root / job_id
    Image.new("RGB", (32, 32)).save(root / "new.png")

    def shared(*args, **kwargs):
        assert kwargs["sync_only"]
        return (
            {"candidates": [original, new], "decoder": {"shared_revision": 1, "shared_seen": [1, 2]}},
            "shared.json",
            "pts.npy",
        )

    monkeypatch.setattr(screenshot_pool, "shared_sample", shared)
    from worker.tasks import execute

    execute(tasks[0].id)
    with session() as db:
        task = db.get(Task, tasks[0].id)
        assert task.status == "SUCCEEDED", task.error_message
        job = db.get(MovieJob, job_id)
        assert job.state == "COMPLETE" and job.completed_at.replace(tzinfo=completed.tzinfo) == completed
        old = db.scalar(select(Screenshot).where(Screenshot.job_id == job_id, Screenshot.candidate_id == 1))
        assert old.info == original and old.selected
        added = db.scalar(select(Screenshot).where(Screenshot.job_id == job_id, Screenshot.candidate_id == 2))
        assert added.shortlisted and not added.selected and added.info["recommendation_rank"] is None
