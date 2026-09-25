"""IMDb metadata and title order shared with BDRip_Scripts' release/catalog.py."""

import copy
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from collections import OrderedDict
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

from shared.config import get_settings
from shared.naming import release_name
from shared.original_languages import canonical_languages

_cache = OrderedDict()
_cache_lock = threading.Lock()


class IMDbLookupError(Exception):
    def __init__(self, message, status_code=502):
        super().__init__(message)
        self.status_code = status_code


def normalize_imdb_id(value):
    value = value.strip()
    if value.startswith(("https://", "http://")):
        url = urlparse(value)
        if url.netloc.lower() not in ("imdb.com", "www.imdb.com", "m.imdb.com"):
            raise ValueError("Use an IMDb title ID or an imdb.com title URL")
        match = re.fullmatch(r"/title/(tt\d{7,10})(?:/[^\s]*)?", url.path, re.I)
        value = match[1] if match else ""
    if not re.fullmatch(r"tt\d{7,10}", value, re.I):
        raise ValueError("IMDb ID must look like tt0133093")
    return value.lower()


def fetch_movie(imdb_id):
    # imdbinfo has no per-call timeout. A separate process gives the entire
    # lookup a deadline and isolates its client/cache state from API requests.
    env = {
        k: v
        for k, v in os.environ.items()
        if k
        in (
            "PATH",
            "LANG",
            "SSL_CERT_FILE",
            "SSL_CERT_DIR",
            "REQUESTS_CA_BUNDLE",
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "NO_PROXY",
            "http_proxy",
            "https_proxy",
            "no_proxy",
        )
    }
    with tempfile.TemporaryDirectory(prefix="bdrip-imdb-") as directory:
        try:
            result = subprocess.run(
                # Exclude the script directory from sys.path: app/queue.py
                # must not shadow the stdlib queue used by the HTTP client.
                [sys.executable, "-I", str(Path(__file__).with_name("imdb_provider.py")), imdb_id],
                cwd=directory,
                env=env,
                capture_output=True,
                text=True,
                timeout=get_settings().imdb_timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            raise IMDbLookupError("IMDb lookup timed out. Please try again.", 504) from None
    if result.returncode == 4:
        raise IMDbLookupError(f"IMDb did not find {imdb_id}. Check the title ID.", 404)
    if result.returncode:
        raise IMDbLookupError(
            "IMDb lookup is unavailable. Please try again or enter the title and year manually."
        )
    try:
        return json.loads(result.stdout)
    except (ValueError, TypeError):
        raise IMDbLookupError("IMDb returned an unreadable response. Please try again.") from None


def normalize_movie(imdb_id, raw):
    if not isinstance(raw, dict) or raw.get("imdb_id") != imdb_id:
        raise IMDbLookupError("IMDb returned metadata for a different title. Please retry the lookup.")
    year = raw.get("year")
    if type(year) is not int or not 1880 <= year <= 2200:
        raise IMDbLookupError("IMDb did not return a usable release year for this title.")
    # Same preference order as BDRip_Scripts: title, localized title, then AKAs.
    alternatives = raw.get("title_akas")
    values = [
        raw.get("title"),
        raw.get("title_localized"),
        *(alternatives if isinstance(alternatives, list) else []),
    ]
    options, seen = [], set()
    for value in values:
        if not isinstance(value, str):
            continue
        title = value.strip()
        if not title or len(title) > 300 or any(ord(c) < 32 for c in title) or title.casefold() in seen:
            continue
        seen.add(title.casefold())
        try:
            filenames = {codec: release_name(title, year, codec) + ".mkv" for codec in ("x264", "x265")}
        except ValueError:
            continue  # An IMDb-provided romanized AKA may still form a usable filename.
        options.append({"title": title, "filenames": filenames})
        if len(options) == 50:
            break
    if not options:
        raise IMDbLookupError(
            "IMDb returned no title usable for WiKi filenames. Enter a romanized title manually."
        )
    return {
        "imdb_id": imdb_id,
        "title": options[0]["title"],
        "year": year,
        "url": f"https://www.imdb.com/title/{imdb_id}/",
        "title_options": options,
        "original_title": raw.get("title"),
        "original_languages": canonical_languages(raw.get("original_languages")),
        "provider": "imdbinfo 0.9.1",
        "fetched_at": datetime.now(UTC).isoformat(),
    }


def clear_metadata_cache():
    with _cache_lock:
        _cache.clear()


def lookup_imdb(value):
    imdb_id = normalize_imdb_id(value)
    with _cache_lock:
        cached = _cache.get(imdb_id)
        if cached and cached[0] > time.monotonic():
            _cache.move_to_end(imdb_id)
            return copy.deepcopy(cached[1])
    movie = normalize_movie(imdb_id, fetch_movie(imdb_id))
    with _cache_lock:
        _cache[imdb_id] = (time.monotonic() + 3600, movie)
        _cache.move_to_end(imdb_id)
        while len(_cache) > 128:
            _cache.popitem(last=False)
    return copy.deepcopy(movie)


def job_identity(body):
    if not body.imdb_id:
        return {"title": body.title, "year": body.year, "imdb_id": None, "imdb_metadata": None}
    movie = lookup_imdb(body.imdb_id)
    title = body.title or movie["title"]
    if title not in {option["title"] for option in movie["title_options"]}:
        raise ValueError("Choose a title from the IMDb lookup, or clear IMDb ID to enter metadata manually")
    if body.year is not None and body.year != movie["year"]:
        raise ValueError("The release year differs from IMDb. Reload the movie metadata.")
    return {"title": title, "year": movie["year"], "imdb_id": movie["imdb_id"], "imdb_metadata": movie}
