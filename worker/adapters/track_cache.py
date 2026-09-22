"""Portable content analysis shared by jobs with the same immutable source and policy."""

import hashlib
import json
from copy import deepcopy

from sqlalchemy import select

from shared.config import ROOT, behavior
from shared.db import session
from shared.models import MovieJob
from shared.naming import automatic_track_name, track_name
from shared.paths import contained, job_dir, write_json
from shared.subtitles import SUBTITLE_ANALYSIS_VERSION
from shared.tracks import FLAG_NAMES, apply_flag_overrides, track_analysis_complete
from worker.adapters.source_tracks import link_file, source_key

ANALYSIS_KEYS = (
    "subtitle_analysis_version",
    "track_review_version",
    "audio_comparison",
    "audio_review_round",
    "audio_review_next",
)
TRACK_KEYS = (
    "language",
    "source_subtitle_metadata",
    "subtitle_detection",
    "audio_analysis",
    "track_review",
    *FLAG_NAMES,
)


def analysis_policy(analysis):
    config = behavior()
    audio = config.get("integrations", {}).get("audio_review", {})
    return {
        "version": 1,
        "subtitle_version": SUBTITLE_ANALYSIS_VERSION,
        "subtitle_cues": config.get("integrations", {}).get("subtitle_detection", {}).get("sample_cues", 96),
        "audio": {
            "transcription": audio.get("transcription", True),
            "model": audio.get("model", "small"),
            "sample_count": max(1, min(8, int(audio.get("sample_count", 3)))),
            "sample_seconds": max(5, min(60, float(audio.get("sample_seconds", 30)))),
            "max_rounds": max(
                1,
                min(
                    30,
                    int(
                        analysis.get("audio_review_policy", {}).get("max_rounds", audio.get("max_rounds", 6))
                    ),
                ),
            ),
        },
        "agent_model": config.get("agent", {}).get("model"),
        "prompts": {
            name: hashlib.sha256((ROOT / "agent/prompts" / name).read_bytes()).hexdigest()
            for name in ("subtitle_classification.md", "track_review.md")
        },
    }


def clean_result(result):
    """Copy content findings only; a donor's human choices never become recommendations."""
    value = {key: deepcopy(result[key]) for key in ANALYSIS_KEYS if key in result}
    value["tracks"] = []
    for track in result["tracks"]:
        item = {
            key: deepcopy(track[key]) for key in ("track_id", "kind", "codec_id", *TRACK_KEYS) if key in track
        }
        reviewed_flags = track.get("track_review", {}).get("flags", {})
        for flag in track.get("flag_overrides", {}):
            item[flag] = reviewed_flags.get(flag)
        if reviewed_flags:
            item.update(reviewed_flags)
        elif "subtitle_detection" in track:
            item["hearing_impaired"] = track["subtitle_detection"].get("hearing_impaired")
        value["tracks"].append(item)
    return value


def evidence_files(result):
    files = {}
    for track in result["tracks"]:
        for field, kind in (("subtitle_detection", "SUBTITLE_DETECTION"), ("track_review", "TRACK_REVIEW")):
            relative = track.get(field, {}).get("report")
            if relative:
                files[relative] = kind
        for sample in track.get("audio_analysis", {}).get("samples", []):
            if sample.get("path"):
                files[sample["path"]] = "AUDIO_SAMPLE"
    return files


class SharedTrackAnalysis:
    def __init__(self, ctx):
        self.ctx = ctx
        self.policy = analysis_policy(ctx.job.analysis)
        self.signature = hashlib.sha256(json.dumps(self.policy, sort_keys=True).encode()).hexdigest()
        self.root = ctx.settings.cache_root / "track-analysis" / source_key(ctx) / self.signature
        self.manifest = self.root / "analysis.json"

    def store(self, result, *, workspace=None, donor=None):
        if not any(
            any(key in t for key in ("subtitle_detection", "audio_analysis", "track_review"))
            for t in result["tracks"]
        ):
            return
        workspace = workspace or self.ctx.workspace
        value = clean_result(result)
        files = {}
        for relative, kind in evidence_files(value).items():
            self.ctx.check()
            source = contained(workspace, relative, exists=True)
            target = contained(self.root / "files", relative)
            link_file(source, target)
            files[relative] = {"kind": kind, "size": target.stat().st_size}
        # OCR images are needed if a completed local read has not yet been reviewed.
        # Retain them independently of the originating job/agent attempt.
        for track in result["tracks"]:
            relative = f"track-analysis/ocr-{track['track_id']}.json"
            pointer = contained(workspace, relative)
            if not pointer.is_file():
                continue
            try:
                record = json.loads(pointer.read_text())
                original = contained(self.ctx.settings.cache_root, record["root"])
                report = json.loads(contained(original, "ocr.json", exists=True).read_text())
                names = [
                    "ocr.json",
                    *report["contact_sheets"],
                    *(sample["image"] for sample in report["samples"]),
                ]
                sources = [contained(original, name, exists=True) for name in names]
                destination = self.root / "ocr" / str(track["track_id"])
                for name, source in zip(names, sources, strict=True):
                    link_file(source, contained(destination, name))
                target = contained(self.root / "files", relative)
                write_json(
                    target, {**record, "root": str(destination.relative_to(self.ctx.settings.cache_root))}
                )
                files[relative] = {"kind": None, "size": target.stat().st_size}
            except (OSError, ValueError, KeyError, TypeError):
                continue  # An incomplete OCR cache can be rebuilt; never publish partial analysis as complete.
        self.ctx.source()  # Check immutability again before publication.
        self.ctx.check()
        write_json(
            self.manifest,
            {
                "signature": self.signature,
                "policy": self.policy,
                "result": value,
                "files": files,
                "job_id": donor
                or result.get("shared_track_analysis", {}).get("source_job_id")
                or self.ctx.job.id,
            },
        )

    def seed_existing(self):
        """Adopt a compatible earlier job so upgrading does not force a new review."""
        with session() as db:
            jobs = list(
                db.scalars(
                    select(MovieJob)
                    .where(
                        MovieJob.id != self.ctx.job.id,
                        MovieJob.source_path == self.ctx.job.source_path,
                        MovieJob.source_size == self.ctx.job.source_size,
                        MovieJob.source_mtime_ns == self.ctx.job.source_mtime_ns,
                    )
                    .order_by(MovieJob.created_at.desc())
                )
            )
        for job in jobs:
            result = job.analysis
            if not track_analysis_complete(result):
                continue
            known = result.get("track_analysis_signature")
            if known and known != self.signature:
                continue
            if not known:
                # Old jobs have no configuration fingerprint: require current schemas,
                # matching limits, and the original evidence defaults before adoption.
                defaults = {"transcription": True, "model": "small", "sample_count": 3, "sample_seconds": 30}
                if (
                    result.get("subtitle_analysis_version") != SUBTITLE_ANALYSIS_VERSION
                    or analysis_policy(result) != self.policy
                    or self.policy["subtitle_cues"] != 96
                    or self.policy["agent_model"] is not None
                    or any(self.policy["audio"][key] != expected for key, expected in defaults.items())
                    or any(
                        t.get("subtitle_detection", {}).get("schema_version") != SUBTITLE_ANALYSIS_VERSION
                        for t in result["tracks"]
                        if t.get("codec_id") == "S_HDMV/PGS"
                    )
                ):
                    continue
            try:
                self.store(result, workspace=job_dir(self.ctx.settings.workspace_root, job.id), donor=job.id)
                self.ctx.log(f"Sharing completed source track analysis from job {job.id}.")
                return
            except (OSError, ValueError, KeyError, TypeError):
                continue  # Deleted/missing evidence is not a usable shared result.

    def restore(self, result):
        if not self.manifest.exists():
            self.seed_existing()
        try:
            saved = json.loads(self.manifest.read_text())
            if saved["signature"] != self.signature:
                return result
            cached = saved["result"]

            def identity(tracks):
                return {(t["track_id"], t["kind"], t.get("codec_id")) for t in tracks}

            if identity(result["tracks"]) != identity(cached["tracks"]):
                return result
            if result.get("audio_review_round", 0) > cached.get("audio_review_round", 0):
                return result
            paths = [
                (relative, detail, contained(self.root / "files", relative, exists=True))
                for relative, detail in saved["files"].items()
            ]
            if any(path.stat().st_size != detail["size"] for _, detail, path in paths):
                return result
            if not set(evidence_files(cached)) <= saved["files"].keys():
                return result
        except (OSError, ValueError, KeyError, TypeError):
            return result
        self.ctx.progress(None, phase="Reusing shared source track analysis")
        for relative, detail, source in paths:
            self.ctx.check()
            destination = contained(self.ctx.workspace, relative)
            link_file(source, destination)
            if detail["kind"]:
                self.ctx.artifact(destination, detail["kind"], info={"shared_analysis": True})
        by_id = {track["track_id"]: track for track in cached["tracks"]}
        tracks = []
        for track in result["tracks"]:
            item = apply_flag_overrides({**track, **deepcopy(by_id[track["track_id"]])})
            item["base_name"] = automatic_track_name({**item, "hearing_impaired": False, "forced": False})
            item["suggested_name"] = automatic_track_name(item)
            item["mux_name"] = track_name(item)
            tracks.append(item)
        self.ctx.log("Reusing shared audio/subtitle evidence, descriptions, and agent flag recommendations.")
        return {
            **result,
            **{key: deepcopy(cached[key]) for key in ANALYSIS_KEYS if key in cached},
            "tracks": tracks,
            "track_analysis_signature": self.signature,
            "shared_track_analysis": {"source_job_id": saved["job_id"], "reused": True},
        }
