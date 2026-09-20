# Test coverage

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
MKV with its generated WiKi filename/title and seven source/encode PNG pairs at
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
