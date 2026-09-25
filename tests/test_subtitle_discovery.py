import json
import socket
import subprocess
import zipfile
from types import SimpleNamespace

import numpy as np
import pytest
from sqlalchemy import select

from backend.app.services import enqueue
from backend.app.subtitle_discovery import ensure_discovery
from backend.app.track_choices import choices_current
from shared.db import session
from shared.models import MovieJob, MovieTrack, Task
from shared.state import Stage
from shared.subtitle_discovery import DiscoveryPolicy, SubtitleReview, missing_languages
from worker.adapters import subtitle_download
from worker.adapters.subtitle_alignment import fit_alignment, read_text, retime_pgs, retime_text
from worker.pipeline import subtitle_discovery as pipeline
from worker.tasks import execute


def review_for(scale=1, offset=0):
    return SubtitleReview(
        usable=True,
        language="zh-Hans",
        hearing_impaired=False,
        issues=[],
        explanation="Matched dialogue.",
        alignment_confident=True,
        scale=scale,
        offset_seconds=offset,
        anchors=[
            {"candidate_id": i, "reference_id": f"2:{i}", "explanation": f"Distinct matching dialogue {i}"}
            for i in range(1, 10)
        ],
    )


def evidence(scale=1, offset=0, duration=120):
    candidates = [
        {"id": i, "seconds": float(t), "end": float(t + 0.4), "text": f"Unique dialogue {i}"}
        for i, t in enumerate(np.linspace(5, 105, 9), 1)
    ]
    references = [
        {**cue, "id": f"2:{cue['id']}", "track_id": 2, "seconds": cue["seconds"] * scale + offset}
        for cue in candidates
    ]
    return candidates, references


@pytest.mark.parametrize("scale,offset", [(1, 2.3), (25 / 24, -1.2), (24000 / 1001 / 25, 4.5)])
def test_alignment_fits_fps_and_offset_and_changes_every_cue(tmp_path, scale, offset):
    candidate, reference = evidence(scale, offset)
    result = fit_alignment(review_for(scale, offset), candidate, reference, 120)
    assert result["scale"] == pytest.approx(scale)
    assert result["offset_seconds"] == pytest.approx(offset)
    import pysubs2

    subs = pysubs2.SSAFile()
    subs.events = [
        pysubs2.SSAEvent(start=round(c["seconds"] * 1000), end=round(c["end"] * 1000), text=c["text"])
        for c in candidate
    ]
    output = tmp_path / "aligned.srt"
    retime_text(subs, output, result, 120)
    reread, _, _ = read_text(output)
    assert [c.start for c in reread] == pytest.approx(
        [(c["seconds"] * scale + offset) * 1000 for c in candidate], abs=1
    )


@pytest.mark.parametrize(
    "problem", ["missing", "duplicate", "different_cut", "weak_coverage", "invented", "wrong_transform"]
)
def test_ambiguous_alignment_never_produces_a_transform(problem):
    candidate, reference = evidence()
    result = review_for()
    if problem == "missing":
        result.anchors = result.anchors[:2]
    elif problem == "duplicate":
        result.anchors[-1] = result.anchors[0]
    elif problem == "different_cut":
        reference[4]["seconds"] += 6
    elif problem == "weak_coverage":
        for cue in reference:
            cue["seconds"] /= 10
    elif problem == "invented":
        result.anchors[-1].reference_id = "2:999"
    else:
        result.offset_seconds = 20
    with pytest.raises(ValueError):
        fit_alignment(result, candidate, reference, 120)


def test_coverage_respects_scripts_languages_and_forced_tracks():
    tracks = [
        {"kind": "subtitles", "language": "eng"},
        {"kind": "subtitles", "language": "zh-Hans"},
        {"kind": "subtitles", "language": "ko", "forced": True},
        {"kind": "subtitles", "language": "zh"},
    ]
    assert missing_languages(tracks, ["en", "ko"]) == ["ko", "zh-Hant"]
    assert DiscoveryPolicy(original_languages=["kor", "ko"]).original_languages == ["ko"]


def test_downloader_rejects_local_dns_before_connecting(tmp_path, monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("127.0.0.1", 443))])
    with pytest.raises(ValueError, match="private or local"):
        subtitle_download.download("https://example.com/file.srt", tmp_path / "download")
    for url in ["file:///etc/passwd", "https://user:password@example.com/sub", "http://example.com:8000/sub"]:
        with pytest.raises(ValueError):
            subtitle_download.public_url(url)


def test_archive_import_never_extracts_paths_and_rejects_bombs(tmp_path):
    path = tmp_path / "bundle.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("../escape.srt", "1\n00:00:01,000 --> 00:00:02,000\nHello\n")
    with pytest.raises(ValueError, match="unsafe"):
        subtitle_download.unpack(path, tmp_path, "https://example.com/sub.zip")
    assert not (tmp_path.parent / "escape.srt").exists()
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("subtitle.srt", "a" * 100000)
    with pytest.raises(ValueError, match="limits"):
        subtitle_download.unpack(path, tmp_path, "https://example.com/sub.zip")


def ready(client, new_job):
    with session() as db:
        job = db.get(MovieJob, new_job["id"])
        job.state = "ENCODING"
        job.analysis = {
            **job.analysis,
            "subtitle_analysis_version": 2,
            "track_review_version": 1,
            "tracks": [],
        }
        for task in db.scalars(select(Task).where(Task.job_id == job.id)):
            task.status = "SUCCEEDED"
        db.commit()
    return client.get(f"/api/jobs/{new_job['id']}").json()


def test_manual_discovery_is_idempotent_runs_beside_encoding_and_is_authenticated(client, new_job):
    ready(client, new_job)
    with session() as db:
        enqueue(db, db.get(MovieJob, new_job["id"]))
        db.commit()
    url = f"/api/jobs/{new_job['id']}/subtitles/discover"
    first = client.post(url, json={"original_languages": ["ko"]})
    assert first.status_code == 202, first.text
    second = client.post(url, json={"original_languages": ["ko"]}).json()
    tasks = [t for t in second["tasks"] if t["status"] == "QUEUED"]
    assert {t["type"] for t in tasks} == {"encode", "discover_subtitles"}
    assert next(t for t in tasks if t["type"] == "discover_subtitles")["lane"] == "tracks"
    assert (
        client.post(
            f"/api/jobs/{new_job['id']}/tracks/selection",
            json={"audio_track_ids": [], "subtitle_track_ids": []},
        ).status_code
        == 409
    )
    client.headers.clear()
    assert client.post(url, json={}).status_code == 401


def test_automatic_discovery_is_opt_in_and_scheduled_only_once(client, new_job):
    ready(client, new_job)
    with session() as db:
        job = db.get(MovieJob, new_job["id"])
        assert not ensure_discovery(db, job)
        job.analysis = {
            **job.analysis,
            "subtitle_discovery_policy": {"enabled": True, "original_languages": []},
        }
        assert ensure_discovery(db, job)
        task = db.scalar(select(Task).where(Task.type == "discover_subtitles"))
        task.status = "FAILED"
        db.flush()
        assert not ensure_discovery(db, job)
        db.commit()


def test_discovery_agent_schema_requires_every_property_and_validates_citations(tmp_path, monkeypatch):
    from agent import subtitle_discovery as agent
    from shared.subtitle_discovery import SearchResult

    for model in (SearchResult, SubtitleReview):
        schema = model.model_json_schema()
        for obj in [schema, *schema.get("$defs", {}).values()]:
            if obj.get("type") == "object":
                assert set(obj["required"]) == set(obj["properties"])
                assert obj["additionalProperties"] is False
    candidate, reference = evidence()
    (tmp_path / "inventory.json").write_text(
        json.dumps({"mode": "align", "candidate_cues": candidate, "reference_cues": reference})
    )
    bad = review_for().model_dump()
    bad["anchors"][0]["candidate_id"] = 999
    monkeypatch.setattr(agent, "invoke", lambda *a, **kw: (json.dumps(bad), "thread"))
    with pytest.raises(ValueError, match="not supplied"):
        agent.CodexSubtitleDiscovery().run(tmp_path)


@pytest.mark.parametrize("source_language,target_language", [("en", "zh-Hans"), ("ko", "en")])
@pytest.mark.parametrize("intake", ["search", "srt", "ass"])
@pytest.mark.parametrize("critical", [False, True])
def test_real_pgs_discovery_alignment_crop_sharing_and_remux_preparation(
    client, environment, monkeypatch, source_language, target_language, intake, critical
):
    """Only search/download and agent reasoning are fixtures; native conversion/cropping/mux are real."""
    from worker.adapters.integrations import pgs_dimensions
    from worker.pipeline import stages

    times = [0.5, 1.7, 3, 4.4, 5.8, 7.2, 8.6, 10, 11]
    duration, scale, offset = 12, 25 / 24, 0.2
    source = environment.source_root / "Movie.mkv"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=size=320x192:rate=24:duration=12",
            "-c:v",
            "ffv1",
            str(source),
        ],
        check=True,
    )
    job = client.post(
        "/api/jobs", json={"source_path": source.name, "title": "Discovery Fixture", "year": 2026}
    ).json()
    ready(client, job)
    workspace = environment.workspace_root / job["id"]
    samples = [
        {"id": i, "seconds": t, "text": f"Distinct source dialogue number {i}"}
        for i, t in enumerate(times, 1)
    ]
    (workspace / "source-subtitles.json").write_text(json.dumps({"samples": samples}))
    track = {
        "track_id": 2,
        "kind": "subtitles",
        "language": source_language,
        "forced": False,
        "subtitle_detection": {"report": "source-subtitles.json"},
        "codec_id": "S_HDMV/PGS",
        "track_review": {"schema_version": 1},
    }
    with session() as db:
        stored = db.get(MovieJob, job["id"])
        db.add(MovieTrack(job_id=job["id"], track_id=2, kind="subtitles", info=track))
        stored.analysis = {
            **stored.analysis,
            "video": {"width": 320, "height": 192, "duration": duration, "fps": "24/1"},
            "crop": {"top": 16, "bottom": 16, "left": 0, "right": 0},
            "tracks": [track],
        }
        db.commit()

    def answer(ctx, inventory):
        if inventory["mode"] == "search":
            assert target_language in inventory["known_missing"]
            return {
                "decision": {
                    "original_languages": [source_language],
                    "original_language_sources": ["https://example.com/movie"],
                    "summary": "Found matching subtitles.",
                    "candidates": [
                        {
                            "language": target_language,
                            "source_url": "https://example.com/subtitles",
                            "download_url": "https://example.com/subtitle.srt",
                            "format": "srt",
                            "release": "Fixture 25fps",
                            "reason": "Complete dialogue for this release.",
                        }
                    ],
                }
            }
        if inventory["mode"] == "clean":
            edits = []
            for cue in inventory["cues"]:
                if "subtitle-spam" in cue["text"]:
                    edits.append(
                        {
                            "cue_id": cue["id"],
                            "original_text": cue["text"],
                            "replacement_text": None,
                            "action": "remove_advertisement",
                            "reason": "Subtitle-site promotion",
                        }
                    )
                elif "子幕" in cue["text"] or "Helo" in cue["text"]:
                    edits.append(
                        {
                            "cue_id": cue["id"],
                            "original_text": cue["text"],
                            "replacement_text": cue["text"].replace("子幕", "字幕").replace("Helo", "Hello"),
                            "action": "correct_text",
                            "reason": "Clear character typo",
                        }
                    )
            return {
                "decision": {
                    "usable": True,
                    "single_language": True,
                    "language": target_language,
                    "edits": edits,
                    "issues": [],
                    "explanation": "Single target language after ad removal and typo repair.",
                    **(
                        {"usable": False, "issues": ["CRITICAL cue 2: uncertain reading retained"]}
                        if critical
                        else {}
                    ),
                }
            }
        value = review_for(scale, offset).model_dump()
        value["language"] = target_language
        if critical:
            value.update(usable=False, issues=["CRITICAL retained text uncertainty; timing verified"])
        return {"decision": value}

    def fetch(candidate, directory, check):
        import pysubs2

        directory.mkdir(parents=True, exist_ok=True)
        srt = directory / "fixture.srt"
        subs = pysubs2.SSAFile()
        subs.events = [
            pysubs2.SSAEvent(
                start=round((t - offset) / scale * 1000),
                end=round(((t - offset) / scale + 0.35) * 1000),
                text=f"这是完整的电影字幕第{i}句。"
                if target_language == "zh-Hans"
                else f"Hello, this is dialogue {i}.",
            )
            for i, t in enumerate(times, 1)
        ]
        subs.events[0].text = subs.events[0].text.replace("字幕", "子幕").replace("Hello", "Helo")
        if critical:
            subs.events[1].text += "\ufffd"
        subs.events.insert(0, pysubs2.SSAEvent(start=0, end=200, text="Visit subtitle-spam.example"))
        subs.save(srt)
        return [(srt, "Chinese.srt", {"url": candidate.download_url})]

    monkeypatch.setattr(pipeline, "agent_request", answer)
    monkeypatch.setattr(pipeline, "fetch_candidate", fetch)
    if intake == "search":
        result = client.post(
            f"/api/jobs/{job['id']}/subtitles/discover", json={"original_languages": [source_language]}
        )
        assert result.status_code == 202, result.text
        task = next(t for t in result.json()["tasks"] if t["type"] == "discover_subtitles")
    else:
        import pysubs2

        from tests.test_subtitle_uploads import upload

        local = fetch(SimpleNamespace(download_url=None), workspace / "upload-input", lambda: None)[0][0]
        data = pysubs2.load(str(local)).to_string(intake).encode()
        if intake == "ass":
            import shutil

            shutil.copy2(source, workspace / "encoded.mkv")
            final = environment.completed_root / job["id"] / "previous.mkv"
            final.parent.mkdir()
            shutil.copy2(source, final)
            with session() as db:
                existing = db.get(MovieJob, job["id"])
                existing.state = "COMPLETE"
                existing.validation = {"valid": True}
                existing.analysis = {
                    **existing.analysis,
                    "encoded_path": "encoded.mkv",
                    "final_path": f"{job['id']}/previous.mkv",
                }
                db.commit()
        result = upload(client, job["id"], data, f"Local.{intake}", code=target_language)
        assert result.status_code == 201, result.text
        task = {"id": result.json()["task_id"]}
        queued = client.get(f"/api/jobs/{job['id']}").json()
        assert len(queued["tracks"]) == 1, "Unreviewed text must not be selectable"
        original = client.get(
            f"/api/jobs/{job['id']}/subtitles/uploads/{result.json()['upload_id']}/download"
        )
        assert original.status_code == 200 and original.content == data
    execute(task["id"])
    result = client.get(f"/api/jobs/{job['id']}").json()
    finished = next(t for t in result["tasks"] if t["id"] == task["id"])
    assert finished["status"] == "SUCCEEDED", finished["error_message"]
    discovered = next(
        (
            t
            for t in result["tracks"]
            if t["info"].get("origin") == ("discovery" if intake == "search" else "upload")
        ),
        None,
    )
    assert discovered, result["subtitle_discovery"]["report"]
    info = discovered["info"]
    assert info["language"] == target_language and info["discovery"]["crop_checked"]
    assert info["discovery"]["alignment"]["scale"] == pytest.approx(scale)
    if intake == "search":
        assert result["subtitle_discovery"]["report"]["missing"] == (
            ["zh-Hant"] if target_language == "zh-Hans" else ["zh-Hans", "zh-Hant"]
        )
    raw = environment.workspace_root / info["upload_path"]
    cleaned = raw.with_name("cleaned.srt").read_text()
    assert "subtitle-spam" not in cleaned and "子幕" not in cleaned and "Helo" not in cleaned
    assert info["discovery"]["cleanup"]["edited_cues"] == 2
    assert info["discovery"]["cleanup"]["reviewed_cues"] == 10
    assert info["discovery"]["requires_attention"] is critical
    assert bool(info["discovery"]["critical_errors"]) is critical
    assert pgs_dimensions(raw) == (320, 192)
    assert pgs_dimensions(raw.with_name("cropped.sup")) == (320, 160)
    download_url = f"/api/jobs/{job['id']}/tracks/{discovered['track_id']}/download"
    download = client.get(download_url)
    assert download.status_code == 200 and download.content == raw.with_name("cropped.sup").read_bytes()
    assert client.get(download_url + "?variant=cleaned").content == cleaned.encode()
    assert client.get(download_url, headers={"Range": "bytes=0-12"}).content == download.content[:13]

    # Retiming PGS must preserve payloads exactly, not OCR or re-render them.
    shifted = workspace / "shifted.sup"
    retime_pgs(raw, shifted, {"scale": 1, "offset_seconds": 0.1}, duration)

    def packets(path):
        data, rows, index = path.read_bytes(), [], 0
        while index < len(data):
            size = int.from_bytes(data[index + 11 : index + 13], "big")
            rows.append(
                (int.from_bytes(data[index + 2 : index + 6], "big"), data[index + 10 : index + 13 + size])
            )
            index += 13 + size
        return rows

    before, after = packets(raw), packets(shifted)
    assert [data for _, data in before] == [data for _, data in after]
    assert all(b - a == 9000 for (a, _), (b, _) in zip(before, after))
    with session() as db:
        assert not choices_current(db, db.get(MovieJob, job["id"]))
    selected = client.post(
        f"/api/jobs/{job['id']}/{'remux' if intake == 'ass' else 'tracks/selection'}",
        json={"audio_track_ids": [], "subtitle_track_ids": [discovered["track_id"]]},
    )
    assert selected.status_code == (202 if intake == "ass" else 200), selected.text
    other = client.post(
        "/api/jobs", json={"source_path": source.name, "title": "Discovery Fixture", "year": 2026}
    ).json()
    assert any(t["track_id"] == discovered["track_id"] for t in other["tracks"])
    with session() as db:
        stored = db.get(MovieJob, job["id"])
        stored.state = Stage.PREPARING_TRACKS.value
        prepare_task = enqueue(db, stored)
        db.commit()
        task_id = prepare_task.id
    execute(task_id)
    result = client.get(f"/api/jobs/{job['id']}").json()
    preparation = next(t for t in result["tasks"] if t["id"] == task_id)
    assert preparation["status"] == "SUCCEEDED", preparation["error_message"]
    prepared = result["analysis"]["prepared_tracks"][0]
    assert pgs_dimensions(workspace / prepared["path"]) == (320, 160)
    prepare_task = next(t for t in result["tasks"] if t["id"] == task_id)
    assert prepare_task["status"] == "SUCCEEDED", prepare_task["error_message"]
    assert not prepare_task["command_json"], "Reuse the checked crop; do not run Sup2sup twice"
    target = workspace / "with-subtitles.mkv"
    ctx = SimpleNamespace(
        settings=environment,
        workspace=workspace,
        source=lambda: source,
        job=SimpleNamespace(
            title="Discovery Fixture",
            year=2026,
            release_name=result["release_name"],
            analysis={"encoded_path": "encoded.mkv", "prepared_tracks": [prepared]},
            validation={"metrics": {"source_first_pts": 0, "encoded_first_pts": 0}},
        ),
    )
    import shutil

    shutil.copy2(source, workspace / "encoded.mkv")
    subprocess.run(stages.mux_command(ctx, target), check=True, capture_output=True)
    inspection = json.loads(subprocess.check_output(["mkvmerge", "-J", str(target)]))
    assert inspection["tracks"][1]["properties"]["language_ietf"] == target_language


@pytest.mark.parametrize(
    "suffix,language,text",
    [(".srt", "ko", "안녕하세요. 영화 자막입니다."), (".ass", "zh-Hant", "這是完整的電影字幕。")],
)
def test_native_subtitleedit_renders_multilingual_srt_and_ass(environment, tmp_path, suffix, language, text):
    import pysubs2

    from worker.adapters.integrations import pgs_dimensions

    source = tmp_path / ("subtitle" + suffix)
    subs = pysubs2.SSAFile()
    subs.events = [pysubs2.SSAEvent(start=1000, end=3000, text=text)]
    subs.save(source)
    context = SimpleNamespace(
        settings=environment,
        run=lambda args: subprocess.run([str(x) for x in args], check=True, capture_output=True),
    )
    output = tmp_path / "subtitle.sup"
    pipeline.render(context, source, output, language, {"width": 1920, "height": 1080, "fps": "24000/1001"})
    assert pgs_dimensions(output) == (1920, 1080)
    assert output.stat().st_size > 500


def test_discovery_endpoint_auth_and_streaming(environment, monkeypatch):
    from uuid import uuid4

    from fastapi.testclient import TestClient

    from agent import main as service

    class Discover:
        def __init__(self, on_event, check):
            self.emit = on_event

        def run(self, root):
            assert root.name == "discovery"
            self.emit({"type": "prompt", "text": "Search and align", "invocation_id": "fixture"})
            self.emit(
                {"type": "delta", "text": "Found evidence", "item_id": "answer", "invocation_id": "fixture"}
            )
            return {"decision": {"summary": "No matching release"}}

    monkeypatch.setattr(service, "CodexSubtitleDiscovery", Discover)
    body = {"job_id": str(uuid4()), "task_id": str(uuid4())}
    with TestClient(service.app) as client:
        assert client.post("/discover-subtitles", json=body).status_code == 401
        response = client.post(
            "/discover-subtitles", json=body, headers={"Authorization": "Bearer test-agent-token"}
        )
        assert response.status_code == 200
        messages = [json.loads(line) for line in response.text.splitlines()]
        assert any(m["type"] == "prompt" for m in messages)
        assert any(m["type"] == "delta" for m in messages)
        assert messages[-1]["type"] == "result"
        assert service.agent_queue.snapshot() == {"running": 0, "waiting": 0}


def test_search_and_cleanup_enable_web_but_alignment_uses_source_evidence(tmp_path, monkeypatch):
    from agent import subtitle_discovery as agent

    modes = []

    def invoke(prompt, images, schema, emit, check, **kwargs):
        modes.append(kwargs["web_search"])
        assert "model" not in kwargs and "reasoning_effort" not in kwargs
        return (
            json.dumps(
                {
                    "original_languages": ["ko"],
                    "original_language_sources": ["https://example.com/movie"],
                    "candidates": [],
                    "summary": "No download",
                }
            )
            if kwargs["web_search"]
            else review_for().model_dump_json()
        ), "thread"

    monkeypatch.setattr(agent, "invoke", invoke)
    (tmp_path / "inventory.json").write_text(json.dumps({"mode": "search"}))
    agent.CodexSubtitleDiscovery().run(tmp_path)
    candidate, reference = evidence()
    (tmp_path / "inventory.json").write_text(
        json.dumps({"mode": "align", "candidate_cues": candidate, "reference_cues": reference})
    )
    agent.CodexSubtitleDiscovery().run(tmp_path)
    from tests.test_subtitle_cleanup import answer

    def clean_invoke(prompt, images, schema, emit, check, **kwargs):
        modes.append(kwargs["web_search"])
        assert kwargs["model"] == "gpt-5.6-luna"
        assert kwargs["reasoning_effort"] == "low"
        assert "source_reference_cues" in prompt and "REPAIR FIRST" in prompt
        return json.dumps(answer()["decision"]), "thread"

    monkeypatch.setattr(agent, "invoke", clean_invoke)
    (tmp_path / "inventory.json").write_text(
        json.dumps({"mode": "clean", "cues": [], "source_reference_cues": []})
    )
    agent.CodexSubtitleDiscovery().run(tmp_path)
    assert modes == [True, False, True]


def test_download_page_skips_broken_link(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from worker.adapters import subtitle_download

    attempts = []

    def download(url, path, check):
        attempts.append(url)
        if url.endswith("/page"):
            path.write_text('<a href="bad.srt">One</a><a href="good.srt">Two</a>')
            return {"url": url, "content_type": "text/html"}
        if url.endswith("bad.srt"):
            raise ValueError("HTTP 404")
        path.write_text("1\n00:00:01,000 --> 00:00:02,000\nHello\n")
        return {"url": url, "content_type": "application/x-subrip"}

    monkeypatch.setattr(subtitle_download, "download", download)
    files = subtitle_download.fetch_candidate(
        SimpleNamespace(source_url="https://example.org/page", download_url=None), tmp_path, lambda: None
    )
    assert len(files) == 1
    assert files[0][1] == "good.srt"
    assert attempts == [
        "https://example.org/page",
        "https://example.org/bad.srt",
        "https://example.org/good.srt",
    ]


def test_automatic_discovery_precedes_remux_with_previously_confirmed_choices(client, new_job):
    from backend.app.services import reconcile

    ready(client, new_job)
    response = client.post(
        f"/api/jobs/{new_job['id']}/tracks/selection",
        json={"audio_track_ids": [], "subtitle_track_ids": []},
    )
    assert response.status_code == 200, response.text
    with session() as db:
        job = db.get(MovieJob, new_job["id"])
        job.state = "WAITING_FOR_TRACK_SELECTION"
        job.validation = {"passed": True}
        job.analysis = {**job.analysis, "subtitle_discovery_policy": {"enabled": True}}
        db.commit()
        reconcile(db)
        assert job.state == "WAITING_FOR_TRACK_SELECTION"
        tasks = list(db.scalars(select(Task).where(Task.job_id == job.id, Task.status == "QUEUED")))
        assert [task.type for task in tasks] == ["discover_subtitles"]


@pytest.mark.parametrize("english", [None, "en", "eng", "en-US", "forced", "commentary"])
def test_english_is_required_for_non_english_movies_unless_full_track_exists(english):
    tracks = [{"kind": "subtitles", "language": code} for code in ("ko", "zh-Hans", "zh-Hant")]
    if english:
        tracks.append(
            {
                "kind": "subtitles",
                "language": "en" if english in ("forced", "commentary") else english,
                "forced": english == "forced",
                "commentary": english == "commentary",
            }
        )
    assert missing_languages(tracks, ["ko"]) == (["en"] if english in (None, "forced", "commentary") else [])
    assert missing_languages([], ["en"]).count("en") == 1


@pytest.mark.parametrize("first_fails", [False, True])
def test_discovery_preserves_agent_quality_ranking_and_falls_back(client, new_job, monkeypatch, first_fails):
    ready(client, new_job)
    with session() as db:
        job = db.get(MovieJob, new_job["id"])
        job.analysis = {**job.analysis, "video": {"duration": 600, "fps": "24/1"}}
        db.commit()
    candidates = [
        {
            "language": "en",
            "format": fmt,
            "source_url": f"https://example.com/{index}",
            "download_url": f"https://example.com/{index}.{fmt}",
            "release": "Matching edition",
            "reason": reason,
        }
        for index, fmt, reason in [
            (1, "ass", "Best evidence: corrected complete translation"),
            (2, "srt", "Fallback: fewer quality references"),
        ]
    ]
    monkeypatch.setattr(
        pipeline,
        "agent_request",
        lambda *a: {
            "decision": {
                "original_languages": ["ko"],
                "original_language_sources": ["https://example.com/movie"],
                "summary": "Compared two editions; prefer the corrected ASS.",
                "candidates": candidates,
            }
        },
    )
    monkeypatch.setattr(pipeline, "source_references", lambda ctx: [{"id": "2:1", "text": "Reference"}])
    monkeypatch.setattr(pipeline, "public_url", lambda url: None)
    monkeypatch.setattr(
        pipeline,
        "fetch_candidate",
        lambda c, directory, check: [(directory / "subtitle.ass", "subtitle.ass", {})],
    )
    attempted = []

    def process(ctx, candidate, *args):
        attempted.append(candidate.format)
        if first_fails and candidate.format == "ass":
            raise ValueError("Different cut fails independent alignment")
        return {"track_id": 1000000, "selection_reason": candidate.reason}

    monkeypatch.setattr(pipeline, "process_file", process)
    response = client.post(
        f"/api/jobs/{new_job['id']}/subtitles/discover", json={"original_languages": ["ko"]}
    )
    task_id = response.json()["subtitle_discovery"]["active_task"]["id"]
    execute(task_id)
    job = client.get(f"/api/jobs/{new_job['id']}").json()
    assert next(t for t in job["tasks"] if t["id"] == task_id)["status"] == "SUCCEEDED"
    assert attempted == (["ass", "srt"] if first_fails else ["ass"])
    report = job["subtitle_discovery"]["report"]
    assert report["candidates"][0]["status"] == ("needs_review" if first_fails else "added")
    assert report["added_tracks"][0]["selection_reason"] == candidates[int(first_fails)]["reason"]
