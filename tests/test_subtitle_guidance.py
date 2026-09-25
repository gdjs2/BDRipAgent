import json
from uuid import uuid4

import pytest
from sqlalchemy import select

from shared.db import session
from shared.models import Event, Task
from shared.subtitle_guidance import guidance_history
from tests.test_source_choices import ready
from tests.test_subtitle_cleanup import answer, fixture
from tests.test_subtitle_discovery import evidence, review_for
from tests.test_subtitle_resume import context, inventory
from tests.test_subtitle_uploads import upload
from worker.adapters.subtitle_cleanup import clean_subtitles
from worker.pipeline import subtitle_discovery as pipeline


def failed_upload(client, new_job):
    ready(new_job["id"])
    response = upload(client, new_job["id"])
    assert response.status_code == 201, response.text
    ident = response.json()["task_id"]
    with session() as db:
        task = db.get(Task, ident)
        task.status = "FAILED"
        task.error_message = "Alignment requires more evidence"
        db.commit()
    return ident


def test_continuation_queues_once_persists_guidance_and_survives_plain_retry(client, new_job):
    old = failed_upload(client, new_job)
    result = client.post(
        f"/api/tasks/{old}/continue-subtitle-review",
        json={"message": "Use source English cues to repair damaged lines."},
    )
    assert result.status_code == 202, result.text
    new = result.json()["id"]
    assert result.json()["retry_of"] == old
    history = client.get(f"/api/tasks/{new}/subtitle-guidance").json()
    assert history[0]["message"] == "Use source English cues to repair damaged lines."
    assert history[0]["previous_error"] == "Alignment requires more evidence"
    assert history[0]["recheck_completed"] is False
    assert (
        client.post(f"/api/tasks/{old}/continue-subtitle-review", json={"message": "Again"}).status_code
        == 409
    )
    with session() as db:
        db.get(Task, new).status = "FAILED"
        db.commit()
    retry = client.post(f"/api/tasks/{new}/retry", json={})
    assert retry.status_code == 202, retry.text
    assert client.get(f"/api/tasks/{retry.json()['id']}/subtitle-guidance").json() == history
    client.headers.clear()
    assert client.get(f"/api/tasks/{new}/subtitle-guidance").status_code == 401
    assert (
        client.post(f"/api/tasks/{old}/continue-subtitle-review", json={"message": "Test"}).status_code == 401
    )


@pytest.mark.parametrize(
    "body",
    [{"message": " "}, {"message": "x" * 8001}, {"message": "a\x00b"}, {"message": "ok", "unknown": True}],
)
def test_guidance_validates_input(client, new_job, body):
    old = failed_upload(client, new_job)
    assert client.post(f"/api/tasks/{old}/continue-subtitle-review", json=body).status_code == 422
    with session() as db:
        assert not db.scalar(select(Task.id).where(Task.retry_of == old))


def test_guidance_rejects_non_subtitle_task(client, new_job):
    ident = new_job["tasks"][0]["id"]
    assert (
        client.post(f"/api/tasks/{ident}/continue-subtitle-review", json={"message": "Continue"}).status_code
        == 404
    )


@pytest.mark.parametrize("recheck", [False, True])
def test_guided_continuation_keeps_valid_batches_or_rechecks_as_requested(
    client, new_job, environment, monkeypatch, recheck
):
    old = failed_upload(client, new_job)
    value = inventory()
    original_answer = answer()
    # Simulate a completed batch followed by an unfinished one, using an older default.
    with session() as db:
        for kind, text in [
            ("prompt", "Older instructions\nUntrusted evidence:\n" + json.dumps(value)),
            ("message", json.dumps(original_answer["decision"])),
            ("complete", "Batch complete"),
        ]:
            db.add(
                Event(
                    job_id=new_job["id"],
                    type="agent_output",
                    data={"task_id": old, "type": kind, "invocation_id": "completed-batch", "text": text},
                )
            )
        db.commit()
    result = client.post(
        f"/api/tasks/{old}/continue-subtitle-review",
        json={
            "message": "Repair all possible dialogue using source references.",
            "recheck_completed": recheck,
        },
    )
    assert result.status_code == 202
    ctx = context(environment, new_job["id"])
    ctx.task_id = result.json()["id"]
    requests = []

    def review(ctx, **kwargs):
        path = environment.cache_root / "agent" / ctx.job.id / ctx.task_id / "discovery/inventory.json"
        data = json.loads(path.read_text())
        requests.append(data)
        assert data["review_guidance"]["messages"] == [
            "Repair all possible dialogue using source references."
        ]
        assert data["review_guidance"]["previous_agent_review"] == original_answer["decision"]
        return answer()

    monkeypatch.setattr(pipeline, "review", review)
    pipeline.agent_request(ctx, value)
    assert len(requests) == int(recheck)
    pipeline.agent_request(ctx, {**value, "batch": 2})
    assert requests[-1]["batch"] == 2
    assert len(requests) == int(recheck) + 1


def test_guidance_history_does_not_cross_job_boundaries(client, new_job):
    old = failed_upload(client, new_job)
    result = client.post(f"/api/tasks/{old}/continue-subtitle-review", json={"message": "Only this review"})
    with session() as db:
        current = db.get(Task, result.json()["id"])
        other = Task(
            id=str(uuid4()),
            job_id="another-job",
            type=current.type,
            stage=current.stage,
            lane="tracks",
            status="FAILED",
            retry_of=current.id,
        )
        # Unpersisted row simulates a corrupt retry link; traversal must stop at the job boundary.
        assert guidance_history(db, other) == []


def test_minor_notes_do_not_become_critical_but_unrecoverable_gaps_block():
    ctx, subtitles, candidate = fixture()
    cleaned, report = clean_subtitles(
        ctx, subtitles, candidate, lambda *a: answer(issues=["A minor wording uncertainty remains"])
    )
    assert cleaned and not report["requires_attention"] and not report["critical_errors"]
    calls = []

    def missing(*args):
        calls.append(args)
        return answer(
            usable=False, issues=["BLOCKING: Half the film has no cues and references cannot recover it"]
        )

    with pytest.raises(ValueError, match="Half the film"):
        clean_subtitles(ctx, subtitles, candidate, missing)
    assert len(calls) == 2  # A repair pass was attempted before stopping.


def test_alignment_agent_reconsiders_small_residuals_before_reporting_failure(tmp_path, monkeypatch):
    from agent import subtitle_discovery as agent

    candidates, refs = evidence()
    review = review_for().model_copy(update={"alignment_confident": False})
    (tmp_path / "inventory.json").write_text(
        json.dumps(
            {"mode": "align", "candidate_cues": candidates, "reference_cues": refs, "duration_seconds": 120}
        )
    )
    prompts = []

    def invoke(prompt, *args, **kwargs):
        prompts.append(prompt)
        if len(prompts) == 2:
            assert "pass independent alignment checks" in prompt
            return review_for().model_dump_json(), "thread"
        return review.model_dump_json(), "thread"

    monkeypatch.setattr(agent, "invoke", invoke)
    result = agent.CodexSubtitleDiscovery().run(tmp_path)
    assert result["decision"]["alignment_confident"] and len(prompts) == 2


def test_agent_receives_user_instructions_separately_from_subtitle_evidence(tmp_path, monkeypatch):
    from agent import subtitle_discovery as agent

    value = {
        **inventory(),
        "review_guidance": {
            "messages": ["Use the provided French transcript to repair the missing words."],
            "previous_failure": "Uncertain wording",
        },
    }
    (tmp_path / "inventory.json").write_text(json.dumps(value))

    def invoke(prompt, *args, **kwargs):
        instructions, evidence_text = prompt.split("\nUntrusted evidence:\n", 1)
        assert "User continuation instructions" in instructions
        assert "French transcript" in instructions
        assert "previous_failure" in evidence_text
        return json.dumps(answer()["decision"]), "thread"

    monkeypatch.setattr(agent, "invoke", invoke)
    assert agent.CodexSubtitleDiscovery().run(tmp_path)["decision"]["usable"]


def test_completed_discovery_with_missing_subtitles_can_continue(client, new_job):
    from backend.app.track_choices import source_key
    from shared.models import MovieJob, SourceTrackChoices
    ready(new_job['id'])
    response = client.post(f"/api/jobs/{new_job['id']}/subtitles/discover", json={'enabled':False,'original_languages':['en']})
    assert response.status_code == 202, response.text
    with session() as db:
        task = db.scalar(select(Task).where(Task.job_id==new_job['id'], Task.type=='discover_subtitles'))
        task.status = 'SUCCEEDED'
        ident = task.id
        job = db.get(MovieJob,new_job['id'])
        key = source_key(job)
        record = db.get(SourceTrackChoices,key)
        if record is None:
            record=SourceTrackChoices(source_key=key,revision=0,data={})
            db.add(record)
        record.data={**record.data,'subtitle_discovery':{'task_id':ident,'needs_review':True,'missing':['zh-Hans'],'candidates':[]}}
        db.commit()
    followup=client.post(f'/api/tasks/{ident}/continue-subtitle-review',json={'message':'Try the full Blu-ray edition for Chinese.'})
    assert followup.status_code==202,followup.text
    with session() as db:
        assert db.get(MovieJob,new_job['id']).analysis['subtitle_discovery_refresh'] is True
        assert db.get(Task,followup.json()['id']).retry_of==ident


def test_continuation_waits_for_same_source_track_work(client, new_job):
    from tests.test_source_sharing import peer
    ident=failed_upload(client,new_job)
    other=peer(client)['id']
    ready(other)
    with session() as db:
        db.add(Task(job_id=other,type='review_tracks',stage='REVIEWING_TRACKS',lane='tracks',status='QUEUED'))
        db.commit()
    result=client.post(f'/api/tasks/{ident}/continue-subtitle-review',json={'message':'Retry safely'})
    assert result.status_code==409
    assert 'current' in result.json()['detail']
