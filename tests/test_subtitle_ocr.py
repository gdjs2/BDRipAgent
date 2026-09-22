"""Native PGS -> pixels -> Tesseract -> OpenCC, using redistributable generated text."""

import json
import runpy
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageFont
from worker.adapters.subtitle_ocr import script_forms
from worker.adapters.subtitle_rules import decide

FONT = Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc")
VENDOR = Path("/opt/sup2sup/src")
pytestmark = pytest.mark.skipif(
    not FONT.exists()
    or not VENDOR.exists()
    or not shutil.which("tesseract")
    or not shutil.which("opencc_dict"),
    reason="Native OCR language packs, OpenCC, pinned Sup2Sup, and CJK fixture font required",
)


def text_sup(lines):
    fixture = runpy.run_path("/opt/sup2sup/tests/fixtures.py")
    font = ImageFont.truetype(str(FONT), size=48, index=2)
    source = bytearray()
    for index, text in enumerate(lines):
        x0, y0, x1, y1 = font.getbbox(text)
        picture = Image.new("L", (x1 - x0 + 12, y1 - y0 + 12))
        ImageDraw.Draw(picture).text((6 - x0, 6 - y0), text, font=font, fill=255)
        width, height = picture.size
        assert width < 1900
        pixels, rle = picture.tobytes(), bytearray()
        for y in range(height):
            row = pixels[y * width : (y + 1) * width]
            x = 0
            while x < width:
                color = int(row[x] > 100)
                end = x + 1
                while end < width and int(row[end] > 100) == color and end - x < 63:
                    end += 1
                rle.extend((0, 0x80 | (end - x), color))
                x = end
            rle.extend((0, 0))
        pts = (index * 10 + 1) * 90000
        source.extend(fixture["pcs"](((1, 0, 0, 10, 900, None),), pts=pts, number=index))
        source.extend(fixture["wds"](pts=pts))
        source.extend(fixture["pds"](pts=pts))
        source.extend(fixture["ods"](width=width, height=height, pixels=bytes(rle), pts=pts))
        source.extend(fixture["end"](pts))
    source.extend(fixture["pcs"]((), pts=pts + 270000, state=0, number=len(lines)))
    source.extend(fixture["end"](pts + 270000))
    return bytes(source)


@pytest.mark.parametrize(
    "script,lines",
    [
        (
            "simplified",
            [
                "我们已经见过这个国家的风景",
                "他们认为这件事情应该这样做",
                "请让我听听你们的话",
                "这里有很多东西可以学习",
                "这个问题还没有发现答案",
                "欢迎来到这个美丽的世界",
            ],
        ),
        (
            "traditional",
            [
                "我們已經見過這個國家的風景",
                "他們認為這件事情應該這樣做",
                "請讓我聽聽你們的話",
                "這裡有很多東西可以學習",
                "這個問題還沒有發現答案",
                "歡迎來到這個美麗的世界",
            ],
        ),
        (
            "english_sdh",
            ["[footsteps]", "[door slams]", "[phone ringing]", "[laughing]", "[sobbing]", "[gunshots]"],
        ),
    ],
)
def test_real_pgs_rendering_and_ocr(script, lines, tmp_path):
    source, directory = tmp_path / "subtitle.sup", tmp_path / "images"
    data = text_sup(lines + [lines[0]])  # Repeated bitmap must not inflate coverage/evidence.
    source.write_bytes(data)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "worker.adapters.subtitle_ocr",
            str(source),
            str(directory),
            "--language",
            "eng" if script == "english_sdh" else "chi",
            "--limit",
            "8",
            "--sup2sup-source",
            str(VENDOR),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert "SUBTITLE_OCR 6/6" in result.stdout
    report = json.loads((directory / "ocr.json").read_text())
    assert report["total_cues"] == 7 and report["sampled_cues"] == report["unique_cues"] == 6
    assert len(report["contact_sheets"]) == 1
    assert all((directory / sample["image"]).is_file() for sample in report["samples"])
    assert source.read_bytes() == data
    decision = decide(report, "eng" if script == "english_sdh" else "chi")
    if script == "english_sdh":
        assert decision.sdh_confident and decision.hearing_impaired is True
    else:
        evidence = set("".join(sample[f"{script}_chars"] for sample in report["samples"]))
        assert len(evidence) >= 12, report["samples"]
        assert decision.script == script
        assert decision.hearing_impaired is None  # No false SDH conclusion from absence.


def test_opencc_form_evidence_includes_real_script_distinctions(tmp_path):
    simplified, traditional = script_forms(tmp_path)
    assert set("这国说听") <= simplified
    assert set("這國說聽") <= traditional
    assert not simplified & traditional
    assert not set("一二三人大小") & (simplified | traditional)


@pytest.mark.parametrize("sdh", [True, False])
def test_task_ocr_and_real_mux_override_incorrect_source_flags(sdh, new_job, environment, monkeypatch):
    from shared.db import session
    from shared.models import Task
    from worker.adapters import subtitles
    from worker.pipeline.stages import mux_command
    from worker.runtime import TaskContext

    with session() as db:
        task = db.get(Task, new_job["tasks"][0]["id"])
        task.status, task.run_token = "RUNNING", "subtitle-native"
        task_id = task.id
        db.commit()
    ctx = TaskContext(task_id, "subtitle-native")
    try:
        source = ctx.workspace / "subtitle.sup"
        source.write_bytes(
            text_sup(
                [
                    "[footsteps]",
                    "[door slams]",
                    "[phone ringing]",
                    "[laughing]",
                    "[sobbing]",
                    "[gunshots]",
                ]
                if sdh
                else [
                    "我们已经见过这个国家的风景",
                    "他们认为这件事情应该这样做",
                    "请让我听听你们的话",
                    "这里有很多东西可以学习",
                    "这个问题还没有发现答案",
                    "欢迎来到这个美丽的世界",
                ]
            )
        )

        def review(*args):
            # Live Codex is deliberately not called in native media tests.
            return {
                "thread_id": "test",
                "decision": {
                    "language": "other" if sdh else "chinese",
                    "language_code": "en" if sdh else "zh-Hans",
                    "script": "not_applicable" if sdh else "simplified",
                    "language_confident": True,
                    "hearing_impaired": sdh,
                    "sdh_confident": True,
                    "language_evidence": [1, 2, 3],
                    "sdh_evidence": [1, 2, 3],
                    "explanation": "Sampled English sound cues." if sdh else "Sampled Chinese dialogue.",
                },
            }

        monkeypatch.setattr(subtitles, "review", review)
        original = {
            "track_id": 4,
            "kind": "subtitles",
            "codec": "HDMV PGS",
            "codec_id": "S_HDMV/PGS",
            "language": "eng" if sdh else "chi",
            "name": "Wrong SDH label" if not sdh else "Dialogue",
            "hearing_impaired": not sdh,
            "default": False,
            "forced": False,
            "path": "subtitle.sup",
        }
        detected = subtitles.classify(ctx, original, source)
        assert detected["hearing_impaired"] is sdh
        with session() as db:
            stored = db.get(Task, task_id)
            assert stored.progress_detail["phase"].startswith("Reviewing")
        video = ctx.workspace / "video.mkv"
        subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "color=size=320x180:rate=24:duration=1",
                "-c:v",
                "ffv1",
                str(video),
            ],
            check=True,
            capture_output=True,
            timeout=30,
        )
        monkeypatch.setattr(ctx, "source", lambda: video)
        ctx.job.release_name = "Subtitle.Fixture.2026.1080p.BluRay.x264-WiKi"
        ctx.job.analysis = {"encoded_path": "video.mkv", "prepared_tracks": [detected]}
        ctx.job.validation = {"metrics": {"source_first_pts": 0, "encoded_first_pts": 0}}
        output = ctx.workspace / "result.mkv"
        ctx.run(mux_command(ctx, output))
        merged = json.loads(ctx.run(["mkvmerge", "-J", output]))
        properties = merged["tracks"][1]["properties"]
        assert properties["track_name"] == ("English PGS SDH" if sdh else "Simplified Chinese PGS")
        assert properties["flag_hearing_impaired"] is sdh
        assert properties["language_ietf"] == ("en" if sdh else "zh-Hans")
    finally:
        ctx.close()


@pytest.mark.parametrize(
    "code,model,lines,word",
    [
        (
            "fra",
            "fra",
            [
                "Nous devons partir avant que la nuit arrive.",
                "Je voudrais savoir pourquoi vous êtes ici.",
                "Cette maison appartient à ma famille.",
                "Nous avons beaucoup de choses à apprendre.",
                "Vous pouvez attendre quelques minutes.",
                "Elle reviendra demain avec ses amis.",
            ],
            "demain",
        ),
        (
            "deu",
            "deu",
            [
                "Wir müssen gehen, bevor die Nacht kommt.",
                "Warum bist du heute hierher gekommen?",
                "Dieses Haus gehört meiner Familie.",
                "Wir haben noch viel Zeit zum Lernen.",
                "Bitte warten Sie einen Augenblick.",
                "Sie kommt morgen mit ihren Freunden.",
            ],
            "morgen",
        ),
        (
            "rus",
            "rus",
            [
                "Нам нужно уйти до наступления ночи.",
                "Почему вы пришли сюда сегодня?",
                "Этот дом принадлежит моей семье.",
                "У нас ещё много времени для учёбы.",
                "Подождите несколько минут, пожалуйста.",
                "Она вернётся завтра со своими друзьями.",
            ],
            "завтра",
        ),
    ],
)
def test_native_non_chinese_pgs_ocr(code, model, lines, word, tmp_path):
    available = subprocess.run(
        ["tesseract", "--list-langs"], capture_output=True, text=True, check=True
    ).stdout.splitlines()
    if model not in available:
        pytest.skip(f"{model} OCR model is not installed")
    source, directory = tmp_path / "source.sup", tmp_path / "images"
    source.write_bytes(text_sup(lines))
    subprocess.run(
        [
            sys.executable,
            "-m",
            "worker.adapters.subtitle_ocr",
            str(source),
            str(directory),
            "--language",
            code,
            "--limit",
            "8",
            "--sup2sup-source",
            str(VENDOR),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )
    report = json.loads((directory / "ocr.json").read_text())
    assert report["ocr_languages"] == model + "+eng"
    assert word in " ".join(sample["text"] for sample in report["samples"]).lower()
    assert report["sampled_cues"] == 6
    # Even readable OCR does not make the source language a content finding.
    assert not decide(report, "eng").language_confident


@pytest.mark.parametrize(
    "code,label",
    [
        ("fr", "French"),
        ("de", "German"),
        ("ru", "Russian"),
        ("ar", "Arabic"),
        ("pt-BR", "Portuguese (Brazil)"),
        ("sr-Latn", "Serbian (Latin)"),
        ("sr-Cyrl", "Serbian (Cyrillic)"),
        ("es-419", "Spanish (Latin America)"),
    ],
)
def test_native_mux_preserves_non_chinese_language_tags_and_names(code, label, tmp_path):
    from shared.naming import track_name

    # Metadata fixture only: native OCR and agent-content behavior are tested separately.
    source, output = tmp_path / "fixture.sup", tmp_path / "tagged.mkv"
    source.write_bytes(text_sup(["A generated subtitle metadata fixture."]))
    name = track_name(dict(kind="subtitles", codec_id="S_HDMV/PGS", language=code, hearing_impaired=True))
    assert name == label + " PGS SDH"
    subprocess.run(
        [
            "mkvmerge",
            "-o",
            str(output),
            "--language",
            f"0:{code}",
            "--track-name",
            f"0:{name}",
            "--hearing-impaired-flag",
            "0:1",
            str(source),
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )
    result = subprocess.run(
        ["mkvmerge", "-J", str(output)], check=True, capture_output=True, text=True, timeout=30
    )
    properties = json.loads(result.stdout)["tracks"][0]["properties"]
    assert properties["language_ietf"] == code
    assert properties["track_name"] == name
    assert properties["flag_hearing_impaired"] is True


def test_single_native_extraction_supplies_review_preparation_and_video_index(
    tmp_path, environment, monkeypatch
):
    from types import SimpleNamespace

    from worker.adapters.source_tracks import extract_tracks

    from tests.test_source_tracks import context
    from worker.adapters import audio_review
    from worker.adapters.frame_index import source_frame_index

    base, source, subtitles = tmp_path / "base.mkv", tmp_path / "source.mkv", tmp_path / "subtitle.sup"
    subtitles.write_bytes(text_sup(["A generated subtitle fixture."]))
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=size=320x180:rate=24:duration=6",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=6",
            "-c:v",
            "ffv1",
            "-c:a",
            "flac",
            str(base),
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )
    subprocess.run(
        ["mkvmerge", "-o", str(source), str(base), str(subtitles)],
        check=True,
        capture_output=True,
        timeout=30,
    )
    calls = []
    ctx = context(tmp_path, source, calls)
    ctx.settings = environment

    def run(command, **kwargs):
        calls.append([str(arg) for arg in command])
        return subprocess.run(calls[-1], check=True, capture_output=True, text=True, timeout=30).stdout

    def output(category, filename):
        path = ctx.workspace / category / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    ctx.run = run
    ctx.output = output
    ctx.artifact = lambda path, *a, **kw: str(path.relative_to(ctx.workspace))
    tracks = [
        dict(track_id=1, kind="audio", codec_id="A_FLAC"),
        dict(track_id=2, kind="subtitles", codec_id="S_HDMV/PGS"),
    ]
    extracted, timestamps = extract_tracks(ctx, tracks, video_track_id=0)
    assert len(calls) == 1 and "tracks" in calls[0] and "timestamps_v2" in calls[0]
    extract_tracks(ctx, tracks)
    assert len(calls) == 1
    monkeypatch.setattr(
        audio_review,
        "behavior",
        lambda: {
            "integrations": {"audio_review": {"transcription": False, "sample_count": 1, "sample_seconds": 5}}
        },
    )
    evidence = audio_review.analyze_audio(
        ctx, [{**tracks[0], "source_track_path": str(extracted[1]["path"].relative_to(ctx.workspace))}], 6
    )
    assert evidence[1]["samples"][0]["silent"] is False
    assert calls[-1][calls[-1].index("-i") + 1] == str(extracted[1]["path"])
    ctx.job = SimpleNamespace(
        analysis={"source_video_timestamps": str(timestamps.relative_to(ctx.workspace))},
        validation={"metrics": {"source_frames": 144, "source_first_pts": 0}},
    )
    points, _ = source_frame_index(ctx)
    assert len(points) == 144 and len(calls) == 2  # One extraction and one short audio decode.
