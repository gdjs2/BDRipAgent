import copy
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from sqlalchemy import select

from backend.app import movie_metadata as metadata
from shared.db import session
from shared.models import MovieJob, Task


@pytest.fixture
def imdb_provider(monkeypatch):
    metadata.clear_metadata_cache()
    calls = []
    movie = {
        "imdb_id": "tt0133093",
        "title": "The Matrix",
        "title_localized": "The Matrix",
        "title_akas": ["Matrix", "the matrix", "", None],
        "year": 1999,
    }

    def fetch(imdb_id):
        calls.append(imdb_id)
        return copy.deepcopy(movie)

    monkeypatch.setattr(metadata, "fetch_movie", fetch)
    yield movie, calls
    metadata.clear_metadata_cache()


@pytest.mark.parametrize(
    "value",
    [
        "tt0133093",
        " TT0133093 ",
        "https://www.imdb.com/title/tt0133093/?ref_=test",
        "https://m.imdb.com/title/tt0133093/reference",
    ],
)
def test_imdb_ids_and_title_urls_are_normalized(value):
    assert metadata.normalize_imdb_id(value) == "tt0133093"


@pytest.mark.parametrize(
    "value",
    [
        "nm0133093",
        "tt123",
        "0133093",
        "https://example.com/title/tt0133093/",
        "http://127.0.0.1/title/tt0133093",
        "https://imdb.com.evil/title/tt0133093/",
        "https://imdb.com@evil/title/tt0133093/",
        "tt0133093; touch /tmp/bad",
    ],
)
def test_lookup_cannot_fetch_arbitrary_urls_or_commands(value, imdb_provider):
    with pytest.raises(ValueError):
        metadata.lookup_imdb(value)
    assert not imdb_provider[1]


def test_metadata_preview_uses_upstream_title_order_and_release_naming(client, imdb_provider):
    response = client.get("/api/metadata/imdb", params={"imdb_id": "tt0133093"})
    assert response.status_code == 200, response.text
    movie = response.json()
    assert (movie["title"], movie["year"]) == ("The Matrix", 1999)
    assert [option["title"] for option in movie["title_options"]] == ["The Matrix", "Matrix"]
    assert movie["title_options"][0]["filenames"] == {
        "x264": "The.Matrix.1999.1080p.BluRay.x264-WiKi.mkv",
        "x265": "The.Matrix.1999.1080p.BluRay.x265.10bit-WiKi.mkv",
    }
    client.headers.clear()
    assert client.get("/api/metadata/imdb", params={"imdb_id": "tt0133093"}).status_code == 401


def test_cache_is_bounded_in_time_and_results_cannot_be_modified(imdb_provider, monkeypatch):
    clock = [1000]
    monkeypatch.setattr(metadata.time, "monotonic", lambda: clock[0])
    first = metadata.lookup_imdb("tt0133093")
    first["title_options"].clear()
    assert metadata.lookup_imdb("tt0133093")["title_options"]
    assert imdb_provider[1] == ["tt0133093"]
    clock[0] += 3601
    metadata.lookup_imdb("tt0133093")
    assert imdb_provider[1] == ["tt0133093", "tt0133093"]


def test_imdb_only_job_persists_identity_and_uses_it_for_filename(client, environment, imdb_provider):
    (environment.source_root / "Movie.mkv").write_bytes(b"fixture")
    response = client.post("/api/jobs", json={"source_path": "Movie.mkv", "imdb_id": "tt0133093"})
    assert response.status_code == 201, response.text
    job = response.json()
    assert job["title"] == "The Matrix" and job["year"] == 1999
    assert job["release_name"] == "The.Matrix.1999.1080p.BluRay.x265.10bit-WiKi"
    assert job["imdb_id"] == "tt0133093"
    assert job["imdb_metadata"]["title"] == "The Matrix"
    assert client.get(f"/api/jobs/{job['id']}").json()["imdb_id"] == "tt0133093"
    manifest = yaml.safe_load((environment.workspace_root / job["id"] / "manifest.yaml").read_text())
    assert manifest["job"]["imdb_id"] == "tt0133093"
    assert manifest["imdb_metadata"]["year"] == 1999


def test_pair_looks_up_once_and_preserves_chosen_imdb_alternative(client, environment, imdb_provider):
    (environment.source_root / "Movie.mkv").write_bytes(b"fixture")
    response = client.post(
        "/api/jobs/pair",
        json={
            "source_path": "Movie.mkv",
            "imdb_id": "tt0133093",
            "title": "Matrix",
            "analysis_profile": "x264-live",
            "second_profile": "x265-live",
        },
    )
    assert response.status_code == 201, response.text
    assert imdb_provider[1] == ["tt0133093"]
    jobs = response.json()
    assert len(jobs) == 2
    assert all(j["title"] == "Matrix" and j["year"] == 1999 for j in jobs)
    assert {j["release_name"] for j in jobs} == {
        "Matrix.1999.1080p.BluRay.x264-WiKi",
        "Matrix.1999.1080p.BluRay.x265.10bit-WiKi",
    }
    assert jobs[0]["imdb_metadata"] == jobs[1]["imdb_metadata"]


@pytest.mark.parametrize("extra", [{"title": "Wrong movie"}, {"year": 2026}])
def test_creation_rejects_mismatched_preview_metadata(client, environment, imdb_provider, extra):
    (environment.source_root / "Movie.mkv").write_bytes(b"fixture")
    response = client.post("/api/jobs", json={"source_path": "Movie.mkv", "imdb_id": "tt0133093", **extra})
    assert response.status_code == 409, response.text
    with session() as db:
        assert not db.scalar(select(MovieJob))
        assert not db.scalar(select(Task))


@pytest.mark.parametrize("status", [404, 502, 504])
def test_lookup_failure_returns_actionable_error_and_creates_no_job(
    client, environment, imdb_provider, monkeypatch, status
):
    def fail(imdb_id):
        raise metadata.IMDbLookupError("IMDb unavailable; retry lookup", status)

    monkeypatch.setattr(metadata, "fetch_movie", fail)
    (environment.source_root / "Movie.mkv").write_bytes(b"fixture")
    response = client.post("/api/jobs/pair", json={"source_path": "Movie.mkv", "imdb_id": "tt0133093"})
    assert response.status_code == status
    assert response.json()["detail"] == "IMDb unavailable; retry lookup"
    assert client.get("/api/jobs").json() == []


def test_bad_or_incomplete_metadata_is_not_used(imdb_provider):
    raw, _ = imdb_provider
    for bad in (
        {**raw, "imdb_id": "tt0000000"},
        {**raw, "year": None},
        {**raw, "year": "1999"},
        {**raw, "title": "", "title_localized": None, "title_akas": []},
    ):
        with pytest.raises(metadata.IMDbLookupError):
            metadata.normalize_movie("tt0133093", bad)
    alternate = metadata.normalize_movie("tt0133093", {**raw, "title": "电影", "title_localized": "A Movie"})
    assert alternate["title"] == "A Movie"


def test_provider_process_has_deadline_and_no_application_credentials(environment, monkeypatch):
    seen = {}

    def run(argv, **kwargs):
        seen.update(argv=argv, **kwargs)
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr(metadata.subprocess, "run", run)
    with pytest.raises(metadata.IMDbLookupError) as caught:
        metadata.fetch_movie("tt0133093")
    assert caught.value.status_code == 504
    assert seen["argv"][1] == "-I"
    assert seen["argv"][-1] == "tt0133093"
    assert seen["timeout"] == 25
    assert not {"API_TOKEN", "DATABASE_URL", "AGENT_TOKEN", "POSTGRES_PASSWORD"} & seen["env"].keys()
    assert not Path(seen["cwd"]).exists()


def test_provider_imports_stdlib_queue_instead_of_application_module(environment, tmp_path, monkeypatch):
    # Exercise the real subprocess startup without making a network request.
    # A sibling queue.py caused imdbinfo's HTTP client to import our job queue.
    (tmp_path / "queue.py").write_text("raise RuntimeError('Application queue was imported')\n")
    (tmp_path / "imdb_provider.py").write_text(
        "import json, queue, sys\n"
        "assert queue.SimpleQueue().empty()\n"
        "print(json.dumps({'imdb_id': sys.argv[1], 'title': 'The Matrix', 'year': 1999}))\n"
    )
    monkeypatch.setattr(metadata, "__file__", str(tmp_path / "movie_metadata.py"))
    assert metadata.fetch_movie("tt0133093") == {
        "imdb_id": "tt0133093",
        "title": "The Matrix",
        "year": 1999,
    }


@pytest.mark.parametrize("exit_code,status", [(4, 404), (3, 502)])
def test_provider_errors_are_normalized(monkeypatch, exit_code, status):
    monkeypatch.setattr(
        metadata.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=exit_code, stdout="")
    )
    with pytest.raises(metadata.IMDbLookupError) as caught:
        metadata.fetch_movie("tt0133093")
    assert caught.value.status_code == status
