# BDRip Agent

A self-hosted encoding dashboard for already-ripped MKVs. Deterministic workers
inspect, extract, encode, validate and remux media. A separate Codex service selects
source-only comparison frames. You choose the preserved tracks and final encoding target.
After choosing screenshots, fill in the release details to generate posting files,
optionally upload the selected comparison pairs, and create a verified private torrent.

The workflow implements [docs/impl.md](docs/impl.md) and the refinements in
[docs/impl-add.md](docs/impl-add.md). The worker includes pinned builds of
BDRip_Scripts (CRF Studio) and Sup2Sup. Live visual selection requires Codex login.
See [remaining decisions](docs/open-questions.md) for the assumptions to review.

## Run with Docker

From the project directory, run:

```sh
./start.sh
```

The launcher creates `.env` with three independent random credentials, creates
storage directories, builds and starts all six services, and waits for startup.
It preserves existing credentials and settings, and reuses your saved Codex login.
On the first interactive run, follow the displayed browser instructions to log
into Codex. Python, Node and the media tools are installed inside containers.
The Linux host needs Docker Engine with the Compose plugin, Bash, OpenSSL and
`flock` (normally included with util-linux), plus access to the Docker daemon.

Open **http://localhost:8080** and sign in with the `API_TOKEN` stored in `.env`.
Movies go in `data/incoming/` by default. To use a NAS or another location, set
`STORAGE_ROOT` in `.env` before starting; the launcher creates the subdirectories
there. You can copy `.env.example` first to customize settings—the placeholder
credentials will be filled automatically. Exported variables override `.env`,
following Docker Compose precedence.

For startup without a Codex login prompt, use `./start.sh --no-login`. In a
noninteractive shell, the launcher starts the server and prints the login command
instead of waiting for browser authentication. Complete it later with:

```sh
docker compose exec agent codex login --device-auth
```

Use `docker compose logs -f api worker agent` to inspect logs, or
`docker compose down` to stop services while retaining data.

Only the frontend port is published, bound to localhost by default. For a remote
browser, use an SSH tunnel or configure `BIND_ADDRESS` and your HTTPS reverse proxy.
Set `COOKIE_SECURE=true` for HTTPS. The API uses an HttpOnly SameSite cookie; clients
can also send `Authorization: Bearer <API_TOKEN>`. Credentials never appear in SSE
or artifact URLs. The source mount is read-only. Removing a job requires confirmation,
cancels its queued tasks, and retains source and generated files. A running task
must be cancelled and allowed to stop before its job can be removed.

Transfer movies as `Movie.mkv.partial` and rename to `Movie.mkv` after copying.
The application cannot determine that a directly copied `.mkv` is complete.

## Workflow

1. Discover a source, enter an IMDb ID (such as `tt0133093`) or title URL, and click
   **Look up IMDb**. The form fills the movie title and release year and previews
   release filenames. Select an alternate IMDb title if desired. Leave IMDb ID
   empty to enter title/year manually. By default the form creates separate x264
   and x265 jobs, each with its own track and encoding decisions; a single codec is also
   supported. The filename's audio tag is finalized after track selection.
2. The worker scans the source, extracts all PGS subtitles for content analysis,
   and samples audio locally. Chinese variants use OCR rules first, then the agent
   reviews SDH, audio descriptions and suggested flags before track selection opens.
   Prompts and responses stream in the Tracks tab.
3. Review descriptions, edit track names and flags, and confirm audio/PGS tracks.
   Unknown flags require a Yes/No choice for selected tracks. The worker reuses
   analyzed PGS files, extracts selected native audio plus timestamps, and crops PGS
   through Sup2sup. Empty selections are permitted.
4. CRF Studio samples the selected profile/crop. Open **CRF analysis** to watch
   overall progress, completed encodes, the current CRF/sample, submitted frames,
   encoder flushing, and elapsed time. The chart plots video bitrate
   (Mbps) against average B-frame QP. Hover to inspect an estimate; click/tap or
   enter an exact bitrate to pin it. **Unpin** or right-click releases the point.
   In **Final encode selection**, choose **Constant quality (CRF)** or
   **Average bitrate (2-pass)** and enter the CRF or target video Mbps. A pinned
   point can fill either target using **Use CRF** or **Use bitrate for 2-pass**.
   **Confirm & add to queue** queues the encode. Predictions outside the measured
   range are labelled as extrapolations.
5. HandBrake runs independently of your browser, with persistent logs and SSE
   progress. Two-pass encoding shows the current pass and overall completion and
   occupies one queue slot for both passes. The **Encoding** tab shows the full saved
   configuration, encoder options, crop/output dimensions and actual HandBrake command.
   **Pause encoding** suspends the running encoder; **Resume encoding** continues it
   without restarting. Validation fully decodes both videos, checks all frame timestamps,
   frame counts, duration, dimensions, color metadata, bit depth and codec.
6. A deterministic mkvmerge plan combines video, ordered selected tracks, chapters
   and global source tags. Audio timestamps are restored. The final output appears
   under `completed/<job-id>/<WiKi-release-name>.mkv`. The MKV title and encoded
   screenshot label use that same release name. Track labels use readable language,
   audio format/channels or subtitle format/SDH/Forced; absent flags add no text.
7. The screenshot subsystem seeks through short windows across the movie, aiming
   for 100 usable candidates. It detects local histogram scene changes,
   rejects poor technical candidates, deduplicates them and creates contact sheets.
   Codex retains up to 40 source frames and ranks the best 15 review choices.
   The default recommendation split is nine representative frames and six encoding
   challenges, with at least ten featuring characters. The job pauses for review.
   Frames must be at least 30 seconds apart, including across x264/x265 jobs for the
   same source path, size and modification time. Conflicting manual replacements
   are rejected; insufficient candidates fail explicitly for review.
8. In **Screenshots**, open **Best 15** to inspect source/encode comparisons and
   choose any **1–15 pairs**. **Shortlist** shows all retained source frames (up to
   40), with full-resolution previews. Use **Remove from best** and **Add to best**
   to edit the saved best list (up to 15). Added frames get comparison pairs when
   rendered. Click **Confirm N & render** to export your
   chosen set. Completed older jobs offer **Prepare best 15** to review their saved
   shortlist without repeating encoding or the source scan.
9. The renderer matches source/encode PTS, applies the stored source crop, and adds
   the three-line yellow overlay. Exports are separate full-resolution PNGs with
   no border, panel, resizing or enhancement. The **Final** view shows the chosen
   pairs; you can return to **Best 15** and confirm a different subset later.
10. When rendering finishes, open **Release** (or **Continue to release**). Enter
    the **Chinese name**, **Source**, optional **extra description**, and **tracker announce
    URL**, including its passkey if needed. **Source** is the original disc/release
    description used in NFO and BBCode. Paste a dotted name and click **Convert dotted
    name** to apply BDRip_Scripts’ source format; review or edit it before saving.
    **Save details** stores a draft.
    **Generate files** creates `.bbcode.txt`, `.nfo`, `.md5`, and `.torrent` files.
    Enable **Upload screenshots to TTG** to upload the selected comparison pairs;
    otherwise the BBCode comparison section remains empty, even if an earlier run
    uploaded images. Uploads default to off for new release forms.
    Download them from the Release tab. The job becomes complete after generation
    and torrent piece verification succeed.

Optional screenshot uploads use BDRip_Scripts' TTG image host. To enable them, add its API token to `.env`:

```dotenv
TU_TTG_TOKEN=your-ttg-image-host-api-token
```

Apply the setting to the API and worker (keep the GPU overlay if you use it):

```bash
docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d api worker
```

A TTG token is required only when uploads are selected. Successful upload URLs
persist, so retrying an upload-enabled generation resumes missing images. Final
files are grouped under `STORAGE_ROOT/artifacts/`:

```text
artifacts/
  20260920-155200 [ART] Movie.2026.1080p.BluRay.x264-WiKi/
    Movie.2026.1080p.BluRay.x264-WiKi/
      Movie.2026.1080p.BluRay.x264-WiKi.mkv
      Movie.2026.1080p.BluRay.x264-WiKi.nfo
      Movie.2026.1080p.BluRay.x264-WiKi.md5
    Movie.2026.1080p.BluRay.x264-WiKi.bbcode.txt
    Movie.2026.1080p.BluRay.x264-WiKi.encoder.txt
    Movie.2026.1080p.BluRay.x264-WiKi.torrent
```

The outer timestamp is UTC at first export and remains stable on retries and
regeneration. Separate jobs receive separate folders. The inner directory and
torrent basename retain the BDRip_Scripts WiKi name; the torrent contains only the
MKV, NFO, and MD5. Exports use hard links where possible. Existing user folders are
never overwritten. The Release tab previews BBCode, NFO, MD5, and encoder notes
inline and offers the torrent download. Torrent generation does not submit to the
tracker or start seeding.

The launcher automatically relocates legacy releases after upgrading the services,
then removes the old torrent directory if it is empty. For a manual upgrade, run
the relocation utility using the updated worker code with the old directory mounted:
`python -m worker.pipeline.release_migration --legacy-torrents /torrents`.
It preserves artifact IDs and payload bytes, updates stored paths, and only removes
old files after the database commits. It also handles retained exports of deleted
jobs, refuses to relocate jobs with active work, and leaves unrelated files alone.
There is no separate torrent directory or mount for new releases.

The **Screenshot agent** panel shows each request's full prompt, attachment names,
and response text as it arrives. Choose an earlier selection attempt to inspect its
recorded transcript. Reloading or reconnecting replays saved events; older attempts
created before transcript recording show an explanatory empty state.

The **Task log** panel appears at the bottom of every job tab. It follows the
latest task automatically and refreshes running output every three seconds.
Choose an earlier attempt from **View task log** to inspect its saved output;
that choice stays selected when switching tabs. Return to **Latest task
(automatic)** to follow new stages.

## Downstream smoke test

After CRF analysis, open **CRF analysis → Test the remaining steps** and click
**Run smoke test — skip encoding**. No CRF or bitrate entry is required. This
uses the current job for testing; create a separate job for a real encode.

The worker reuses the immutable source video and skips HandBrake encoding and
full encode validation. Validation is explicitly **SKIPPED**, not passed. Real
remuxing, source candidate generation, configured screenshot-agent selection and
PNG rendering then run through the normal queue. Source decoding, remux I/O and
agent calls still take time; the agent still needs its usual login.
After choosing screenshots, smoke jobs also offer the Release tab. Its outputs
retain the SMOKE-TEST filename and identify reused source video in the release text.

The job, final MKV and exported comparison PNGs are marked `SMOKE-TEST`. The final
MKV contains uncropped source video with the selected prepared tracks, so this
does not test encoded quality or cropped-subtitle alignment. Both comparison
images use cropped source frames, and the right image is labeled as source reuse.
Smoke selections do not reserve screenshot frames against real encoding jobs.
Source files and normal CRF/two-pass behavior are unchanged.

## GPU screenshot scanning and B-frame comparisons

Choose **CPU** or **NVIDIA GPU (CPU fallback)** under **Screenshot scan decoder**
when creating a job, or save the choice in its **Screenshots** tab. GPU is the
default for new jobs and legacy jobs without a saved decoder; explicit CPU choices are retained. Changes apply to the next candidate scan or
retry; cancel an active scan before changing its decoder.

On a host with working NVIDIA drivers and NVIDIA Container Toolkit, start with:

```sh
./start.sh --gpu
```

This adds `docker-compose.gpu.yml` to give the worker GPU access. Continue using
`--gpu` on subsequent starts to retain that access. Ordinary `./start.sh` supports
CPU-only hosts. For direct Compose commands, use
`docker compose -f docker-compose.yml -f docker-compose.gpu.yml ...`.

GPU mode uses CUDA/NVDEC for the candidate scan. Initialization or unsupported-codec
failures fall back to CPU and are recorded in the task log and scan report. Decode
errors after frames have been emitted fail the attempt; choose CPU and retry.
The scan progress reports the actual decoder and advances even when no new
candidate is saved. This option does not change CRF analysis or final encoding.

**Both images in every final comparison must be B-frames.** Short CPU seeks refine
each candidate within the following half-second, stopping at detected scene changes,
to find a suitable B-frame pair while preserving exact source frame numbering and
PTS alignment. Only verified pairs reach the screenshot agent or manual replacement
list. Final full-resolution rendering uses CPU decoding and rechecks both picture
types, including in smoke mode. If too few pairs qualify, the task reports an error
instead of using I/P frames; sources without B-frames cannot satisfy this requirement.

## Queue and job removal

Open **Queue** to set **Maximum simultaneous jobs**, pause/resume scheduling,
move queued jobs up/down, or hold/resume an individual queued job. The default
limit is one. The worker can provide up to eight slots without a restart; change
`WORKER_CAPACITY` in `.env` and restart services to change that ceiling (1–64).
Higher concurrency shares CPU and memory between jobs.

The limit covers all automatic processing stages, including source analysis,
CRF analysis, encoding and validation. Each job runs only one stage at a time.
Track and CRF decision screens use no slot. Jobs enter the queue after a decision
or when an automatic stage finishes; ready stages start in queue order, normally
within ten seconds. Held jobs retain their position and are skipped until resumed.
Pausing the queue or lowering its limit lets current work finish its stage. Queue
settings, order and holds survive restarts. Failed tasks wait for an explicit retry.

For a running final encode, use **Pause encoding** on its progress card or Queue
row. **Resume encoding** continues the same encoder process, including a two-pass
encode. Paused encodes retain progress, memory and their queue slot; cancellation
still works. The worker keeps renewing its lease and excludes paused time from
its timeout and active elapsed counter. Keep the worker/container running: pause
is not a checkpoint that survives a service restart. A lost worker requires retry
from the beginning of encoding. Pause controls appear once a supported worker
starts HandBrake; CRF sampling and other stages continue to use queue controls.
Database migration `0004` adds the requested/acknowledged pause state.

Use **Remove** beside a job on the dashboard to hide it from the list and cancel
its queued work. The confirmation identifies the movie/profile. Files are retained;
this action does not clean up storage.

## Persistence and recovery

PostgreSQL owns jobs, task attempts, selections, profiles, artifacts, screenshots,
agent decisions and events. Redis is transport only. The worker's parent process
dispatches queued database rows every ten seconds; failed delivery is retried.
Database locking and a unique active-task constraint prevent duplicate execution.
All worker processes recheck the queue limit, pause, holds and ordering under a
shared database lock before admitting work. Celery autoscaling supplies worker
processes up to the configured ceiling; the database owns the actual running limit.

Running tasks renew a lease every five seconds. Expired leases are fenced and
marked failed at the existing stage. Retry creates a new attempt and preserves
the previous attempt/log. Each attempt has its own output directory. A lost DB
connection stops worker activity; process groups are terminated on cancellation
or timeout. Streaming agent requests check cancellation on heartbeat messages;
closing the request cancels the agent subprocess. Results are accepted only while
the worker still owns its task lease.

Restarting the API does not interrupt a worker with a live lease. Restarting a
worker during a command interrupts that stage; after lease expiry, retry it from
the UI. Encodes restart from the beginning of that encode attempt; HandBrake does
not checkpoint an unfinished bitstream. Earlier completed stages are preserved.

Human-readable manifests are database-derived snapshots regenerated at transitions
and API startup. They are not an independent mutable source of state. PostgreSQL,
Redis and Codex authentication use named volumes; movie files use bind mounts.
IMDb jobs retain their ID and a metadata snapshot in the database and manifest;
later encoding and remuxing use the saved title/year. Run `./start.sh` after updating
the code to rebuild services and apply database migrations automatically. Existing
jobs and files are preserved.

## Configuration and implementation

Deployment settings live in `.env`; behavior lives in `config/application.yaml`.
Encoder parameters live in `profiles/*.yaml`. The final encode uses the profile
snapshot from CRF analysis. To compare another profile, create another job.

| Directory | Responsibility |
| --- | --- |
| `backend/app` | FastAPI, authentication, decisions, state transitions, SSE, artifact/log access |
| `shared` | Configuration, SQLAlchemy models, paths and pipeline states |
| `backend/migrations` | Alembic schema history |
| `worker/adapters` | Deterministic CLI arguments, parsing and vendor integration contracts |
| `worker/pipeline` | Analysis, extraction, encoding, validation, mux and screenshots |
| `worker/tasks.py` | Celery execution, database outbox and recovery |
| `agent` | Python Codex CLI service, structured selection and diversity checks |
| `frontend` | React/TypeScript dashboard and ECharts curves |
| `tests` | API/workflow invariants and generated-media tests |

API routes follow the spec under `/api`; OpenAPI is available inside the API
container at `/docs`. The frontend exposes eight views: dashboard, new job,
overview, tracks, CRF analysis, encode status, screenshots and artifacts/logs.
The Release tab generates and previews publication files. Tracker submission and seeding remain outside V1.

## Development and verification

For contributors only, optional local development uses Python 3.12+, uv and Node 22.18+:

```sh
uv sync
uv run pytest -q
uv run ruff check .
cd frontend
npm ci
npm test
npm run build
```

Media tests use FFmpeg to create a tiny synthetic clip. Deployment never requires
host media tools. To generate the overlay calibration fixture:

```sh
uv run python -m scripts.calibrate_overlay --output data/calibration/overlay.png
uv run python -m scripts.calibrate_overlay --reference docs/43631.src.png
uv run python -m scripts.calibrate_overlay --reference docs/43631.wiki.png \
  --label No.Other.Choice.2025.1080p.BluRay.x264-WiKi
```

The overlay tests compare both supplied references. Baseline, line pitch, color,
outline and character spacing are calibrated; the open Liberation Sans font is
visually close but does not reproduce every reference glyph pixel.
The container tests exercise real CRF Studio, Sup2Sup, HandBrake and MKVToolNix.
Each harness documents its fixture boundaries. See [test notes](docs/testing.md).

Subtitle content is analyzed **before track selection**, including tracks you may
later discard. The Tracks tab shows Chinese script/Cantonese findings, SDH,
coverage, agent descriptions, and suggested flags. Edit the final MKV name and
Default, Forced, SDH/hearing-impaired, Audio description, and Commentary flags
before confirming. Original evidence and agent suggestions remain available;
explicit user choices take precedence during preparation and final muxing.

Audio review decodes three 30-second samples across each track locally and uses
[faster-whisper](https://github.com/SYSTRAN/faster-whisper) for local speech
recognition (CPU int8, `small` model by default). The first analysis downloads
model weights into `cache/speech-models`; subsequent jobs reuse them. The agent
receives metadata, signal measurements and transcripts, not raw audio. Descriptions
state sampling limits; failed/unavailable transcription is marked clearly. Set
`integrations.audio_review` in `config/application.yaml` to adjust the model,
sample count/duration, CPU threads, or disable transcription.

Inconclusive subtitles stay visible for review. Uncertain SDH can be overridden
explicitly; unresolved language/script still requires a successful content check
before muxing. Existing jobs waiting for track selection have an **Analyze tracks**
action. Rebuild the worker, agent, API and frontend to activate this workflow.
See [track detection](docs/integrations.md#naming) for configuration and limits.
