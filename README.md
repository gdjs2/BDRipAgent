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

Use `docker compose logs -f api worker encoder agent` to inspect logs, or
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
2. The worker scans the source, then starts CRF analysis and background track
   review independently. Track review extracts supported audio, PGS subtitles, and
   audio/video timestamps together. Local preparation overlaps agent review; finished
   descriptions appear incrementally and selection opens automatically. OCR rules run
   first; visual agent review identifies
   subtitle languages, supported script/region variants and SDH from content.
   The agent describes audio and suggests flags before track selection opens.
   Prompts and responses stream in the Tracks tab.
3. Select audio/PGS tracks from the compact rows. Open **Details & edit** for names,
   flags and evidence. Unknown flags require a Yes/No choice for selected tracks.
   Drag the ⠿ handles to reorder audio or subtitles within their own groups; mouse,
   touch, and keyboard arrow keys are supported. **Confirm tracks** / **Update track
   choices** saves the order. The final MKV uses video first, then selected audio,
   then selected subtitles, preserving the displayed order within each group.
   The worker reuses extracted native audio, timestamps and analyzed PGS files,
   then crops selected PGS through Sup2sup just before remux. Empty selections are
   permitted. Choices can be saved and revised during CRF analysis or encoding;
   they are locked when remux preparation starts.
4. CRF Studio samples the selected profile/crop without waiting for track choices. Open **CRF analysis** to watch
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
   While the encode is queued, cancelled, or failed, **Adjust encoding target** lets
   you change the bitrate or CRF, including switching rate-control mode. Use **Save
   encoding target** to keep a queued job's position; cancelled/failed jobs stay
   stopped until you choose **Retry this stage**. Running and paused encodes must
   finish cancelling before their target can be changed.
   **Pause encoding** suspends the running encoder; **Resume encoding** continues it
   without restarting. Validation fully decodes both videos, checks all frame timestamps,
   frame counts, duration, dimensions, color metadata, bit depth and codec.
6. After video validation, the job waits for track choices if necessary, prepares
   selected tracks, then a deterministic mkvmerge plan combines video, ordered selected tracks, chapters
   and global source tags. Audio timestamps are restored. The final output appears
   under `completed/<job-id>/<WiKi-release-name>.mkv`. The MKV container title uses
   `Movie Name (Year)`, preserving the saved title's punctuation and Unicode. The encoded
   screenshot label uses the release filename. Track labels use readable language,
   audio format/channels or subtitle format/SDH/Forced; absent flags add no text.
7. The screenshot subsystem seeks through short windows across the movie, aiming
   for 100 usable candidates. It detects local histogram scene changes,
   rejects poor technical candidates, deduplicates them and creates contact sheets.
   Codex retains up to 40 source frames and ranks the requested number of best
   review choices (20 by default, adjustable from 2–40). The representative/challenge
   ratio follows your policy, with at least two-thirds featuring characters.
   The job pauses for review.
   Frames must be at least 30 seconds apart, including across x264/x265 jobs for the
   same source path, size and modification time. Conflicting manual replacements
   are rejected; insufficient candidates fail explicitly for review.
8. In **Screenshots**, open **Best** to inspect source/encode comparisons and
   choose any **1–15 pairs**. **Shortlist** shows all retained source frames (up to
   40), with full-resolution previews. Use **Remove from best** and **Add to best**
   to edit the saved best list (up to 40). Added frames get comparison pairs when
   rendered. Click **Confirm N & render** to export your
   chosen set. Completed older jobs offer **Prepare best N** to review their saved
   shortlist without repeating encoding or the source scan.
9. The renderer matches source/encode PTS, applies the stored source crop, and adds
   the three-line yellow overlay. Exports are separate full-resolution PNGs with
   no border, panel, resizing or enhancement. The **Final** view shows the chosen
   pairs; you can return to **Best** and confirm a different subset later.
10. When rendering finishes, open **Release** (or **Continue to release**). Enter
    the **Chinese name**, **Source**, optional **extra description**, and **tracker announce
    URL**, including its passkey if needed. **Source** is the original disc/release
    description used in NFO and BBCode. Paste a dotted name and click **Convert dotted
    name** to apply BDRip_Scripts’ source format; review or edit it before saving.
    **Movie description** accepts the full BBCode description block used by
    BDRip_Scripts. Leave it blank to generate the description from movie details and
    synopsis. **Extra description** is separate text beside the title heading.
    **Save details** persists release information for the source video. Existing
    and future encodes of the same unchanged source share the Chinese name, source
    description, extra description, full BBCode movie description, tracker and
    screenshot-upload preference. The information survives job removal and restarts.
    Updates appear automatically unless you have unsaved edits; conflicting edits
    require **Load shared release information** before saving.
    Each generation uses a saved snapshot, so changing the shared information does
    not change a running release task or regenerate another encode's existing files.
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

The **Encode Status** tab shows the completed encoder summary (the same content
exported as `*.encoder.txt`), including frame counts and average QP. Existing jobs
can read this summary from their retained encoding logs. **Overview → Source
details** shows video-track bitrate; it displays “Not reported” when unavailable
instead of substituting the bitrate of the entire container.

**Artifacts → Final video** provides an on-demand player with seeking, volume,
fullscreen, speed, chapter navigation, and video/audio/subtitle track selection.
The browser compatibility preview converts only requested six-second segments to
H.264 at up to 720p with stereo AAC. Selected text or bitmap/PGS subtitles are
rendered into the preview. It is for checking content and tracks; download the MKV
for original-quality playback. The API limits preview subprocesses to two and
keeps a bounded 64 MiB memory cache; closing the player cancels pending requests.
The expandable **MediaInfo · text report** shows metadata from the actual final
file. Preview and metadata requests require the normal authenticated session.

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

Open **Queue** to set three independent limits: **Maximum encoding tasks**
(default **1**), **Maximum CRF analysis tasks** (default **1**), and **Maximum other
tasks** (default **3**). Final video encoding uses the encoding pool, CRF analysis
uses the CRF pool, and source scans, track review/extraction, validation, remuxing,
screenshots, and release generation use the other pool. Limits count running tasks
across all jobs. A full pool does not block ready work in another pool.

The `encoder`, `crf`, and general `worker` containers consume `bdrip.encoding`,
`bdrip.crf`, and `bdrip.other`, respectively. The general worker dispatches all
three queues. Every worker checks the task type before claiming it; misplaced
or older deliveries cannot bypass pool limits. Migration `0008` adds the CRF
limit without changing existing limits or queue pause state.

The workers provide up to eight combined task slots by default. Change
`WORKER_CAPACITY` in `.env` and update all workers while idle to change this shared ceiling
(1–64). Each pool limit can be set up to that ceiling; combined running work also
stays within total capacity. Higher concurrency shares CPU and memory.

Pause/resume scheduling, reorder queued tasks, or hold/resume an individual task
from the same page. Within each pool, ready tasks start in queue order, normally
within ten seconds. Pausing the queue or lowering a limit lets running tasks
finish; paused encodes continue to occupy an encoding slot. Settings, order and
holds survive restarts. Migration `0006` replaces the old combined job limit with
the new 1/3 defaults while preserving queue pause, tasks, ordering and holds.
Failed tasks wait for an explicit retry.

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

Use `./start.sh` for normal application updates. It rebuilds the API, frontend,
general worker, CRF worker, and agent while preserving the existing `encoder` container and
its image. `encoder` does not depend on API/agent startup. Updating those services
therefore leaves an in-progress encode running with its database lease.

The media worker image builds **HandBrake CLI 1.11.2** from the
[official source release](https://github.com/HandBrake/HandBrake/releases/tag/1.11.2),
verified with its published SHA-256. It no longer installs Bookworm's older
`handbrake-cli` package. The first build compiles HandBrake and its bundled
libraries; later application rebuilds reuse that Docker layer. Compilation defaults
to four jobs (`docker compose build --build-arg HANDBRAKE_BUILD_JOBS=4 worker encoder crf`).
A compatibility launcher translates the previous two-pass option names so saved
encoding configurations remain usable. Check the installed version with
`docker compose exec -T encoder HandBrakeCLI --version`.

To update encoder code or its environment settings, pause scheduling in **Queue**,
wait until active encoder tasks have finished, then run
`./start.sh --update-encoder` (add `--gpu` if used for screenshot scanning).
The launcher checks these conditions before replacing an existing encoder. Resume
the queue afterward. Directly stopping the encoder or all services still stops
encoding; HandBrake has no restartable bitstream checkpoint.

On the first upgrade from the mixed worker, existing encodes stay in that container.
The launcher preserves it while encoding/CRF work is active or scheduling is unpaused.
After those tasks finish, pause the queue, run `./start.sh` again to replace the idle
legacy worker with the general worker, then resume scheduling. Future final encodes run in the separate encoder container; CRF analysis runs in
the separate CRF worker. A CRF task already running in an older encoder is allowed to
finish there before that container is updated.

Human-readable manifests are database-derived snapshots regenerated at transitions
and API startup. They are not an independent mutable source of state. PostgreSQL,
Redis and Codex authentication use named volumes; movie files use bind mounts.
IMDb jobs retain their ID and a metadata snapshot in the database and manifest;
later encoding and remuxing use the saved title/year. Run `./start.sh` after updating
the code to rebuild application services and apply database migrations automatically;
the encoder is preserved unless explicitly updated. Existing
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
later discard. The Tracks tab shows detected language codes and names, script or
regional variants supported by content, SDH,
coverage, agent descriptions, and suggested flags. Edit the final MKV name and
Default, Forced, SDH/hearing-impaired, Audio description, and Commentary flags
before confirming. Audio and subtitle groups initially follow the original MKV
track order. Drag tracks within their group to change the final order; saved manual
orders take precedence. Original evidence and agent suggestions remain available;
explicit user choices take precedence during preparation and final muxing.
Language tags use canonical BCP 47 codes, such as `fr`, `pt-BR`, `sr-Latn`,
`zh-Hans`, and `yue-Hant`; original source labels are never the language fallback.
A completed review never requires an extra **Analyze tracks** step. Missing initial
reviews start automatically; inconclusive findings do not trigger another run.
Failed tasks can be retried from Overview: completed subtitle reviews and OCR
are retained, so the worker continues with the unfinished work.

Audio review starts with matching 30-second samples across each track and uses
[faster-whisper](https://github.com/SYSTRAN/faster-whisper) for local speech
recognition (CPU int8, `small` model by default). The first analysis downloads
model weights into `cache/speech-models`; subsequent jobs reuse them. The agent
receives metadata, signal measurements and transcripts, not raw audio. Descriptions
state sampling limits; failed/unavailable transcription is marked clearly. Set
`integrations.audio_review` in `config/application.yaml` to adjust the model,
initial sample count/duration, CPU threads, or disable transcription. The agent
compares tracks directly: dialogue, dub/language, commentary, audio description,
and technical differences. When a difference remains unclear, it requests another
round for those tracks and their comparison partners. New samples spread through
the largest unexamined intervals; previous samples and transcripts are reused.
The agent stops early when it can explain the differences. A hard limit defaults
to six comparison rounds: set **Audio analysis maximum rounds** when creating a
job (1–30), or change `integrations.audio_review.max_rounds` for the server default.
The final allowed round must summarize possible differences and unresolved
questions for human review, without requesting an extra summary call.
If the hard limit or usable intervals are exhausted, or transcription is unavailable, the comparison
finishes with explicit uncertainty. The Tracks tab shows the evolving comparison
and its unresolved question. Silent samples skip speech inference, and identical
sampled mono PCM shares a transcript within the batch (this does not establish
identical multichannel mixes).

Release details can be saved at any stage, including while the video encodes.
Generation and screenshot upload still wait for the final MKV and rendered pairs.
Two independent task lanes per job have separate progress, cancellation and retry.
Release generation runs **one task at a time across all jobs**, including uploads
and torrent creation. It uses an other-task slot; additional release jobs wait
without blocking other kinds of work. A cancellation retains this slot until the
worker stops.
The queue applies separate encoding, CRF, and other-task limits, with a shared
worker-capacity ceiling; pausing an encoder does not pause its track review. Migration `0005` adds the lane field
and replaces the single-task constraint. Run the normal launcher after active
work finishes to migrate and rebuild all services together.

Inconclusive subtitles stay visible for review. Uncertain SDH can be overridden
explicitly; skip tracks whose language could not be identified. Source extractions
are cached by immutable source identity and shared by x264/x265 jobs and retries.
Content analysis, local transcripts/OCR, audio comparisons, and agent recommendations
are also shared when the source and analysis settings match. Concurrent jobs wait
for one shared review; completed checkpoints remain reusable after a failure.
Different analysis settings or a changed source require a separate review.
Confirmed audio/subtitle selections, their order, final track names, and flags are
shared across all encodes of that unchanged source. Save in any editable Tracks tab
to update the other encodes; new jobs adopt those choices after their review finishes.
Each encode freezes its choices when preparation for remux starts. Already prepared
or completed outputs retain their saved choices. Open tabs refresh untouched choices;
if another encode saves while you are editing, load the shared choices before saving.
Migration `0007` stores the source-wide choices. Existing confirmations are adopted
from the most recently saved selection for that source on reconciliation.
Rebuild the worker, agent, API and frontend to activate this workflow.
See [track detection](docs/integrations.md#naming) for configuration and limits.


## CPU monitor and logs

The floating **CPU** button shows total server utilization. Expand it to see a
scrolling, one-minute block chart, individual logical-core meters, **1-, 5-, and
15-minute load averages**, and **average current CPU frequency** in GHz. Each column
shows one two-second sample with stacked height bands: emerald from 0–30%, amber
from 30–70%, and coral from 70–100%. A tall column includes all three colors. Hover over the chart or focus it and use left/right arrow
keys to inspect a sample. Missing measurements leave gaps in the history. Drag its handle, or
focus the handle and use arrow keys, to reposition it. Collapse/expand preference
is remembered in the browser; the grid scrolls on hosts with many cores.

Measurements come from the host's read-only `/proc/stat`, `/proc/loadavg`, and
`/proc/cpuinfo` files, sampled once per second by the API and refreshed every two seconds in the browser. They cover
the whole Linux host, including other applications, rather than only the encoder.
On Docker Desktop this means its Linux VM. New or unavailable measurements appear
as a dash or “Unavailable”. Frequency averages the current MHz readings reported
for logical cores; it does not use the advertised model speed. Load averages count
runnable or I/O-waiting tasks and can be compared with the displayed core count.
Browser polling pauses when the tab is hidden.

Task logs and agent transcripts start folded. Expand their heading to read live
output; collapsing them stops log polling and closes the agent event stream.


## Revise tracks after encoding

On **Tracks**, choose **Edit tracks & remux** after the video has been muxed and
the current task has finished. Change included tracks, their order, names, flags,
or language codes, then choose **Save choices & remux**. The validated encoded
video, native audio/subtitle files and timestamps are retained, including
unselected tracks from analysis. Cropped subtitles are reused when available.
The original source remains required for chapters and container tags.

Language codes are checked against the installed language registry and normalized
(e.g. `jpn` → `ja`, `fre` → `fr`). The corresponding language name is shown next
to the code. Manual corrections preserve the original analysis evidence and are
shared with other editable encodes of the same source. A finished sibling needs
its own explicit remux request.

Remux uses a separate version directory and never reruns the encoder. Existing
screenshot choices are retained and rendered again so filename overlays match the
new video. On **Release**, choose **Generate files** to regenerate BBCode, torrent,
MD5 and NFO from that mux. Every generation gets a separate timestamped ART bundle;
a failed replacement leaves the previous files available.

Replaced outputs appear under **Artifacts → Backups** with their expiry date.
`OUTPUT_BACKUP_DAYS=3` keeps them for three days after successful replacement;
`0` disables automatic expiry. The general worker checks hourly and defers cleanup
while the job has active tasks. Encoded video, source tracks, timestamps, and current
outputs never expire through this cleanup. Modified backup files are retained.


During an upgrade from the original combined worker, `start.sh` starts a temporary
`general-upgrade` service and stops the old worker from accepting new tasks.
Its in-progress encodes keep running; automatic restart of that legacy container
is disabled so a reboot cannot restore its outdated task consumer. New general work uses the updated code,
and encoding deliveries on the legacy queue are forwarded to the dedicated
encoder. Once the original encodes finish, pause the queue and run `./start.sh`
once more to retire the bridge, then resume the queue.

You can add external subtitles in **Tracks → Add subtitles from a file**. Supported
formats are SRT, ASS/SSA (UTF-8 or UTF-16, up to 16 MB), and PGS `.sup` (up to
128 MB). Confirm the language and SDH status, upload, then select and order the
new track before saving. Text uploads (SRT/ASS/SSA) are queued for full agent
cleanup, language/SDH review, alignment against existing source subtitles, local
Subtitle Edit PGS conversion, and Sup2sup crop checks. They become selectable only
after language and timing verification and conversion. Remaining text problems
are reported without blocking PGS generation. Review progress, failures, retry controls, and the retained
original download appear under **Uploaded subtitle reviews**. A failed alignment
does not add an unverified track. PGS uploads keep their supplied timing/layout.
Font attachments are not uploaded.
Files are shared across encodes of the same source and retained for later remuxes.
For a finished video, use **Edit tracks & remux**, then regenerate release files.

### Find and align missing subtitles

Enable **Find missing subtitles during analysis** when creating a job, or use
**Tracks → Find missing subtitles** after track analysis. Discovery checks the
movie's original language(s), English, Simplified Chinese and Traditional Chinese.
English is included even when the movie's original language is not English; an
existing full English subtitle prevents a duplicate download. You can
supply original-language codes; otherwise the agent verifies them online with
sources. Dubbed audio is not treated as proof of a movie's original language.

The agent compares single-language SRT/ASS downloads (including ZIP bundles) by
movie/cut match, completeness, translation quality, terminology and timing evidence.
It explains its ranking and any unverified claims. The worker tries that order,
falling back only when a preferred candidate fails download, cleanup or alignment;
file format does not override quality between editable candidates.
Every dialogue cue passes through bounded cleanup batches before alignment or
conversion. The agent rejects bilingual/uncertain-language text, removes ads and
site promotions, and corrects clear typos, OCR errors and punctuation without
inventing dialogue. To repair damaged cues it cross-matches source dialogue and,
when needed, searches alternate subtitles or transcripts in other languages. It
can translate a reliably matched reference cue into the requested language and
records the local cue IDs or reference URLs and matching rationale. For Chinese, the agent repairs mixed scripts
and normalizes vocabulary, common expressions, grammar and terminology to the
requested variant: Mainland usage for Simplified Chinese, Taiwan usage for
Traditional Chinese unless a specific region is requested. Recent corrections
carry into subsequent batches for consistency. All supported repairs happen
before PGS rendering. Flagged batches receive a final reference-assisted repair
pass. Unrecoverable dialogue retains the best available text and is listed as a
critical error in the report; it does not block PGS rendering of a valid, aligned
single-language subtitle. The track has a red critical-issues badge. Each
edit records its original text, replacement/removal and reason. Originals and
cleaned SRT files are retained; the track's Details panel shows the cleanup audit.
ASS display formatting is normalized to plain subtitle text for consistent local
rendering. The agent also reviews SDH and edition, and matches dialogue against
existing source subtitles. The worker independently fits and checks the timing
transform, including FPS conversion and displacement. Acceptance requires at
least six distinct matches spread across the opening, middle and ending; a
mismatched cut, inconsistent drift, or insufficient source evidence goes to human
review. The workflow does not invent missing dialogue or translate an entire
bilingual file into a replacement subtitle.

Aligned text is rendered with the pinned Subtitle Edit 5.2.0 command-line
converter and Noto fonts, then checked and cropped by Sup2sup. PGS-only downloads
require human review; automatic discovery imports require editable text so that
the complete subtitle can be cleaned and rendered locally. Manual subtitle uploads
remain available.
**Agent-found PGS** tracks show source/download links, the agent's selection
reason, quality notes, matched dialogue, timing scale, offset and measured error.
They remain unselected until you review them. New imports require fresh track
confirmation before remux, including when you keep them unselected. They are
shared across encodes of the same source and retained for later remuxing.

Discovery runs in the other-jobs pool without holding up CRF analysis or encoding.
It can be canceled; **Search again** explicitly retries a search, including after
muxing when the video is ready for **Edit tracks & remux**. Review and select the
new tracks before remuxing; the existing video stays unchanged during the search.
Automatic mode makes one attempt per job and reuses completed source-wide results. Configure
`integrations.subtitle_discovery.max_candidates` (default 9, maximum 12) and
`max_seconds` (default 1800, maximum 7200) in `config/application.yaml`. Pages
requiring login, unsupported archives, and subtitles that cannot be aligned
reliably remain linked in the report for manual review or upload. Manually
PGS uploads retain their existing timing; text uploads and agent-discovered
subtitles both receive automatic alignment.

Migration `0009` adds the source release-information table. API startup backfills
the latest valid saved information for each source, including information from
removed jobs. Source identity uses the same path, size and modification time as
shared track choices; replacing the source file starts a separate record. Existing
release files remain available, with a notice when they use older information.


The number of **Best screenshot candidates** is adjustable from 2–40 when creating
a job and on its Screenshots tab. New jobs default to 20; older jobs retain the
previous default of 15 until changed. This controls the agent’s recommendations
per encode, independently of the final screenshot count. For example, request 20
and choose 7 for x264 and 7 for x265. Saving the count applies to the next review;
**Refresh best N** also saves the displayed count and reuses the existing shortlist
(up to 40 frames), retaining current final images until you confirm replacements.
If fewer eligible frames remain, the agent returns fewer choices. You can curate
up to 40 best choices manually and select 1–15 final pairs; cross-codec spacing and
B-frame checks still apply. A running review must finish or be cancelled before
its count can be changed.


After a full encode finishes, **Encode Status → Re-encode video** lets you choose
a new CRF or average bitrate and **Queue re-encode**. The saved codec/profile,
source analysis, track selections and release details are reused. Active work
must finish or be cancelled and stopped first; queued downstream pipeline tasks
are superseded atomically. The new encode uses a separate task output, preserving
the previous video and release files until replacements succeed. Validation,
B-frame candidate review, screenshot selection and release generation run again
for the new video. Existing final/release output backup retention still applies
(default three days after replacement). Sibling encodes are not restarted.

In **Tracks → Find missing subtitles → Agent-found subtitle tracks**, use
**Remove subtitle track** to remove an unwanted discovery from the shared source
inventory and future track choices across all encodes. Review and confirm the
remaining tracks afterward. The search report marks that result as removed.
Existing MKVs and their saved mux snapshots remain unchanged; use **Edit tracks &
remux** to update an existing video. Retained subtitle files are preserved for
older outputs. Removal waits while discovery, track review, track preparation or
remux work is queued/running for this source. Source-native and manually uploaded
tracks cannot be removed through this action. Removed IDs are never reused.


Every audio and subtitle row offers **Download track** in its expanded details.
Reviewed text uploads and agent-found subtitles also offer **Original subtitle**
and **Cleaned SRT** downloads; the main download is their checked, cropped PGS.
Native source tracks reuse retained extraction files. If an extraction is not yet
available, the download stream-copies just that track into an MKA/MKS container
and caches it for subsequent downloads, without re-encoding.


All agent operations share one first-in, first-out queue: screenshot selection,
subtitle search/cleanup/alignment/classification, and audio/track review. One
request runs at a time. Each subtitle cleanup batch rejoins the queue, allowing
other waiting reviews to run between batches. Waiting tasks show their queue
position, stay cancellable, and retain prepared inputs. The queue lives in the
agent service; persistent pipeline tasks remain retryable after a service restart.
`agent.busy_timeout_seconds` in `config/application.yaml` bounds the queue wait
(default 1800 seconds), separately from the model execution timeout. Screenshot
connection failures retain a separate four-attempt recovery limit.


**Agent Live** in the left navigation shows prompts and streamed agent responses
across all movies, including subtitle search/repair, track reviews and screenshot
selection. Conversations show the movie, encoding profile, review stage and status.
Expand a prompt to read it in full, load earlier conversations, or use **Follow live**
to return to the latest output after scrolling back. History survives refreshes;
reconnections resume from the last received event without duplicating response text.


Subtitle cleanup and repair use `agent.subtitle_cleanup.model: gpt-5.6-luna`
with `reasoning_effort: low` in `config/application.yaml`. These overrides apply
to text-cleanup batches; other agent operations keep the general model setting.
Agent Live records the selected model and effort. Cleanup and repair progress
retain their language and batch number while waiting in the agent queue and
when the request begins running.
