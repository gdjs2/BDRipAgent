# Movie Encoding Automation Server

## 1. Project Goal

Build a fully containerized, self-hosted movie encoding automation system with a web-based frontend.

The system takes an already-ripped Blu-ray MKV as its input and automates the remaining encoding workflow:

1. Inspect the source MKV.
2. Scan the source with HandBrake and obtain automatic crop information.
3. Let the user select audio and subtitle tracks through the web UI.
4. Extract selected audio and PGS subtitle tracks with MKVToolNix.
5. Crop selected PGS subtitles with Sup2sup CLI using the HandBrake crop result.
6. Run CRF Studio through its CLI and expose bitrate/QP curves in the web UI.
7. Let the user make the final encoding decision.
8. Perform the full encode with HandBrakeCLI.
9. Validate the encoded video.
10. Remux encoded video, selected audio tracks, processed subtitles, chapters, and metadata.
11. Automatically select comparison screenshots using a Codex-powered visual agent.
12. Generate source/encode comparison screenshots.
13. Later extend the system with release generation, NFO, torrent, BBCode, checksums, and PT publishing.

The main design principle is:

> Deterministic media-processing tasks must be handled by deterministic code. Codex should only be used where semantic or visual reasoning is useful.

The most important Agent-driven task is screenshot selection.

---

# 2. Explicit Scope

## 2.1 Input Boundary

MakeMKV is NOT part of the server.

Blu-ray ripping happens externally.

The server receives:

```text
Blu-ray Disc
    ↓
MakeMKV on another machine
    ↓
Source MKV
    ↓
Movie Encoding Server
```

The input to this application is therefore always an existing MKV file.

---

## 2.2 Fully Containerized Requirement

All application components must run in containers.

The host should only provide:

* Linux
* Docker Engine / Docker Compose
* mounted storage/NAS filesystems
* kernel/device drivers if ever needed

The host must NOT require application-level installation of:

* HandBrake
* FFmpeg
* MKVToolNix
* Sup2sup
* CRF Studio
* Python environment
* Codex
* frontend runtime

Everything above must exist in Docker images.

---

# 3. User Decision Boundaries

The system must intentionally preserve two user-controlled decisions.

## Human Gate 1 — Track Selection

The system analyzes all tracks and displays them in the web UI.

The user decides:

* which audio tracks to preserve;
* which PGS subtitle tracks to preserve.

The Agent must NOT make the final selection automatically.

---

## Human Gate 2 — Encode Selection

CRF Studio performs sampling and curve generation.

The frontend displays:

* CRF;
* predicted bitrate;
* measured sample bitrate;
* average QP;
* bitrate/QP curves;
* any other statistics CRF Studio provides.

The user decides the final encode target.

The user may select:

* CRF directly;
* or another encoding target supported by CRF Studio.

Codex must NOT make the final encoding decision.

---

## Autonomous Decision — Screenshots

Screenshot selection should be autonomous.

The screenshot Agent receives a user-configurable natural-language screenshot policy and chooses representative comparison frames automatically.

Manual override should still be possible, but it is not a required workflow gate.

---

# 4. System Architecture

Use a multi-container architecture.

```text
                         Browser
                            │
                            ▼
                 ┌─────────────────────┐
                 │ Frontend Container  │
                 │ React + TypeScript  │
                 └──────────┬──────────┘
                            │
                       REST + SSE
                            │
                            ▼
                 ┌─────────────────────┐
                 │ API Container       │
                 │ FastAPI             │
                 │ State Machine       │
                 │ Job Management      │
                 └───────┬─────┬───────┘
                         │     │
              ┌──────────┘     └───────────┐
              ▼                            ▼
     ┌─────────────────┐          ┌─────────────────┐
     │ Redis Container │          │ Agent Container │
     │ Task Queue      │          │ Codex SDK       │
     │ Events          │          │ Visual Reasoning│
     └────────┬────────┘          └────────┬────────┘
              │                            │
              ▼                            │
     ┌──────────────────────────┐          │
     │ Worker Container         │◄─────────┘
     │                          │
     │ HandBrakeCLI             │
     │ MKVToolNix               │
     │ MediaInfo                │
     │ FFmpeg                   │
     │ Sup2sup CLI              │
     │ CRF Studio CLI           │
     │ Pillow                   │
     │ screenshot utilities     │
     └────────────┬─────────────┘
                  │
                  ▼
             Movie Storage

     ┌──────────────────────────┐
     │ PostgreSQL Container     │
     │ Persistent System State  │
     └──────────────────────────┘
```

---

# 5. Recommended Technology Stack

## Frontend

Use:

```text
React
TypeScript
Vite
TanStack Query
React Router
```

For plots:

```text
Plotly.js
```

or:

```text
Apache ECharts
```

Either is acceptable.

No SSR or SEO functionality is required.

This is an application dashboard.

---

## Backend

Use:

```text
Python 3.12+
FastAPI
Pydantic
SQLAlchemy
Alembic
PostgreSQL
```

The API service owns:

* MovieJob state;
* validation;
* application state machine;
* user decisions;
* task scheduling;
* artifact metadata;
* API authentication;
* frontend communication.

---

## Task Queue

Use:

```text
Redis
Celery
```

Celery schedules background tasks.

However, PostgreSQL must remain the authoritative source of task state.

Redis/Celery state must NOT be considered authoritative.

---

## Agent Service

Use:

```text
Python
Codex SDK
Codex CLI / App Server where required
```

Codex should be authenticated through ChatGPT subscription login rather than an OpenAI API key where possible.

Persist authentication using a Docker volume:

```text
codex-auth
```

For a headless server, support device-code authentication.

The browser should never communicate directly with Codex App Server.

Correct architecture:

```text
Browser
   ↓
FastAPI
   ↓
Agent Service
   ↓
Codex
```

---

# 6. Container Layout

Initial Docker Compose deployment:

```text
frontend
api
worker
agent
postgres
redis
```

Do NOT split every media tool into separate microservices in V1.

The media worker should initially be a single large worker image.

It should contain:

```text
HandBrakeCLI
MKVToolNix
FFmpeg
ffprobe
MediaInfo
Sup2sup CLI
CRF Studio CLI
Python
Pillow
optional VapourSynth
```

This keeps shared movie-file access simple.

---

# 7. Storage Layout

Use bind-mounted storage rather than Docker named volumes for movie files.

Recommended host structure:

```text
/storage/movie-agent/
│
├── incoming/
│
├── jobs/
│
├── completed/
└── cache/
```

Containers should see:

```text
/source
/workspace
/completed
/cache
```

Suggested mapping:

```text
/storage/movie-agent/incoming   → /source
/storage/movie-agent/jobs       → /workspace
/storage/movie-agent/completed  → /completed
/storage/movie-agent/cache      → /cache
```

---

# 8. Source MKV Immutability

Source MKVs must be treated as immutable.

The worker should mount the incoming directory read-only where possible:

```text
/source:ro
```

The source file must never be modified in place.

Derived files belong under:

```text
/workspace/<job-id>/
```

---

# 9. Incoming File Protocol

Do not rely on the existence of a `.mkv` file alone to determine whether transfer has completed.

Recommended protocol:

```text
Movie.mkv.partial
```

Transfer the file.

After successful transfer:

```text
rename Movie.mkv.partial → Movie.mkv
```

The system ignores:

```text
*.partial
```

and only shows complete `.mkv` files.

Alternatively, support an explicit:

```text
Movie.mkv
Movie.ready
```

marker protocol later.

---

# 10. Job Workspace

Each MovieJob receives its own workspace:

```text
/workspace/<job-id>/
│
├── manifest.yaml
├── source/
├── metadata/
├── audio/
├── subtitles/
├── crf/
├── encode/
├── mux/
├── screenshots/
│   ├── candidates/
│   ├── contact-sheets/
│   ├── selected/
│   └── comparisons/
├── release/
└── logs/
```

Avoid copying the source MKV unnecessarily.

Where possible, reference the original source path or create a hardlink if the filesystem supports it.

---

# 11. Manifest

Each MovieJob should have a human-readable manifest.

Example:

```yaml
job:
  id: "..."
  title: "Example Movie"
  year: 2026

source:
  path: "/source/Example.Movie.mkv"
  size: 42949672960
  duration: 7200.123

video:
  codec: "AVC"
  width: 1920
  height: 1080
  fps: "24000/1001"
  bit_depth: 8

crop:
  source: "handbrake"
  top: 138
  bottom: 138
  left: 0
  right: 0

tracks:
  audio: []
  subtitles: []

crf_studio:
  status: "complete"
  results_path: "crf/results.json"

encode:
  encoder: "x265"
  preset: "..."
  crf: 17.0
  selected_by: "user"
  status: "pending"

screenshots:
  status: "pending"
  requested_count: 10
```

PostgreSQL remains authoritative, but the manifest provides:

* reproducibility;
* debugging;
* portability;
* human readability.

---

# 12. Main State Machine

Use explicit application states.

Recommended V1 state machine:

```text
NEW
 ↓
ANALYZING_SOURCE
 ↓
WAITING_FOR_TRACK_SELECTION
 ↓
PREPARING_TRACKS
 ↓
RUNNING_CRF_ANALYSIS
 ↓
WAITING_FOR_ENCODE_SELECTION
 ↓
ENCODING
 ↓
VALIDATING_ENCODE
 ↓
REMUXING
 ↓
SCREENSHOT_CANDIDATE_GENERATION
 ↓
SCREENSHOT_AGENT_SELECTION
 ↓
SCREENSHOT_RENDERING
 ↓
COMPLETE
```

Failure states should not destroy the logical pipeline stage.

Example:

```text
ENCODING
  +
task.status = FAILED
```

rather than changing the entire MovieJob to an ambiguous global `FAILED`.

The user should be able to retry failed tasks.

---

# 13. MovieJob vs Task

Keep MovieJob and Task separate.

One MovieJob contains many Tasks.

Example:

```text
MovieJob
├── inspect_source
├── handbrake_scan
├── extract_audio_1
├── extract_subtitle_3
├── crop_subtitle_3
├── crf_analysis
├── encode
├── validate
├── mux
├── screenshot_candidate_generation
├── screenshot_agent_selection
└── screenshot_render
```

Each Task stores:

```text
id
job_id
type
status
created_at
started_at
finished_at
progress
command
exit_code
log_path
error_message
worker_id
```

Possible Task states:

```text
QUEUED
RUNNING
SUCCEEDED
FAILED
CANCELLED
```

---

# 14. Source Analysis

After creating a MovieJob, run the following analysis:

```text
mkvmerge -J
MediaInfo --Output=JSON
ffprobe
HandBrakeCLI --scan
```

Store raw results as artifacts.

Normalize useful information into the database.

The analysis should determine:

* video codec;
* resolution;
* frame rate;
* duration;
* bit depth;
* color information;
* audio tracks;
* subtitle tracks;
* chapters;
* languages;
* track names;
* default/forced flags;
* HandBrake automatic crop.

---

# 15. HandBrake Crop

HandBrake is authoritative for crop determination.

For example:

```text
source: 1920x1080

crop:
top: 138
bottom: 138
left: 0
right: 0

output:
1920x804
```

Persist this crop in the manifest and database.

The same crop values must be reused consistently by:

* HandBrake full encode;
* Sup2sup;
* source screenshot generation;
* comparison renderer.

Do not independently calculate crop in different modules.

---

# 16. Track Selection UI

Once source analysis completes, transition to:

```text
WAITING_FOR_TRACK_SELECTION
```

Display:

## Audio tracks

For each track show:

* track ID;
* codec;
* language;
* channel layout;
* sample rate;
* bit depth where relevant;
* bitrate;
* track name;
* default flag;
* commentary indication where identifiable.

Example:

```text
☑ Track 1
English
DTS-HD MA 5.1
24-bit

☐ Track 2
English
AC3 5.1
640 kbps

☐ Track 3
English Commentary
AC3 2.0
192 kbps
```

## Subtitle tracks

Display:

* track ID;
* language;
* codec;
* track name;
* forced/default status.

The user confirms the selected tracks.

Store this decision permanently.

---

# 17. Track Extraction

After track confirmation:

```text
mkvextract
```

extract selected audio streams and selected PGS streams.

Preserve:

* codec-native audio where possible;
* source metadata;
* language;
* track title.

Do not transcode audio unless explicitly configured later.

---

# 18. Sup2sup Processing

Selected PGS tracks should be processed through Sup2sup CLI.

Sup2sup must use the exact HandBrake crop values.

Conceptually:

```text
Source PGS
   ↓
Sup2sup CLI
   +
HandBrake crop
   ↓
Cropped PGS
```

Store:

* original extracted subtitle;
* processed subtitle;
* Sup2sup logs.

---

# 19. CRF Studio Integration

CRF Studio currently provides GUI and CLI.

The server must only depend on its CLI.

Create a dedicated adapter:

```python
class CRFStudioAdapter:
    def run_analysis(...):
        ...
```

The adapter should normalize CRF Studio output into machine-readable JSON.

Store:

```text
sample CRF values
sample bitrate
average QP
predicted curve
raw CRF Studio output
generated graph data
```

Do not require the original CRF Studio GUI.

---

# 20. CRF Analysis Web Page

After CRF Studio finishes, transition to:

```text
WAITING_FOR_ENCODE_SELECTION
```

Display interactive charts.

At minimum:

## Chart 1

```text
CRF → bitrate
```

## Chart 2

```text
CRF → average QP
```

Optionally:

```text
QP → bitrate
```

Show actual sample points separately from predicted points.

Hovering should show exact values.

Example:

```text
CRF 16.5

Predicted bitrate: 9.24 Mbps
Sample bitrate:    9.46 Mbps
Average B QP:      19.12
```

The user chooses the final encode setting.

Persist:

```text
selected CRF
selected preset
selected codec
selected encoder parameters
timestamp
selected_by=user
```

---

# 21. Encode Policy

Do not let Codex freely invent encoder parameters.

Encoder parameters should come from deterministic configuration profiles.

Example:

```text
profiles/
├── x264-live.yaml
├── x265-live.yaml
├── x264-animation.yaml
└── x265-animation.yaml
```

The application selects or lets the user select a profile.

The final encode command is constructed deterministically.

The initial main encoder backend should be:

```text
HandBrakeCLI
```

because:

* HandBrake provides mature presets;
* crop handling is integrated;
* filters and metadata handling are consistent;
* CLI operation is easy to automate.

---

# 22. Full Encode

Start HandBrakeCLI as a long-running worker subprocess.

The HTTP request must not remain open.

Correct flow:

```text
Frontend
   ↓
POST /encode
   ↓
FastAPI
   ↓
enqueue Task
   ↓
Worker
   ↓
HandBrakeCLI
```

The encode may take many hours.

Closing the browser must not affect it.

---

# 23. Progress Tracking

Parse HandBrake progress output.

Persist periodically:

```text
percentage
fps
ETA
current frame if available
elapsed time
```

Expose progress through Server-Sent Events.

Example UI:

```text
Full Encode

██████████████░░░░░░ 68.3%

FPS: 13.8
Elapsed: 05:11:42
ETA: 02:31:22
```

---

# 24. Logs

All external tools must have persistent logs.

For example:

```text
logs/
├── source-analysis.log
├── handbrake-scan.log
├── extraction.log
├── sup2sup.log
├── crf-studio.log
├── encode.log
├── mux.log
└── screenshot.log
```

The frontend must provide a log viewer.

Logs should remain available after container restart.

---

# 25. Encode Validation

A successful process exit code is not sufficient.

Create an explicit EncodeValidator.

Compare source vs encoded video.

Validate at minimum:

```text
duration
frame rate
resolution
crop result
aspect ratio
frame count where meaningful
color primaries
transfer characteristics
matrix coefficients
bit depth
HDR metadata if applicable
timestamp continuity
```

Also collect:

```text
final bitrate
file size
encoder statistics
average I/P/B QP if available
```

Validation should produce structured output:

```json
{
  "valid": true,
  "warnings": [],
  "errors": [],
  "metrics": {}
}
```

Only continue automatically if validation passes.

---

# 26. Remux

Remux should be completely deterministic.

Construct a mux plan first.

Example:

```json
{
  "video": "encode/video.hevc",
  "audio": [
    "audio/track1.dtsma"
  ],
  "subtitles": [
    "subtitles/eng.cropped.sup"
  ],
  "chapters": "metadata/chapters.xml"
}
```

Then execute with:

```text
mkvmerge
```

Preserve:

* language tags;
* track names;
* track order;
* default flags;
* forced flags;
* chapters;
* relevant metadata.

Codex should not generate arbitrary mkvmerge commands.

---

# 27. Screenshot Subsystem

The screenshot system should be implemented as an independent subsystem.

```text
ScreenshotPipeline
│
├── CandidateGenerator
├── CandidateFilter
├── ContactSheetGenerator
├── CodexScreenshotSelector
├── DiversityValidator
└── ComparisonRenderer
```

The Agent is only one component.

---

# 28. Screenshot Objective

Screenshot selection serves two purposes.

## A. Representative / aesthetic frames

Examples:

* major characters;
* visually attractive cinematography;
* wide scenes;
* representative locations;
* interesting lighting;
* recognizable movie scenes.

## B. Encoding-comparison frames

Examples:

* film grain;
* facial texture;
* hair;
* foliage;
* fabric;
* shadows;
* dark gradients;
* smoke;
* water;
* complex backgrounds;
* high-frequency texture.

Final selection should balance both categories.

Make the ratio configurable.

Example:

```yaml
screenshots:
  count: 10

  categories:
    representative: 6
    encode_challenging: 4
```

---

# 29. Screenshot Policy

Allow the user to provide natural-language screenshot-selection requirements.

Example:

```yaml
screenshots:
  policy: |
    Prefer visually interesting and representative frames.

    Include:
    - character close-ups
    - detailed backgrounds
    - dark scenes with shadow detail
    - visible film grain
    - wide cinematic shots
    - scenes with fine texture

    Avoid:
    - motion blur
    - transitions
    - fade frames
    - black frames
    - credits
    - repeated shots
    - visually uninteresting frames

    Screenshots should be distributed across the movie.
```

Store the policy per user or globally.

---

# 30. Candidate Generation

Do not send the entire movie to Codex.

A typical two-hour film may have roughly 170,000 frames.

Use traditional algorithms first.

Pipeline:

```text
Full Movie
   ↓
Scene Detection
   ↓
Scene Candidates
   ↓
Technical Filtering
   ↓
200–300 Frames
   ↓
Codex
```

Possible scene detection methods:

```text
FFmpeg scene detection
PySceneDetect
custom histogram/SSIM logic
```

Avoid selecting the exact scene-cut frame.

Instead sample slightly inside the scene, e.g.:

```text
scene_start + 1–4 seconds
```

depending on scene length.

---

# 31. Candidate Filtering

Deterministically reject frames that are:

* almost black;
* almost entirely white;
* severe motion blur;
* fade in/out;
* transition frames;
* credits;
* obvious title cards when undesired;
* near-duplicates;
* extremely low-detail frames.

Useful traditional metrics may include:

```text
brightness
contrast
edge density
Laplacian blur score
perceptual hash
SSIM
histogram similarity
```

These filters reduce Agent workload.

---

# 32. Contact Sheet Selection Stage

Do not initially send 200 full-resolution frames individually.

Create contact sheets.

Example:

```text
4 × 4 grid
16 candidates per sheet
```

Each thumbnail must contain a visible candidate ID.

Example:

```text
#001
#002
#003
...
```

Ask Codex to shortlist candidates.

Codex should return structured JSON only.

Example:

```json
{
  "selected": [
    {
      "candidate_id": 13,
      "score": 0.93,
      "category": "encode_challenging",
      "reason": "Detailed facial texture with fine grain and dark background."
    }
  ]
}
```

Use strict schema validation.

---

# 33. Second-Stage Screenshot Selection

Take shortlisted candidates, for example:

```text
200 initial candidates
        ↓
40 shortlisted candidates
```

Provide higher-quality images to Codex.

Ask the Agent to choose the final target count.

The Agent should consider:

```text
composition
semantic importance
visual quality
texture
lighting
grain
dark detail
scene diversity
character diversity
timeline coverage
encoding difficulty
```

---

# 34. Diversity Validation

Do not rely exclusively on Codex to enforce diversity.

Use deterministic checks.

Examples:

* avoid multiple frames within a short interval;
* avoid too many frames from the same scene;
* avoid all screenshots containing the same character;
* ensure broad timeline coverage;
* ensure mixture of close-ups and wide shots;
* ensure category target distribution.

If the selected set violates constraints:

```text
DiversityValidator
    ↓
request replacement candidates
    ↓
Codex
```

---

# 35. Source-Only Selection

The Agent should select frames using the SOURCE video only.

Do NOT show encoded output during semantic frame selection.

Correct:

```text
Source Frame
    ↓
Screenshot Agent
    ↓
Timestamp selected
```

Then:

```text
timestamp
   ├── source frame
   └── encoded frame
```

This prevents selection bias toward encoded results.

---

# 36. Frame Identity

Persist both:

```text
source frame number
source PTS/timestamp
```

The timestamp should be the primary matching identity.

Example:

```json
{
  "source_frame": 38291,
  "pts_seconds": 1596.845
}
```

This helps maintain correspondence if filtering affects frame numbering.

---

# 37. Screenshot Crop

Source screenshots must receive the same crop as HandBrake.

Example:

```text
Source:
1920×1080

HandBrake crop:
top 138
bottom 138

Source screenshot:
1920×804

Encoded screenshot:
1920×804
```

This makes comparison images directly comparable.

---

# 38. Comparison Screenshot Rendering

The generated comparison screenshots must reproduce the existing AvsPmod/ffinfo-style screenshot format as closely as possible.

The output must consist of **two independent full-resolution PNG images for each selected frame**:

```text
43631.src.png
43631.encode.png
```

Do not create:

* a bottom metadata panel;
* a side panel;
* a border around the image;
* a combined side-by-side image;
* a resized preview as the final artifact.

The exported Source and Encode images must retain the exact cropped video resolution.

Example:

```text
Source Blu-ray frame:
1920 × 1080

HandBrake crop:
top    = 138
bottom = 138
left   = 0
right  = 0

Final screenshot:
1920 × 804
```

The Source frame must have the exact same crop as the encoded output so that both screenshots have identical dimensions.

The rendering pipeline should be:

```text
Selected source frame / timestamp
        │
        ├── Source
        │     ↓
        │  Decode source frame
        │     ↓
        │  Apply HandBrake crop
        │     ↓
        │  Convert to final RGB representation
        │     ↓
        │  Draw ffinfo-style overlay
        │     ↓
        │  <frame_number>.src.png
        │
        └── Encode
              ↓
           Locate corresponding encoded frame
              ↓
           Decode frame
              ↓
           Convert to final RGB representation
              ↓
           Draw the same ffinfo-style overlay
              ↓
           <frame_number>.encode.png
```

Use lossless PNG output.

Do not apply any additional:

```text
resizing
sharpening
denoising
contrast adjustment
color enhancement
```

The informational text must be rendered only after the final video frame has been extracted and converted to the image representation used for the PNG.

Pillow may be used for the text-overlay stage. FFmpeg, PyAV, or VapourSynth may be used for frame extraction.

The screenshot-selection Agent is responsible only for selecting the frame/timestamp. It must not render or format screenshots.

---

# 39. ffinfo-Style Screenshot Overlay

The screenshot overlay must closely reproduce the format shown in the provided reference screenshots.

It is a small three-line text overlay placed directly in the upper-left corner of the video frame.

There is **no background box**.

For a 1920-pixel-wide screenshot, the observed reference layout is approximately:

```yaml
overlay:
  x: 8
  y: 3

  font_size: 16
  font_weight: bold
  font_family: Liberation Sans

  text_color: yellow
  outline_color: black
  outline_width: 1

  line_pitch: 18
```

The exact implementation should be visually calibrated against the supplied reference screenshots.

The observed text bounds in the 1920×804 reference are approximately:

```text
left edge:        8–9 px
first line:       y = 4–14
second line:      y = 22–35
third line:       y = 40–53
```

Use approximately an 18-pixel vertical line pitch.

The preferred Linux font is:

```text
Liberation Sans Bold
```

because it is visually close to the Arial-style bold sans-serif font used by the reference workflow.

Fallback order:

```text
Liberation Sans Bold
Arial Bold
DejaVu Sans Bold
```

Do not use a monospaced font.

The text should use:

```text
bright yellow fill
+
thin black outline
```

The black outline is important because the text must remain readable over both bright and dark image content.

## Source screenshot format

The Source image must contain exactly three lines:

```text
Frame Number: {source_frame_number} of {source_total_frames}
Picture Type: {picture_type_display}
Source
```

Example:

```text
Frame Number: 43631 of 200484
Picture Type: B (Bi-dir predicted)
Source
```

## Encode screenshot format

The Encode image must also contain exactly three lines:

```text
Frame Number: {source_frame_number} of {source_total_frames}
Picture Type: {picture_type_display}
{release_name}
```

Example:

```text
Frame Number: 43631 of 200484
Picture Type: B (Bi-dir predicted)
No.Other.Choice.2025.1080p.BluRay.x264-WiKi
```

The encoded screenshot must therefore use the **source-frame numbering domain**, so a Source/Encode pair visibly carries the same frame number.

Do not replace this with an independently calculated encoded-frame number.

`release_name` must come from the MovieJob configuration or release configuration. The renderer must not infer or invent it.

## Picture Type

The renderer should reproduce the existing ffinfo-style picture-type wording.

Examples:

```text
Picture Type: I
Picture Type: P (Predicted)
Picture Type: B (Bi-dir predicted)
```

The actual picture type must be determined from frame metadata rather than guessed.

Persist normalized frame information such as:

```json
{
  "source_frame_number": 43631,
  "source_total_frames": 200484,
  "source_pts_seconds": 1819.609,
  "picture_type": "B",
  "picture_type_display": "B (Bi-dir predicted)"
}
```

Where possible, obtain frame metadata through FFmpeg/ffprobe, PyAV, or another decoder capable of exposing per-frame picture type and timestamp.

## Overlay Calibration

The initial implementation must include a calibration test against the supplied reference images.

Render the following exact text:

```text
Frame Number: 43631 of 200484
Picture Type: B (Bi-dir predicted)
Source
```

onto a 1920×804 test frame.

Tune only:

```text
font family
font size
x/y offset
outline width
line spacing
```

until the result visually matches the reference.

The target is to reproduce the existing screenshot style, not redesign or modernize it.

The overlay should remain configurable so that small adjustments can be made later without changing rendering code.

Example configuration:

```yaml
screenshot_overlay:
  font_family: "Liberation Sans"
  font_weight: "bold"
  font_size: 16

  x: 8
  y: 3
  line_pitch: 18

  text_color: "#FFFF00"
  outline_color: "#000000"
  outline_width: 1
```

The frontend may display Source and Encode screenshots side by side for convenience, but the actual exported artifacts must remain separate full-resolution PNG files.

---

# 40. Screenshot UI

Provide a gallery page.

Display:

```text
Candidates: 240
Shortlisted: 38
Final: 10
```

For each final screenshot show:

```text
thumbnail
timestamp
category
Agent reason
```

Clicking a screenshot opens:

```text
Source image
Encoded image
metadata
reason
timestamp
```

Provide optional:

```text
Replace Screenshot
```

Manual override does NOT block automated workflow.

---

# 41. Codex Agent Permissions

Codex should have minimal filesystem permissions.

Prefer:

```text
/source:ro
/workspace/screenshots:ro
```

where practical.

Codex returns structured decisions to the API.

It should not directly:

* delete movie files;
* modify source MKVs;
* execute arbitrary HandBrake commands;
* publish releases;
* decide selected tracks;
* decide final CRF.

---

# 42. Codex Thread Persistence

Store:

```text
codex_thread_id
```

on the MovieJob if persistent context is useful.

However:

> Codex conversation history must never be the authoritative system state.

Authoritative state is:

```text
PostgreSQL
+
manifest.yaml
```

The Codex thread is only reasoning context.

---

# 43. API Design

Suggested initial API.

## Incoming sources

```text
GET /api/sources
```

Returns completed MKVs under `/source`.

```text
POST /api/jobs
```

Creates a MovieJob from a source.

---

## Jobs

```text
GET /api/jobs
GET /api/jobs/{job_id}
DELETE /api/jobs/{job_id}
```

Deletion should require explicit confirmation and should never delete the immutable source.

---

## Analysis

```text
POST /api/jobs/{job_id}/analyze
GET  /api/jobs/{job_id}/analysis
```

---

## Tracks

```text
GET /api/jobs/{job_id}/tracks

POST /api/jobs/{job_id}/tracks/selection
```

Example request:

```json
{
  "audio_track_ids": [1],
  "subtitle_track_ids": [4, 5]
}
```

---

## CRF Studio

```text
POST /api/jobs/{job_id}/crf-analysis
GET  /api/jobs/{job_id}/crf-analysis
```

---

## Encode selection

```text
POST /api/jobs/{job_id}/encode-selection
```

Example:

```json
{
  "codec": "x265",
  "profile": "x265-live",
  "crf": 17.0
}
```

---

## Encode

```text
POST /api/jobs/{job_id}/encode
GET  /api/jobs/{job_id}/encode
```

---

## Screenshots

```text
POST /api/jobs/{job_id}/screenshots/generate-candidates
POST /api/jobs/{job_id}/screenshots/select
POST /api/jobs/{job_id}/screenshots/render

GET /api/jobs/{job_id}/screenshots
```

Optional override:

```text
POST /api/jobs/{job_id}/screenshots/{screenshot_id}/replace
```

---

## Tasks

```text
GET /api/jobs/{job_id}/tasks
GET /api/tasks/{task_id}
POST /api/tasks/{task_id}/retry
POST /api/tasks/{task_id}/cancel
```

---

## Logs

```text
GET /api/tasks/{task_id}/logs
```

---

## Events

Use Server-Sent Events:

```text
GET /api/jobs/{job_id}/events
```

Events:

```text
state_changed
task_started
task_progress
task_completed
task_failed
artifact_created
user_action_required
```

---

# 44. Database Model

Minimum tables:

```text
users
movie_jobs
movie_tracks
track_selections
tasks
artifacts
crf_results
encode_configs
screenshots
agent_runs
```

---

## movie_jobs

Suggested fields:

```text
id UUID
source_path
title
year
state
manifest_path
codex_thread_id nullable
created_at
updated_at
completed_at nullable
```

---

## tasks

```text
id UUID
job_id UUID
type
status
progress
command_json
worker_id
started_at
finished_at
exit_code
log_path
error_message
```

---

## artifacts

Track generated files.

```text
id
job_id
task_id
artifact_type
path
size
checksum nullable
metadata JSONB
```

Artifact types could include:

```text
SOURCE_METADATA
AUDIO
SUBTITLE_ORIGINAL
SUBTITLE_CROPPED
CRF_RESULTS
ENCODED_VIDEO
FINAL_MKV
SCREENSHOT_SOURCE
SCREENSHOT_ENCODE
COMPARISON
LOG
```

---

# 45. Frontend Pages

Implement at least:

## 1. Dashboard

Display:

```text
Active Jobs
Waiting for Input
Recently Completed
Failed Tasks
```

---

## 2. New Job

Display incoming MKVs.

Example:

```text
Dune.Part.Two.2024.mkv
71.4 GB
[Create Job]
```

---

## 3. Job Overview

Timeline:

```text
Source Analysis        ✓
Track Selection        ✓
Track Preparation      ✓
CRF Analysis           ✓
Encode Selection       ✓
Encode                 ● 68%
Validation             ○
Remux                  ○
Screenshots            ○
```

---

## 4. Track Selection

Human Gate #1.

---

## 5. CRF Analysis

Interactive charts plus encode selection.

Human Gate #2.

---

## 6. Encode Status

Progress, ETA, logs, statistics.

---

## 7. Screenshot Gallery

Agent results and comparisons.

---

## 8. Artifacts / Logs

Browse generated outputs and logs.

---

# 46. Docker Compose

Create a root-level:

```text
docker-compose.yml
```

Services:

```yaml
services:
  frontend:
  api:
  worker:
  agent:
  postgres:
  redis:
```

Persistent named volumes should be used for:

```text
postgres-data
redis-data
codex-auth
```

Movie data should use bind mounts.

---

# 47. Suggested Repository Structure

```text
movie-agent/
│
├── docker-compose.yml
├── .env.example
│
├── frontend/
│   ├── src/
│   ├── package.json
│   └── Dockerfile
│
├── backend/
│   ├── app/
│   │   ├── api/
│   │   ├── db/
│   │   ├── models/
│   │   ├── schemas/
│   │   ├── services/
│   │   ├── state_machine/
│   │   └── main.py
│   └── Dockerfile
│
├── worker/
│   ├── adapters/
│   │   ├── handbrake.py
│   │   ├── mkvtoolnix.py
│   │   ├── mediainfo.py
│   │   ├── ffmpeg.py
│   │   ├── sup2sup.py
│   │   └── crf_studio.py
│   │
│   ├── pipeline/
│   │   ├── analysis.py
│   │   ├── extraction.py
│   │   ├── subtitles.py
│   │   ├── encoding.py
│   │   ├── validation.py
│   │   ├── muxing.py
│   │   └── screenshots.py
│   │
│   ├── tasks/
│   └── Dockerfile
│
├── agent/
│   ├── screenshot_agent.py
│   ├── schemas.py
│   ├── prompts/
│   │   └── screenshot_selection.md
│   └── Dockerfile
│
├── shared/
│   ├── models/
│   ├── config/
│   └── utilities/
│
└── profiles/
    ├── x264-live.yaml
    ├── x265-live.yaml
    ├── x264-animation.yaml
    └── x265-animation.yaml
```

---

# 48. Configuration

Use environment variables only for deployment-specific values.

Example:

```env
DATABASE_URL=
REDIS_URL=

SOURCE_ROOT=/source
WORKSPACE_ROOT=/workspace
COMPLETED_ROOT=/completed

CRF_STUDIO_BIN=/opt/crf-studio/crf-studio
SUP2SUP_BIN=/opt/sup2sup/sup2sup
HANDBRAKE_BIN=/usr/bin/HandBrakeCLI
MKVMERGE_BIN=/usr/bin/mkvmerge
MKVEXTRACT_BIN=/usr/bin/mkvextract
```

Application behavior should use YAML configuration rather than environment variables where possible.

---

# 49. Failure Recovery

Every pipeline stage must be restartable.

Container restart must not lose:

```text
MovieJob state
user selections
task history
generated files
logs
Codex authentication
```

At application startup:

1. detect Tasks marked RUNNING;
2. determine whether associated subprocess/job survived;
3. reconcile state;
4. mark interrupted jobs appropriately;
5. allow retry.

Never restart the entire movie pipeline because one task failed.

---

# 50. Security

Important rules:

### Source files

Read-only.

### Agent

Minimal filesystem access.

### Upload/publish credentials

Never expose to Codex context.

### Commands

Use argument arrays:

```python
subprocess.Popen(["HandBrakeCLI", "-i", input_path, ...])
```

Avoid shell command interpolation.

### Paths

Validate all paths.

Reject:

```text
../
```

style traversal.

Only allow files under configured storage roots.

---

# 51. Implementation Phases

Do not implement the entire system simultaneously.

## Phase 1 — Core Infrastructure

Implement:

```text
Docker Compose
PostgreSQL
Redis
FastAPI
React frontend
MovieJob model
Task model
state machine
incoming-source discovery
```

Acceptance condition:

A user can see an incoming MKV and create a MovieJob.

---

## Phase 2 — Source Analysis

Implement adapters:

```text
mkvmerge
MediaInfo
ffprobe
HandBrake scan
```

Acceptance condition:

The UI displays normalized source metadata and HandBrake crop.

---

## Phase 3 — Track Selection

Implement:

```text
track UI
selection persistence
mkvextract
Sup2sup
```

Acceptance condition:

The user selects tracks and the system prepares audio/subtitle artifacts correctly.

---

## Phase 4 — CRF Studio

Implement:

```text
CRF Studio CLI adapter
results normalization
curve API
interactive frontend charts
```

Acceptance condition:

The system runs CRF Studio and the user can visually choose encoding parameters.

---

## Phase 5 — Full Encode

Implement:

```text
HandBrake profiles
HandBrakeCLI adapter
Celery encode Task
SSE progress
log parsing
```

Acceptance condition:

A multi-hour encode can continue after the browser is closed.

---

## Phase 6 — Validation and Remux

Implement:

```text
EncodeValidator
mkvmerge mux planning
final MKV
```

Acceptance condition:

The system generates a validated final MKV.

---

## Phase 7 — Screenshot Candidate Generation

Implement:

```text
scene detection
candidate sampling
black-frame rejection
blur rejection
duplicate rejection
contact sheets
```

Acceptance condition:

A two-hour movie can be reduced to roughly 100–300 meaningful candidate frames automatically.

---

## Phase 8 — Codex Screenshot Agent

Implement:

```text
Codex authentication
structured Agent output
contact-sheet selection
second-stage selection
ScreenshotPolicy
diversity validation
```

Acceptance condition:

The Agent autonomously produces a final screenshot set matching the screenshot policy.

---

## Phase 9 — Comparison Renderer

Implement:

```text
source extraction
encode extraction
crop
metadata overlays
PNG generation
gallery UI
```

Acceptance condition:

The system produces PT-ready source/encode comparison screenshots.

---

## Phase 10 — Release Pipeline

Leave this outside V1.

Future work:

```text
NFO
MD5
torrent generation
BBCode
release naming
upload/publishing integration
```

Design interfaces now, but do not block V1 on them.

---

# 52. Important Architectural Constraints

The implementation must respect the following constraints.

## Constraint 1

Codex is not the state machine.

The application is.

---

## Constraint 2

Codex is not responsible for pipeline correctness.

Deterministic Python code is.

---

## Constraint 3

Track selection is user-controlled.

---

## Constraint 4

Final CRF/bitrate selection is user-controlled.

---

## Constraint 5

Crop is determined by HandBrake.

The crop result is reused everywhere.

---

## Constraint 6

HandBrakeCLI is the primary full-encode backend.

---

## Constraint 7

CRF Studio runs via CLI.

Its GUI is not required on the server.

---

## Constraint 8

Sup2sup runs via CLI.

---

## Constraint 9

Screenshot selection is the primary autonomous Agent task.

---

## Constraint 10

Screenshot semantic selection is based on source frames only.

---

## Constraint 11

Movie files are stored outside container writable layers.

---

## Constraint 12

All application-level software runs inside containers.

---

# 53. Definition of V1 Completion

V1 should be considered complete when the following end-to-end workflow works reliably:

```text
1. Copy Source.mkv into incoming storage.

2. Open the web UI.

3. Create a MovieJob.

4. Server automatically:
   - analyzes MKV;
   - performs HandBrake scan;
   - determines crop.

5. Web UI asks user to select tracks.

6. Server:
   - extracts selected tracks;
   - processes selected PGS subtitles.

7. Server runs CRF Studio.

8. Web UI displays CRF/QP/bitrate curves.

9. User chooses final encoding parameters.

10. Server starts HandBrakeCLI encode.

11. Browser may be closed.

12. Encode continues independently.

13. Server validates encoded output.

14. Server remuxes final MKV.

15. Screenshot subsystem automatically:
    - generates candidates;
    - filters candidates;
    - generates contact sheets;
    - asks Codex to select frames;
    - validates diversity;
    - extracts matching source/encode frames;
    - generates comparison images.

16. Web UI displays:
    - completed MKV;
    - encode statistics;
    - final screenshots;
    - logs;
    - artifacts.

17. The entire system survives container/server restart without losing job state.
```

---

# 54. Development Philosophy

Favor explicitness over Agent autonomy.

A movie encode can consume many hours of CPU time and produce tens of gigabytes of data. Therefore actions should be:

```text
reproducible
idempotent where possible
restartable
observable
auditable
deterministic
```

The Agent should only be introduced where traditional deterministic automation performs poorly.

For this project, that is primarily:

```text
semantic screenshot understanding
visual screenshot quality evaluation
screenshot diversity reasoning
```

Everything else should remain normal software engineering.
