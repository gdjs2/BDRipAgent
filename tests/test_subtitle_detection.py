import json
from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from agent import main as service
from agent.subtitle_agent import CodexSubtitleClassifier
from shared.naming import track_name
from shared.subtitles import SubtitleDecision, detected_language, validate_evidence
from worker.adapters import subtitles
from worker.adapters.subtitle_rules import decide


def samples(script="simplified", sdh=True, cantonese=False):
    texts = [
        "我们已经见过这个国家的风景",
        "他们认为这件事情应该这样做",
        "请让我听听你们的话",
        "这里有很多东西可以学习",
        "这个问题还没有发现答案",
        "欢迎来到这个美丽的世界",
    ]
    if script == "traditional":
        texts = [
            "我們已經見過這個國家的風景",
            "他們認為這件事情應該這樣做",
            "請讓我聽聽你們的話",
            "這裡有很多東西可以學習",
            "這個問題還沒有發現答案",
            "歡迎來到這個美麗的世界",
        ]
    suffixes = ["唔係我做嘅", "佢哋喺邊度", "我冇咗啲嘢", "你想去邊度", "大家一齊行", "好啊"]
    return [
        {
            "id": i + 1,
            "seconds": i * 500,
            "image": f"cue-{i}.png",
            "text": t + (" [脚步声]" if sdh else "") + (suffixes[i] if cantonese else ""),
            "confidence": 95,
            "simplified_chars": "们经见过这国家风认应该样请让听话东西学习问题发现欢迎丽"
            if script == "simplified"
            else "",
            "traditional_chars": "們經見過這國風認應該樣請讓聽話東學習問題發現歡麗"
            if script == "traditional"
            else "",
            "script_disagreement": False,
        }
        for i, t in enumerate(texts)
    ]


def report(**kwargs):
    cues = samples(**kwargs)
    return {
        "samples": cues,
        "sampled_cues": len(cues),
        "unique_cues": 1400,
        "total_cues": 1450,
        "contact_sheets": ["sheet-00.jpg"],
    }


@pytest.mark.parametrize("script,expected", [("simplified", "zh-Hans"), ("traditional", "zh-Hant")])
def test_program_distinguishes_chinese_scripts_and_positive_sdh(script, expected):
    decision = decide(report(script=script), "chi")
    assert decision.language_confident and decision.sdh_confident
    assert decision.language == "chinese" and decision.hearing_impaired is True
    assert detected_language(decision, "chi") == expected


@pytest.mark.parametrize("script", ["simplified", "traditional"])
def test_cantonese_is_independent_of_script(script):
    decision = decide(report(script=script, cantonese=True), "zh")
    assert decision.language == "cantonese" and decision.language_confident
    assert detected_language(decision, "chi") == ("yue-Hans" if script == "simplified" else "yue-Hant")


@pytest.mark.parametrize("case", ["mixed", "ocr_disagreement", "low_quality", "dialect_hint", "one_marker"])
def test_ambiguous_language_needs_visual_review(case):
    data, language = report(), "chi"
    if case == "mixed":
        data["samples"][0]["traditional_chars"] = "國認這們聽話"
    if case == "ocr_disagreement":
        data["samples"][0]["script_disagreement"] = True
    if case == "low_quality":
        for cue in data["samples"]:
            cue["confidence"] = 10
    if case == "dialect_hint":
        language = "yue"
    if case == "one_marker":
        data["samples"][0]["text"] += "嘅"
    assert not decide(data, language).language_confident


@pytest.mark.parametrize(
    "marker", ["", "[Tokyo]", "(in French)", "♪ A song lyric ♪", "JOHN: Hello", "我说[笑声]这个词"]
)
def test_missing_or_ambiguous_sdh_markers_need_agent(marker):
    data = report(sdh=False)
    # Even one literal sound word in spoken dialogue cannot establish SDH.
    data["samples"][0]["text"] += marker
    decision = decide(data, "chi")
    assert decision.hearing_impaired is None and not decision.sdh_confident


def test_japanese_is_not_classified_as_chinese_from_kanji():
    data = report()
    for cue in data["samples"]:
        cue["text"] += "ありがとうございます"
    assert not decide(data, "jpn").language_confident


@pytest.mark.parametrize(
    "code,label",
    [
        ("zh-Hans", "Simplified Chinese"),
        ("zh-Hant", "Traditional Chinese"),
        ("yue-Hant", "Cantonese (Traditional)"),
        ("yue-Hans", "Cantonese (Simplified)"),
    ],
)
def test_detected_language_names_and_sdh_flags(code, label):
    track = {
        "kind": "subtitles",
        "codec_id": "S_HDMV/PGS",
        "codec": "HDMV PGS",
        "language": code,
        "hearing_impaired": False,
        "forced": False,
    }
    assert track_name(track) == label + " PGS"
    assert track_name({**track, "hearing_impaired": True}) == label + " PGS SDH"


@pytest.fixture
def context(environment, tmp_path, monkeypatch):
    data = report()
    events, artifacts, commands = [], [], []
    root = tmp_path / "workspace"
    root.mkdir()

    def run(args, **kwargs):
        commands.append(args)
        cache = args[3]
        (cache / "ocr.json").write_text(json.dumps(data))
        (cache / "sheet-00.jpg").write_bytes(b"image")
        callback = kwargs["progress_parser"]
        assert callback("SUBTITLE_O") is None
        assert callback("CR 6/6\n")["percentage"] == 99

    ctx = SimpleNamespace(
        settings=environment,
        task_id=str(uuid4()),
        job=SimpleNamespace(id=str(uuid4())),
        workspace=root,
        run=run,
        check=lambda: None,
        progress=lambda *a, **kw: None,
        log=lambda message: events.append(message),
        output=lambda category, name: root / name,
        artifact=lambda path, kind, **kw: artifacts.append((path, kind)),
    )
    track = {
        "track_id": 4,
        "kind": "subtitles",
        "codec_id": "S_HDMV/PGS",
        "codec": "PGS",
        "language": "chi",
        "name": "Traditional Chinese SDH",
        "hearing_impaired": True,
        "source_properties": {"flag_hearing_impaired": True},
        "forced": False,
    }
    return ctx, track, data, artifacts, commands


def test_confident_content_does_not_call_agent_or_trust_source_script(context, monkeypatch):
    ctx, track, _, artifacts, _ = context
    monkeypatch.setattr(subtitles, "review", lambda *a: pytest.fail("Unexpected agent call"))
    track["hearing_impaired"] = False
    result = subtitles.classify(ctx, track, ctx.workspace / "source.sup")
    assert result["language"] == "zh-Hans" and result["hearing_impaired"] is True
    assert result["mux_name"] == "Simplified Chinese PGS SDH"
    assert result["subtitle_detection"]["method"] == "program"
    assert artifacts[-1][1] == "SUBTITLE_DETECTION"


def reviewed(data, **changes):
    decision = decide(data, "chi").model_dump()
    decision.update(
        hearing_impaired=False,
        sdh_confident=True,
        sdh_evidence=[1, 2, 3],
        explanation="Sampled cues contain dialogue without accessibility descriptions.",
    )
    decision.update(changes)
    return {"decision": decision, "thread_id": "fixture"}


def test_agent_content_clears_incorrect_source_sdh_flag_and_name(context, monkeypatch):
    ctx, track, data, _, _ = context
    data.update(report(sdh=False))

    def review(ctx, track_id):
        inventory_path = next((ctx.settings.cache_root / "agent").rglob("inventory.json"))
        inventory = json.loads(inventory_path.read_text())
        assert "source_metadata" not in inventory and "source_properties" not in inventory
        assert track["name"] not in inventory_path.read_text()
        return reviewed(data)

    monkeypatch.setattr(subtitles, "review", review)
    result = subtitles.classify(ctx, track, ctx.workspace / "source.sup")
    assert result["hearing_impaired"] is False
    assert result["mux_name"] == "Simplified Chinese PGS"
    assert result["source_subtitle_metadata"]["hearing_impaired"] is True
    assert result["subtitle_detection"]["method"] == "program + agent"


@pytest.mark.parametrize("fault", ["unknown", "invented_cue", "unavailable"])
def test_inconclusive_or_failed_review_cannot_fall_back_to_source_flags(context, monkeypatch, fault):
    ctx, track, data, artifacts, _ = context
    data.update(report(sdh=False))

    def review(*args):
        if fault == "unavailable":
            raise RuntimeError("Agent unavailable")
        return reviewed(
            data,
            **(
                {"sdh_confident": False, "hearing_impaired": None}
                if fault == "unknown"
                else {"sdh_evidence": [99999]}
            ),
        )

    monkeypatch.setattr(subtitles, "review", review)
    with pytest.raises((ValueError, RuntimeError)):
        subtitles.classify(ctx, track, ctx.workspace / "source.sup")
    assert artifacts[-1][1] == "SUBTITLE_DETECTION"
    assert track["hearing_impaired"] is True and "subtitle_detection" not in track
    recorded = json.loads(artifacts[-1][0].read_text())
    assert "error" in recorded or not recorded["decision"]["sdh_confident"]


def test_subtitle_agent_streams_prompt_and_validates_cited_evidence(tmp_path, monkeypatch):
    data = report(sdh=False)
    inventory = {**data, "track_id": 3}
    (tmp_path / "inventory.json").write_text(json.dumps(inventory))
    (tmp_path / "sheet-00.jpg").write_bytes(b"image")
    events = []

    def invoke(prompt, images, schema, emit, check):
        assert "source track name" in prompt
        assert schema["additionalProperties"] is False and len(images) == 1
        emit({"type": "delta", "item_id": "answer", "text": "Response"})
        return json.dumps(reviewed(data)["decision"]), "thread"

    monkeypatch.setattr("agent.subtitle_agent.invoke", invoke)
    result = CodexSubtitleClassifier(on_event=events.append).classify(tmp_path)
    assert result["decision"]["hearing_impaired"] is False
    assert [e["type"] for e in events] == ["prompt", "delta", "complete"]
    assert events[0]["stage"] == "Subtitle track 3"
    invalid = SubtitleDecision.model_validate(reviewed(data, sdh_evidence=[111])["decision"])
    with pytest.raises(ValueError, match="outside"):
        validate_evidence(invalid, {s["id"] for s in data["samples"]})


def test_subtitle_endpoint_auth_streaming_and_busy_lock(environment, monkeypatch):
    def classify(self, root):
        assert root.name == "subtitle-4"
        self.on_event({"type": "prompt", "invocation_id": "test", "text": "Review"})
        return {"decision": reviewed(report())["decision"]}

    monkeypatch.setattr(CodexSubtitleClassifier, "classify", classify)
    body = {"job_id": str(uuid4()), "task_id": str(uuid4()), "track_id": 4}
    headers = {"Authorization": f"Bearer {environment.agent_token}"}
    with TestClient(service.app) as client:
        assert client.post("/classify-subtitles", json=body).status_code == 401
        service.lock.acquire()
        try:
            assert client.post("/classify-subtitles", json=body, headers=headers).status_code == 409
        finally:
            service.lock.release()
        response = client.post("/classify-subtitles", json=body, headers=headers)
        assert response.status_code == 200
        messages = [json.loads(line) for line in response.text.splitlines()]
        assert any(m["type"] == "prompt" for m in messages) and messages[-1]["type"] == "result"
        assert not service.lock.locked()
        assert (
            client.post("/classify-subtitles", json={**body, "track_id": -1}, headers=headers).status_code
            == 422
        )


def test_agent_decision_cannot_claim_confidence_in_mixed_or_unknown_script():
    value = deepcopy(reviewed(report())["decision"])
    value["script"] = "mixed"
    with pytest.raises(ValueError):
        SubtitleDecision.model_validate(value)


def test_legacy_prepared_subtitles_are_classified_before_mux(context, monkeypatch):
    from worker.pipeline import stages

    ctx, track, _, _, _ = context
    ctx.job.analysis = {"prepared_tracks": [{**track, "path": "old.sup"}]}
    ctx.job.validation = {"valid": True}
    ctx.job.release_name = "Fixture.2026.1080p.BluRay.x264-WiKi"
    (ctx.workspace / "old.sup").write_bytes(b"legacy")
    classified = []

    def classify(ctx, value, source):
        assert source.read_bytes() == b"legacy"
        classified.append(value["track_id"])
        return {
            **value,
            "language": "zh-Hant",
            "hearing_impaired": False,
            "subtitle_detection": {"schema_version": 1},
        }

    class StopBeforeTools(Exception):
        pass

    def command(ctx, output):
        assert classified == [track["track_id"]]
        prepared = ctx.job.analysis["prepared_tracks"][0]
        assert prepared["language"] == "zh-Hant" and prepared["hearing_impaired"] is False
        raise StopBeforeTools()

    monkeypatch.setattr(stages, "classify_subtitle", classify)
    monkeypatch.setattr(stages, "mux_command", command)
    with pytest.raises(StopBeforeTools):
        stages.mux(ctx)


def test_initial_review_asks_agent_for_sdh_even_when_program_is_confident(context, monkeypatch):
    ctx, track, data, _, _ = context
    assert decide(data, "chi").sdh_confident
    monkeypatch.setattr(subtitles, "review", lambda *a: reviewed(data))
    result = subtitles.classify(
        ctx, track, ctx.workspace / "source.sup", require_confident=False, agent_review=True
    )
    assert result["hearing_impaired"] is False
    assert result["subtitle_detection"]["method"] == "program + agent"


def test_inconclusive_sdh_can_be_described_before_selection_and_explicitly_overridden(context, monkeypatch):
    from shared.subtitles import subtitle_is_resolved

    ctx, track, data, _, _ = context
    data.update(report(sdh=False))
    monkeypatch.setattr(
        subtitles, "review", lambda *a: reviewed(data, sdh_confident=False, hearing_impaired=None)
    )
    result = subtitles.classify(ctx, track, ctx.workspace / "source.sup", require_confident=False)
    assert result["subtitle_detection"]["status"] == "inconclusive"
    assert result["hearing_impaired"] is None and not subtitle_is_resolved(result)
    result.update(flag_overrides={"hearing_impaired": False}, hearing_impaired=False)
    assert subtitle_is_resolved(result)
    result["name_override"] = "User subtitle label"
    strict = subtitles.classify(ctx, result, ctx.workspace / "source.sup")
    assert strict["hearing_impaired"] is False and strict["mux_name"] == "User subtitle label"
    assert strict["subtitle_detection"]["hearing_impaired"] is None


def test_busy_agent_waits_for_other_jobs_without_using_network_retry_budget(context, monkeypatch):
    import httpx

    ctx, _, _, _, _ = context
    original_client = httpx.Client
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if len(calls) <= 5:
            return httpx.Response(409, json={"detail": "busy"})
        return httpx.Response(
            200,
            headers={"content-type": "application/x-ndjson"},
            text=json.dumps({"type": "result", "result": {"tracks": []}}) + "\n",
        )

    monkeypatch.setattr(
        subtitles.httpx, "Client", lambda **kw: original_client(transport=httpx.MockTransport(handler), **kw)
    )
    monkeypatch.setattr(subtitles.time, "sleep", lambda seconds: None)
    assert subtitles.review(ctx) == {"tracks": []}
    assert calls == ["/review-tracks"] * 6
