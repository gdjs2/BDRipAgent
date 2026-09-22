import threading
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from types import SimpleNamespace

from sqlalchemy import select

from backend.app.services import advance, enqueue, reconcile
from shared.db import session
from shared.models import MovieJob, MovieTrack, SourceTrackChoices, TrackSelection
from shared.tracks import FLAG_NAMES
from tests import test_track_preparation as preparation_fixtures
from tests.conftest import gate
from tests.test_source_sharing import peer
from worker.pipeline.stages import HANDLERS
from worker.pipeline.track_analysis import save_analysis
from worker.tasks import execute

extraction_job = preparation_fixtures.extraction_job


def reviewed_tracks():
    return [
        {
            "track_id": track_id,
            "kind": kind,
            "codec_id": codec,
            "extractable": True,
            "language": "en",
            **dict.fromkeys(FLAG_NAMES, False),
            "track_review": {
                "schema_version": 1,
                "description": "Reviewed",
                "flags": dict.fromkeys(FLAG_NAMES, False),
            },
            **(
                {
                    "subtitle_detection": {
                        "schema_version": 2,
                        "language_code": "en",
                        "language_confident": True,
                        "hearing_impaired": False,
                    }
                }
                if kind == "subtitles"
                else {}
            ),
        }
        for track_id, kind, codec in (
            (4, "audio", "A_AC3"),
            (8, "audio", "A_AC3"),
            (9, "subtitles", "S_HDMV/PGS"),
            (12, "subtitles", "S_HDMV/PGS"),
        )
    ]


def ready(job_id, stage="ENCODING"):
    tracks = reviewed_tracks()
    gate(job_id, stage, [{"track_id": t["track_id"], "kind": t["kind"], "info": t} for t in tracks])
    with session() as db:
        job = db.get(MovieJob, job_id)
        job.analysis = {
            "video": {"track_id": 0, "duration": 600},
            "crop": {"left": 0, "right": 0, "top": 0, "bottom": 0},
            "tracks": tracks,
            "track_review_version": 1,
        }
        db.commit()


def read(client, job_id):
    return client.get(f"/api/jobs/{job_id}").json()


def save(client, job_id, body):
    return client.post(f"/api/jobs/{job_id}/tracks/selection", json=body)


CHOICES = {
    "audio_track_ids": [8, 4],
    "subtitle_track_ids": [12, 9],
    "track_names": {"8": "Main soundtrack", "9": "English dialogue"},
    "track_flags": {"8": {"default": True}, "9": {"forced": False, "hearing_impaired": True}},
    "shared_revision": 0,
}


def test_confirmed_choices_and_order_update_all_editable_encodes(client, new_job):
    other = peer(client)
    for job in (new_job, other):
        ready(job["id"])
    response = save(client, new_job["id"], CHOICES)
    assert response.status_code == 200, response.text
    for job in (new_job, other):
        current = read(client, job["id"])
        assert current["track_selection"]["audio_track_ids"] == [8, 4]
        assert current["track_selection"]["subtitle_track_ids"] == [12, 9]
        assert current["shared_track_selection"]["applied_revision"] == 1
        tracks = {t["track_id"]: t["info"] for t in current["tracks"]}
        assert tracks[8]["mux_name"] == "Main soundtrack" and tracks[8]["default"] is True
        assert tracks[9]["hearing_impaired"] is True and tracks[9]["forced"] is False
        assert tracks[9]["subtitle_detection"]["hearing_impaired"] is False
        stored = {t["track_id"]: t for t in current["analysis"]["tracks"]}
        assert stored[8]["mux_name"] == "Main soundtrack"
    updated = {
        "audio_track_ids": [4, 8],
        "subtitle_track_ids": [9],
        "track_names": {"9": "Shared revised name"},
        "shared_revision": 1,
    }
    assert save(client, other["id"], updated).status_code == 200
    assert read(client, new_job["id"])["track_selection"]["audio_track_ids"] == [4, 8]
    assert (
        next(t for t in read(client, new_job["id"])["tracks"] if t["track_id"] == 9)["info"]["name_override"]
        == "Shared revised name"
    )
    stale = save(client, new_job["id"], {**CHOICES, "shared_revision": 1})
    assert stale.status_code == 409 and "another encoding" in stale.text
    invalid = save(
        client, other["id"], {"audio_track_ids": [99], "subtitle_track_ids": [], "shared_revision": 2}
    )
    assert invalid.status_code == 409
    assert read(client, new_job["id"])["shared_track_selection"]["revision"] == 2


def test_pending_review_and_future_jobs_apply_choices_without_another_confirmation(
    client, new_job, monkeypatch
):
    ready(new_job["id"])
    waiting = peer(client)
    ready(waiting["id"], "WAITING_FOR_TRACK_SELECTION")
    with session() as db:
        job = db.get(MovieJob, waiting["id"])
        job.analysis = {**job.analysis, "track_review_version": 0}
        job.validation = {"valid": True}
        review = enqueue(db, job, stage="ANALYZING_TRACKS")
        review_id = review.id
        db.commit()
    assert save(client, new_job["id"], CHOICES).status_code == 200
    pending = read(client, waiting["id"])
    assert pending["track_selection"] is None and pending["shared_track_selection"]["pending"]

    def reviewed(ctx):
        result = {**ctx.job.analysis, "track_review_version": 1}
        return lambda db, job: save_analysis(db, job, result)

    monkeypatch.setitem(HANDLERS, "review_tracks", reviewed)
    execute(review_id)
    completed = read(client, waiting["id"])
    assert completed["state"] == "PREPARING_TRACKS", completed["tasks"]
    assert completed["track_selection"]["audio_track_ids"] == [8, 4]
    assert len([t for t in completed["tasks"] if t["type"] == "prepare_tracks"]) == 1
    future = peer(client)
    ready(future["id"])
    with session() as db:
        reconcile(db)
    assert read(client, future["id"])["track_selection"]["subtitle_track_ids"] == [12, 9]


def test_preparation_freezes_choices_and_changed_or_deleted_sources_are_isolated(
    client, new_job, environment
):
    ready(new_job["id"])
    frozen = peer(client)
    ready(frozen["id"])
    deleted = peer(client)
    ready(deleted["id"])
    with session() as db:
        from shared.models import now

        db.get(MovieJob, deleted["id"]).deleted_at = now()
        db.commit()
    assert save(client, new_job["id"], CHOICES).status_code == 200
    with session() as db:
        job = db.get(MovieJob, frozen["id"])
        job.state = "VALIDATING_ENCODE"
        job.validation = {"valid": True}
        advance(db, job)
        db.commit()
        assert job.state == "PREPARING_TRACKS"
    snapshot = read(client, frozen["id"])
    assert (
        save(
            client, new_job["id"], {"audio_track_ids": [], "subtitle_track_ids": [], "shared_revision": 1}
        ).status_code
        == 200
    )
    current = read(client, frozen["id"])
    assert (
        current["tracks"] == snapshot["tracks"] and current["track_selection"] == snapshot["track_selection"]
    )
    assert current["shared_track_selection"]["applied_revision"] == 1
    assert save(client, frozen["id"], {**CHOICES, "shared_revision": 2}).status_code == 409
    (environment.source_root / "Movie.mkv").write_bytes(b"a different source file")
    changed = peer(client)
    ready(changed["id"])
    with session() as db:
        reconcile(db)
        assert db.scalar(select(TrackSelection).where(TrackSelection.job_id == deleted["id"])) is None
    assert read(client, changed["id"])["track_selection"] is None
    assert read(client, changed["id"])["shared_track_selection"] is None


def test_legacy_confirmed_choices_are_adopted_and_inventory_mismatch_requires_review(client, new_job):
    ready(new_job["id"])
    with session() as db:
        db.add(TrackSelection(job_id=new_job["id"], audio_track_ids=[8, 4], subtitle_track_ids=[9]))
        db.commit()
    other = peer(client)
    ready(other["id"])
    with session() as db:
        reconcile(db)
    assert read(client, other["id"])["track_selection"]["audio_track_ids"] == [8, 4]
    bad = peer(client)
    ready(bad["id"], "WAITING_FOR_TRACK_SELECTION")
    with session() as db:
        job = db.get(MovieJob, bad["id"])
        job.validation = {"valid": True}
        row = db.scalar(select(MovieTrack).where(MovieTrack.job_id == job.id, MovieTrack.track_id == 4))
        row.info = {**row.info, "codec_id": "A_DTS"}
        db.commit()
        reconcile(db)
    current = read(client, bad["id"])
    assert current["state"] == "WAITING_FOR_TRACK_SELECTION" and current["track_selection"] is None
    assert "inventory" in current["shared_track_selection"]["error"]


def test_simultaneous_shared_edits_have_one_winner_and_consistent_snapshots(client, new_job):
    other = peer(client)
    for job in (new_job, other):
        ready(job["id"])
    barrier = threading.Barrier(2)

    def submit(job):
        barrier.wait(timeout=5)
        body = deepcopy(CHOICES)
        body["track_names"]["8"] = job["id"]
        return save(client, job["id"], body)

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(submit, (new_job, other)))
    assert sorted(r.status_code for r in responses) == [200, 409]
    winner = next(r.json()["id"] for r in responses if r.status_code == 200)
    with session() as db:
        assert len(db.scalars(select(SourceTrackChoices)).all()) == 1
    for job in (new_job, other):
        tracks = read(client, job["id"])["tracks"]
        assert next(t for t in tracks if t["track_id"] == 8)["info"]["mux_name"] == winner


def test_shared_choices_reach_preparation_and_exact_mux_order(client, extraction_job, environment):
    from worker.pipeline.stages import mux_command

    donor_id, calls, cropped, _ = extraction_job
    other = peer(client)
    ready(other["id"], "WAITING_FOR_TRACK_SELECTION")
    with session() as db:
        db.get(MovieJob, other["id"]).validation = {"valid": True}
        db.commit()
    assert save(client, donor_id, CHOICES).status_code == 200
    job = read(client, other["id"])
    task = next(t for t in job["tasks"] if t["type"] == "prepare_tracks")
    execute(task["id"])
    job = read(client, other["id"])
    assert job["state"] == "REMUXING", job["tasks"]
    prepared = job["analysis"]["prepared_tracks"]
    assert [t["track_id"] for t in prepared] == [8, 4, 12, 9]
    assert len(calls) == 1 and cropped == ["track-12.sup", "track-9.sup"]
    root = environment.workspace_root / other["id"]
    (root / "encoded.mkv").write_bytes(b"encoded video")
    ctx = SimpleNamespace(
        settings=environment,
        workspace=root,
        source=lambda: environment.source_root / "Movie.mkv",
        job=SimpleNamespace(
            title=job["title"],
            year=job["year"],
            release_name=job["release_name"],
            analysis={**job["analysis"], "encoded_path": "encoded.mkv"},
            validation={"metrics": {"source_first_pts": 0, "encoded_first_pts": 0}},
        ),
    )
    command = mux_command(ctx, root / "final.mkv")
    assert command[-2:] == ["--track-order", "0:0,1:0,2:0,3:0,4:0"]
    assert command.index("0:Main soundtrack") < command.index("0:English dialogue")
    paths = [str(root / track["path"]) for track in prepared]
    assert sorted(paths, key=command.index) == paths
    i = command.index("0:English dialogue")
    assert command[i + 1 : i + 7] == [
        "--default-track-flag",
        "0:0",
        "--forced-display-flag",
        "0:0",
        "--hearing-impaired-flag",
        "0:1",
    ]


def test_legacy_adoption_uses_latest_save_not_first_confirmation(client, new_job):
    from datetime import timedelta

    from shared.models import Event, now

    other = peer(client)
    for job in (new_job, other):
        ready(job["id"])
    with session() as db:
        db.add(
            TrackSelection(
                job_id=new_job["id"],
                audio_track_ids=[8, 4],
                subtitle_track_ids=[],
                created_at=now() - timedelta(days=2),
            )
        )
        db.add(
            TrackSelection(
                job_id=other["id"],
                audio_track_ids=[4],
                subtitle_track_ids=[],
                created_at=now() - timedelta(days=1),
            )
        )
        db.add(Event(job_id=new_job["id"], type="tracks_selected", data={}, created_at=now()))
        db.commit()
        reconcile(db)
    for job in (new_job, other):
        current = read(client, job["id"])
        assert current["track_selection"]["audio_track_ids"] == [8, 4]
        assert current["shared_track_selection"]["source_job_id"] == new_job["id"]
