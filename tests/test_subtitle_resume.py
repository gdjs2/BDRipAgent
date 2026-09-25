import json
from types import SimpleNamespace
from uuid import uuid4

import pysubs2
import pytest

from shared.db import session
from shared.models import Event, Task
from shared.paths import write_json
from tests.test_subtitle_cleanup import answer
from tests.test_subtitle_discovery import evidence, review_for
from worker.adapters import subtitle_resume
from worker.adapters.subtitle_cleanup import clean_subtitles
from worker.pipeline import subtitle_discovery as pipeline


def context(environment, job_id=None):
    job_id = job_id or str(uuid4())
    workspace = environment.workspace_root / job_id
    workspace.mkdir(exist_ok=True)
    messages = []
    return SimpleNamespace(
        workspace=workspace,
        settings=environment,
        task_id=str(uuid4()),
        job=SimpleNamespace(id=job_id, title="Movie", year=2026, imdb_id=None, analysis={}),
        check=lambda: None,
        progress=lambda *a, **kw: messages.append(kw),
        log=messages.append,
        messages=messages,
    )


def inventory():
    return {
        "mode": "clean",
        "requested_language": "en",
        "batch": 1,
        "total_batches": 1,
        "cues": [{"id": 1, "text": "Hello", "start_ms": 0, "end_ms": 1000}],
    }


def test_retry_reuses_completed_batch_and_repair_pass(environment, monkeypatch):
    ctx = context(environment)
    candidate = SimpleNamespace(language="en")
    subtitles = pysubs2.SSAFile()
    subtitles.events = [
        pysubs2.SSAEvent(start=i * 1000, end=i * 1000 + 800, text=f"Line {i}") for i in range(201)
    ]
    calls = []
    interrupted = [False]

    def review(ctx, **kwargs):
        path = environment.cache_root / "agent" / ctx.job.id / ctx.task_id / "discovery/inventory.json"
        value = json.loads(path.read_text())
        calls.append((value["batch"], bool(value.get("previous_review"))))
        if value["batch"] == 2 and not interrupted[0]:
            interrupted[0] = True
            raise TimeoutError("Interrupted next batch")
        if value["batch"] == 1:
            if not value.get("previous_review"):
                return answer(usable=False, issues=["Repair the opening line"])
            return answer(
                edits=[
                    {
                        "cue_id": 1,
                        "original_text": "Line 0",
                        "replacement_text": "Opening line",
                        "action": "correct_text",
                        "reason": "Matched reference",
                    }
                ]
            )
        return answer()

    monkeypatch.setattr(pipeline, "review", review)
    with pytest.raises(TimeoutError):
        clean_subtitles(ctx, subtitles, candidate, pipeline.agent_request)
    assert calls == [(1, False), (1, True), (2, False)]
    ctx.task_id = str(uuid4())
    ctx._subtitle_resume_seeded = False
    cleaned, report = clean_subtitles(ctx, subtitles, candidate, pipeline.agent_request)
    assert calls == [(1, False), (1, True), (2, False), (2, False), (3, False)]
    assert cleaned[0].plaintext == "Opening line" and len(cleaned) == 201
    assert report["edited_cues"] == 1 and report["reviews"][0]["initial_review"]
    assert any(
        isinstance(message, dict) and "Resuming saved review" in message.get("phase", "")
        for message in ctx.messages
    )


def test_resume_invalidates_changed_cues_references_and_policy(environment, monkeypatch):
    ctx, value = context(environment), inventory()
    subtitle_resume.save(ctx, value, answer())
    assert subtitle_resume.load(ctx, value)
    assert subtitle_resume.load(ctx, {**value, "source_reference_cues": [{"text": "Different"}]}) is None
    assert subtitle_resume.load(ctx, {**value, "cues": [{**value["cues"][0], "text": "Changed"}]}) is None
    monkeypatch.setattr(subtitle_resume, "prompt_text", lambda mode: "New repair instructions")
    assert subtitle_resume.load(ctx, value) is None


def test_corrupt_or_invalid_checkpoint_is_not_reused(environment):
    ctx, value = context(environment), inventory()
    key = subtitle_resume.signature(value)
    path = subtitle_resume.checkpoint_path(ctx, key)
    write_json(
        path,
        {
            "signature": key,
            "answer": answer(
                edits=[
                    {
                        "cue_id": 99,
                        "original_text": "Invented",
                        "replacement_text": "Wrong",
                        "action": "correct_text",
                        "reason": "Wrong cue",
                    }
                ]
            ),
        },
    )
    assert subtitle_resume.load(ctx, value) is None
    path.write_text('{"unfinished":')
    assert subtitle_resume.load(ctx, value) is None


def test_alignment_response_can_resume_after_interruption_before_pgs(environment):
    ctx = context(environment)
    cues, refs = evidence()
    value = {
        "mode": "align",
        "candidate": {"language": "zh-Hans"},
        "candidate_cues": cues,
        "reference_cues": refs,
        "duration_seconds": 120,
    }
    subtitle_resume.save(ctx, value, {"decision": review_for().model_dump()})
    ctx.task_id = str(uuid4())
    assert subtitle_resume.load(ctx, value)["decision"]["scale"] == 1


def test_legacy_recovery_requires_complete_valid_response_in_retry_ancestry(client, new_job, environment):
    ctx, value = context(environment, new_job["id"]), inventory()
    original = str(uuid4())
    with session() as db:
        db.add(
            Task(
                id=original,
                job_id=ctx.job.id,
                type="review_uploaded_subtitle",
                lane="tracks",
                stage="REVIEWING_UPLOADED_SUBTITLE",
                status="FAILED",
            )
        )
        db.flush()
        db.add(
            Task(
                id=ctx.task_id,
                job_id=ctx.job.id,
                retry_of=original,
                type="review_uploaded_subtitle",
                lane="tracks",
                stage="REVIEWING_UPLOADED_SUBTITLE",
                status="QUEUED",
            )
        )
        for invocation, complete, result in [
            ("valid", True, answer()),
            ("partial", False, answer()),
            ("invalid", True, answer(language="ko")),
        ]:
            request = {**value, "batch": {"valid": 1, "partial": 2, "invalid": 3}[invocation]}
            events = [
                {
                    "type": "prompt",
                    "text": subtitle_resume.prompt_text("clean")
                    + "\nUntrusted evidence:\n"
                    + json.dumps(request),
                },
                {"type": "message", "text": json.dumps(result["decision"])},
            ]
            if complete:
                events.append({"type": "complete", "text": "Subtitle cleanup and repair complete"})
            for event in events:
                db.add(
                    Event(
                        job_id=ctx.job.id,
                        type="agent_output",
                        data={**event, "task_id": original, "invocation_id": invocation},
                    )
                )
        db.commit()
    subtitle_resume.recover_previous_attempts(ctx)
    assert subtitle_resume.load(ctx, value)
    assert subtitle_resume.load(ctx, {**value, "batch": 2}) is None
    assert subtitle_resume.load(ctx, {**value, "batch": 3}) is None
    assert any("Recovered 1 completed" in str(message) for message in ctx.messages)
    # A later policy change must not reseed old conversations as new-policy results.
    ctx._subtitle_resume_seeded = False
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(subtitle_resume, "prompt_text", lambda mode: "New instructions")
        subtitle_resume.recover_previous_attempts(ctx)
        assert subtitle_resume.load(ctx, value) is None


@pytest.mark.parametrize("repair", [True, False])
def test_agent_validates_split_dialogue_before_completion_and_checkpoint(
    environment, tmp_path, monkeypatch, repair
):
    from agent import subtitle_discovery as agent

    value = {
        **inventory(),
        "cues": [
            {"id": 1097, "text": "每天我都像死一样的受着折磨", "start_ms": 6187270, "end_ms": 6189330},
            {"id": 1098, "text": "自从你重回到我的生活中以来", "start_ms": 6189330, "end_ms": 6191400},
        ],
        "requested_language": "zh-Hans",
    }
    bad = answer(
        language="zh-Hans",
        edits=[
            dict(
                cue_id=1097,
                original_text=value["cues"][0]["text"],
                replacement_text="自从你重回我的生活，我每天都备受煎熬。",
                action="correct_text",
                reason="Merged dialogue",
            ),
            dict(
                cue_id=1098,
                original_text=value["cues"][1]["text"],
                replacement_text=None,
                action="remove_duplicate",
                reason="Merged into cue 1097",
            ),
        ],
    )
    good = answer(
        language="zh-Hans",
        edits=[
            dict(
                cue_id=1097,
                original_text=value["cues"][0]["text"],
                replacement_text="每天我都备受煎熬，",
                action="correct_text",
                reason="Repair grammar within original interval",
            ),
            dict(
                cue_id=1098,
                original_text=value["cues"][1]["text"],
                replacement_text="自从你重回我的生活以来。",
                action="correct_text",
                reason="Keep the second half in its original interval",
            ),
        ],
    )
    prompts, events = [], []

    def invoke(prompt, images, schema, emit, check, **kw):
        prompts.append(prompt)
        if len(prompts) > 1:
            assert "Cue 1098: remove_duplicate requires" in prompt
            assert "undo any related merge" in prompt
        return json.dumps((good if repair and len(prompts) > 1 else bad)["decision"]), "thread"

    monkeypatch.setattr(agent, "invoke", invoke)
    monkeypatch.setattr("agent.review.behavior", lambda: {"agent": {"selection_attempts": 2}})
    (tmp_path / "inventory.json").write_text(json.dumps(value))
    selector = agent.CodexSubtitleDiscovery(on_event=events.append)
    if not repair:
        with pytest.raises(ValueError, match="Cue 1098"):
            selector.run(tmp_path)
        assert not any(e["type"] == "complete" for e in events)
        return
    result = selector.run(tmp_path)
    assert len(prompts) == 2
    assert [e["type"] for e in events] == ["prompt", "error", "prompt", "complete"]
    assert result["decision"] == good["decision"]
    ctx = context(environment)
    subtitle_resume.save(ctx, value, result)
    assert subtitle_resume.load(ctx, value) == result
    subtitles = pysubs2.SSAFile()
    subtitles.events = [
        pysubs2.SSAEvent(start=c["start_ms"], end=c["end_ms"], text=c["text"]) for c in value["cues"]
    ]
    # The real cleanup assigns consecutive IDs starting at 1.
    final = {
        **result,
        "decision": {
            **result["decision"],
            "edits": [{**edit, "cue_id": index} for index, edit in enumerate(result["decision"]["edits"], 1)],
        },
    }
    cleaned, _ = clean_subtitles(ctx, subtitles, SimpleNamespace(language="zh-Hans"), lambda *args: final)
    assert len(cleaned) == 2
    assert [(c.start, c.end) for c in cleaned] == [(c.start, c.end) for c in subtitles]
    assert cleaned[1].plaintext == "自从你重回我的生活以来。"


def test_batch_schema_excludes_context_and_retry_identifies_invalid_cues(environment, tmp_path, monkeypatch):
    from agent import subtitle_discovery as agent

    value = inventory()
    value["total_batches"] = 13
    value["context_cues"] = [
        {"id": 101, "text": "Read-only neighbor", "start_ms": 1000, "end_ms": 2000},
        {"id": 102, "text": "Another neighbor", "start_ms": 2000, "end_ms": 3000},
    ]
    (tmp_path / "inventory.json").write_text(json.dumps(value))
    prompts, events = [], []

    def invoke(prompt, images, schema, emit, check, **kwargs):
        prompts.append(prompt)
        assert schema["$defs"]["SubtitleEdit"]["properties"]["cue_id"]["enum"] == [1]
        assert "Other batches will be reviewed separately" in prompt
        if len(prompts) == 1:
            return json.dumps(
                answer(
                    edits=[
                        dict(
                            cue_id=c["id"],
                            original_text=c["text"],
                            replacement_text="Changed context",
                            action="correct_text",
                            reason="Mistaken context edit",
                        )
                        for c in value["context_cues"]
                    ]
                )["decision"]
            ), "thread"
        assert "non-editable cue IDs [101, 102]" in prompt
        assert "only editable cue IDs in this batch are [1]" in prompt
        return json.dumps(answer()["decision"]), "thread"

    monkeypatch.setattr(agent, "invoke", invoke)
    result = agent.CodexSubtitleDiscovery(on_event=events.append).run(tmp_path)
    assert result["decision"]["usable"]
    assert len(prompts) == 2
    assert events[-1]["text"] == "Subtitle cleanup batch 1/13 complete"
