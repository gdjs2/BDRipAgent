"""Bridge executed by BDRip_Scripts' pinned Python, without importing the app.

Reuse its upload cache, templates, MediaInfo formatting, MD5 and torrent verifier.
Paths and inputs are prepared by the trusted worker; no shell commands are used.
"""

import json
import logging
import os
import re
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path
from urllib.parse import quote, quote_plus


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


class Progress:
    def __init__(self, path):
        self.path, self.phase = path, None

    def report(self, percentage, phase, **detail):
        write_json(self.path, {"percentage": percentage, "phase": phase, **detail})
        if phase != self.phase:
            print(f"Release: {phase}", flush=True)
            self.phase = phase

    def update(self, renderable):
        # BDRip_Scripts' torrent helper reports verified piece counts as Rich Text.
        text = renderable.plain
        verifying = "verifying torrent" in text
        match = re.search(r"\((\d+)/(\d+)\)", text)
        current, total = map(int, match.groups()) if match else (0, 0)
        start, span = (90, 9) if verifying else (72, 18)
        self.report(
            start + span * current / max(1, total),
            "Verifying torrent pieces" if verifying else "Hashing torrent pieces",
            completed_pieces=current,
            total_pieces=total,
        )


def metadata(request, progress):
    cache = Path(request["metadata_cache"])
    imdb_id = request.get("imdb_id")
    if imdb_id and cache.is_file():
        try:
            saved = json.loads(cache.read_text())
            if saved.get("imdb_id") == imdb_id:
                return saved["metadata"], []
        except (ValueError, KeyError):
            pass
    if imdb_id:
        progress.report(2, "Reading IMDb release metadata")
        try:
            response = subprocess.run(
                [sys.executable, "-I", str(Path(__file__).resolve()), "--metadata", imdb_id],
                capture_output=True,
                text=True,
                timeout=request.get("metadata_timeout", 25),
                check=True,
            )
            value = json.loads(response.stdout)
            write_json(cache, {"imdb_id": imdb_id, "metadata": value})
            return value, []
        except (subprocess.SubprocessError, ValueError):
            warning = "IMDb release metadata unavailable; saved title/year used, other movie details marked Unknown."
    else:
        warning = "No IMDb ID on this job; saved title/year used, other movie details marked Unknown."
    return {
        "name": request["title"],
        "year": request["year"],
        "genre": "Unknown",
        "rating": "N/A",
        "imdb_url": f"https://www.imdb.com/title/{imdb_id}/" if imdb_id else "Unknown",
        "release_date": "Unknown",
        "plot": "",
        "poster_url": "",
    }, [warning]


def run(request, token):
    from bdrip.release import bbcode, media, nfo, screenshots
    from bdrip.release.pipeline import extract_encoder_info
    from bdrip.release.torrent import create_private_torrent, md5_file

    details = request["details"]
    source = details.get("source", "")
    if not isinstance(source, str) or not source.strip() or any(ord(c) < 32 or ord(c) == 127 for c in source):
        raise ValueError("Enter a single-line Source in the Release form before generating files")
    source = source.strip()
    if not token.strip():
        raise ValueError("TU_TTG_TOKEN is required for screenshot uploads")
    output = Path(request["output_dir"])
    output.mkdir(parents=True, exist_ok=True)
    progress = Progress(output / "progress.json")
    progress.report(0, "Preparing release")
    movie = Path(request["movie_path"])
    original = movie.stat()
    package = Path(request["package_dir"])
    package.mkdir(parents=True, exist_ok=True)
    distribution_movie = package / movie.name
    if not distribution_movie.exists():
        # Both paths live under COMPLETED_ROOT; large movies need no second copy.
        try:
            os.link(movie, distribution_movie)
        except OSError:
            progress.report(1, "Copying release media (hard links unavailable)")
            shutil.copyfile(movie, distribution_movie)
    info, warnings = metadata(request, progress)
    info["name"], info["year"] = request["title"], request["year"]
    for warning in warnings:
        print(warning, flush=True)
    progress.report(5, "Reading final media information")
    technical = media.media_metadata(distribution_movie)
    smoke = request.get("smoke_test", False)
    encoder = "SMOKE TEST - Source reused" if smoke else "WiKi"
    if smoke:
        encoder_info = (
            "SMOKE TEST: video encoding and encode validation were skipped. Source video was reused."
        )
    elif request.get("encoder_log"):
        encoder_info = extract_encoder_info(Path(request["encoder_log"]), request["codec"])
    else:
        raise ValueError("The successful encoding task log is required for the release summary")
    encoder_path = output / "encoder-info.txt"
    encoder_path.write_text(encoder_info + "\n", encoding="utf-8")
    description = "\n".join(
        [
            f"◎译　　名　{details['chinese_name']}",
            f"◎片　　名　{info['name']}",
            f"◎年　　代　{info['year']}",
            f"◎类　　别　{info['genre']}",
            f"◎IMDb评分　{info['rating']}",
            f"◎IMDb链接　{info['imdb_url']}",
            "",
            "◎简　　介",
            "",
            info["plot"] or "暂无简介",
        ]
    )
    cache_dir = Path(request["cache_dir"])
    comparison = cache_dir / "Screenshots/Comparison"
    comparison.mkdir(parents=True, exist_ok=True)
    for pair in request["pairs"]:
        for index, kind in enumerate(("src", "encode")):
            # The upstream renderer sorts naturally: explicit 0/1 keeps Source on the left.
            name = f"{pair['frame_number']:09d}.{index}.{kind}.png"
            if smoke:
                name = "SMOKE-TEST." + name
            target = comparison / name
            if not target.exists():
                shutil.copyfile(pair[kind], target)
    cache_path = cache_dir / "uploads.json"
    uploads = json.loads(cache_path.read_text()) if cache_path.is_file() else {}
    # Retry every missing image, including unvisited images after an interruption.
    uploads.pop("failed_screenshot_uploads", None)
    cached = screenshots.cached_screenshot_urls(uploads, comparison.parent)
    total = len(request["pairs"]) * 2
    done = len(cached.get("Comparison", {}))
    progress.report(
        10 + 40 * done / total, "Uploading selected screenshot pairs", uploaded=done, total_images=total
    )

    def upload_event(event, path, attempt, attempts, error):
        nonlocal done
        if event == "success":
            done += 1
        progress.report(
            10 + 40 * done / total,
            "Uploading selected screenshot pairs",
            uploaded=done,
            total_images=total,
            image=path.name,
            attempt=attempt,
        )
        print(f"Screenshot {path.name}: {event} (attempt {attempt}/{attempts})", flush=True)

    uploaded, failures = screenshots.upload_screenshots_cached(
        uploads,
        cache_path,
        comparison.parent,
        f"{screenshots.remote_release_folder(movie)}/{request['remote_suffix']}",
        token,
        event_callback=upload_event,
        verbose=False,
    )
    if failures or len(uploaded.get("Comparison", {})) != total:
        raise RuntimeError("Some screenshot uploads failed. Retry this stage to resume the missing images.")
    config = {
        "file_path": str(distribution_movie),
        "screenshots_dir": str(comparison.parent),
        "imdb_id": request.get("imdb_id") or "unavailable",
        "movie_name": request["title"],
        "english_name": movie.stem.replace(".", " "),
        **details,
        "encoder": encoder,
        "source": source,
        "encoder_info": encoder_info,
        "movie_description": description,
    }
    progress.report(52, "Generating BBCode and NFO")
    # render() otherwise always refetches IMDb, even with complete overrides. This
    # isolated process supplies the one bounded/cached lookup to the upstream template.
    lookup = bbcode.imdb_metadata
    try:
        bbcode.imdb_metadata = lambda _: dict(info)
        post = bbcode.render(config, output, uploaded)
    finally:
        bbcode.imdb_metadata = lookup
    bbcode_path = output / f"{movie.stem}.bbcode.txt"
    bbcode_path.write_text(post.replace("[img][/img]\n\n", ""), encoding="utf-8")
    nfo_metadata = {
        **technical,
        **info,
        "file_name": movie.stem,
        "encoded_by": encoder,
        "imdb": info["imdb_url"],
        "source": source,
        "release_date": date.today().isoformat(),
        "hdr_format": None,
        "language": technical["languages"],
        "framerate": technical["frame_rate"],
        "resolution": f"{technical['resolution']} ({technical['aspect_ratio']})",
    }
    nfo_path = package / f"{movie.stem}.nfo"
    nfo_path.write_bytes(nfo.render_nfo(nfo_metadata, "cp437"))
    progress.report(58, "Calculating movie MD5")
    checksum = md5_file(distribution_movie)
    md5_path = package / f"{movie.stem}.md5"
    md5_path.write_text(f"{checksum}  {movie.name}\n", encoding="ascii")
    progress.report(72, "Hashing torrent pieces")
    torrent_path = output / f"{movie.stem}.torrent"
    infohash = create_private_torrent(
        {"torrent": {"tracker": details["tracker"], "verify": True}},
        package,
        torrent_path,
        status=progress,
    )
    current = movie.stat()
    if (original.st_size, original.st_mtime_ns) != (current.st_size, current.st_mtime_ns):
        raise RuntimeError("Final media changed while generating the release")
    result = {
        "infohash": infohash,
        "md5": checksum,
        "uploaded_images": total,
        "upload_host": "TTG",
        "smoke_test": smoke,
        "warnings": warnings,
        "screenshots": [
            {"filename": p.name, "url": full, "thumbnail_url": thumb}
            for p, (full, thumb) in sorted(uploaded["Comparison"].items())
        ],
        "artifacts": [
            {"path": str(path), "kind": kind, "storage": storage}
            for path, kind, storage in [
                (bbcode_path, "RELEASE_BBCODE", "workspace"),
                (torrent_path, "RELEASE_TORRENT", "workspace"),
                (encoder_path, "RELEASE_ENCODER_INFO", "workspace"),
                (nfo_path, "RELEASE_NFO", "completed"),
                (md5_path, "RELEASE_MD5", "completed"),
                (distribution_movie, "RELEASE_MEDIA", "completed"),
            ]
        ],
    }
    write_json(output / "result.json", result)
    progress.report(100, "Release files ready", uploaded=total, total_images=total)
    return result


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "--metadata":
        from bdrip.release.media import imdb_metadata

        print(json.dumps(imdb_metadata(sys.argv[2]), ensure_ascii=False))
        return
    request = json.loads(Path(sys.argv[1]).read_text())
    token = os.environ.get("TU_TTG_TOKEN", "")
    secrets = [token, quote(token, safe=""), quote_plus(token), request["details"]["tracker"]]

    def redact(value):
        for secret in secrets:
            if secret:
                value = value.replace(secret, "[redacted]")
        return value

    class SecretFilter(logging.Filter):
        def filter(self, record):
            record.msg, record.args = redact(record.getMessage()), ()
            return True

    handler = logging.StreamHandler()
    handler.addFilter(SecretFilter())
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
    try:
        run(request, token)
    except Exception as error:
        print("Release generation failed: " + redact(str(error)), file=sys.stderr, flush=True)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
