# Test coverage

## They Will Kill You acceptance review

See [the 2026-09-21 acceptance report](e2e-review-2026-09-21.md) for the real
x264/x265 excerpt runs, full-movie review recovery, page-by-page browser checks,
and the limits of what was verified. `tests/container_movie_e2e.py` is an opt-in
actual-media harness that requires isolated `/review` storage.

## Encoding pause and configuration

`tests/test_encoding_pause.py` exercises authenticated controls, unsupported and
terminal states, idempotent requests, real process-group suspension, same-PID
resume, frozen progress, heartbeat renewal beyond the command timeout, retained
queue capacity, paused cancellation, and lease fencing/retry cleanup. The migration
test verifies old tasks keep their data with pause disabled. Frontend tests cover
pause acknowledgements, cancellation states, exact command selection per attempt,
literal shell-argument display, and crop/smoke dimensions. The configuration panel
uses the persisted profile snapshot and recorded command, not current profile files.

## HandBrake version upgrades

`tests/container_handbrake.py` checks the installed HandBrake **1.11.2** against all
four project profiles, each in CRF and two-pass bitrate mode. It uses real native
commands and checks automatic crop, progress from both passes, frame count, PTS,
codec, bit depth, color metadata and strict FFmpeg decoding. It also exercises the
compatibility launcher with spaces and shell metacharacters in filenames. Run it
without production mounts or credentials:

```sh
docker compose build worker
docker run --rm --init --network none \
  -v "$PWD/tests/container_handbrake.py:/app/container_handbrake.py:ro" \
  bdripagent-worker python /app/container_handbrake.py
```

The fixture and outputs use a temporary directory in the disposable container.
Run `tests/test_encoding_pause.py::test_real_handbrake_can_pause_resume_and_finish_valid_video`
in a development test image with the upgraded CLI to verify real process-group
pause/resume and complete output decoding as well.

Verified on 2026-09-21: all eight native profile/rate-control combinations passed
in the final worker image. The media, encode-target, scan-output and encoding-pause
regression suites passed 53 tests with the upgraded CLI.

## Initial track review

`tests/test_track_review.py` checks all PGS/audio descriptions before the selection
gate, cached extraction reuse, Unicode manual names, flag overrides, strict
validation, unknown flags requiring a user choice, legacy reanalysis and agent
stream authentication/evidence validation. `tests/test_audio_review.py` uses real
FFmpeg samples from two distinct streams to catch incorrect track-index mapping,
checks transcription failure reporting and cancellation, and verifies transcript
fields. Subtitle tests cover initial agent SDH review even with confident program
findings, inconclusive descriptions, and explicit user overrides.

`tests/container_audio_review.py` passed with real local `small` faster-whisper
inference on generated espeak commentary and FFmpeg sampling. Model weights are
downloaded on first use; no remote agent is called. `tests/container_track_review.py`
checks real MKVToolNix output with custom Unicode labels and explicit true/false
values for all five flags. Run these harnesses in an isolated worker test image;
espeak is only needed for the speech fixture, not in production.

## Track ordering

The browser track suite exercises mouse/touch dragging, keyboard reordering, cancelled
and cross-group drags, polling, deselect/reselect, saving and reloading, retained
names/flags, and disabled handles after remux preparation begins. API tests verify
that reordered ID arrays survive repeated saves and cannot change after the gate.
`tests/container_track_extraction.py` feeds the actual prepared files to MKVToolNix
and inspects its output: video first, then two reversed audio tracks and two reversed
PGS tracks, plus cases with either or both groups omitted.

## MKVToolNix progress

`tests/test_mkvtoolnix_progress.py` covers GUI and carriage-return output, every
split point in a progress record, duplicate and invalid records, bounded buffering,
subprocess updates persisted to the task API and event stream, warning/failure
exits, and the final report at process exit. It also exercises real `mkvmerge` and
batched `mkvextract` output when those binaries are installed. Tool completion
remains below 100% task progress until the worker accepts the stage's outputs.
Timestamp indexing maps measured progress into the first 5% of candidate generation.

The real batched-extraction container check and source-reuse smoke pipeline cover
track preparation, remuxing, and screenshot timestamp indexing with GUI output enabled.

## Agent streaming and separate release exports

`tests/test_agent_stream.py` uses a fake Codex app-server that waits for the client
to acknowledge a response delta before it can finish. It covers displayed prompts,
image attachments, structured outputs, omission of reasoning events, timeout and
cancellation cleanup, the internal stream, persisted task events, authenticated SSE,
replay, and reconnect cursors. The installed Codex CLI also passed an offline
initialize/thread-start protocol check without starting a model turn. No live model
selection is part of these checks.

`frontend/tests/agent-transcript.test.mjs` checks incremental text, final-message
replacement, and independent correction requests. Release tests cover upload-disabled
requests without credentials, strict booleans, new storage downloads, complete output
sets, repeat exports, and ownership conflicts. The native `container_release.py`
check verifies an empty BBCode comparison section with uploads disabled even when
cached image URLs exist, and independently verifies the resulting torrent pieces.

## Release stage

`tests/test_release.py` exercises the release-details gate, draft persistence,
manual-field validation (including required Source), credential presence/redaction, selected-image filtering,
missing/unverified image rejection, existing completed jobs, duplicate submissions,
active-task locking, result invalidation, output registration, and progress updates.

`tests/container_release.py` passed in the real worker image using the pinned
BDRip_Scripts interpreter. It creates a small MKV and comparison images, produces
real BBCode/NFO/MD5/torrent outputs, independently verifies every torrent piece,
checks source-left comparison ordering and the exact three-file torrent payload,
and verifies the original MKV remains unchanged. TTG upload responses are stubbed,
with networking disabled. The test simulates a failed upload followed by interruption
and proves retry uploads only failed or unvisited images; completed uploads are reused.
Both smoke mode and a normal x264 release with a real encoder summary passed.
At the release-stage checkpoint, the Python suite passed **236 tests**; frontend
tests and the production build also passed. The updated media pipeline smoke check reaches the release-details
gate for both profiles after rendering one confirmed comparison pair.

```bash
docker run --rm --init --network none \
  -v "$PWD/tests/container_release.py:/app/container_release.py:ro" \
  bdripagent-worker /opt/crf-studio/.venv/bin/python /app/container_release.py
```

Browser checks cover navigation from final screenshots and all release fields,
draft saving and polling, explicit generation, progress and shared task logs, four
artifact downloads, missing-token guidance, and mobile layout. Live uploads require
the operator's `TU_TTG_TOKEN` and were not performed by these checks.

The earlier media pipeline smoke scripts now stop at `WAITING_FOR_RELEASE_DETAILS`;
they leave upload/publication choices to the user. The separate release test above
verifies the new stage without publishing synthetic screenshots.

The Source-field regressions cover missing/blank/multiline values, legacy drafts and
retries, optional dotted-name conversion, custom text preservation, authentication,
result invalidation, and propagation of the entered source into the worker request.
The offline native test compares the formatter with BDRip_Scripts' installed
`source_description` and checks the actual NFO and BBCode source text.
Browser checks additionally verify required Source blocking, conversion without
saving or queueing a release, failed conversion preserving input, custom edits,
reload persistence, and old drafts prompting for Source while keeping other values.
The focused release suite passes 39 tests; the full Python suite passes 267 tests.
Native offline NFO/BBCode generation passes with the entered source in both outputs.

## Sparse screenshot sampling and editable best list

`tests/test_screenshot_sampling.py` uses real B-frame video with audio as the first
track and a 125 ms video offset. It checks bounded forward/backward seeks, early
stopping at the candidate target, coverage across the movie, exact zero-based frame
numbers, full frame counts, monotonic progress, and preservation of verified B-frame
pairs. Timestamp-index checks also cover variable frame intervals and count mismatch.

`tests/test_screenshot_review.py` covers 1, 3, 10, and 15 final pairs; persistent
best-list removal, additions from the shortlist, clearing and refilling; invalid
IDs, unverified frames, authentication, and edits during rendering. Curation preserves
existing exports. Confirmation retains spacing and cross-codec checks. Mocked visual
selection reuses all 40 shortlist images and returns 15 ranked recommendations.
The release gate accepts all 15 rendered pairs.

The isolated `tests/container_smoke_skip.py` test with `SMOKE_FINAL_COUNT=15` passes
for x264-live and x265-live: real analysis, extraction, remux, timestamp indexing,
sparse sampling, B-frame verification, 30 review PNGs, human confirmation, and 30
final PNGs, stopping at release details. Encoding is skipped; visual rankings are
fixtures. No production job or source file is modified.

An isolated CUDA benchmark on `They Will Kill You-SEG_FPL_MainFeature_t00.mkv`
retained 95 verified candidates from 142 windows. The scan decoded 11,852 frames
versus 136,034 in the full movie (91.3% fewer scan frames). Sampling including B-frame
verification took 223.6 seconds; indexing and contact sheets brought total time to
247.3 seconds. The decoded scan count excludes the additional short CPU verification
seeks. The target is 100; unusable buckets stop after four attempts.

Chromium checks cover saved add/remove edits, a full 15-slot list, all 40 shortlist
previews, chosen-item removal, reload/polling persistence, mobile layout, and at least
64 pixels between the gallery and task log. GPU is the default for new and legacy
jobs without a decoder; explicit CPU settings persist. Existing connection-recovery
checks exercise open previews through dropped background fetches. The deployed gallery
loads full-resolution images, exposes the new controls, and preserves the existing
40-image shortlist, ten recommendations, and seven final pairs until the user edits
or refreshes that job.

The full Python suite passes 249 tests, plus the subsequently added 15-pair release
and 95-candidate/40-shortlist regressions pass individually. Frontend unit tests and production builds pass.

## Existing coverage

The Python suite covers authentication, source discovery, path traversal and
symlink rejection, human gates, selected track IDs/codecs, profile consistency,
idempotent scheduling, retries, cancellation of queued tasks, lease recovery,
duplicate delivery, source-preserving deletion, bounded logs, CLI argument safety,
HandBrake parsing, CRF numeric validation, diversity validation and schema limits.

Generated-media tests use FFmpeg/PyAV to verify decoded frame count, exact PTS
matching, source crop, decoder picture type and full-resolution overlay behavior.

## Container pipeline test

This test uses real HandBrakeCLI, MKVToolNix, ffprobe, MediaInfo and PyAV from the
worker image, plus the pinned real CRF Studio CLI. Only screenshot choices are
fixtures; no live Codex account is used. A separate `container_subtitles.py` test
exercises the real Sup2Sup adapter and muxed subtitle/audio names and flags.

Generate a 12-second 640×360, 24 fps `Fixture.mkv` with AC3 audio, and place it in a
test incoming directory. Use FFmpeg in the worker image if it is not installed on
the host:

```sh
docker compose build worker
mkdir -p data/test-incoming
docker compose run --rm --no-deps -v "$PWD/data/test-incoming:/test-output" worker \
  ffmpeg -v error -f lavfi -i testsrc2=size=640x360:rate=24:duration=12 \
  -f lavfi -i sine=frequency=440:sample_rate=48000:duration=12 \
  -c:v libx264 -preset ultrafast -bf 3 -crf 15 -pix_fmt yuv420p -c:a ac3 -threads 2 \
  /test-output/Fixture.mkv
```

Copy `config/application.yaml` to a test file and change `candidate_count` to 20 and
`duplicate_hash_distance` to 0 (the synthetic pattern intentionally repeats).
For a quick smoke test, set `integrations.crf_studio.count` to 2 and `seconds` to 2;
the two CRF endpoints still use real native encodes. Then run the following,
substituting your worker image name and absolute paths:

```sh
docker run --rm --init --network none \
  -v "$PWD/data/test-incoming:/source:ro" \
  -v "$PWD/config/test.yaml:/app/config/test.yaml:ro" \
  -v "$PWD/tests/container_pipeline.py:/app/container_pipeline.py:ro" \
  -e DATABASE_URL=sqlite:////tmp/pipeline.sqlite \
  -e WORKSPACE_ROOT=/tmp/jobs -e COMPLETED_ROOT=/tmp/completed -e CACHE_ROOT=/tmp/cache \
  -e CONFIG_PATH=/app/config/test.yaml \
  -e API_TOKEN=container-test-token-24-characters \
  bdripagent-worker /app/.venv/bin/python /app/container_pipeline.py
```

The test expects COMPLETE, 288 source frames, a passing validation report, a final
MKV with its generated WiKi filename, `Movie Name (Year)` container title, and seven source/encode PNG pairs at
exactly 640×360. It also verifies that the mux restores the source video origin.
All generated state is inside the disposable test container. It does not consume production credentials.
Set `SMOKE_PROFILE=x265-live` in the test container to exercise the default 10-bit
x265 profile; both paths passed with the real vendor integration during refinement.

To test subtitle cropping, mount `tests/container_subtitles.py` as
`/app/container_subtitles.py` and the same fixture source directory, then run
`/app/.venv/bin/python /app/container_subtitles.py` in the worker image. It uses the
pinned Sup2Sup repository's redistributable synthetic cue generator, verifies
asymmetric crops at native/2× video resolution, and checks timestamps, palettes,
ordinary bitmap preservation, final container title and default/forced/SDH/commentary
flags through actual MKVToolNix inspection.

Initial verification also included all six Compose services starting successfully,
real source analysis and audio extraction through the API, persistent state and SSE
replay after container recreation, and Chromium checks of login, all dashboard
tabs, the new-job form and mobile layout. The refined form was also checked in
Chromium against an isolated real API: paired job creation, opposite-codec profile
choices, seven-frame defaults, dashboard redirect and desktop/mobile layout passed.

The original media/workflow suite contains 57 passing tests, including audio/language/subtitle
labels, upstream release naming, HDR/interlace rejection, seven-frame defaults,
character emphasis, paired creation and cross-codec/manual replacement conflicts.
Both supplied overlay PNGs are regression references; text-mask overlap is about
0.61 (source) and 0.63 (encode) with the supplied geometry and open font, so exact
font rasterization remains a documented limitation.

## Batched track extraction

`tests/test_track_preparation.py` covers mixed/audio-only/subtitle-only/empty
selections, selected track order, one extraction call including audio timestamps,
missing or empty outputs, and command failures. Failed batches do not publish
prepared tracks or begin subtitle cropping.

The native test generates two AC3 tracks (one with a delay) and two PGS tracks,
runs the actual preparation stage, compares extracted tracks and timestamp files
byte-for-byte against separate extraction, and verifies real Sup2Sup crop output
and cue preservation. Paths contain spaces and selection order is reversed.
All fixtures and outputs stay in a disposable container:

```sh
docker compose build worker
docker run --rm --init --network none \
  -v "$PWD/tests/container_track_extraction.py:/app/container_track_extraction.py:ro" \
  bdripagent-worker /app/.venv/bin/python /app/container_track_extraction.py
```

## Worker startup and late imports

The Compose worker and standalone image launch with `python -m celery`, keeping
the application root on Python's import path for the lifetime of each worker.
The console-script entry point only temporarily included it while loading the
Celery app, causing `No module named 'agent'` when the screenshot module was first
imported inside a later task even though `/app/agent` existed in the image.

`tests/container_worker_imports.py` launches a real Celery prefork worker and
imports the screenshot modules in a child task after startup. It uses an in-memory
broker, disables production dispatch, and needs no database, network, media or
credentials. Unlike calling pipeline functions from a Python test runner, this
checks the worker startup path itself:

```sh
docker run --rm --init --network none \
  -v "$PWD/tests/container_worker_imports.py:/app/container_worker_imports.py:ro" \
  bdripagent-worker python /app/container_worker_imports.py
```

Appending `--console-script` reproduces the old import failure.

## GPU screenshot scanning and B-frame checks

The Python suite passes 179 tests. The added decoder tests cover persisted CPU/GPU
choices, authentication, rejecting changes during a running scan, startup fallback,
first-frame preservation, decoder cleanup, and propagating midstream errors without
duplicating frames. B-frame tests cover agent decisions, manual replacement, real
source/encode timestamp and frame-number alignment, and rendering's independent
check of actual decoded picture types.

`container_smoke_skip.py` now creates an explicit B-frame source. Set
`SMOKE_SCREENSHOT_DECODER=cuda` and `EXPECT_SCREENSHOT_DECODER=cuda` when running it
with `--gpus all -e NVIDIA_DRIVER_CAPABILITIES=compute,video,utility`; both codec
smoke flows passed on the Tesla T4. Running without GPU access and with
`EXPECT_SCREENSHOT_DECODER=cpu` verifies fallback. Both tests check the actual
recorded backend and all fourteen final images' B-picture metadata per job.
CRF reports and visual choices remain fixtures; extraction, scans, remuxing,
B-frame refinement and image rendering are real.

Browser checks with mocked API data cover the legacy CPU default, saving/reloading
GPU choices, locking the setting during a scan, cancel/retry controls, progress,
filtering manual replacements to verified B-frame pairs, and GPU choices on new
paired jobs. Desktop and mobile layouts pass without horizontal page overflow.

## Screenshot selection recovery

`tests/test_screenshot_recovery.py` covers dropped connections, temporary service
unavailability, busy responses, bounded retries, cancellation during reconnects,
permanent errors, and retaining verified candidates across failed task attempts.
The focused recovery, B-frame, agent-schema and workflow checks pass 34 tests.
A browser regression check simulates an older tab retrying an already-retried task:
the view refreshes to the newer attempt and clears both stale error messages.

On 2026-09-20, the live source-reuse smoke job completed actual Codex selection and
rendered seven comparison pairs (fourteen PNGs). Every source/encoded image had
verified B-picture metadata. Its original HTTP disconnect coincided with replacement
of the agent container; the newer attempt succeeded. This checks live selection and
rendering, while encode-quality assessment still requires a normal encoded job.

## Screenshot gallery connection recovery

The event-stream and workflow regression checks pass 21 tests. Fresh streams skip
historical events, reconnects replay missed updates, invalid cursors are rejected,
and authentication remains required. Frontend API tests cover dropped requests,
temporary proxy errors, interrupted response bodies, bounded read retries, and
avoiding automatic mutation retries or false session-expired notifications.

A browser check sends 200 artifact events and observes one job read and one gallery
read. Simulated outages preserve the open comparison, image elements and unsaved
decoder choice, then reconnect automatically. Initial configuration failures recover
without displaying login; a real 401 still displays login. The frontend build passes.

## Live acceptance still needed

- Review sample predictions and PGS placement on representative feature films.
- Review visual selection quality on additional representative films.
- Exercise a full-length movie and a worker/host interruption during a long encode.
- Verify uncommon audio formats, track delays, chapters and relevant metadata on
  representative source files.

## Startup launcher

`tests/test_launcher.py` adds six focused regression tests for `./start.sh`. These
cover private independent credentials, repeated startup without credential rotation,
quoted/custom storage paths and exported overrides, treating `.env` as data rather
than shell code, unavailable Docker, failed startup, and duplicate settings. The
tests replace Docker with a controlled executable and do not deploy containers or
request a real Codex login. Run them with `uv run pytest -q tests/test_launcher.py`.

The launcher was also checked with Bash syntax validation and the installed Docker
Compose configuration parser. Existing container/media and browser checks above
cover the services themselves.

## IMDb metadata

`tests/test_imdb.py` and `tests/test_imdb_migration.py` add 26 cases covering ID/URL
validation, authentication, title preference and filename previews, cache expiry,
single/paired creation, persisted snapshots and manifests, mismatched metadata,
malformed upstream responses, failures/timeouts, and the provider process deadline.
The migration test upgrades an existing database and verifies that completed jobs
retain their metadata and file references. Unit tests replace the network lookup
with deterministic results. The full Python suite, including the scan and CRF
profile and queue regressions below, passes **110 tests**.

A live check retrieved `tt0133093` as **The Matrix (1999)** using both the local
client and a freshly built API container. Chromium then exercised that isolated
API with a disposable SQLite database: ID/URL lookup, title/year/filename autofill,
invalid or changed IDs, paired job creation, persisted IMDb links after reload,
and desktop/mobile layout all passed. No production credentials or jobs were used.

## HandBrake scan regression

HandBrake 1.6.1 can write its exit diagnostic to stderr while stdout is still
writing the JSON title set. Merging those streams corrupted a subtitle attribute
inside the affected movie's JSON. Analysis now saves stdout directly to
`handbrake-scan.txt` and retains stderr in the task log. The fallback crop parser
accepts both `autocrop:` and `autocrop =`, but still rejects conflicting values.

Regression tests force a diagnostic write between two JSON chunks and check both
text formats, zero crop, repeated equal values and conflicting crops. A live
analysis of `They Will Kill You-SEG_FPL_MainFeature_t00.mkv` passed for both x264
and x265 jobs: crop `104:104:0:0`, output dimensions **1920×872**, six selectable
tracks, and state `WAITING_FOR_TRACK_SELECTION`. This checks analysis only; it
does not encode the movie or choose tracks.

To repeat an isolated scan with the current worker image:

```sh
docker compose build worker
docker run --rm --init --network none \
  -v "$PWD/data/incoming:/source:ro" \
  -v "$PWD/tests/container_scan.py:/app/container_scan.py:ro" \
  -e DATABASE_URL=sqlite:////tmp/scan.sqlite \
  -e WORKSPACE_ROOT=/tmp/jobs -e COMPLETED_ROOT=/tmp/completed -e CACHE_ROOT=/tmp/cache \
  -e API_TOKEN=scan-test-token-24-characters \
  bdripagent-worker /app/.venv/bin/python /app/container_scan.py \
  '/source/They Will Kill You-SEG_FPL_MainFeature_t00.mkv'
```

Existing failed jobs can use **Retry this stage** after `./start.sh` rebuilds and
starts the updated worker. Earlier attempt logs remain available.

## CRF profile level regression

An unquoted YAML `level: 4.1` produced a JSON number, which the pinned CRF Studio
rejected with `codecs.x264.level must be null or a nonempty string`. The adapter
now converts a configured level to a string. Four regression cases cover decimal,
integer, string and null levels and check consistency with the HandBrake command.
The saved profile is preserved.

Native CRF Studio validation passed for all four current encoder profiles. A
container check on `They Will Kill You-SEG_FPL_MainFeature_t00.mkv` used the actual
x264 placebo profile and crop `1920:872:0:104`, with only sampling reduced to two
one-second clips. Both CRF 13 and 20 completed; the application accepted the
schema-version-5 report and generated all eight prediction points. Production
sampling remains ten ten-second clips.

## CRF analysis progress

`tests/test_crf_progress.py` covers the desktop GUI's overall percentage formula,
partial sample progress, the 99% completion ceiling, malformed/missing progress
files, duplicate suppression, live persistence/API reads and events, process
failure, and the last update written immediately before process exit. The process
tests use a silent child writing atomic JSON, so console output cannot supply
the progress accidentally.

`tests/container_crf_progress.py` exercises the actual CRF Studio executable with
both current codec profiles and the movie's `1920:872:0:104` crop. It requires an
isolated SQLite database and workspace under `/tmp`, a read-only `/source` mount,
and `TEST_SOURCE=They Will Kill You-SEG_FPL_MainFeature_t00.mkv`. Only sampling is
reduced to two one-second clips. It checks intermediate percentages and frame
updates, four completed sample encodes per codec, and accepted final reports.

Browser checks cover preparation, live updates without navigation, flushing,
elapsed time through polling/reload, queued/held/cancelling/failed states, retries,
completed results with the interactive curve, and desktop/mobile layout. Fixture
API routes keep production jobs unchanged during those checks.

The native x264 and x265 checks both passed, including live frame updates and all
four sample encodes per codec. Chromium checks passed on desktop and mobile,
including an elapsed duration over 24 hours. The Python suite passes 122 tests;
the six frontend prediction tests and the production build also pass.

## Interactive bitrate / QP chart

`cd frontend && npm test` runs six prediction tests using Node's built-in test
runner (Node 22.18+). They cover the measured endpoints, the logarithmic midpoint,
fractional CRFs, extrapolation, missing B-frame QP, equal endpoint rates, invalid
input and incomplete measurements. Frontend TypeScript and production build checks
also pass.

Chromium checks use a read-only copy of a completed movie job and expected values
calculated by the pinned BDRip_Scripts Python model. They cover typed and clicked
pins, hover, right-click release, persistence through polling/reload, explicit CRF
selection without submitting an encode, extrapolation/profile bounds, mobile
layout and touch pinning, missing QP and flat bitrate handling. No production job
is changed by these UI checks.

## CRF and two-pass bitrate selection

`tests/test_encode_targets.py` checks both codecs, saved profile/target and manifest
contents, a single queued encoding task, repeated confirmation, invalid/mixed
targets, integer bitrate limits, human/profile gates, legacy CRF commands, and
progress/ETA across the two passes. Existing CRF API requests remain covered by
the workflow suite. The Python suite passes 143 tests; the six frontend prediction
tests and production frontend build also pass.

`tests/container_two_pass.py` runs the actual API and worker against disposable
SQLite/storage paths under `/tmp`. It generates a 12-second 640×360 fixture and
uses both current profiles. Source analysis, HandBrake two-pass encoding and full
frame/timestamp validation are real; the CRF decision gate is seeded with the
profile snapshot to focus this harness on final encoding. Run it in the worker
image with `DATABASE_URL=sqlite:////tmp/test.sqlite`, `SOURCE_ROOT=/tmp/source`,
`WORKSPACE_ROOT=/tmp/jobs`, `COMPLETED_ROOT=/tmp/completed`, `CACHE_ROOT=/tmp/cache`,
and a test `API_TOKEN`. Do not use production storage or credentials.

Both codecs passed with real pass-one/pass-two progress and 288 validated output
frames. Browser checks cover default and legacy CRF selection, Mbps/kbit/s
conversion, explicit confirmation payloads, pinned bitrate/CRF actions, invalid
inputs, saved selection after reload, pass-aware progress, and desktop/mobile
layout. Browser fixture routes do not enqueue production work.

## Queue and list removal

`tests/test_queue.py` covers persistent/authenticated settings, limit validation,
FIFO and reordered dispatch, broker delivery reservations, lost delivery retries,
concurrent admission, lowering the limit, pause/hold/resume, stale broker messages,
duplicate work, queued removal with retained files, and refusal to remove running
jobs. Migration checks preserve existing jobs/tasks and seed a limit of one with
the queue running.

`tests/container_queue.py` is a separate integration harness for a disposable
PostgreSQL/Redis stack. Mount it as `/app/container_queue.py` in the worker image,
start Celery with `-A container_queue:celery worker --autoscale=3,1`, and execute the
file in a separate process with the same test database/storage settings. It replaces
source analysis with a six-second fixture task, observes two real worker processes
overlapping, lowers the limit to one, pauses/resumes, holds/releases a job, verifies
subsequent tasks do not overlap, and removes a queued fixture while retaining its
source. This harness must use an isolated database, since it creates jobs and
changes queue settings.

The real PostgreSQL/Redis/Celery harness passed, including observed simultaneous
worker processes. Chromium checks against the isolated API also passed for saved
limits, pause/resume, ordering, hold/release, removal confirmation/cancellation,
retained source files and desktop/mobile layout. Existing production jobs were
retained when applying migration `0003`.

## Skipping encoding for downstream smoke tests

`tests/test_smoke_mode.py` covers explicit selection, preparation/authentication
gates, job/queue/manifest labels, encoder avoidance, a skipped validation report
instead of a fabricated pass, retries, changed-source rejection, remux guards,
and isolation from real screenshot reservations. The Python suite passes 151 tests.

`tests/container_smoke_skip.py` runs both x264 and x265 profile jobs through source
analysis, audio extraction, smoke selection, skipped encoding/validation, real
remux, candidate generation and PNG rendering. It generates its own 12-second
source with an audio track before the video track, a timestamp offset and black
margins. CRF reports and visual screenshot choices are fixtures; a guard forbids
HandBrake encoding. Both jobs reached COMPLETE with preserved timestamps, an
unchanged source, a marked test MKV and 14 marked comparison PNGs. The comparisons
match below their overlays after applying the detected crop.

Run the harness in the worker image, mounted as `/app/container_smoke_skip.py`,
with isolated settings such as `DATABASE_URL=sqlite:////tmp/smoke.sqlite`,
`SOURCE_ROOT=/tmp/source`, `WORKSPACE_ROOT=/tmp/jobs`,
`COMPLETED_ROOT=/tmp/completed`, `CACHE_ROOT=/tmp/cache` and a test `API_TOKEN`.
All storage/database paths must be under `/tmp`; do not use production storage.
It generates media and adjusts only its disposable candidate configuration.

Chromium checks passed for running a smoke test with an empty CRF field, request
failure/retry, disabled ordinary confirmation afterward, persistence after reload,
marked job/queue, skipped validation, shared logs and desktop/mobile layout.
Production jobs were not started by these checks. Live screenshot-agent behavior
and subtitle placement on an actual feature remain outside this fixture test.

## Shared task log

The production frontend build and Chromium checks passed for one shared log panel
at the bottom of all six job tabs. Checks cover retained task selection across
navigation without remounting, automatic following of new tasks, running-log
polling, final output after completion, empty task history, and desktop/mobile
layout. Fixture API routes are read-only and leave production jobs unchanged.

Subtitle content tests (`tests/test_subtitle_detection.py`) cover Simplified and
Traditional Chinese, Cantonese in both scripts, ambiguous OCR, incorrect source
SDH flags, evidence validation, the streamed agent endpoint, and legacy prepared
jobs. `tests/test_subtitle_ocr.py` generates original PGS bitmaps and exercises real
Sup2Sup rendering, Tesseract, OpenCC, task progress, and MKVToolNix names/IETF tags/SDH
flags. These native tests require the worker OCR packages and `fonts-noto-cjk` for
fixture generation. The latter is test-only; production renders existing bitmaps.
`tests/test_subtitle_languages.py` covers canonical ISO aliases/BCP 47 tags,
non-Chinese labels, script and region distinctions, invalid/private codes, source
mislabeling, OCR model choice, stale-analysis invalidation and corrective retries.
Track-review tests accept actual subtitle cue citations, reject cross-track or
invented IDs, and exercise successful correction and bounded failure. Native OCR
fixtures include French, German and Russian, and MKV metadata checks include Latin,
Cyrillic, Arabic and regional language tags. Model responses are mocked in tests,
so no live Codex account or model call is used.

Release bundle tests cover timestamp ownership, collision handling, regeneration,
copy fallback, and preserving the original three-file WiKi torrent payload. Legacy
migration tests keep artifact IDs and download links, reject active jobs, and leave
unrelated files intact. Text preview tests cover authentication, traversal, bounded
reads, and NFO CP437 decoding. Native `tests.container_release` verifies torrent
pieces against the inner payload inside the ART wrapper.

`tests.container_screenshot_reservations` is an optional PostgreSQL concurrency test.
Run it only against a disposable database named `screenshot_reservation_test`; it
creates fixture rows and confirms simultaneous x264/x265 requests produce one winner
and one conflict for frame 1000. Unit/API tests additionally cover nearby frames,
the exact spacing boundary, gallery reservation markers, and deleted-job cleanup.
Screenshot rendering tests check that the last row uses the final MKV filename.


Track analysis pipeline regressions (`test_track_analysis_pipeline.py`) use synchronization
barriers to verify that preparing the next subtitle overlaps the current agent review.
They check automatic/idempotent scheduling, checkpoint recovery after a late failure,
and cancellation of background preparation. `test_source_tracks.py` races paired jobs
against the shared extraction cache, verifies one source pass, missing-timestamp repair,
legacy PGS reuse and changed-source invalidation. Native OCR tests also exercise one
real audio/PGS/video-timestamp extraction reused for audio sampling and frame indexing.

The optional `frontend/tests/browser-tracks.cjs` runs against a built frontend with
Playwright installed (or provided via `NODE_PATH`). It mocks all API requests and checks
compact rows, automatic analysis, manual names, explicit false flag overrides, and
mobile overflow. `UI_SCREENSHOT_DIR` chooses the output folder for its screenshots.


Adaptive audio and independent workflow regression tests are in
`tests/test_adaptive_audio.py` and `tests/test_parallel_workflow.py`. They prove
that the agent can request successive rounds within its configured budget, the
hard limit produces an inconclusive human handoff, a cancelled round reuses
prepared evidence, exhaustion remains inconclusive, and cited tracks/samples must
exist. A thread-barrier test runs CRF and track review concurrently
while saving a release draft, then verifies all results survive. Additional
checks cover early editable track choices, the late remux join, lane-scoped retry,
worker capacity, and migration of the active-task uniqueness constraint.


`tests/test_queue_pools.py` verifies independent encoding, CRF, and other-task
limits, cross-pool admission when one pool is full, held tasks, shared worker
capacity, and paused/cancelling encoder reservations. A worker-claim concurrency
test starts one encode alongside CRF and three source scans, rejects additional
other tasks, and checks duplicate delivery after slots reopen.
`tests/test_queue_pool_migration.py` exercises upgrade/downgrade, preservation of
queue pause, defaults, and database constraints. The optional Playwright harness
`frontend/tests/browser-queue.cjs` checks all three controls, usage counters, persisted
updates/reload, queue pause, and desktop/mobile layout with mocked APIs.


Phase progress uses completed work, with separate budgets for extraction, local
analysis, agent review, rendering, and publication. Tool percentages remain visible
as tool metrics; they do not finish the whole task. Parallel track preparation and
review share an aggregate progress tree. Unknown-duration operations (model loading,
agent calls, subtitle cropping, encoder flushing) use an activity indicator. Only a
successfully committed task reaches 100%. Validation reports decoded frames and
relative timestamps; release checksums and copies report processed bytes.

`tests/test_phase_progress.py` checks nested/concurrent progress, streaming callbacks,
transcription completion, validation frames, publication bytes, and the 100% boundary.
`frontend/tests/task-progress.test.mjs` covers task states and older worker reports.
With Playwright available, run `npm run build && node tests/browser-progress.cjs`
from `frontend/` to check measured progress, unknown work, completion, CRF flushing,
and desktop/mobile layout. `UI_SCREENSHOT_DIR` selects the screenshot folder.


Source-sharing regressions in `tests/test_source_sharing.py` cover original track
order, reuse without copying manual choices, cache invalidation, missing evidence,
legacy completed-job adoption, and portable audio/OCR evidence after donor cleanup.
`test_track_analysis_pipeline.py` also verifies that concurrent codec jobs perform
one shared review and resume from completed subtitle checkpoints after a failure.
Release tests cover same-source defaults, independent saved drafts, and multiline
movie-description BBCode. The native release harness checks the custom description
in the post generated by BDRip_Scripts, alongside torrent piece verification.
The track browser harness checks initial source order, drag ordering, shared release
prefill, preservation of unsaved edits during polling, and saving a custom description.


`tests/test_source_choices.py` checks bidirectional sharing of selections/order/names/
flags, existing and future encodes, deferred application after review, legacy adoption,
source isolation, frozen remux snapshots, and simultaneous edits with one revision
winner. A worker regression follows automatically shared choices through extraction,
preparation, and exact mux argument ordering. The track browser harness checks live
updates from another encode, preservation of local edits and drag order, and explicit
loading of the new shared revision before saving again.


`tests/test_cpu_monitor.py` covers host-counter deltas, guest-time double-counting,
I/O wait, resets/hotplug, unreadable data, authenticated access, and stable sampling
across multiple browser reads. `tests/test_worker_routing.py` verifies encoding-only
routing, CRF routing to its dedicated queue, stale-queue redelivery, and rejection of work delivered to the wrong worker role. Launcher tests
cover preserving an existing encoder, legacy mixed-worker protection, and explicit
encoder-update readiness checks.

`tests/container_worker_isolation.py` is a disposable PostgreSQL/Redis/Celery harness
restricted to `worker_isolation_test`. With a general worker consuming `bdrip.other`
a CRF worker consuming `bdrip.crf`, and an encoder consuming `bdrip.encoding`, it runs a 35-second fixture task, replaces
the general container, and verifies the encoder retains its lease and finishes;
CRF runs in its own worker; only final encodes use the encoder.
The fixture substitutes waits for media processing and never touches real movies.

With Playwright installed, `npm run build && node tests/browser-system.cjs` checks
folded logs stop traffic, agent streams open on demand, CPU readings update, a
96-core grid fits desktop/mobile viewports, dragging/keyboard movement stays on
screen, collapse preference survives reload, and missing data is not shown as 0%.


### Remux revisions, language corrections and CPU history

`tests/test_remux.py` covers explicit post-mux revisions, canonical language
verification, preserving detection evidence, shared manual corrections, separate
ART generations, three-day expiry, and guards against deleting current, modified,
linked or busy outputs. Its native fixture runs real MKVToolNix, screenshot
rendering and the pinned BDRip release tools, verifies changed language/order and
new MD5/torrent infohash, checks that the encoded video stays byte-identical, and
exercises an interrupted second remux and expired backup cleanup.

`frontend/tests/cpu-history.test.mjs` checks time slots, missing samples, expiry and
color boundaries. `browser-system.cjs` covers scrolling history, reduced motion,
keyboard inspection, mobile bounds and both live-profile defaults.
`browser-tracks.cjs` also covers completed-job remux editing, rejected/normalized
language codes, readable language names and the mobile track editor.
The launcher and routing tests cover a temporary general-worker bridge that stops
legacy consumption and forwards old-queue encoding tasks without claiming them.


### Monitor metrics and encoding-target edits

`tests/test_cpu_monitor.py` covers host 1/5/15-minute load averages, averaging
current per-core frequency readings, invalid/missing samples, and independent
metric availability. `tests/test_encode_target_edit.py` covers queued edits,
preserved hold/priority/profile, cancelled/failed edits followed by retry,
rate-control switching, numeric validation, and rejection after worker admission
or while paused/stopping. No active encoder settings are changed by these tests.

`node tests/browser-encode-target.cjs` checks saving/reloading a queued bitrate,
saving a cancelled job without starting it, explicit retry, changing rate-control
mode, read-only running/completed/smoke tasks, and mobile layout. The system browser
harness also checks live load/frequency updates and unavailable readings without
hiding valid CPU utilization.

## Subtitle discovery and timing alignment

`tests/test_subtitle_discovery.py` covers strict agent schemas, language/script
coverage, web-search isolation, source-shared scheduling, cancellation/selection
gates, unsafe downloads/archives, and timing alignment. Known offset/FPS fixtures
are accepted; invented or duplicate citations, insufficient timeline coverage,
wrong transforms and different-cut drift are rejected.

The native integration fixture supplies deterministic search, dialogue-match and
download responses, then uses real Subtitle Edit, Sup2sup and MKVToolNix. It
converts and aligns Chinese SRT, verifies source/cropped PGS dimensions, confirms
source-wide sharing and the fresh-review requirement, reuses the cropped PGS,
and checks the remuxed language. Additional fixtures render Korean SRT and
Traditional Chinese ASS and verify PGS timestamp changes preserve bitmap payloads.
No real movie or remote subtitle download is required for these tests.

`frontend/tests/browser-subtitle-discovery.cjs` tests automatic opt-in, manual
search, progress/cancel controls, the confirmation gate, unselected imported
tracks, source links, alignment details and mobile overflow using mocked APIs.
Run it like the other Playwright browser checks after building the frontend.

Verification on 2026-09-21: 110 focused backend/worker regressions passed, along
with the discovery browser acceptance and TypeScript production build. A separate
live read-only agent search returned cited candidates through the authenticated
endpoint. A live bilingual alignment fixture matched nine Chinese/English cue
pairs and recovered the known 25/24 timing scale and +1.200-second offset. These
probes did not import subtitles or change existing movie jobs. Current movie
workspaces were checked read-only and contain source OCR cues spanning the film;
a complete real-movie download/alignment remains dependent on a suitable online
subtitle and is not implied by the synthetic media tests.

## Persistent source release information

`tests/test_source_release_details.py` covers database persistence after all source
jobs are removed, new database sessions, revision conflicts, no-op saves, retained
generation snapshots, latest-valid legacy backfill (including removed jobs), and
the additive `0009` migration in both directions. Source-sharing tests ensure an
existing saved draft follows later source-wide edits and changed source identities
stay isolated. The release-worker test edits a sibling after queueing generation
and verifies the worker still receives its original source description.

`frontend/tests/browser-source-release.cjs` exercises two open encode pages: shared
values refresh, unsaved text survives incoming changes, stale saves are rejected
and recoverable, removed donor jobs have no broken link, and existing artifacts
show a notice when their information is older. It checks the form on desktop and
mobile.

## Artifact playback and encoder information

`tests/test_artifact_media.py` renders a synthetic MKV with two video tracks, two
audio tracks, SRT and PGS. It checks real preview segments, subtitle cues spanning
seek boundaries, stream selection, silent playback, timestamps, authorization,
path containment, stale-file rejection, subprocess limits, timeout/disconnect
cleanup, legacy encoder-log summaries and video-only bitrate fallback.

For the browser check, run `python -m tests.container_media_preview` in the native
media test environment (FFmpeg, MediaInfo, MKVToolNix and Subtitle Edit required).
This starts an isolated API on port 8000 with synthetic media and a temporary
SQLite database. Build the frontend and run `tests/browser-media-preview.cjs`
with Playwright available and `MEDIA_TEST_API` pointing to that server. The check
plays real HLS, seeks across segment boundaries, changes video/audio/PGS/text
tracks while paused and playing, preserves position/speed, and checks the
MediaInfo, encoder summary, source bitrate and mobile layout.

Verification on 2026-09-22: 83 focused backend regressions and the real Chromium
playback check passed. The deployed API also returned encoder summaries for both
They Will Kill You encodes, the source video bitrate (32,860,648 bit/s), and the
final x265 MediaInfo report. A six-second preview from 03:00 converted HEVC/AC3
with a selected PGS track to playable H.264/AAC. This was a bounded playback check;
original files and running processing workers were left unchanged.

## Adjustable screenshot recommendations

`tests/test_screenshot_review.py` checks recommendation counts of 2, 15, 20 and
40, reuse of the existing shortlist, strict request bounds, the running-review
guard, preserving existing finals, manual curation beyond 15, and choosing seven
final pairs from twenty options. The fallback count is bounded by available
candidates. New jobs default to 20; old policies without this field retain 15.

`frontend/tests/browser-screenshot-count.cjs` checks saving and refreshing the
count, preserving existing images on save, disabling edits during review,
cross-codec reservations, choosing seven from twenty, limiting the bulk selection
to fifteen, and mobile layout. Verification: 65 focused screenshot regressions,
the browser check, and the production frontend build passed.

## Re-encoding and subtitle cleanup

`tests/test_reencode.py` checks CRF/bitrate revisions, immutable-source checks,
profile limits, double submissions, queued follow-up cancellation, active-task
protection, preserving previous output files and source settings, and invalidating
old screenshot/validation state. The encoding browser regression also queues a
replacement from a completed job.

`tests/test_subtitle_cleanup.py` checks full-cue coverage across batches, ad removal,
typo correction, timing preservation, bilingual/wrong-language rejection, uncertain
text, invented evidence, unsupported duplicate removals, markup and strict schemas.
The native subtitle-discovery test removes an ad and corrects a Chinese typo before
real Subtitle Edit rendering, alignment validation, Sup2sup cropping, shared track
registration and remux preparation. 78 focused backend regressions passed, plus
encoding/subtitle browser checks and the frontend production build.

Live deployed-agent checks also passed on synthetic dialogue: it removed a
subtitle-site advertisement, corrected a missing apostrophe, and rejected parallel
English/Chinese translations. These checks imported no tracks and changed no movie
jobs. The deployed API reports re-encoding available for the completed They Will
Kill You and The Phoenician Scheme jobs; no real re-encode was started during
verification. Active encodes, validation and release generation were preserved.

## Removing discovered subtitles

`tests/test_subtitle_removal.py` checks shared removal from existing/future encodes,
retained files and completed mux snapshots, removal from editable selections,
report updates, repeat requests, monotonic external-track IDs, active sibling
work, authentication, and rejection of native/manual tracks. The subtitle browser
check removes a selected agent-found track and verifies that its row disappears
and the report marks it removed. 47 focused backend regressions and the browser
check passed; the frontend production build and Ruff checks passed.
