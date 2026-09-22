from pathlib import Path

import pytest
from sqlalchemy import select

from shared.db import session
from shared.models import MovieJob, SourceTrackChoices
from tests.test_source_choices import CHOICES, read, ready, save
from tests.test_source_sharing import peer

SRT = b"1\n00:00:00,100 --> 00:00:01,100\nHello world\n\n2\n00:00:01,200 --> 00:00:02,200\n[Music]\n"
ASS = b"[Script Info]\nScriptType: v4.00+\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\nDialogue: 0,0:00:00.10,0:00:01.00,Default,,0,0,0,,Hello world\n"


def upload(client, job_id, data=SRT, filename="English.srt", **params):
    return client.post(
        f"/api/jobs/{job_id}/tracks/upload",
        params={"filename": filename, "code": "eng", "hearing_impaired": "true", **params},
        content=data,
        headers={"Content-Type": "application/octet-stream"},
    )


def registered_upload(client, job_id, data=SRT, filename="English.srt", **params):
    """Legacy registry fixture; text ingestion itself is tested via upload()."""
    from types import SimpleNamespace
    from uuid import uuid4

    from backend.app.subtitle_uploads import register_upload
    from shared.config import get_settings
    from shared.languages import language_tag

    ident = str(uuid4())
    path = (
        get_settings().workspace_root / "registered-fixtures" / ident / ("subtitle" + Path(filename).suffix)
    )
    path.parent.mkdir(parents=True)
    path.write_bytes(data)
    result = register_upload(
        job_id,
        path,
        filename,
        language_tag(params.get("code", "en")),
        params.get("hearing_impaired", "true") == "true",
        ident,
    )
    return SimpleNamespace(status_code=201, json=lambda: result, text=str(result))


def test_registered_uploads_share_files_and_choices_without_touching_source_analysis(
    client, new_job, environment
):
    job_id = new_job["id"]
    ready(job_id)
    sibling = peer(client)
    ready(sibling["id"])
    before = read(client, job_id)
    response = registered_upload(client, job_id)
    assert response.status_code == 201, response.text
    track = response.json()["track"]
    ident = track["track_id"]
    assert track["info"]["language"] == "en" and track["info"]["language_name"] == "English"
    assert track["info"]["hearing_impaired"] is True
    assert (environment.workspace_root / track["info"]["upload_path"]).is_file()
    current = read(client, job_id)
    assert current["analysis"]["tracks"] == before["analysis"]["tracks"]
    assert current["tasks"] == before["tasks"]
    assert current["shared_track_selection"] is None
    assert read(client, sibling["id"])["tracks"][-1]["track_id"] == ident
    duplicate = registered_upload(client, sibling["id"])
    assert duplicate.json()["duplicate"] and duplicate.json()["track"]["track_id"] == ident
    assert len(list(environment.workspace_root.glob("registered-fixtures/*/subtitle.srt"))) == 1
    assert (
        client.get(f"/api/jobs/{job_id}/tracks/{ident}/language", params={"code": "jpn"}).json()[
            "language_name"
        ]
        == "Japanese"
    )
    choices = {
        **CHOICES,
        "subtitle_track_ids": [ident, 9],
        "track_names": {str(ident): "Custom subtitle"},
        "track_languages": {str(ident): "jpn"},
    }
    selected = save(client, job_id, choices)
    assert selected.status_code == 200, selected.text
    for job in (job_id, sibling["id"]):
        current = read(client, job)
        assert current["track_selection"]["subtitle_track_ids"] == [ident, 9]
        assert current["analysis"]["uploaded_tracks"][0]["mux_name"] == "Custom subtitle"
        assert current["analysis"]["uploaded_tracks"][0]["language"] == "ja"
    future = peer(client)
    ready(future["id"])
    from backend.app.services import reconcile

    with session() as db:
        reconcile(db)
    assert read(client, future["id"])["track_selection"]["subtitle_track_ids"] == [ident, 9]
    assert registered_upload(client, job_id).json()["duplicate"]
    # Adding another upload does not discard confirmed selections or their revision.
    another = registered_upload(client, job_id, ASS, "French.ass", code="fre", hearing_impaired="false")
    assert another.status_code == 201, another.text
    current = read(client, job_id)
    assert current["shared_track_selection"]["revision"] == 1
    assert current["track_selection"]["subtitle_track_ids"] == [ident, 9]


@pytest.mark.parametrize(
    "filename,data",
    [
        ("bad.srt", b"not subtitles"),
        ("bad.sup", b"PG"),
        ("audio.ac3", b"audio"),
        ("../outside.srt", SRT),
        ("bad.srt", b"\x00garbage"),
    ],
)
def test_invalid_uploads_leave_no_files_or_registry(client, new_job, environment, filename, data):
    ready(new_job["id"])
    response = upload(client, new_job["id"], data, filename)
    assert response.status_code == 409, response.text
    assert not list(environment.workspace_root.glob("uploaded-subtitles/*/*/*"))
    with session() as db:
        assert db.scalar(select(SourceTrackChoices)) is None


def test_limits_language_auth_and_phase_are_enforced(client, new_job, environment, monkeypatch):
    ready(new_job["id"])
    import backend.app.subtitle_uploads as uploads

    monkeypatch.setattr(uploads, "MAX_TEXT_BYTES", 10)
    assert upload(client, new_job["id"]).status_code == 413
    # Streaming body without Content-Length is also bounded.
    assert upload(client, new_job["id"], iter([SRT[:5], SRT[5:]])).status_code == 413
    assert upload(client, new_job["id"], code="und").status_code == 409
    monkeypatch.setattr(uploads, "MAX_TEXT_BYTES", 1024)
    with session() as db:
        db.get(MovieJob, new_job["id"]).state = "REMUXING"
        db.commit()
    assert upload(client, new_job["id"]).status_code == 409
    client.headers.pop("Authorization")
    assert upload(client, new_job["id"]).status_code == 401
    assert not list(environment.workspace_root.glob("uploaded-subtitles/*/*/*"))


@pytest.mark.parametrize(
    "filename,data",
    [("Unicode.srt", SRT.decode().encode("utf-16")), ("English.ass", ASS), ("English.ssa", ASS)],
)
def test_supported_text_encodings_and_formats(client, new_job, filename, data):
    ready(new_job["id"])
    response = upload(client, new_job["id"], data, filename)
    assert response.status_code == 201, response.text
    assert response.json()["queued"]
    assert read(client, new_job["id"])["subtitle_uploads"][0]["status"] == "QUEUED"


def test_pgs_upload(client, new_job):
    import runpy

    fixture = Path("/opt/sup2sup/tests/fixtures.py")
    if not fixture.exists():
        pytest.skip("PGS fixture available in the native integration image")
    ready(new_job["id"])
    data = runpy.run_path(str(fixture))["simple"]()
    response = upload(client, new_job["id"], data, "English.sup")
    assert response.status_code == 201, response.text


def test_upload_language_preview_uses_registered_names(client):
    response = client.get("/api/languages/verify", params={"code": "jpn"})
    assert response.status_code == 200, response.text
    assert response.json() == {"code": "ja", "language_name": "Japanese"}
    assert client.get("/api/languages/verify", params={"code": "und"}).status_code == 409
    client.headers.pop("Authorization")
    assert client.get("/api/languages/verify", params={"code": "en"}).status_code == 401
