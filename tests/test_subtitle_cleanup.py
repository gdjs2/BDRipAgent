from types import SimpleNamespace

import pysubs2
import pytest

from shared.subtitle_discovery import SubtitleCleanup
from worker.adapters.subtitle_cleanup import clean_subtitles


def fixture():
    text = pysubs2.SSAFile()
    text.events = [
        pysubs2.SSAEvent(start=i * 1000, end=i * 1000 + 800, text=s)
        for i, s in enumerate(["Visit subtitle-spam.example", "Helo, Anna.", "We will go tomorrow."])
    ]
    ctx = SimpleNamespace(
        check=lambda: None, progress=lambda *a, **k: None, job=SimpleNamespace(title="Movie", year=2026)
    )
    return ctx, text, SimpleNamespace(language="en")


def answer(**changes):
    return {
        "decision": {
            "usable": True,
            "single_language": True,
            "language": "en",
            "edits": [],
            "issues": [],
            "explanation": "Single-language English",
            **changes,
        }
    }


def test_cleanup_removes_ad_repairs_typo_preserves_timings_and_dialogue():
    ctx, text, candidate = fixture()
    edits = [
        {
            "cue_id": 1,
            "original_text": text[0].plaintext,
            "replacement_text": None,
            "action": "remove_advertisement",
            "reason": "Subtitle site promotion",
        },
        {
            "cue_id": 2,
            "original_text": text[1].plaintext,
            "replacement_text": "Hello, Anna.",
            "action": "correct_text",
            "reason": "Clear typo",
        },
    ]
    cleaned, report = clean_subtitles(ctx, text, candidate, lambda *a: answer(edits=edits))
    assert [e.plaintext for e in cleaned] == ["Hello, Anna.", "We will go tomorrow."]
    assert cleaned[0].start == 1000 and cleaned[1].end == 2800
    assert report["reviewed_cues"] == 3 and report["edited_cues"] == 2


@pytest.mark.parametrize(
    "changes",
    [
        {"single_language": False},
        {"language": "ko"},
    ],
)
def test_cleanup_rejects_bilingual_wrong_language_or_uncertain_text(changes):
    with pytest.raises(ValueError):
        clean_subtitles(*fixture(), lambda *a: answer(**changes))


def test_cleanup_visits_every_cue_including_late_ads():
    ctx, text, candidate = fixture()
    text.events = [
        pysubs2.SSAEvent(start=i * 1000, end=i * 1000 + 900, text=f"Dialogue {i}") for i in range(241)
    ]
    seen = []

    def request(ctx, inventory):
        seen.extend(c["id"] for c in inventory["cues"])
        return answer()

    cleaned, report = clean_subtitles(ctx, text, candidate, request)
    assert seen == list(range(1, 242)) and len(cleaned) == 241 and len(report["reviews"]) == 3


@pytest.mark.parametrize(
    "cue,text,action,replacement",
    [
        (99, "missing", "correct_text", "Fixed"),
        (1, "wrong evidence", "remove_advertisement", None),
        (3, "We will go tomorrow.", "remove_duplicate", None),
        (2, "Helo, Anna.", "correct_text", "<script>bad</script>"),
    ],
)
def test_cleanup_rejects_unsupported_edits(cue, text, action, replacement):
    edit = {
        "cue_id": cue,
        "original_text": text,
        "action": action,
        "replacement_text": replacement,
        "reason": "Test",
    }
    with pytest.raises(ValueError):
        clean_subtitles(*fixture(), lambda *a: answer(edits=[edit]))


def test_cleanup_schema_requires_every_field_for_strict_agent_output():
    schema = SubtitleCleanup.model_json_schema()
    for obj in [schema, *schema["$defs"].values()]:
        assert set(obj["required"]) == set(obj["properties"])
        assert obj["additionalProperties"] is False


@pytest.mark.parametrize(
    "language,original,expected",
    [
        ("zh-Hans", "這個软體讓我們搭計程车回家。", "这个软件让我们打出租车回家。"),
        ("zh-Hant", "这个軟件让我們打出租車回家。", "這個軟體讓我們搭計程車回家。"),
    ],
)
def test_chinese_normalization_preserves_timings_and_carries_editorial_context(language, original, expected):
    ctx, subtitles, _ = fixture()
    subtitles.events = [
        pysubs2.SSAEvent(start=i * 1000, end=i * 1000 + 800, text=original) for i in range(101)
    ]
    requests = []

    def request(ctx, inventory):
        requests.append(inventory)
        assert inventory["requested_language"] == language
        if inventory["batch"] == 2:
            assert inventory["previous_corrections"]
            assert inventory["previous_corrections"][0]["replacement_text"] == expected
            assert len(inventory["previous_corrections"]) <= 32
            assert sum(len(v) for c in inventory["previous_corrections"] for v in c.values()) <= 8000
        return answer(
            language=language,
            edits=[
                {
                    "cue_id": cue["id"],
                    "original_text": cue["text"],
                    "replacement_text": expected,
                    "action": "correct_text",
                    "reason": "Normalize script, regional vocabulary and grammar",
                }
                for cue in inventory["cues"]
            ],
        )

    cleaned, report = clean_subtitles(ctx, subtitles, SimpleNamespace(language=language), request)
    assert len(requests) == 2 and len(cleaned) == 101
    assert all(cue.plaintext == expected for cue in cleaned)
    assert [(c.start, c.end) for c in cleaned] == [(c.start, c.end) for c in subtitles]
    assert report["edited_cues"] == 101 and report["language"] == language


def test_unresolved_corruption_gets_repair_pass_then_report_without_losing_dialogue():
    ctx, text, candidate = fixture()
    requests = []

    def request(ctx, inventory):
        requests.append(inventory)
        return answer(
            usable=False, issues=["CRITICAL cue 2 at 1s: damaged dialogue; no reliable alternate found"]
        )

    cleaned, report = clean_subtitles(ctx, text, candidate, request)
    assert len(requests) == 2 and requests[1]["previous_review"]["usable"] is False
    assert [c.plaintext for c in cleaned] == [c.plaintext for c in text]
    assert report["requires_attention"] and "cue 2" in report["critical_errors"][0]
    assert report["reviews"][0]["initial_review"]


def test_cross_language_repair_receives_source_evidence_and_records_citation():
    ctx, text, candidate = fixture()
    references = [{"id": "4:50", "track_id": 4, "seconds": 1, "language": "fr", "text": "Bonjour, Anna."}]
    calls = []

    def request(ctx, inventory):
        calls.append(inventory)
        assert inventory["source_reference_cues"] == references
        if len(calls) == 1:
            return answer(usable=False, issues=["Cue 2 corrupted"])
        return answer(
            edits=[
                {
                    "cue_id": 2,
                    "original_text": "Helo, Anna.",
                    "replacement_text": "Hello, Anna.",
                    "action": "correct_text",
                    "reason": "Source cue 4:50 French greeting matches surrounding dialogue; https://example.com/transcript confirms Anna.",
                }
            ]
        )

    cleaned, report = clean_subtitles(ctx, text, candidate, request, references=references)
    assert cleaned[1].plaintext == "Hello, Anna."
    assert not report["requires_attention"] and not report["critical_errors"]
    assert "https://example.com/transcript" in report["reviews"][0]["edits"][0]["reason"]
    assert cleaned[1].start == text[1].start


def test_followup_preserves_initial_edits_and_reports_damaged_characters():
    ctx, text, candidate = fixture()
    text[2].plaintext = "Damaged \ufffd dialogue"
    count = 0

    def request(ctx, inventory):
        nonlocal count
        count += 1
        edits = (
            []
            if count == 2
            else [
                {
                    "cue_id": 2,
                    "original_text": "Helo, Anna.",
                    "replacement_text": "Hello, Anna.",
                    "action": "correct_text",
                    "reason": "Clear typo",
                }
            ]
        )
        return answer(edits=edits, issues=["CRITICAL cue 3 damaged"])

    cleaned, report = clean_subtitles(ctx, text, candidate, request)
    assert cleaned[1].plaintext == "Hello, Anna."
    assert "\ufffd" in cleaned[2].plaintext
    assert any("damaged replacement characters" in error for error in report["critical_errors"])
