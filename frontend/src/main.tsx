import { useEffect, useState, type ReactNode } from "react";
import { createRoot } from "react-dom/client";
import {
  QueryClient,
  QueryClientProvider,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import {
  BrowserRouter,
  Link,
  NavLink,
  Route,
  Routes,
  useNavigate,
  useParams,
} from "react-router-dom";
import type { TrackFlag } from "./types";
import { EncodingConfiguration } from "./EncodingConfiguration";
import { EncodingPauseButton } from "./EncodingPauseButton";
import { encodingPauseState } from "./encoding-pause";
import { AgentTranscript } from "./AgentTranscript";
import { BitrateCurve } from "./BitrateCurve";
import { CRFProgress } from "./CRFProgress";
import { QueuePage, QueueSummary } from "./Queue";
import { RemoveJob } from "./RemoveJob";
import { Release } from "./Release";
import { ScreenshotGallery } from "./ScreenshotGallery";
import { ApiError, api, clock, isConnectionError, readable, size } from "./api";
import type { Config, IMDbMovie, Job, RateControl, Task } from "./types";
import "./style.css";

const client = new QueryClient({
  defaultOptions: { queries: { retry: false, refetchOnWindowFocus: true } },
});
const ErrorBox = ({ error }: { error: Error | null }) =>
  error ? (
    <div className="error" role="alert">
      {error.message}
    </div>
  ) : null;
const Badge = ({ children }: { children: string }) => (
  <span className={`badge ${children.toLowerCase()}`}>
    {readable(children)}
  </span>
);

function App() {
  const [expired, setExpired] = useState(false);
  const config = useQuery({
    queryKey: ["config"],
    queryFn: () => api<Config>("/config"),
    enabled: !expired,
    refetchInterval: (query) =>
      isConnectionError(query.state.error) ? 5000 : false,
  });
  useEffect(() => {
    const listener = () => setExpired(true);
    window.addEventListener("session-expired", listener);
    return () => window.removeEventListener("session-expired", listener);
  }, []);
  if (
    expired ||
    (config.error instanceof ApiError && config.error.status === 401)
  )
    return (
      <Login
        onLogin={() => {
          setExpired(false);
          client.resetQueries();
        }}
      />
    );
  if (!config.data && config.error)
    return (
      <div className="login">
        <h1>Connection interrupted</h1>
        <ErrorBox error={config.error} />
        <button disabled={config.isFetching} onClick={() => config.refetch()}>
          {config.isFetching ? "Reconnecting…" : "Reconnect"}
        </button>
      </div>
    );
  if (!config.data)
    return <div className="loading">Connecting to BDRip Agent…</div>;
  return (
    <>
      <aside>
        <Link className="brand" to="/">
          <span className="brand-icon">◧</span> BDRip<span>AGENT</span>
        </Link>
        <div className="nav-label">WORKSPACE</div>
        <NavLink to="/" end>
          ▦ &nbsp; Dashboard
        </NavLink>
        <NavLink to="/new">＋ &nbsp; New movie job</NavLink>
        <NavLink to="/queue">☷ &nbsp; Queue</NavLink>
        <div className="sidebar-foot">
          <i /> Self-hosted encoding
          <br />
          <small>Source → encode → compare</small>
          <button
            className="text-button"
            onClick={() =>
              api("/session", undefined, "DELETE").then(() => {
                client.clear();
                setExpired(true);
              })
            }
          >
            Sign out
          </button>
        </div>
      </aside>
      <main>
        <ErrorBox error={config.error} />
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/new" element={<NewJob config={config.data} />} />
          <Route path="/queue" element={<QueuePage />} />
          <Route
            path="/jobs/:id/*"
            element={<JobPage config={config.data} />}
          />
        </Routes>
      </main>
    </>
  );
}

function Login({ onLogin }: { onLogin: () => void }) {
  const [token, setToken] = useState("");
  const login = useMutation({
    mutationFn: () => api("/session", { token }),
    onSuccess: onLogin,
  });
  return (
    <div className="login">
      <div className="eyebrow">BDRIP AGENT</div>
      <h1>Your encoding workspace.</h1>
      <p>Enter the access token configured for this server.</p>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          login.mutate();
        }}
      >
        <label>
          Access token
          <input
            autoFocus
            type="password"
            required
            value={token}
            onChange={(e) => setToken(e.target.value)}
            autoComplete="current-password"
          />
        </label>
        <button disabled={login.isPending}>Open workspace →</button>
        <ErrorBox error={login.error} />
      </form>
    </div>
  );
}

function Dashboard() {
  const jobs = useQuery({
    queryKey: ["jobs"],
    queryFn: () => api<Job[]>("/jobs"),
    refetchInterval: 5000,
  });
  const data = jobs.data ?? [];
  const waiting = data.filter((j) => j.state.startsWith("WAITING"));
  const failed = data.filter((j) => latestTask(j)?.status === "FAILED");
  return (
    <>
      <header>
        <div>
          <div className="eyebrow">MOVIE ENCODING AUTOMATION</div>
          <h1>Workspace overview</h1>
          <p>Keep every encode, decision, and comparison in one place.</p>
        </div>
        <Link className="button" to="/new">
          ＋ New movie job
        </Link>
      </header>
      <div className="stats">
        {[
          ["Active jobs", data.filter((j) => j.state !== "COMPLETE").length],
          ["Waiting for input", waiting.length],
          ["Completed", data.filter((j) => j.state === "COMPLETE").length],
          ["Failed tasks", failed.length],
        ].map(([label, count]) => (
          <div className="stat" key={label}>
            <span>{label}</span>
            <strong>{count}</strong>
          </div>
        ))}
      </div>
      <ErrorBox error={jobs.error} />
      <QueueSummary />
      <section>
        <div className="section-heading">
          <h2>Movie jobs</h2>
          <span>{data.length} in your library</span>
        </div>
        {data.length === 0 ? (
          <Empty title="Your first encode starts here">
            Copy a completed MKV into your incoming folder, then create a job.
          </Empty>
        ) : (
          data.map((j) => (
            <div className="job-row" key={j.id}>
              <div className="film-icon">▤</div>
              <Link to={`/jobs/${j.id}`} className="grow">
                <h3>
                  {j.title} {j.year && <span className="muted">{j.year}</span>}
                </h3>
                <span className="muted">
                  {j.analysis_profile} · {j.source_path}
                </span>
              </Link>
              <Badge>{j.state}</Badge>
              {j.analysis.smoke_test && <Badge>SMOKE TEST</Badge>}
              {latestTask(j)?.status === "FAILED" && <Badge>FAILED</Badge>}
              <RemoveJob job={j} />
            </div>
          ))
        )}
      </section>
    </>
  );
}

function Empty({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="empty">
      <div>◫</div>
      <h3>{title}</h3>
      <p>{children}</p>
    </div>
  );
}
function latestTask(job: Job) {
  return [...job.tasks].sort((a, b) =>
    b.created_at.localeCompare(a.created_at),
  )[0];
}

function NewJob({ config }: { config: Config }) {
  const sources = useQuery({
    queryKey: ["sources"],
    queryFn: () => api<{ path: string; size: number }[]>("/sources"),
    refetchInterval: 10000,
  });
  const [source, setSource] = useState(""),
    [title, setTitle] = useState(""),
    [year, setYear] = useState("");
  const [paired, setPaired] = useState(true);
  const [imdbId, setImdbId] = useState("");
  const [movie, setMovie] = useState<IMDbMovie | null>(null);
  const [secondProfile, setSecondProfile] = useState("x264-live");
  const [profile, setProfile] = useState("x265-live"),
    [policy, setPolicy] = useState(config.screenshots);
  const navigate = useNavigate();
  const lookup = useMutation({
    mutationFn: () =>
      api<IMDbMovie>(
        `/metadata/imdb?imdb_id=${encodeURIComponent(imdbId.trim())}`,
      ),
    onSuccess: (result) => {
      setMovie(result);
      setTitle(result.title);
      setYear(String(result.year));
    },
  });
  const imdbActive = imdbId.trim().length > 0;
  const titleOption = movie?.title_options.find(
    (option) => option.title === title,
  );
  const previewCodecs = paired
    ? [config.profiles[profile].codec, config.profiles[secondProfile].codec]
    : [config.profiles[profile].codec];
  const create = useMutation({
    mutationFn: () =>
      api<Job | Job[]>(paired ? "/jobs/pair" : "/jobs", {
        source_path: source,
        title,
        year: Number(year),
        ...(imdbActive && movie ? { imdb_id: movie.imdb_id } : {}),
        ...(paired ? { second_profile: secondProfile } : {}),
        analysis_profile: profile,
        screenshot_policy: policy,
      }),
    onSuccess: (result) =>
      navigate(Array.isArray(result) ? "/" : `/jobs/${result.id}`),
  });
  function choose(path: string) {
    setSource(path);
    if (!imdbActive)
      setTitle(
        path
          .split("/")
          .pop()!
          .replace(/\.mkv$/i, "")
          .replaceAll(".", " "),
      );
  }
  return (
    <>
      <header>
        <div>
          <div className="eyebrow">START A WORKFLOW</div>
          <h1>New movie job</h1>
          <p>Select an incoming MKV. Source analysis starts automatically.</p>
        </div>
      </header>
      <div className="two-column">
        <section>
          <h2>Incoming sources</h2>
          <p className="muted">
            Files transferred with the .partial → .mkv rename protocol.
          </p>
          <ErrorBox error={sources.error} />
          {sources.data?.length === 0 && (
            <Empty title="No incoming movies">
              Add an MKV to your configured incoming folder.
            </Empty>
          )}
          {sources.data?.map((s) => (
            <button
              key={s.path}
              className={`source-choice ${source === s.path ? "selected" : ""}`}
              onClick={() => choose(s.path)}
            >
              <span>▤ &nbsp; {s.path}</span>
              <small>{size(s.size)}</small>
            </button>
          ))}
        </section>
        <section>
          <h2>Job configuration</h2>
          <form
            onSubmit={(e) => {
              e.preventDefault();
              if (lookup.isPending || (imdbActive && !movie)) return;
              create.mutate();
            }}
          >
            <label>
              IMDb ID or URL
              <input
                value={imdbId}
                placeholder="tt0133093 or https://www.imdb.com/title/tt0133093/"
                disabled={lookup.isPending || create.isPending}
                onChange={(e) => {
                  setImdbId(e.target.value);
                  setMovie(null);
                  setTitle("");
                  setYear("");
                  lookup.reset();
                  create.reset();
                }}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && imdbActive) {
                    e.preventDefault();
                    setMovie(null);
                    lookup.mutate();
                  }
                }}
              />
            </label>
            <button
              type="button"
              disabled={!imdbActive || lookup.isPending || create.isPending}
              onClick={() => {
                setMovie(null);
                lookup.mutate();
              }}
            >
              {lookup.isPending ? "Looking up IMDb…" : "Look up IMDb"}
            </button>
            <p className="muted">
              Look up a movie to fill its title, year and release filenames.
              Leave IMDb ID empty to enter them manually.
            </p>
            <ErrorBox error={lookup.error} />
            <label>
              Movie title
              {movie && movie.title_options.length > 1 ? (
                <select
                  aria-label="Movie title"
                  value={title}
                  disabled={create.isPending}
                  onChange={(e) => setTitle(e.target.value)}
                >
                  {movie.title_options.map((option) => (
                    <option key={option.title} value={option.title}>
                      {option.title}
                    </option>
                  ))}
                </select>
              ) : (
                <input
                  aria-label="Movie title"
                  required
                  value={title}
                  readOnly={imdbActive}
                  onChange={(e) => setTitle(e.target.value)}
                />
              )}
            </label>
            <label>
              Release year
              <input
                required
                type="number"
                min="1880"
                max="2200"
                value={year}
                readOnly={imdbActive}
                placeholder="2026"
                onChange={(e) => setYear(e.target.value)}
              />
            </label>
            {movie && titleOption && (
              <div className="movie-preview" aria-live="polite">
                <a href={movie.url} target="_blank" rel="noreferrer">
                  {movie.imdb_id} · View on IMDb ↗
                </a>
                <p>Release filename{paired ? "s" : ""}</p>
                {previewCodecs.map((codec) => (
                  <code key={codec}>{titleOption.filenames[codec]}</code>
                ))}
                <small>The audio tag is finalized after track selection.</small>
              </div>
            )}
            <label>
              Analysis & encode profile
              <select
                value={profile}
                onChange={(e) => {
                  setProfile(e.target.value);
                  const codec = config.profiles[e.target.value].codec;
                  setSecondProfile(
                    Object.keys(config.profiles).find(
                      (name) => config.profiles[name].codec !== codec,
                    )!,
                  );
                }}
              >
                {Object.entries(config.profiles).map(([name, p]) => (
                  <option key={name} value={name}>
                    {name} · {p.bit_depth}-bit · {p.preset}
                  </option>
                ))}
              </select>
            </label>
            <label className="track">
              <input
                type="checkbox"
                checked={paired}
                onChange={(e) => setPaired(e.target.checked)}
              />
              Create both x264 and x265 encodes
            </label>
            {paired && (
              <label>
                Second encode profile
                <select
                  value={secondProfile}
                  onChange={(e) => setSecondProfile(e.target.value)}
                >
                  {Object.entries(config.profiles)
                    .filter(
                      ([, p]) => p.codec !== config.profiles[profile].codec,
                    )
                    .map(([name, p]) => (
                      <option key={name} value={name}>
                        {name} · {p.bit_depth}-bit · {p.preset}
                      </option>
                    ))}
                </select>
              </label>
            )}
            <p className="muted">
              WiKi filenames and MKV titles are generated from the movie title,
              year, codec and selected audio. Each encode has seven comparison
              frames, spaced at least {policy.min_spacing_seconds} seconds from
              the other encode's frames.
            </p>
            <label>
              Representative frames (remaining frames show encoding challenges)
              <input
                type="number"
                min="0"
                max="7"
                value={policy.representative}
                onChange={(e) =>
                  setPolicy({
                    ...policy,
                    count: 7,
                    representative: +e.target.value,
                    encode_challenging: 7 - +e.target.value,
                  })
                }
              />
            </label>
            <label>
              Screenshot scan decoder
              <select
                value={policy.decoder ?? "cuda"}
                onChange={(e) =>
                  setPolicy({
                    ...policy,
                    decoder: e.target.value as "cpu" | "cuda",
                  })
                }
              >
                <option value="cpu">CPU</option>
                <option value="cuda">NVIDIA GPU (CPU fallback)</option>
              </select>
            </label>
            <p className="muted">
              GPU accelerates the candidate scan when available. Final
              screenshots use CPU decoding to preserve picture-type labels.
            </p>
            <label>
              Screenshot selection policy
              <textarea
                rows={5}
                value={policy.policy}
                onChange={(e) =>
                  setPolicy({ ...policy, policy: e.target.value })
                }
              />
            </label>
            <button
              disabled={
                !source ||
                create.isPending ||
                lookup.isPending ||
                (imdbActive && !movie)
              }
            >
              {create.isPending ? "Creating…" : "Create job & analyze →"}
            </button>
            <ErrorBox error={create.error} />
          </form>
        </section>
      </div>
    </>
  );
}

function JobPage({ config }: { config: Config }) {
  const { id } = useParams(),
    query = useQueryClient();
  const job = useQuery({
    queryKey: ["job", id],
    queryFn: () => api<Job>(`/jobs/${id}`),
    refetchInterval: 5000,
  });
  useEffect(() => {
    const stream = new EventSource(`/api/jobs/${id}/events`);
    let refreshTimer: number | undefined;
    const refresh = () => {
      if (refreshTimer !== undefined) return;
      refreshTimer = window.setTimeout(() => {
        refreshTimer = undefined;
        // Coalesce bursts and let in-flight reads finish instead of cancelling
        // and restarting them for every artifact/progress event.
        query.invalidateQueries(
          { queryKey: ["job", id] },
          { cancelRefetch: false },
        );
        query.invalidateQueries(
          { queryKey: ["screenshots", id] },
          { cancelRefetch: false },
        );
      }, 1000);
    };
    [
      "ready",
      "state_changed",
      "task_started",
      "task_pause_requested",
      "task_pause_available",
      "task_pause_unavailable",
      "task_paused",
      "task_resumed",
      "task_progress",
      "task_failed",
      "task_completed",
      "artifact_created",
      "screenshot_best_updated",
      "screenshot_decoder_selected",
    ].forEach((name) => stream.addEventListener(name, refresh));
    return () => {
      stream.close();
      window.clearTimeout(refreshTimer);
    };
  }, [id, query]);
  if (job.error && (!job.data || !isConnectionError(job.error)))
    return <ErrorBox error={job.error} />;
  if (!job.data) return <div className="loading">Loading job…</div>;
  const j = job.data;
  return (
    <>
      <Link className="back" to="/">
        ← All jobs
      </Link>
      {job.error && (
        <p className="error" role="status">
          Connection interrupted. Showing the last update while reconnecting.
        </p>
      )}
      <header>
        <div>
          <div className="eyebrow">MOVIE WORKSPACE</div>
          <h1>{j.title}</h1>
          <p>{j.source_path}</p>
          {j.imdb_id && (
            <a
              href={`https://www.imdb.com/title/${j.imdb_id}/`}
              target="_blank"
              rel="noreferrer"
            >
              IMDb: {j.imdb_id} ↗
            </a>
          )}
        </div>
        <Badge>{j.state}</Badge>
      </header>
      {j.analysis.smoke_test && (
        <div className="callout" role="status">
          <h2>Smoke test — source video reused</h2>
          <p>
            Video encoding and encode validation are skipped. The remux uses the
            uncropped source video; comparison images reuse cropped source
            frames. Outputs are marked SMOKE-TEST.
          </p>
        </div>
      )}
      <nav className="tabs">
        {[
          ["", "Overview"],
          ["tracks", "Tracks"],
          ["crf", "CRF analysis"],
          ["encode", "Encode status"],
          ["screenshots", "Screenshots"],
          ["release", "Release"],
          ["artifacts", "Artifacts & logs"],
        ].map(([path, label]) => (
          <NavLink key={path} end={path === ""} to={`/jobs/${id}/${path}`}>
            {label}
          </NavLink>
        ))}
      </nav>
      <Routes>
        <Route index element={<Overview job={j} stages={config.stages} />} />
        <Route path="tracks" element={<Tracks key={j.id} job={j} />} />
        <Route path="crf" element={<CRF job={j} />} />
        <Route path="encode" element={<EncodeStatus job={j} />} />
        <Route path="screenshots" element={<Gallery job={j} />} />
        <Route
          path="release"
          element={
            <Release
              key={j.id}
              job={j}
              config={config}
              controls={(() => {
                const task = [...j.tasks]
                  .filter((t) => t.type === "generate_release")
                  .sort((a, b) => b.created_at.localeCompare(a.created_at))[0];
                return task ? <TaskCard task={task} /> : null;
              })()}
            />
          }
        />
        <Route path="artifacts" element={<Artifacts job={j} />} />
      </Routes>
      <JobTaskLog key={j.id} job={j} />
    </>
  );
}

function Overview({ job, stages }: { job: Job; stages: string[] }) {
  const index = stages.indexOf(job.state),
    video = job.analysis.video;
  return (
    <>
      <div className="two-column">
        <section>
          <h2>Workflow</h2>
          <div className="timeline">
            {stages
              .filter((s) => s !== "NEW")
              .map((s) => (
                <div
                  key={s}
                  className={
                    s === job.state
                      ? "current"
                      : stages.indexOf(s) < index
                        ? "done"
                        : ""
                  }
                >
                  <b>
                    {stages.indexOf(s) < index
                      ? "✓"
                      : s === job.state
                        ? "●"
                        : "○"}
                  </b>
                  {readable(s)}
                </div>
              ))}
          </div>
        </section>
        <div>
          {job.state === "WAITING_FOR_TRACK_SELECTION" && (
            <Callout to="tracks" title="Choose the tracks to preserve">
              Review audio and PGS subtitles to continue.
            </Callout>
          )}
          {job.state === "WAITING_FOR_ENCODE_SELECTION" && (
            <Callout to="crf" title="Your encode decision is ready">
              Choose a CRF, a two-pass bitrate, or run a downstream smoke test.
            </Callout>
          )}
          {job.state === "WAITING_FOR_SCREENSHOT_SELECTION" && (
            <Callout to="screenshots" title="Choose your final screenshots">
              Review the best 15 and choose 1–15 comparison pairs.
            </Callout>
          )}
          {job.state === "WAITING_FOR_RELEASE_DETAILS" && (
            <Callout to="release" title="Add your release details">
              Enter the Chinese name, source, extra description, and tracker to
              generate release files, with optional screenshot uploads.
            </Callout>
          )}
          <section>
            <h2>Source details</h2>
            {video ? (
              <>
                <div className="metrics">
                  <div>
                    <small>VIDEO</small>
                    <strong>
                      {video.codec.toUpperCase()} · {video.bit_depth}-bit
                    </strong>
                  </div>
                  <div>
                    <small>RESOLUTION</small>
                    <strong>
                      {video.width} × {video.height}
                    </strong>
                  </div>
                  <div>
                    <small>FRAME RATE</small>
                    <strong>{video.fps}</strong>
                  </div>
                  <div>
                    <small>DURATION</small>
                    <strong>{clock(video.duration)}</strong>
                  </div>
                </div>
                <h3>HandBrake crop</h3>
                <div className="crop">
                  {Object.entries(job.analysis.crop ?? {}).map(([k, v]) => (
                    <div key={k}>
                      <small>{k}</small>
                      <strong>{v}px</strong>
                    </div>
                  ))}
                </div>
              </>
            ) : (
              <p className="muted">Source analysis is queued or running.</p>
            )}
          </section>
          {latestTask(job) && <TaskCard task={latestTask(job)!} />}
        </div>
      </div>
    </>
  );
}
function Callout({
  to,
  title,
  children,
}: {
  to: string;
  title: string;
  children: ReactNode;
}) {
  return (
    <div className="callout">
      <small>YOUR INPUT IS NEEDED</small>
      <h2>{title}</h2>
      <p>{children}</p>
      <Link className="button" to={to}>
        Review & continue →
      </Link>
    </div>
  );
}

function Tracks({ job }: { job: Job }) {
  const query = useQueryClient();
  const [audio, setAudio] = useState<number[]>(
    job.track_selection?.audio_track_ids ?? [],
  );
  const [subs, setSubs] = useState<number[]>(
    job.track_selection?.subtitle_track_ids ?? [],
  );
  const [names, setNames] = useState<Record<number, string>>({});
  const [flags, setFlags] = useState<
    Record<number, Partial<Record<TrackFlag, boolean>>>
  >({});
  const flagLabels: [TrackFlag, string][] = [
    ["default", "Default"],
    ["forced", "Forced"],
    ["hearing_impaired", "SDH / hearing impaired"],
    ["visual_impaired", "Audio description"],
    ["commentary", "Commentary"],
  ];
  const waiting = job.state === "WAITING_FOR_TRACK_SELECTION";
  const subtitles = job.tracks.filter(
    (t) => t.kind === "subtitles" && t.info.extractable,
  );
  const needsAnalysis =
    job.tracks.length > 0 &&
    (job.analysis.track_review_version !== 1 ||
      subtitles.some((t) => t.info.subtitle_detection?.schema_version !== 1));
  const inconclusive = subtitles.some(
    (t) => t.info.subtitle_detection?.status === "inconclusive",
  );
  const analyze = useMutation({
    mutationFn: () => api(`/jobs/${job.id}/tracks/analyze`, {}),
    onSuccess: () => query.invalidateQueries({ queryKey: ["job", job.id] }),
  });
  const enabled = waiting && !needsAnalysis && !analyze.isPending;
  const save = useMutation({
    mutationFn: () =>
      api(`/jobs/${job.id}/tracks/selection`, {
        audio_track_ids: audio,
        subtitle_track_ids: subs,
        track_flags: Object.fromEntries(
          Object.entries(flags).filter(([id]) =>
            [...audio, ...subs].includes(Number(id)),
          ),
        ),
        track_names: Object.fromEntries(
          Object.entries(names).filter(([id]) =>
            [...audio, ...subs].includes(Number(id)),
          ),
        ),
      }),
    onSuccess: () => query.invalidateQueries({ queryKey: ["job", job.id] }),
  });
  const invalidNames = [...audio, ...subs].some(
    (id) => names[id] !== undefined && !names[id].trim(),
  );
  const unresolvedFlags = job.tracks.some(
    (t) =>
      [...audio, ...subs].includes(t.track_id) &&
      flagLabels.some(
        ([key]) => (flags[t.track_id]?.[key] ?? t.info[key]) == null,
      ),
  );
  const toggle = (list: number[], n: number) =>
    list.includes(n) ? list.filter((x) => x !== n) : [...list, n];
  const activeAnalysis = job.tasks.find(
    (t) => t.type === "analyze" && ["QUEUED", "RUNNING"].includes(t.status),
  );
  return (
    <>
      <div className="section-heading">
        <div>
          <h2>Review descriptions and choose tracks</h2>
          <p>
            PGS subtitles are checked before selection for Chinese script,
            Cantonese, and SDH content. Audio keeps its original codec and
            selected subtitles use the saved crop. The agent describes locally
            sampled audio and suggests flags. Review or change the final MKV
            names and flags below.
          </p>
        </div>
        <Badge>HUMAN GATE 1</Badge>
      </div>
      {job.state === "ANALYZING_SOURCE" && (
        <p className="callout" role="status">
          {activeAnalysis
            ? activeAnalysis.progress_detail.phase ||
              "Analyzing the source and subtitle content…"
            : "Source analysis has stopped. Check the task on Overview and retry to continue."}{" "}
          Track selection opens when analysis finishes. Agent reviews appear
          below.
        </p>
      )}
      {waiting && (needsAnalysis || inconclusive) && (
        <div className="callout">
          <p>
            {needsAnalysis
              ? "This job needs the initial track review. Run analysis for audio descriptions, subtitle findings, and suggested flags before selection."
              : "Some findings are inconclusive. You can skip those tracks or retry analysis. You can override uncertain SDH explicitly below; uncertain language still needs another content check before muxing."}
          </p>
          <button
            disabled={analyze.isPending || save.isPending}
            onClick={() => analyze.mutate()}
          >
            {analyze.isPending ? "Queuing analysis…" : "Analyze tracks"}
          </button>
        </div>
      )}
      <ErrorBox error={analyze.error} />
      {["audio", "subtitles"].map((kind) => (
        <section key={kind}>
          <h2>{kind === "audio" ? "Audio tracks" : "PGS subtitles"}</h2>
          {job.tracks
            .filter((t) => t.kind === kind)
            .map((t) => {
              const allowed = t.info.extractable;
              const list = kind === "audio" ? audio : subs;
              const detection = t.info.subtitle_detection;
              const editable = enabled && allowed && !save.isPending;
              const effectiveFlags = {
                ...t.info,
                ...(waiting ? flags[t.track_id] : {}),
              };
              const suggested =
                kind === "subtitles" && t.info.base_name
                  ? [
                      t.info.base_name,
                      effectiveFlags.hearing_impaired && "SDH",
                      effectiveFlags.forced && "Forced",
                    ]
                      .filter(Boolean)
                      .join(" ")
                  : (t.info.suggested_name ?? t.info.mux_name ?? t.info.name);
              const name = waiting
                ? (names[t.track_id] ?? t.info.name_override ?? suggested)
                : (t.info.mux_name ?? t.info.name);
              return (
                <article
                  className={`track ${!allowed ? "disabled" : ""}`}
                  key={t.track_id}
                >
                  <input
                    id={`include-track-${t.track_id}`}
                    type="checkbox"
                    disabled={!editable}
                    checked={list.includes(t.track_id)}
                    onChange={() =>
                      kind === "audio"
                        ? setAudio(toggle(audio, t.track_id))
                        : setSubs(toggle(subs, t.track_id))
                    }
                  />
                  <div className="grow">
                    <h3>
                      <label htmlFor={`include-track-${t.track_id}`}>
                        Track {t.track_id} · {t.info.language} · {t.info.codec}
                      </label>
                    </h3>
                    <p>
                      {[
                        suggested,
                        t.info.channel_layout ??
                          (t.info.channels && `${t.info.channels} channels`),
                        t.info.bit_depth && `${t.info.bit_depth}-bit`,
                        t.info.sample_rate && `${t.info.sample_rate} Hz`,
                        t.info.bitrate && `${t.info.bitrate} bps`,
                      ]
                        .filter(Boolean)
                        .join(" · ") || "No additional track metadata"}
                    </p>
                    {t.info.track_review && (
                      <div className="subtitle-description">
                        <p>{t.info.track_review.description}</p>
                        <small>
                          Agent confidence: {t.info.track_review.confidence}
                        </small>
                      </div>
                    )}
                    {t.info.audio_analysis && (
                      <details className="subtitle-description">
                        <summary>
                          Local audio evidence ·{" "}
                          {t.info.audio_analysis.sampled_seconds ?? 0}s sampled
                        </summary>
                        <p>{t.info.audio_analysis.method}</p>
                        {t.info.audio_analysis.limitations.map((text) => (
                          <p key={text}>{text}</p>
                        ))}
                        {t.info.audio_analysis.samples.map((sample) => (
                          <div key={sample.id}>
                            <small>
                              Sample {sample.id} ·{" "}
                              {sample.start_seconds.toFixed(1)}s ·{" "}
                              {sample.duration_seconds.toFixed(1)}s duration
                              {sample.detected_language &&
                                ` · detected ${sample.detected_language} (${Math.round((sample.language_probability ?? 0) * 100)}%)`}
                            </small>
                            <p>
                              {sample.segments
                                ?.map((segment) => segment.text)
                                .join(" ") ||
                                "No speech transcript available for this sample."}
                            </p>
                          </div>
                        ))}
                      </details>
                    )}
                    {kind === "subtitles" && allowed && (
                      <div className="subtitle-description">
                        {detection ? (
                          <>
                            <p>
                              <strong>
                                {detection.status === "inconclusive"
                                  ? "Inconclusive content check"
                                  : "Content checked"}
                              </strong>
                              {" · "}SDH:{" "}
                              {detection.hearing_impaired == null
                                ? "unknown"
                                : detection.hearing_impaired
                                  ? "yes"
                                  : "no"}
                              {detection.language_confident === false &&
                                " · Language/script uncertain"}
                            </p>
                            <p>{detection.explanation}</p>
                            <small>
                              {detection.method} · {detection.sampled_cues}/
                              {detection.unique_cues} distinct cues inspected
                            </small>
                          </>
                        ) : (
                          <p>
                            Language and SDH content check pending; source
                            labels are unverified.
                          </p>
                        )}
                        {t.info.source_subtitle_metadata?.name && (
                          <small>
                            Original source label:{" "}
                            {t.info.source_subtitle_metadata.name}
                          </small>
                        )}
                      </div>
                    )}
                    {allowed ? (
                      <div className="track-name-field">
                        <label htmlFor={`track-name-${t.track_id}`}>
                          Final MKV track name
                        </label>
                        <input
                          id={`track-name-${t.track_id}`}
                          type="text"
                          maxLength={255}
                          value={name}
                          disabled={!editable}
                          onChange={(event) =>
                            setNames((current) => ({
                              ...current,
                              [t.track_id]: event.target.value,
                            }))
                          }
                        />
                        {editable && name !== suggested && (
                          <button
                            className="secondary"
                            onClick={() =>
                              setNames((current) => {
                                const next = { ...current };
                                delete next[t.track_id];
                                return next;
                              })
                            }
                          >
                            Use suggested name
                          </button>
                        )}
                        {kind === "subtitles" && (
                          <small>
                            The name is a label. Language and flag settings are
                            stored separately.
                          </small>
                        )}
                      </div>
                    ) : (
                      <small>
                        Unsupported codec for native extraction in this version
                      </small>
                    )}
                    {allowed && (
                      <fieldset className="track-flags" disabled={!editable}>
                        <legend>Final MKV flags</legend>
                        <div className="track-flag-grid">
                          {flagLabels.map(([key, label]) => {
                            const value = waiting
                              ? (flags[t.track_id]?.[key] ?? t.info[key])
                              : t.info[key];
                            return (
                              <label key={key}>
                                {label}
                                <select
                                  value={
                                    value == null ? "unknown" : String(value)
                                  }
                                  onChange={(event) =>
                                    setFlags((current) => ({
                                      ...current,
                                      [t.track_id]: {
                                        ...current[t.track_id],
                                        [key]: event.target.value === "true",
                                      },
                                    }))
                                  }
                                >
                                  <option value="unknown" disabled>
                                    Unknown — choose
                                  </option>
                                  <option value="true">Yes</option>
                                  <option value="false">No</option>
                                </select>
                              </label>
                            );
                          })}
                        </div>
                        {t.info.track_review && (
                          <small>{t.info.track_review.flag_explanation}</small>
                        )}
                      </fieldset>
                    )}
                  </div>
                  {effectiveFlags.default && <Badge>DEFAULT</Badge>}
                  {effectiveFlags.forced && <Badge>FORCED</Badge>}
                  {effectiveFlags.commentary && <Badge>COMMENTARY</Badge>}
                  {effectiveFlags.hearing_impaired &&
                    (kind === "audio" || detection) && <Badge>SDH</Badge>}
                </article>
              );
            })}
          {job.tracks.filter((t) => t.kind === kind).length === 0 && (
            <p className="muted">
              {job.state === "ANALYZING_SOURCE"
                ? "Waiting for analysis…"
                : "No tracks available."}
            </p>
          )}
        </section>
      ))}
      {unresolvedFlags && (
        <p className="error">
          Choose Yes or No for unknown flags on selected tracks.
        </p>
      )}
      {invalidNames && (
        <p className="error">Selected tracks need a nonempty name.</p>
      )}
      <button
        disabled={!enabled || save.isPending || invalidNames || unresolvedFlags}
        onClick={() => save.mutate()}
      >
        Confirm {audio.length} audio + {subs.length} subtitle tracks →
      </button>
      <ErrorBox error={save.error} />
      <AgentTranscript job={job} subtitles />
    </>
  );
}

function CRF({ job }: { job: Job }) {
  const [mode, setMode] = useState<RateControl>(
    job.encode_config?.data.rate_control ?? "crf",
  );
  const [crf, setCRF] = useState(job.encode_config?.data.crf?.toString() ?? "");
  const [bitrate, setBitrate] = useState(
    job.encode_config?.data.bitrate_kbps
      ? String(job.encode_config.data.bitrate_kbps / 1000)
      : "",
  );
  const query = useQueryClient();
  const confirm = useMutation({
    mutationFn: () =>
      api(`/jobs/${job.id}/encode-selection`, {
        codec: job.analysis_profile.startsWith("x264") ? "x264" : "x265",
        profile: job.analysis_profile,
        rate_control: mode,
        ...(mode === "crf"
          ? { crf: Number(crf) }
          : { bitrate_kbps: Math.round(Number(bitrate) * 1000) }),
      }),
    onSuccess: () => query.invalidateQueries({ queryKey: ["job", job.id] }),
  });
  const smoke = useMutation({
    mutationFn: () => api<Job>(`/jobs/${job.id}/smoke-test`, {}),
    onSuccess: (result) => {
      query.setQueryData(["job", job.id], result);
      query.invalidateQueries({ queryKey: ["jobs"] });
      query.invalidateQueries({ queryKey: ["queue"] });
    },
  });
  const task = latestTask({
    ...job,
    tasks: job.tasks.filter((t) => t.type === "crf_analysis"),
  });
  if (!job.crf && task)
    return (
      <>
        <div className="section-heading">
          <div>
            <h2>Calibrating your encoding target</h2>
            <p>
              CRF Studio measures CRF 13 and 20 using {job.analysis_profile}.
              The bitrate / QP curve will appear when analysis completes.
            </p>
          </div>
        </div>
        <TaskCard task={task} />
      </>
    );
  if (!job.crf)
    return (
      <Empty title="CRF analysis is not queued yet">
        Complete source analysis and track selection to start CRF analysis.
      </Empty>
    );
  const { samples, profile_snapshot } = job.crf.data;
  const crfMin = profile_snapshot?.crf_min ?? 0;
  const crfMax = profile_snapshot?.crf_max ?? 51;
  return (
    <>
      {task && <TaskCard task={task} />}
      <div className="section-heading">
        <div>
          <h2>Find your encoding target</h2>
          <p>
            Measured samples and predicted values for {job.analysis_profile}.
            Pin a bitrate to inspect its predicted QP and approximate CRF.
          </p>
        </div>
        <Badge>HUMAN GATE 2</Badge>
      </div>
      <BitrateCurve
        key={job.id}
        jobId={job.id}
        samples={samples}
        canChoose={job.state === "WAITING_FOR_ENCODE_SELECTION"}
        crfMin={crfMin}
        crfMax={crfMax}
        choose={(n) => {
          setMode("crf");
          setCRF(String(n));
        }}
        chooseBitrate={(kbps) => {
          setMode("bitrate");
          setBitrate(String(kbps / 1000));
        }}
      />
      <section>
        <h2>Final encode selection</h2>
        <form
          className="inline-form"
          onSubmit={(e) => {
            e.preventDefault();
            confirm.mutate();
          }}
        >
          <label>
            Profile
            <input value={job.analysis_profile} readOnly />
          </label>
          <label>
            Rate control
            <select
              aria-label="Rate control"
              value={mode}
              disabled={
                job.state !== "WAITING_FOR_ENCODE_SELECTION" ||
                confirm.isPending ||
                smoke.isPending
              }
              onChange={(e) => {
                setMode(e.target.value as RateControl);
                confirm.reset();
              }}
            >
              <option value="crf">Constant quality (CRF)</option>
              <option value="bitrate">Average bitrate (2-pass)</option>
            </select>
          </label>
          {mode === "crf" ? (
            <label>
              CRF
              <input
                required
                type="number"
                min={crfMin}
                max={crfMax}
                step="0.1"
                value={crf}
                disabled={job.state !== "WAITING_FOR_ENCODE_SELECTION"}
                onChange={(e) => setCRF(e.target.value)}
              />
            </label>
          ) : (
            <label>
              Target video bitrate (Mbps)
              <input
                required
                type="number"
                min="0.001"
                max="1000"
                step="0.001"
                placeholder="e.g. 8"
                value={bitrate}
                disabled={job.state !== "WAITING_FOR_ENCODE_SELECTION"}
                onChange={(e) => setBitrate(e.target.value)}
              />
            </label>
          )}
          <button
            disabled={
              (mode === "crf" ? crf : bitrate) === "" ||
              confirm.isPending ||
              smoke.isPending ||
              job.state !== "WAITING_FOR_ENCODE_SELECTION"
            }
          >
            Confirm & add to queue →
          </button>
        </form>
        <p className="muted">
          {mode === "bitrate"
            ? "Two full passes target the average video bitrate. Audio and container overhead add to the final file size."
            : "Constant quality adjusts the bitrate to maintain your chosen CRF."}
        </p>
        <ErrorBox error={confirm.error} />
        <p className="muted">
          Queued jobs start when a slot is available and continue when you close
          your browser. <Link to="/queue">Manage queue →</Link>
        </p>
      </section>
      {job.state === "WAITING_FOR_ENCODE_SELECTION" && (
        <section>
          <h2>Test the remaining steps</h2>
          <p>
            Reuse the source video to test remuxing, screenshot selection, and
            rendering. Encoding and encode validation are skipped. This job and
            its outputs will be marked SMOKE-TEST; use a separate job for a real
            encode.
          </p>
          <button
            type="button"
            className="secondary"
            disabled={confirm.isPending || smoke.isPending}
            onClick={() => smoke.mutate()}
          >
            {smoke.isPending
              ? "Queueing smoke test…"
              : "Run smoke test — skip encoding"}
          </button>
          <ErrorBox error={smoke.error} />
        </section>
      )}
      <section>
        <h2>Measured samples</h2>
        <table>
          <thead>
            <tr>
              <th>CRF</th>
              <th>Bitrate</th>
              <th>Average B-frame QP</th>
            </tr>
          </thead>
          <tbody>
            {samples.map((p) => (
              <tr key={p.crf}>
                <td>{p.crf}</td>
                <td>{(p.bitrate_kbps / 1000).toFixed(3)} Mbps</td>
                <td>{p.average_qp ?? "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
    </>
  );
}

function TaskCard({ task }: { task: Task }) {
  return <TaskAttempt key={task.id} task={task} />;
}

function TaskAttempt({ task }: { task: Task }) {
  const pause = encodingPauseState(task);
  const query = useQueryClient();
  const waitingForTool =
    task.status === "RUNNING" &&
    typeof task.progress_detail.tool === "string" &&
    typeof task.progress_detail.tool_percentage !== "number";
  const action = useMutation({
    mutationFn: (verb: string) => api(`/tasks/${task.id}/${verb}`, {}),
    // Refresh even after a stale retry request, and reset mutation errors when
    // a newer attempt replaces this keyed component.
    onSettled: () => query.invalidateQueries({ queryKey: ["job"] }),
  });
  return (
    <section className="task-card">
      <div className="section-heading">
        <h2>
          {task.type === "crf_analysis"
            ? "CRF analysis progress"
            : readable(task.type)}
        </h2>
        <Badge>{pause?.status ?? task.status}</Badge>
      </div>
      {pause && (task.pause_requested || task.paused_at) && (
        <p role="status" className="callout">
          {pause.status}. Your progress is retained. The encode keeps its queue
          slot and can be resumed or cancelled.
        </p>
      )}
      {task.type === "crf_analysis" ? (
        <CRFProgress task={task} />
      ) : (
        <>
          {typeof task.progress_detail.phase === "string" && (
            <p role="status">{task.progress_detail.phase}</p>
          )}
          <progress
            aria-label="Task progress"
            value={waitingForTool ? undefined : task.progress}
            max="100"
          />
          <div className="section-heading">
            <span>
              {waitingForTool
                ? "Waiting for tool progress…"
                : `${task.progress.toFixed(1)}%`}
            </span>
            <small>Attempt {task.attempt}</small>
          </div>
          {typeof task.progress_detail.tool_percentage === "number" && (
            <p className="muted">
              {String(task.progress_detail.tool)} ·{" "}
              {task.progress_detail.tool_percentage.toFixed(1)}%
            </p>
          )}
          {typeof task.progress_detail.pass_number === "number" && (
            <p className="muted">
              Pass {task.progress_detail.pass_number} of{" "}
              {task.progress_detail.pass_count}
              {typeof task.progress_detail.pass_percentage === "number" &&
                ` · ${task.progress_detail.pass_percentage.toFixed(1)}% of this pass`}
            </p>
          )}
          <div className="metrics">
            {Object.entries(task.progress_detail)
              .filter(
                ([k]) =>
                  ![
                    "pass_number",
                    "pass_count",
                    "pass_percentage",
                    "phase",
                    "tool",
                    "tool_percentage",
                  ].includes(k),
              )
              .map(([k, v]) => (
                <div key={k}>
                  <small>{readable(k)}</small>
                  <strong>
                    {typeof v === "number"
                      ? k.includes("seconds")
                        ? clock(v)
                        : v.toFixed(1)
                      : String(v ?? "—")}
                  </strong>
                </div>
              ))}
          </div>
        </>
      )}
      {task.error_message && <div className="error">{task.error_message}</div>}
      {["FAILED", "CANCELLED"].includes(task.status) && (
        <button
          disabled={action.isPending}
          onClick={() => action.mutate("retry")}
        >
          Retry this stage
        </button>
      )}
      <EncodingPauseButton task={task} />
      {["RUNNING", "QUEUED"].includes(task.status) && (
        <button
          className="secondary"
          disabled={action.isPending || task.cancel_requested}
          onClick={() => action.mutate("cancel")}
        >
          {task.cancel_requested ? "Cancelling…" : "Cancel task"}
        </button>
      )}
      <ErrorBox error={action.error} />
    </section>
  );
}

function EncodeStatus({ job }: { job: Job }) {
  const task = latestTask({
    ...job,
    tasks: job.tasks.filter((item) =>
      ["encode", "validate", "mux"].includes(item.type),
    ),
  });
  return (
    <>
      {task ? (
        <TaskCard task={task} />
      ) : (
        <Empty title="No running encode">
          Select tracks and review the CRF analysis to continue.
        </Empty>
      )}
      <EncodingConfiguration job={job} />
      {job.validation.valid !== undefined && (
        <section>
          <h2>
            Encode validation{" "}
            <Badge>
              {job.validation.skipped
                ? "SKIPPED"
                : job.validation.valid
                  ? "PASSED"
                  : "FAILED"}
            </Badge>
          </h2>
          {job.validation.errors?.map((e) => (
            <p className="error" key={e}>
              {e}
            </p>
          ))}
          {job.validation.warnings?.map((e) => (
            <p key={e}>{e}</p>
          ))}
          <pre>{JSON.stringify(job.validation.metrics, null, 2)}</pre>
        </section>
      )}
    </>
  );
}

function ScreenshotDecoderSettings({ job }: { job: Job }) {
  const current = job.screenshot_policy.decoder ?? "cuda";
  const [decoder, setDecoder] = useState(current);
  const query = useQueryClient();
  useEffect(() => setDecoder(current), [current, job.id]);
  const running = job.tasks.some(
    (task) => task.type === "generate_candidates" && task.status === "RUNNING",
  );
  const save = useMutation({
    mutationFn: () =>
      api<Job>(`/jobs/${job.id}/screenshots/decoder`, { decoder }, "PATCH"),
    onSuccess: (updated) => {
      query.setQueryData(["job", job.id], updated);
      query.invalidateQueries({ queryKey: ["job", job.id] });
    },
  });
  return (
    <section>
      <h2>Screenshot decoding</h2>
      <label>
        Screenshot scan decoder
        <select
          value={decoder}
          disabled={running || save.isPending}
          onChange={(e) => {
            setDecoder(e.target.value as "cpu" | "cuda");
            save.reset();
          }}
        >
          <option value="cpu">CPU</option>
          <option value="cuda">NVIDIA GPU (CPU fallback)</option>
        </select>
      </label>
      <p className="muted">
        Applies to the next candidate scan or retry. GPU initialization failures
        fall back to CPU and are recorded in the task log. Final screenshots use
        CPU decoding to preserve picture-type labels.
      </p>
      {running && (
        <p className="muted">
          The current scan is running. To change its decoder, cancel the task,
          save your choice, then retry the stage.
        </p>
      )}
      <button
        disabled={running || save.isPending || decoder === current}
        onClick={() => save.mutate()}
      >
        {save.isPending ? "Saving…" : "Save decoder"}
      </button>
      <ErrorBox error={save.error} />
    </section>
  );
}

function Gallery({ job }: { job: Job }) {
  const task = [...job.tasks]
    .filter((task) =>
      [
        "generate_candidates",
        "select_screenshots",
        "render_screenshots",
      ].includes(task.type),
    )
    .sort((a, b) => b.created_at.localeCompare(a.created_at))[0];
  return (
    <ScreenshotGallery
      key={job.id}
      job={job}
      controls={
        <>
          <ScreenshotDecoderSettings key={job.id} job={job} />
          {task && <TaskCard task={task} />}
          <AgentTranscript key={`agent-${job.id}`} job={job} />
        </>
      }
    />
  );
}

function JobTaskLog({ job }: { job: Job }) {
  const [selected, setSelected] = useState("");
  const selectedTask = job.tasks.find((task) => task.id === selected);
  const task = selectedTask ?? latestTask(job);
  return (
    <section className="task-log" aria-labelledby="task-log-heading">
      <div className="section-heading">
        <h2 id="task-log-heading">Task log</h2>
        {task && <Badge>{task.status}</Badge>}
      </div>
      <label>
        View task log
        <select
          aria-label="View task log"
          value={selectedTask?.id ?? ""}
          disabled={job.tasks.length === 0}
          onChange={(e) => setSelected(e.target.value)}
        >
          <option value="">Latest task (automatic)</option>
          {[...job.tasks]
            .sort((a, b) => b.created_at.localeCompare(a.created_at))
            .map((t) => (
              <option key={t.id} value={t.id}>
                {readable(t.type)} · attempt {t.attempt} · {readable(t.status)}
              </option>
            ))}
        </select>
      </label>
      {task ? (
        <>
          <small>
            {readable(task.type)} · attempt {task.attempt}
            {task.status === "RUNNING" && " · Live · refreshes every 3 seconds"}
          </small>
          <LogViewer task={task} />
        </>
      ) : (
        <p className="muted">
          No tasks yet. Logs will appear when processing begins.
        </p>
      )}
    </section>
  );
}

function LogViewer({ task }: { task: Task }) {
  const logs = useQuery({
    // Fetch the final tail when a task finishes, even after live polling stops.
    queryKey: ["log", task.id, task.status],
    queryFn: () => api<{ text: string }>(`/tasks/${task.id}/logs?tail=true`),
    refetchInterval: task.status === "RUNNING" ? 3000 : false,
  });
  return (
    <>
      <ErrorBox error={logs.error} />
      <pre className="logs" aria-label="Task log output">
        {logs.isPending
          ? "Loading task log…"
          : logs.data?.text || "No log output yet."}
      </pre>
    </>
  );
}

function Artifacts({ job }: { job: Job }) {
  return (
    <>
      <section>
        <h2>Generated artifacts</h2>
        {job.artifacts.length === 0 ? (
          <p className="muted">No artifacts yet.</p>
        ) : (
          <table>
            <thead>
              <tr>
                <th>Artifact</th>
                <th>Type</th>
                <th>Size</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {job.artifacts.map((a) => (
                <tr key={a.id}>
                  <td className="artifact-path">{a.path}</td>
                  <td>{readable(a.artifact_type)}</td>
                  <td>{size(a.size)}</td>
                  <td>
                    <a
                      href={`/api/artifacts/${a.id}`}
                      target="_blank"
                      rel="noreferrer"
                    >
                      Open ↗
                    </a>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </>
  );
}

createRoot(document.getElementById("root")!).render(
  <QueryClientProvider client={client}>
    <BrowserRouter>
      <App />
    </BrowserRouter>
  </QueryClientProvider>,
);
