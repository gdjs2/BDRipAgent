"""Multilingual content tags, OCR models, evidence boundaries and mux metadata."""

import json

import pytest
from agent.subtitle_agent import CodexSubtitleClassifier
from worker.adapters.subtitle_ocr import ocr_languages
from worker.adapters.subtitle_rules import decide

from shared.languages import language_tag
from shared.naming import track_name
from shared.subtitles import SubtitleDecision, subtitle_is_resolved
from tests.test_subtitle_detection import context as context
from tests.test_subtitle_detection import report
from worker.adapters import subtitles


@pytest.mark.parametrize(
    "given,expected",
    [
        ("eng", "en"),
        ("fre", "fr"),
        ("fra", "fr"),
        ("ger", "de"),
        ("por-br", "pt-BR"),
        ("srp-Latn", "sr-Latn"),
        ("rus", "ru"),
        ("jpn", "ja"),
        ("kor", "ko"),
        ("ara", "ar"),
        ("heb", "he"),
        ("hin", "hi"),
        ("fas", "fa"),
        ("es-419", "es-419"),
        ("zh-Hant", "zh-Hant"),
        ("yue-Hans", "yue-Hans"),
    ],
)
def test_canonical_language_codes(given, expected):
    assert language_tag(given) == expected


@pytest.mark.parametrize(
    "code", ["jp", "xx", "en-XX", "x-movie", "qaa", "und", "mul", "zxx", "English", "en-u-ca-gregory"]
)
def test_invalid_or_nonspecific_codes_are_not_accepted(code):
    with pytest.raises(ValueError):
        language_tag(code)


def answer(code="fr", **changes):
    value = dict(
        language="other",
        language_code=code,
        script="not_applicable",
        language_confident=True,
        hearing_impaired=False,
        sdh_confident=True,
        language_evidence=[1, 2, 3],
        sdh_evidence=[1, 2, 3],
        explanation="The sampled French dialogue supports French, with no regional distinction.",
    )
    value.update(changes)
    return value


@pytest.mark.parametrize(
    "changes",
    [
        {"language_code": "und"},
        {"language_code": "zh-Hans"},
        {"language": "chinese", "language_code": "fr", "script": "simplified"},
        {"language": "unknown", "language_confident": False, "language_code": "fr"},
    ],
)
def test_decision_requires_consistent_concrete_language(changes):
    with pytest.raises(ValueError):
        SubtitleDecision.model_validate(answer(**changes))


@pytest.mark.parametrize("source", ["eng", "fra", "chi", "und", "rus"])
def test_non_chinese_source_language_is_never_evidence(source):
    data = report(sdh=False)
    for cue in data["samples"]:
        cue.update(
            text="Nous devons partir maintenant, avant que la nuit arrive.",
            simplified_chars="",
            traditional_chars="",
        )
    decision = decide(data, source)
    assert decision.language_code == "und" and not decision.language_confident


@pytest.mark.parametrize(
    "code,name",
    [
        ("fr", "French"),
        ("de", "German"),
        ("ja", "Japanese"),
        ("ko", "Korean"),
        ("ar", "Arabic"),
        ("hi", "Hindi"),
        ("ru", "Russian"),
        ("uk", "Ukrainian"),
        ("pt-BR", "Portuguese (Brazil)"),
        ("pt-PT", "Portuguese (Portugal)"),
        ("sr-Latn", "Serbian (Latin)"),
        ("sr-Cyrl", "Serbian (Cyrillic)"),
        ("es-419", "Spanish (Latin America)"),
        ("fa", "Persian"),
    ],
)
def test_review_replaces_incorrect_source_code_and_name(context, monkeypatch, code, name):
    ctx, track, data, _, _ = context
    data.update(report(sdh=False))
    for cue in data["samples"]:
        cue.update(text="OCR needs visual language review", simplified_chars="", traditional_chars="")
    monkeypatch.setattr(subtitles, "review", lambda *a: {"decision": answer(code)})
    result = subtitles.classify(ctx, track, ctx.workspace / "subtitle.sup", agent_review=True)
    assert result["language"] == code
    assert result["mux_name"] == name + " PGS"
    assert subtitle_is_resolved(result)
    assert {c["id"] for c in result["subtitle_detection"]["evidence_cues"]} == {1, 2, 3}
    assert track_name({**result, "name_override": "My label"}) == "My label"


def test_legacy_source_derived_language_requires_reanalysis():
    track = dict(
        language="en",
        hearing_impaired=False,
        subtitle_detection=dict(schema_version=1, status="resolved", language="other"),
    )
    assert not subtitle_is_resolved(track)
    track["subtitle_detection"].update(schema_version=2, language_code="fr")
    assert not subtitle_is_resolved(track)  # Cannot reuse a decision for a different tag.


@pytest.mark.parametrize(
    "code,model",
    [
        ("fre", "fra"),
        ("pt-BR", "por"),
        ("de", "deu"),
        ("ru", "rus"),
        ("ja", "jpn"),
        ("ko", "kor"),
        ("ar", "ara"),
        ("fa", "fas"),
        ("he", "heb"),
        ("hi", "hin"),
        ("th", "tha"),
        ("sr-Latn", "srp_latn"),
        ("az-Cyrl", "aze_cyrl"),
        ("uz-Cyrl", "uzb_cyrl"),
    ],
)
def test_ocr_uses_the_language_model_available_for_each_hint(code, model):
    assert ocr_languages(code, {"chi_tra", "chi_sim", "eng", model}) == model + "+eng"


def test_missing_or_bad_ocr_hint_still_allows_visual_review():
    for hint in ("und", "invalid-tag", "fr"):
        assert ocr_languages(hint, {"eng"}) == "eng"
    assert ocr_languages("eng", {"eng"}) == "eng"


def test_subtitle_agent_corrects_invalid_language_tag_before_returning(tmp_path, monkeypatch, environment):
    (tmp_path / "inventory.json").write_text(json.dumps({**report(), "track_id": 7}))
    (tmp_path / "sheet-00.jpg").write_bytes(b"image")
    prompts, events = [], []

    def invoke(prompt, images, schema, emit, check):
        prompts.append(prompt)
        return json.dumps(answer("jp" if len(prompts) == 1 else "ja")), "thread"

    monkeypatch.setattr("agent.subtitle_agent.invoke", invoke)
    result = CodexSubtitleClassifier(on_event=events.append).classify(tmp_path)
    assert result["decision"]["language_code"] == "ja"
    assert len(prompts) == 2 and "Validation error" in prompts[1]
    assert [e["type"] for e in events] == ["prompt", "error", "prompt", "complete"]
