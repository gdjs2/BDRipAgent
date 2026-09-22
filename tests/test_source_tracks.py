import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

from worker.adapters.source_tracks import extract_tracks, source_key


def context(tmp_path, source, calls, name="job"):
    workspace = tmp_path / name
    workspace.mkdir(exist_ok=True)

    def run(command, **kwargs):
        calls.append([str(arg) for arg in command])
        for arg in command[2:]:
            if arg in ("tracks", "timestamps_v2", "--gui-mode"):
                continue
            _, filename = str(arg).split(":", 1)
            Path(filename).write_text(
                "# timestamp format v2\n0\n40\n" if filename.endswith(".txt") else "native track data"
            )

    return SimpleNamespace(
        workspace=workspace,
        settings=SimpleNamespace(cache_root=tmp_path / "cache", mkvextract_bin="mkvextract"),
        source=lambda: source,
        check=lambda: None,
        progress=lambda *a, **kw: None,
        log=lambda *a: None,
        run=run,
    )


TRACKS = [
    dict(track_id=1, kind="audio", codec_id="A_AC3"),
    dict(track_id=2, kind="subtitles", codec_id="S_HDMV/PGS"),
]


def test_paired_jobs_and_retries_share_one_extraction_and_all_timestamps(tmp_path):
    source = tmp_path / "source.mkv"
    source.write_bytes(b"source")
    calls = []
    contexts = [context(tmp_path, source, calls, name) for name in ("x264", "x265")]
    barrier = threading.Barrier(2)

    def extract(ctx):
        barrier.wait(timeout=5)
        return extract_tracks(ctx, TRACKS, video_track_id=0)

    with ThreadPoolExecutor(max_workers=2) as pool:
        outputs = list(pool.map(extract, contexts))
    assert len(calls) == 1
    assert calls[0].count("tracks") == calls[0].count("timestamps_v2") == 1
    for ctx, (tracks, video) in zip(contexts, outputs, strict=True):
        assert tracks[1]["timestamps"].is_file() and video.is_file()
        assert tracks[2]["path"].is_file()
        extract_tracks(ctx, [TRACKS[0]])  # Selection reuses only the chosen tracks.
    assert len(calls) == 1
    assert outputs[0][0][1]["path"].stat().st_ino == outputs[1][0][1]["path"].stat().st_ino


def test_missing_timestamp_is_repaired_without_reextracting_native_tracks(tmp_path):
    source = tmp_path / "source.mkv"
    source.write_bytes(b"source")
    calls = []
    ctx = context(tmp_path, source, calls)
    extract_tracks(ctx, TRACKS)
    root = ctx.settings.cache_root / "source-tracks" / source_key(ctx)
    (root / "track-1.timestamps.txt").unlink()
    extract_tracks(ctx, TRACKS)
    assert len(calls) == 2 and calls[-1][2] == "timestamps_v2"
    assert "tracks" not in calls[-1]


def test_legacy_extracted_subtitle_is_reused(tmp_path):
    source = tmp_path / "source.mkv"
    source.write_bytes(b"source")
    calls = []
    ctx = context(tmp_path, source, calls)
    (ctx.workspace / "previous.sup").write_bytes(b"original subtitle")
    result, _ = extract_tracks(ctx, [{**TRACKS[1], "subtitle_source_path": "previous.sup"}])
    assert calls == [] and result[2]["path"].read_bytes() == b"original subtitle"


def test_changed_source_does_not_reuse_previous_extraction(tmp_path):
    source = tmp_path / "source.mkv"
    source.write_bytes(b"source")
    calls = []
    ctx = context(tmp_path, source, calls)
    extract_tracks(ctx, TRACKS)
    source.write_bytes(b"different source")
    extract_tracks(ctx, TRACKS)
    assert len(calls) == 2
