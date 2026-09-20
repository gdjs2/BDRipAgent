"""Conservative OCR rules. Source names and impairment flags are never inputs."""

import re

from shared.subtitles import SubtitleDecision

# Colloquial Cantonese grammar, not region names or isolated shared Han characters.
CANTONESE = re.compile(r"唔[係系使好知得想會会明]|[佢哋嘅喺冇咗啲嚟噉瞓攞畀乜嘢]|點解|点解")
CJK = re.compile(r"[\u3400-\u9fff]")
OTHER_EAST_ASIAN = re.compile(r"[\u3040-\u30ff\uac00-\ud7af]")
SOUND = re.compile(
    r"\b(?:sighs?|sobbing|sobs?|laughing|laughs?|chuckles?|screams?|screaming|"
    r"gasps?|gasping|sobbing|applause|footsteps|gunshots?|gunfire|door (?:opens|closes|slams)|"
    r"phone ring(?:s|ing)|music (?:plays|playing)|speaking (?:indistinctly|foreign))\b|"
    r"(?:笑聲|笑声|嘆氣|叹气|啜泣|哭泣|尖叫|喘氣|喘气|掌聲|掌声|腳步聲|脚步声|"
    r"槍聲|枪声|敲門聲|敲门声|鈴聲|铃声|音樂響起|音乐响起)",
    re.I,
)
BRACKETS = re.compile(r"[\[（(【][^\]）)】]{1,100}[\]）)】]")
SPEAKER = re.compile(r"^(?:[A-Z][A-Z .'-]{1,24}|[\u3400-\u9fff]{1,6})[:：]", re.M)


def decide(report, original_language):
    samples = report["samples"]
    readable = [s for s in samples if s["confidence"] >= 65 and len(s["text"].strip()) >= 3]
    text = "\n".join(s["text"] for s in readable)
    han = CJK.findall(text)
    chinese = len(han) >= 30 and not OTHER_EAST_ASIAN.search(text)
    language, script, confident = "unknown", "unknown", False
    reasons, language_ids = [], []
    if chinese:
        simple = set().union(*(set(s["simplified_chars"]) for s in readable))
        traditional = set().union(*(set(s["traditional_chars"]) for s in readable))
        total = len(simple) + len(traditional)
        if total >= 12 and max(len(simple), len(traditional)) / total >= 0.95:
            script = "simplified" if len(simple) > len(traditional) else "traditional"
        elif len(simple) >= 4 and len(traditional) >= 4:
            script = "mixed"
        dialect = [(s, CANTONESE.findall(re.sub(r"\s+", "", s["text"]))) for s in readable]
        matches = {match for _, tokens in dialect for match in tokens}
        matching_cues = [s["id"] for s, tokens in dialect if tokens]
        language = "cantonese" if len(matches) >= 3 and len(matching_cues) >= 3 else "chinese"
        # A few dialect markers, disagreement between OCR models, or a conflicting
        # source language hint needs visual review; never force standard Chinese.
        uncertain_dialect = bool(matches) and language != "cantonese"
        conflict = any(s.get("script_disagreement") for s in readable)
        source_cantonese = original_language.lower().split("-")[0] == "yue"
        confident = (
            script in ("simplified", "traditional")
            and len(readable) >= 6
            and not uncertain_dialect
            and not conflict
            and not (source_cantonese and language != "cantonese")
        )
        language_ids = (matching_cues if language == "cantonese" else [s["id"] for s in readable])[:12]
        reasons.append(
            f"{len(simple)} distinct simplified and {len(traditional)} traditional forms; "
            f"{len(matches)} Cantonese markers in {len(matching_cues)} cues"
        )
    elif (
        len(readable) >= 6
        and len(han) < 5
        and original_language.lower().split("-")[0]
        not in (
            "zh",
            "zho",
            "chi",
            "cmn",
            "yue",
            "und",
        )
    ):
        language, script, confident = "other", "not_applicable", True
        language_ids = [s["id"] for s in readable[:12]]
        reasons.append("Readable non-Chinese dialogue; retaining the source language tag")

    # Brackets or a music symbol alone can mean translation, signs, or lyrics.
    # Require repeated explicit non-dialogue sound descriptions for a positive rule.
    sound_ids, suspicious = [], []
    for sample in readable:
        value = sample["text"]
        bracketed = BRACKETS.findall(value)
        if any(SOUND.search(re.sub(r"(?<=[\u3400-\u9fff])\s+(?=[\u3400-\u9fff])", "", b)) for b in bracketed):
            sound_ids.append(sample["id"])
        if bracketed or SPEAKER.search(value) or re.search(r"[♪♫]|聲|声", value):
            suspicious.append(sample["id"])
    sdh = True if len(sound_ids) >= 3 else None
    # Absence in a sample is not proof of absence across the track. Negative
    # decisions always go to the visual agent, including fully OCR'd short tracks.
    reasons.append(
        f"{len(sound_ids)} cues with explicit bracketed sound descriptions; "
        f"{len(suspicious)} cues need SDH interpretation"
    )
    return SubtitleDecision(
        language=language,
        script=script,
        language_confident=confident,
        hearing_impaired=sdh,
        sdh_confident=sdh is True,
        language_evidence=language_ids,
        sdh_evidence=sound_ids[:12],
        explanation="; ".join(reasons),
    )
