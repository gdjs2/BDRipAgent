import pytest

from shared.db import session
from shared.models import Artifact


def artifact(environment, job, kind="RELEASE_BBCODE", data=b"[b]Movie[/b]\n<script>alert(1)</script>"):
    path = environment.artifacts_root / "bundle" / "movie.bbcode.txt"
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(data)
    with session() as db:
        row = Artifact(
            job_id=job["id"],
            task_id=job["tasks"][0]["id"],
            artifact_type=kind,
            path=str(path.relative_to(environment.artifacts_root)),
            storage="artifacts",
            size=len(data),
        )
        db.add(row)
        db.commit()
        return row.id


@pytest.mark.parametrize("kind", ["RELEASE_BBCODE", "RELEASE_NFO", "RELEASE_MD5", "RELEASE_ENCODER_INFO"])
def test_release_preview_is_authenticated_plain_text_json(client, environment, new_job, kind):
    item = artifact(environment, new_job, kind)
    response = client.get(f"/api/artifacts/{item}/preview")
    assert response.status_code == 200 and response.headers["content-type"].startswith("application/json")
    assert response.json() == {
        "filename": "movie.bbcode.txt",
        "text": "[b]Movie[/b]\n<script>alert(1)</script>",
        "truncated": False,
    }
    assert "content-disposition" not in response.headers
    client.headers.clear()
    assert client.get(f"/api/artifacts/{item}/preview").status_code == 401


def test_preview_is_bounded_and_rejects_binary_artifacts(client, environment, new_job):
    item = artifact(environment, new_job, data=b"\xef\xbb\xbf" + b"x" * (1024 * 1024 + 20))
    value = client.get(f"/api/artifacts/{item}/preview").json()
    assert value["truncated"] and len(value["text"]) <= 1024 * 1024
    assert not value["text"].startswith("\ufeff")
    with session() as db:
        row = db.get(Artifact, item)
        row.artifact_type = "RELEASE_TORRENT"
        db.commit()
    assert client.get(f"/api/artifacts/{item}/preview").status_code == 409


def test_preview_cannot_escape_storage_root(client, environment, new_job):
    item = artifact(environment, new_job)
    outside = environment.artifacts_root.parent / "outside.txt"
    outside.write_text("outside storage")
    with session() as db:
        row = db.get(Artifact, item)
        (environment.artifacts_root / row.path).unlink()
        (environment.artifacts_root / row.path).symlink_to(outside)
        db.commit()
    assert client.get(f"/api/artifacts/{item}/preview").status_code == 409


def test_nfo_preview_preserves_upstream_cp437_artwork(client, environment, new_job):
    text = "█▀▄ WiKi ▄▀█\nMovie résumé\n"
    item = artifact(environment, new_job, "RELEASE_NFO", data=text.encode("cp437"))
    assert client.get(f"/api/artifacts/{item}/preview").json()["text"] == text
