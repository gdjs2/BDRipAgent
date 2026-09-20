# External integrations

The repositories supplied in `impl-add.md` are installed automatically by the
worker image. Revisions and upstream lockfiles make the tool contracts reproducible:

| Tool | Pinned revision | CLI |
| --- | --- | --- |
| [BDRip_Scripts](https://github.com/gdjs2/BDRip_Scripts) | `529552b7b219f3fb9d7df8ce651c4e5fb265d484` | `/opt/crf-studio/.venv/bin/bdrip` |
| [Sup2Sup](https://github.com/gdjs2/Sup2Sup) | `608477404527f68d7c4764f694a01118b39c67ba` | `/opt/sup2sup/.venv/bin/sup2sup` |

The worker uses Python 3.13. Its application, CRF Studio and Sup2Sup have separate
virtual environments because their PyAV version requirements differ. Both tools
are installed without GUI extras. Build arguments `BDRIP_SCRIPTS_REV` and
`SUP2SUP_REV` allow deliberate upgrades; validate contracts before updating pins.
BDRip_Scripts also supplies the release stage's BBCode/NFO rendering, MD5/torrent
creation, and screenshot uploads, described below.

## IMDb metadata

The API reuses the `imdbinfo==0.9.1` client used by BDRip_Scripts'
[`release/catalog.py`](https://github.com/gdjs2/BDRip_Scripts/blob/529552b7b219f3fb9d7df8ce651c4e5fb265d484/src/bdrip/release/catalog.py).
It reads IMDb's public title data; it is not the paid official IMDb API and requires
no additional API key. The New movie job form accepts a `tt` title ID or IMDb title
URL. **Look up IMDb** fills the year and offers usable titles in upstream order:
original title, localized title, then alternate titles, with duplicates removed.
The existing WiKi naming function generates both codec filename previews.

Authenticated clients can use `GET /api/metadata/imdb?imdb_id=tt0133093`. Job creation
accepts `imdb_id` with optional title/year; the server resolves metadata itself and
checks any chosen title/year against the lookup. Paired jobs share one snapshot.
The ID and snapshot are saved in the database and job manifest, so later stages
do not need another network request. Leaving the ID empty preserves manual entry.

Lookups have a 25-second total deadline, configurable with `IMDB_TIMEOUT_SECONDS`
(1–55 seconds), and successful results are cached for one hour, up to 128 titles
per API process. Missing titles, unavailable metadata and timeouts produce errors
without creating jobs. If IMDb has no usable release year or romanized title,
clear the IMDb ID and enter title/year manually. The nullable database migration
preserves existing jobs and their filenames.

The lookup subprocess runs in Python isolated mode (`-I`), keeping application
modules such as `backend/app/queue.py` out of the IMDb client's import path so
they cannot shadow standard-library modules.

## Track extraction

Source analysis extracts all PGS tracks in one `mkvextract` pass for review before
selection. Preparation reuses those original files and their detection results,
extracting only selected audio and any missing/legacy PGS files in one further
pass with audio `timestamps_v2`. All requested files must exist and be nonempty
before registration/cropping. Empty selections skip extraction. Prepared tracks
retain selection order; retries write new outputs under the new task directory.
Metadata-based FFmpeg stream indexes are mapped separately from MKVToolNix IDs
for local audio samples; unmatched streams are reported instead of guessed.

Extraction, timestamp indexing and final merging enable MKVToolNix's
[`--gui-mode`](https://mkvtoolnix.download/doc/mkvextract.html) and parse its
untranslated progress records from the persistent task log once per second.
Updates are saved on the task and sent through the existing `task_progress` SSE
events. The frontend shows the current phase and the tool's reported percentage;
it waits for an actual report before displaying determinate tool progress.
Task progress stops at 99% until output checks and remaining processing succeed.
Timestamp indexing occupies the first 5% of screenshot candidate generation.

## Sup2Sup

The adapter calls `sup2sup crop input.sup output.sup --crop LEFT TOP RIGHT BOTTOM
--video-source WIDTH HEIGHT --fit margin --margin 20 --report report.json`.
HandBrake's top/bottom/left/right crop is explicitly reordered. Original video
size allows exact mapping onto a different native subtitle canvas, without
resampling glyphs. Fractional subtitle-pixel margins are rejected rather than rounded.

Ordinary cues intersecting the crop are moved inside the canvas with a 20-pixel
margin. Upstream handles fullscreen cues by cropping their bitmaps. The adapter
checks output composition dimensions, presentation timestamp preservation,
palette preservation, and unchanged ordinary-cue bitmap data. Original SUP,
processed SUP, JSON preservation report and command logs remain available.
`integrations.sup2sup` configures fit mode and margin.

## CRF Studio

The adapter calls `bdrip crf source.mkv --codec x264|x265 --config config.json
--output-dir <attempt> --progress-file <attempt>/progress.json
--crop WIDTH:HEIGHT:LEFT:TOP`. The crop is always explicit;
CRF Studio cannot run an independent crop detector. Preset, tune, profile, level,
bit depth and extra encoder parameters are passed from the application profile.
Upstream's default extra parameters are replaced, even when our profile has none.
Encoder levels may be YAML numbers (`level: 4.1`) or strings (`level: "4.1"`).
The adapter converts either to CRF Studio's required JSON string, matching the
HandBrake CLI argument; `level: null` remains unset.

CRF Studio measures CRFs **13 and 20**, by default using ten stratified ten-second
clips. `integrations.crf_studio` controls count, seconds and random seed. Native
schema-version-5 results must be complete and contain both endpoints for the
requested codec. Bitrates are converted from Mbps to kbps; B-frame QP may be null
when a sample has no B-frames. Predicted curves interpolate the upstream linear-QP
and log-linear-bitrate models between the measured endpoints.

The worker polls the same atomic progress JSON used by CRF Studio's desktop GUI
once per second, persists the latest update on the task, and emits `task_progress`
events. Overall percent is `floor(100 × (completed + sample_fraction) / total)`,
capped at 99 until the process exits successfully and its results are accepted.
Each sample at each measured CRF counts as one encode (normally 20 encodes per
job). Frame submission contributes partial progress; flushing the encoder does
not mark that sample complete. Missing or malformed progress files are skipped
without interrupting analysis, and the final update is read after process exit.

The CRF analysis and Overview tabs show the status message, overall bar, completed
encodes, current codec/CRF and sample, frames submitted, sample bar, flushing status,
and elapsed time. Before the sample plan is known the bar is indeterminate. Queued
and held attempts link to the queue; failed/cancelled attempts expose retry and logs.
Elapsed time survives reloads, and completed results automatically replace the
waiting view with the interactive curve. Progress files are retained with reports.
Attempts started before this integration do not have frame progress recorded.

The dashboard matches BDRip_Scripts' Bitrate / QP view: video Mbps on the horizontal
axis, average B-frame QP vertically, and measured markers at CRF 13 and 20. It
reconstructs the same continuous model from saved measurements, with
`ln(Mbps) = d + e × CRF` and `QP = a + b × CRF`. Hovering or pinning a bitrate
inverts the first equation to calculate QP and approximate CRF without another
encode. The drawn curve spans the measured bitrate range; out-of-range pins are
labelled as extrapolations. Equal endpoint bitrates and missing B-frame QPs display
an explanation instead of a fabricated QP estimate.

Pins survive polling, navigation and reloads within the browser tab, separately
for each job. Exact bitrate entry supports keyboard and mobile use; **Unpin**,
right-click or Escape releases the pin. **Use CRF** copies a value rounded to one
decimal place into the final selection, respecting the saved profile's CRF bounds.
**Use bitrate for 2-pass** copies the pinned video bitrate, rounded to the nearest
1 kbit/s, and selects bitrate mode. This action does not depend on whether a valid
CRF prediction exists or falls within profile bounds. Starting the encode still
requires the existing confirmation button.

JSON, CSV and plotted reports are registered as artifacts. Raw per-sample records,
model statistics, library versions and the exact application profile are retained.
The final encode uses that saved profile. CRF Studio uses native PyAV encoders,
while the final encode uses HandBrake's bundled encoders, so sampled bitrate is an
estimate rather than a promise of the final bitrate. V1 keeps the human encoding
selection gate.

## Final encoding target

Both x264 and x265 support constant quality or average bitrate with two full
passes. The API stores one target with the immutable analyzed profile snapshot:

```json
{"codec":"x265","profile":"x265-live","rate_control":"crf","crf":17.5}
```

```json
{"codec":"x265","profile":"x265-live","rate_control":"bitrate","bitrate_kbps":8000}
```

Send either body to `POST /api/jobs/{job_id}/encode-selection` after analysis.
CRF is constrained by the saved profile limits. Bitrate is an integer from 1 to
1,000,000 kbit/s; the UI accepts 0.001–1500 Mbps in 0.001 Mbps steps. Bitrate is
for the video stream: audio and container overhead add to the total file size.
Targets are averages, and encoder constraints can affect the achieved bitrate.
Mixed targets, a missing target, unknown modes and unsupported fields are rejected.

CRF mode generates `-q <crf> --no-two-pass`. Bitrate mode generates
`--vb <kbit/s> --two-pass --no-turbo`, keeping the selected preset and other
profile options for both passes. Both modes retain the source crop, dimensions,
timestamps, encoder bit depth and existing validation/remux pipeline. One task
owns both passes and occupies one queue slot; retries rerun the encoding stage.

Running final encodes expose `POST /tasks/{id}/pause` and `/resume` (202). Both
require an active, supported encoder and reject cancellation/terminal/other-stage
requests. The worker advertises `can_pause` while supervising HandBrake, polls
`pause_requested`, and acknowledges suspension through `paused_at` and task events.
It sends SIGSTOP/SIGCONT to its own process group, retaining the current pass and
output; the API never signals stored PIDs. Paused tasks remain RUNNING and reserve
their queue slot. Heartbeats and cancellation continue, and paused time is excluded
from command timeout/active elapsed time. Cancellation sends SIGTERM followed by
SIGCONT so a stopped process can terminate. Completion, errors and lease recovery
clear pause fields; retries create an unpaused attempt. Worker restart requires a
fresh encode, not a resume from disk. Migration `0004` defaults old tasks to having
no pause capability.

The Encoding tab displays the saved profile snapshot (including tune, video
profile/level and complete encoder options), rate control/passes, source/crop/output
geometry, frame handling, filters, track/chapter handling and saved selection
metadata. A JSON view retains all saved fields. The displayed HandBrake command
comes from `command_json` on the latest encode attempt; a queued retry never shows
an old command as its own, and smoke mode explicitly reports skipped encoding.
Progress scales each pass into the overall percentage (0–50%, then 50–100%) and
labels HandBrake's ETA as the time remaining in the current pass.

Selections and manifests include `rate_control` and only the relevant target.
Previously saved configs and API requests without `rate_control` continue to use
CRF. Storage uses the existing JSON field, so no database migration is needed.

## Source-reuse smoke mode

`POST /api/jobs/{job_id}/smoke-test` selects smoke mode only at
`WAITING_FOR_ENCODE_SELECTION`, after analysis and track preparation. It persists
`execution_mode: smoke` in the existing encode config, marks job analysis as a
smoke test, prefixes the release name with `SMOKE-TEST.`, and queues one encoding
stage. It does not require or invent CRF/bitrate settings and cannot be selected
implicitly through the ordinary encode-selection request.

The encoding stage checks source identity and identifies its video track; it
never invokes an encoder or creates an `ENCODED_VIDEO` artifact. Validation reads
the first source frame and records `valid: null`, `skipped: true`, source metadata
and actual first PTS. It neither performs full encode validation nor claims it
passed. Remuxing accepts this report only for an explicitly marked smoke job with
a readable source. Ordinary jobs still require a passing encode validation.

Remuxing reads the source video track by its actual mkvmerge ID, preserving the
existing audio/subtitle/chapter handling. It creates a `SMOKE_TEST_MKV` artifact.
The video remains uncropped and retains its original codec/bit depth. Source
candidate generation and the screenshot agent run normally; rendering reuses a
cropped source frame for the comparison image and marks its overlay and filename.
Test runs do not participate in cross-codec frame reservations for real releases.
Retry, cancellation, source immutability and queue limits still apply. A smoke job
remains a smoke job; create another job to perform real encoding.

## Naming

`shared/naming.py` follows the supplied release builder's convention:
`Title.Year.1080p.BluRay.x264[.Audio]-WiKi` or
`Title.Year.1080p.BluRay.x265.10bit[.Audio]-WiKi`. Title punctuation becomes dots,
Latin accents are normalized, and a usable romanized title/year are required.
Core audio is preferred for the release token; plain Dolby Digital omits the token.
DTS uses `.DTS`, while lossless DTS-MA uses e.g. `.DTS.MA5.1` when no core is retained.
The token is calculated from the selected audio, not discarded source tracks.
The upstream naming scheme assumes 1080p Blu-ray; other release classes remain a
question in `open-questions.md`. The application does not upscale video to 1080p.

The filename stem is the MKV container title. The encoded screenshot’s last row
shows the actual final MKV basename, including `.mkv`; smoke tests also identify
the reused source. Source screenshots retain their `Source` label.
Audio labels use e.g. `English DTS-MA 5.1` or `English Dolby Atmos 7.1`.
Subtitle labels use e.g. `English PGS SDH Forced`; unset flags are omitted.
Default, forced, hearing/visual impairment and commentary flags are explicitly
written and inspected after remux. Subtitle SDH is determined from content, never
copied from the source flag or name. All PGS tracks are rendered before selection through the
pinned Sup2Sup parser, sampled uniformly across up to 96 distinct bitmap cues, and
read with [Tesseract](https://tesseract-ocr.github.io/tessdoc/Command-Line-Usage.html).
[OpenCC](https://github.com/BYVoid/OpenCC) character dictionaries distinguish Chinese
scripts; colloquial grammar identifies written Cantonese independently of script.
Examples: `Simplified Chinese PGS`, `Traditional Chinese PGS SDH`, and
`Cantonese (Traditional) PGS`, with IETF tags `zh-Hans`, `zh-Hant`, and `yue-Hant`.
Japanese and Korean OCR packs avoid treating those scripts as Chinese.

Program rules require multiple distinct cues and strong script evidence. Repeated
explicit sound descriptions can establish SDH; absence of markers always requires
visual agent review. Initial analysis always asks the agent to review SDH, including
program-positive results. Ambiguous script/dialect or OCR disagreement also goes to the
agent, which receives original cue images and OCR but no source track name or SDH
flag. Prompts and responses stream on the Tracks page. A saved `SUBTITLE_DETECTION`
report records coverage, source metadata for comparison, decisions, and evidence.
The Tracks page displays detected names, descriptions and coverage before selection.

A second streamed agent review receives all track evidence and recommends Default,
Forced, Hearing impaired, Visual impaired/audio description and Commentary flags.
It preserves the subtitle visual review's SDH decision. Unknown content flags stay
null until the user explicitly chooses Yes/No on selected tracks. The selection
API accepts `track_names: {track_id: name}` and `track_flags: {track_id: {flag: bool}}`
for selected tracks only. Overrides are stored alongside evidence in track metadata;
manual names are single-line Unicode strings of 1–255 characters. They are passed
as argv values, preserved through retries, and verified after MKVToolNix merging.
No database migration is required. Editing a label alone does not alter flags.

Audio is decoded locally to mono 16 kHz WAV samples for analysis only; the final
output retains original audio. The worker's isolated faster-whisper environment
runs CPU int8 inference under a shared cache lock to bound concurrent memory/CPU
use. The agent receives transcripts, confidence, timestamps, signal measurements,
and metadata, never raw audio. `integrations.audio_review` configures transcription
(default true), model (default `small`, also accepts an installed model path),
sample_count (1–8, default 3), sample_seconds (5–60, default 30), and cpu_threads
(1–8, default 2). Model weights download on first use into `cache/speech-models`.
Thereafter cached models can run offline. If decoding or transcription fails, the
review reports that limitation rather than fabricating content. `AUDIO_ANALYSIS`,
`AUDIO_SAMPLE`, `AUDIO_TRANSCRIPTS`, and `TRACK_REVIEW` artifacts preserve evidence.

This is content sampling, not exhaustive transcription. Inconclusive subtitle
findings remain visible before selection; a user may explicitly override SDH.
Unresolved language/script still stops preparation instead of guessing. Increase
`integrations.subtitle_detection.sample_cues` (8–192) and retry to widen evidence.
`POST /jobs/{id}/tracks/analyze` lets waiting jobs rerun initial review; it refuses
already selected or active jobs. Older prepared jobs are checked before their next
mux. Existing outputs are not modified. Rebuild worker, agent, API and frontend.
The selection gate continues to accept PGS only.

## Codex screenshot selection

`screenshot_policy.decoder` accepts `cuda` (default, with CPU fallback) or `cpu`. The CUDA candidate
scan uses PyAV `HWAccel` with implicit software fallback disabled, probes a first
frame and checks `is_hwaccel`, then reports the actual backend. Failed startup
reopens the source on CPU before emitting any frames. Failures after startup stop
the attempt to avoid duplicating frame indexes. GPU access is opt-in through
`docker-compose.gpu.yml` / `./start.sh --gpu`, following Docker's
[GPU reservation support](https://docs.docker.com/compose/how-tos/gpu-support/).
`PATCH /api/jobs/{id}/screenshots/decoder` saves the next scan's choice and rejects
changes during an active scan. No database migration is needed.

Candidate generation seeks into uniformly distributed time buckets instead of decoding
all source frames. It targets `candidate_count: 100` usable B-frame pairs, sampling
`candidate_window_seconds: 3.0` seconds per window with at most
`candidate_attempts_per_bucket: 4` distinct positions in each unsuccessful bucket.
It stops once every bucket has a candidate or the bounded retry budget is exhausted.
Black/white, blur, transition, and global duplicate checks remain active. Scene IDs
identify locally detected scenes inside sampling windows; this is not an exhaustive
scene map. Timeline spacing and visual duplicate checks still apply globally.
The progress report includes attempted windows, retained candidates, decoded scan
frames, target count, and the actual decoder.

Exact zero-based frame numbers come from the source PTS array retained by encode
validation. Older jobs and smoke jobs build it with `mkvextract timestamps_v2`,
which reads container timestamps without decoding video and discovers the video
track with `mkvmerge -J`. It supports nonzero starting timestamps and variable
frame intervals; FPS estimates are not used for labels. The index is checked against
validated frame counts and first PTS, stored as an artifact, and reused on subsequent
scans. Only candidate sampling is abbreviated; full encode validation is unchanged.

Both source and encoded screenshots must be B-pictures. CUDA omits picture-type
metadata, so candidate refinement uses CPU seeks over at most the following
half-second. It stops at a histogram-detected scene change, reruns quality filters,
and checks the corresponding encoded frame using the validated timestamp origins.
Updated frame numbers count decoded frames from the exact original candidate;
they are not estimated from frame rate. Smoke mode checks the reused source frame.
Too few qualifying pairs fail the stage. Legacy candidate indexes are refined
before agent selection. The agent sees only the resulting source images.
Agent validation, manual replacement and final CPU rendering enforce the same
rule; rendering rechecks actual decoded types before writing each image pair.
Candidate records store `b_frames_verified`, source `picture_type` and
`encoded_picture_type`. Final PNGs retain the original crop and resolution.

The Python service uses the pinned CLI's `codex app-server --stdio` protocol,
with ephemeral read-only threads, image attachments, a strict `outputSchema`,
disabled shell tools and web search, no interactive approvals, and an explicit
timeout. It consumes `item/agentMessage/delta` and completed agent messages from the
[documented app-server interface](https://learn.chatgpt.com/docs/app-server).
Reasoning events are not forwarded to the browser.

The worker requests an NDJSON stream from the internal agent service. Each invocation
emits its full prompt and attachment names, followed by response text deltas and a
completed message. The worker batches deltas and persists fenced `agent_output` events.
Authenticated `GET /api/tasks/{id}/agent-events` replays that attempt's transcript and
streams new records, honoring `Last-Event-ID`. The screenshot page separates shortlist,
review, and correction requests, and offers earlier attempts. Heartbeats keep the
connection open and allow worker cancellation checks; disconnecting cancels the agent
subprocess. The JSON response mode remains available for older internal callers.

Authenticate inside the container using the documented
[device-code login](https://learn.chatgpt.com/docs/auth):

```sh
docker compose exec agent codex login --device-auth
docker compose exec agent codex login status
```

Complete the login in your browser when prompted. Authentication persists in the
`codex-auth` volume. The application does not read or display auth tokens. Configure
`agent.model` in YAML if you want an explicit model; otherwise Codex uses its default.

The agent container receives **only** a read-only source-image cache. It has no
movie storage mounts, database credentials, or API token. Its Python service uses
an internal bearer token for calls from the worker; the Codex subprocess receives
a reduced environment without that token. The service port is not published.
No browser connects directly to Codex. Selection proceeds through contact sheets,
a bounded shortlist, higher-resolution images, and deterministic validation with
up to three correction attempts. Recommendations and successful run traces are stored
as database records and JSON artifacts. If authentication or selection fails, the
job remains at screenshot selection and can be retried.

The shortlist retains at most 40 source images and their selection reasons. The
agent ranks 15 recommendations (fewer only if the shortlist has fewer candidates).
The worker prepares full-resolution source/encode comparison PNGs for these choices
and small JPEG thumbnails for browsing the shortlist. Both images in each comparison
are checked again for B-picture type. Encoded images are never sent to the agent.
Successful selection advances to `WAITING_FOR_SCREENSHOT_SELECTION`, which enqueues
no further work until the user confirms 1–15 candidate IDs from the recommendations
via `POST /api/jobs/{id}/screenshots/selection`. This saves the chosen count and IDs
in `analysis.screenshot_selection` and queues rendering. Category/character/timeline
coverage quotas guide the recommendations; a user's subset retains mandatory
B-frame, scene-spacing, and cross-codec separation checks.

The gallery exposes Best 15, Shortlist, and Final views. Shortlisted frames outside
the recommendations have full-resolution source previews; recommendations also have
source/encode comparisons. `POST /api/jobs/{id}/screenshots/review` re-ranks a completed
job's saved shortlist, preserving existing final images until the user confirms a
new selection. It skips candidate scanning and contact-sheet shortlisting.
`PATCH /api/jobs/{id}/screenshots/best` accepts an ordered list of 0–15 unique
shortlisted IDs. It persists best-list edits without regenerating candidates or
changing existing final exports. Both-picture B-frame verification is mandatory.
Added shortlist frames have a source preview until final rendering creates their
comparison pair. Spacing and cross-codec reservations are checked at confirmation.
An empty best list can be repopulated from the shortlist.

The same endpoint is available while waiting for a choice: **Refresh best 15**
rechecks recommendations against other codec jobs' newly confirmed reservations.

The worker reconnects up to four times after short agent connection interruptions,
using the same job/task IDs and delays of 5, 10 and 20 seconds. Each attempt opens a
new connection so container replacements resolve to the current service address.
Busy responses respect the agent's existing selection lock; authentication and
selection errors fail immediately. The task log distinguishes connection recovery
from frame preparation and visual selection. Completed B-frame refinement is saved
before contacting the agent, so a later task retry reuses the verified candidates.
Newer task attempts also clear stale retry errors in the web interface.

## Release generation after screenshot review

`SCREENSHOT_RENDERING` advances to `WAITING_FOR_RELEASE_DETAILS`. The Release tab
collects required `chinese_name` and `source`, optional `extra_description`, and one HTTP(S)/UDP `tracker`
announce URL, plus `upload_screenshots` (default `false`). `PATCH /api/jobs/{id}/release` saves these fields in
`analysis.release_details`; `POST` on the same route confirms them and queues
`GENERATING_RELEASE`. Completed older jobs can use the same endpoint once their
selected comparison PNGs exist. Active tasks lock release editing. Successful
generation advances to `COMPLETE`; failed attempts use the existing retry/cancel UI.

`source` is a required, single-line description (maximum 1,000 characters), not the
input MKV filename. It is stored with the release details and used verbatim (trimmed)
in both NFO and BBCode. Old drafts need this field before generating again. The
worker also rejects a legacy retry with no source before creating files or uploading
images. Changing the source invalidates the previous release-result view.

**Convert dotted name** calls authenticated `POST /api/release/source-description`
with `{ "source": "Movie.2026.1080p.Blu-ray.AVC.DTS-HD.MA.5.1@GROUP" }`. It returns
`{ "source": "1080p Blu-ray AVC DTS-HD MA 5.1-GROUP" }` without saving or queueing work.
`shared.naming.source_description` reuses the small formatting body of the pinned
BDRip_Scripts `release/pipeline.py::source_description`: drop a dotted title/year
prefix, convert `@` to `-` and dots to spaces, restore 2.0/2.1/5.0/5.1/7.0/7.1 notation,
and collapse whitespace. The adapter takes the entered name rather than a filesystem
path, preserving slashes in custom descriptions. Conversion is explicit; saving does
not transform a manually edited value. A native test checks parity with the installed
upstream function and verifies the source in generated NFO and BBCode.

The worker launches `worker/adapters/release_runner.py` with the pinned BDRip_Scripts
Python at `/opt/crf-studio/.venv/bin/python`. It directly reuses
`bdrip.release.bbcode.render`, `nfo.render_nfo`, MediaInfo helpers,
`screenshots.upload_screenshots_cached`, `torrent.md5_file`, and
`torrent.create_private_torrent`. The tracker and other settings are in a local
request file rather than command arguments. The TTG token comes only from
`TU_TTG_TOKEN`; configuration endpoints expose its presence, never its value.
The agent service has no access to this token.

Only `Screenshot.selected` rows with complete, B-frame-verified final comparison
paths are accepted. Source files are sorted before encoded files in every BBCode
row. With uploads disabled, no upload helper is called and no cached URLs are reused;
the BBCode comparison heading remains with an empty image section. No TTG token is
required. Legacy saved requests lacking the flag preserve their original upload behavior
on task retry. The 40-frame shortlist and unused best-15 previews are never uploaded. Upload
URLs are cached per image-set fingerprint, and retries include both failed images
and images not reached before interruption. Any missing upload fails the stage
instead of silently publishing a partial comparison section.

The private v1 torrent, BBCode, encoder notes, and unchanged WiKi payload directory
share `ARTIFACTS_ROOT/<UTC timestamp> [ART] <release-name>/`. The timestamp is allocated
at first export and reused by the same job. A separate torrent root is no longer
required. The authenticated artifact preview endpoint returns bounded plain-text
JSON for BBCode/NFO/MD5/encoder files; the Release tab renders it as selectable text.
Legacy exports can be relocated with `worker.pipeline.release_migration` before
removing the old torrent directory, preserving artifact IDs, torrent bytes, and payload.
The launcher performs this migration automatically on upgrade using a temporary
legacy mount. NFO previews decode the upstream CP437 artwork; other text uses UTF-8.

IMDb release metadata is fetched once with a bounded subprocess and cached for
retries. A failed lookup uses the saved title/year and explicit Unknown/N/A fields,
recording a warning in the release result. The isolated renderer supplies that
snapshot to the upstream BBCode template, which otherwise unconditionally repeats
the network lookup. NFO uses the upstream CP437 artwork and final MediaInfo values;
Chinese name and extra description appear in the UTF-8 BBCode post heading.

## Browser connections and live updates

Fresh job event streams start at the latest saved event and emit a `ready` event,
instead of replaying the entire job history. Reconnecting streams honor the
browser's `Last-Event-ID` and replay missed events, following the
[server-sent events protocol](https://html.spec.whatwg.org/multipage/server-sent-events.html#the-last-event-id-header).
The UI groups event bursts into at most one refresh per second and allows existing
reads to finish. Five-second polling remains available if the event stream drops.

Transient GET failures (connection loss or HTTP 502/503/504) receive up to three
attempts with backoff. Requests have a 60-second timeout. Mutations are not replayed
automatically because a dropped response does not prove the action failed. Loaded
job/gallery data and open comparisons survive temporary read failures; only an
actual 401 switches to login. Initial connection failures offer reconnect and retry
automatically. The Docker frontend uses runtime DNS resolution for the API so
container replacements do not leave the proxy pointing at an old address; see
[NGINX proxy resolution](https://nginx.org/en/docs/http/ngx_http_proxy_module.html#proxy_pass).

Contact sheets use resized previews; exported comparisons never resize video.
Frame numbers are **zero-based source decode indexes**. Matching uses decoded PTS
and the validated source/encode timeline origins. Picture type comes from each
image's own decoded frame; both overlays use the same source frame number.

Both images use the same source-derived SDR matrix and range for RGB conversion.
If source color metadata is absent, the explicit fallback is limited range with
BT.709 for HD and BT.601 for SD. Validation records missing metadata as warnings.
HDR/wide-gamut comparison conversion remains outside the current accepted media scope.

## Tool references

Command builders follow the [HandBrake CLI reference](https://handbrake.fr/docs/en/latest/cli/command-line-reference.html),
[mkvextract documentation](https://mkvtoolnix.download/doc/mkvextract.html), and
[mkvmerge documentation](https://mkvtoolnix.download/doc/mkvmerge.html).
Media/frame handling uses [PyAV](https://pyav.basswood.io/docs/stable/api/video.html).
The worker image's actual HandBrake 1.6.1 and MKVToolNix binaries are exercised by
the container smoke test; newer documentation may list additional flags that this
implementation does not use.

The screenshot gallery polls current cross-codec reservations and disables identical
or nearby frames before confirmation. The authoritative confirmation/replacement
check uses a PostgreSQL transaction advisory lock for the immutable source, so two
simultaneous requests cannot both reserve conflicting frames. The larger of the two
jobs' spacing settings applies (30 seconds by default); exact frame reuse is forbidden
even with zero spacing. Deleted jobs do not reserve frames. Smoke-test variants
reserve against other smoke variants while remaining independent of real releases.
