import { AgentLive } from "./AgentLive";
import { AgentPrompts } from "./AgentPrompts";
import { EncoderSummary } from "./EncoderSummary";
import { JobStatus } from "./JobStatus";
import { CpuMonitor } from "./CpuMonitor";
import { taskProgress } from "./task-progress";
import { Tracks } from "./Tracks";
import { automaticLogTask, failedTasks, taskPage } from "./job-status";
import { Artifacts } from "./Artifacts";
import { useEffect, useRef, useState, type ReactNode } from "react";
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
  useLocation,
  useParams,
} from "react-router-dom";
import { EncodingConfiguration } from "./EncodingConfiguration";
import { EncodeTargetEditor } from "./EncodeTargetEditor";
import { editableEncodingTask } from "./encoding-configuration";
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
        <NavLink to="/agent" end>
          ✦ &nbsp; Agent Live
        </NavLink>
        <NavLink to="/agent/prompts">✎ &nbsp; Agent prompts</NavLink>
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
          <Route path="/agent" element={<AgentLive />} />
          <Route path="/agent/prompts" element={<AgentPrompts />} />
          <Route
            path="/jobs/:id/*"
            element={<JobPage config={config.data} />}
          />
        </Routes>
      </main>
      <CpuMonitor />
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
  const waiting = data.filter(
    (j) =>
      j.state.startsWith("WAITING") ||
      (j.tracks_editable &&
        j.track_analysis_complete &&
        (!j.track_selection || j.subtitle_discovery?.review_required)),
  );
  const failed = data.flatMap(failedTasks);
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
          <div
            className={`stat ${["Waiting for input", "Failed tasks"].includes(String(label)) && Number(count) > 0 ? "needs-input" : ""}`}
            key={label}
          >
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
              <JobStatus job={j} />
              {j.analysis.smoke_test && <Badge>SMOKE TEST</Badge>}
              {failedTasks(j).length > 0 && <Badge>NEEDS ATTENTION</Badge>}
              {!j.state.startsWith("WAITING") &&
                j.tracks_editable &&
                j.track_analysis_complete &&
                !j.track_selection && <Badge>TRACK SELECTION NEEDED</Badge>}
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
  // Prefer live-action profiles independently for each codec. Config keys are
  // alphabetically sorted by the API, which otherwise puts animation first.
  const profileNames = Object.keys(config.profiles).sort((a, b) => {
    const rank = (name: string) =>
      name.endsWith("-live")
        ? 0
        : config.profiles[name].tune === "animation"
          ? 2
          : 1;
    return rank(a) - rank(b) || a.localeCompare(b);
  });
  const firstProfile =
    profileNames.find((name) => config.profiles[name].codec === "x265") ??
    profileNames[0] ??
    "";
  const otherProfile =
    profileNames.find(
      (name) =>
        config.profiles[name].codec !== config.profiles[firstProfile]?.codec,
    ) ?? "";
  const [paired, setPaired] = useState(Boolean(otherProfile));
  const [audioRounds, setAudioRounds] = useState(
    config.audio_review?.max_rounds ?? 6,
  );
  const [findSubtitles, setFindSubtitles] = useState(false);
  const [originalLanguages, setOriginalLanguages] = useState("");
  const [imdbId, setImdbId] = useState("");
  const [movie, setMovie] = useState<IMDbMovie | null>(null);
  const [secondProfile, setSecondProfile] = useState(otherProfile);
  const [profile, setProfile] = useState(firstProfile),
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
    ? [config.profiles[profile]?.codec, config.profiles[secondProfile]?.codec]
    : [config.profiles[profile]?.codec];
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
        audio_review_max_rounds: audioRounds,
        subtitle_discovery: {
          enabled: findSubtitles,
          original_languages: originalLanguages.split(/[,\s]+/).filter(Boolean),
        },
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
                aria-label="Analysis & encode profile"
                value={profile}
                onChange={(e) => {
                  setProfile(e.target.value);
                  const codec = config.profiles[e.target.value].codec;
                  setSecondProfile((current) =>
                    config.profiles[current] &&
                    config.profiles[current].codec !== codec
                      ? current
                      : (profileNames.find(
                          (name) => config.profiles[name].codec !== codec,
                        ) ?? ""),
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
                disabled={!otherProfile}
                onChange={(e) => setPaired(e.target.checked)}
              />
              Create both x264 and x265 encodes
            </label>
            {paired && (
              <label>
                Second encode profile
                <select
                  aria-label="Second encode profile"
                  value={secondProfile}
                  onChange={(e) => setSecondProfile(e.target.value)}
                >
                  {Object.entries(config.profiles)
                    .filter(
                      ([, p]) => p.codec !== config.profiles[profile]?.codec,
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
              WiKi filenames use the movie title, year, codec and selected
              audio. The MKV title is Movie Name (Year). Each encode has seven
              comparison frames, spaced at least {policy.min_spacing_seconds}{" "}
              seconds from the other encode's frames.
            </p>
            <fieldset className="subtitle-discovery-options">
              <legend>Missing subtitles</legend>
              <label className="track">
                <input
                  type="checkbox"
                  checked={findSubtitles}
                  onChange={(e) => setFindSubtitles(e.target.checked)}
                />
                Find missing subtitles during analysis
              </label>
              <p className="muted">
                Search for missing original-language, English, Simplified
                Chinese, and Traditional Chinese subtitles. The agent aligns
                them against source subtitles, checks them, and prepares PGS
                tracks for your review.
              </p>
              <label>
                Original language codes (optional)
                <input
                  value={originalLanguages}
                  onChange={(e) => setOriginalLanguages(e.target.value)}
                  placeholder="e.g. ko or ja,en"
                />
                <small>
                  Used for original-language track flags and subtitle searches.
                  You can also confirm these codes in Track Selection.
                </small>
              </label>
            </fieldset>
            <label>
              Audio analysis maximum rounds
              <input
                type="number"
                required
                min="1"
                max="30"
                step="1"
                value={audioRounds}
                onChange={(e) => setAudioRounds(Number(e.target.value))}
              />
              <small>
                The agent stops earlier when differences are clear. At this
                limit, unresolved differences are summarized for your review.
              </small>
            </label>
            <label>
              Screenshot selection strategy
              <select
                aria-label="Screenshot selection strategy"
                value={policy.strategy ?? "local"}
                onChange={(e) =>
                  setPolicy({
                    ...policy,
                    strategy: e.target.value as "local" | "agent",
                  })
                }
              >
                <option value="local">
                  Local filtering · 200 scene samples (default)
                </option>
                <option value="agent">
                  Agent review · sample more until enough good choices
                </option>
              </select>
              <small>
                Local mode checks image quality and duplicates without calling
                the agent. Best starts empty; add frames from Shortlist
                yourself.
              </small>
            </label>
            {policy.strategy === "agent" && (
              <>
                <label>
                  Best screenshot candidates
                  <input
                    type="number"
                    required
                    min="2"
                    max="40"
                    step="1"
                    value={policy.best_count ?? 30}
                    onChange={(e) =>
                      setPolicy({
                        ...policy,
                        best_count: Number(e.target.value),
                      })
                    }
                  />
                  <small>
                    Review 30–40 choices, then pick your final pairs, such as 7
                    per codec. Agent mode defaults to 30 and samples additional
                    windows when needed.
                  </small>
                </label>
                <label>
                  Representative frames (remaining frames show encoding
                  challenges)
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
              </>
            )}
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
                !config.profiles[profile] ||
                (paired && !config.profiles[secondProfile]) ||
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
  const tabNav = useRef<HTMLElement>(null);
  const { pathname } = useLocation();
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
      "tracks_selected",
      "release_details_saved",
      "task_started",
      "task_pause_requested",
      "task_pause_available",
      "task_pause_unavailable",
      "task_paused",
      "task_resumed",
      "task_progress",
      "task_failed",
      "task_completed",
      "tracks_updated",
      "artifact_created",
      "screenshot_best_updated",
      "screenshot_decoder_selected",
    ].forEach((name) => stream.addEventListener(name, refresh));
    return () => {
      stream.close();
      window.clearTimeout(refreshTimer);
    };
  }, [id, query]);
  useEffect(() => {
    const nav = tabNav.current;
    const active = nav?.querySelector<HTMLElement>('[aria-current="page"]');
    if (nav && active)
      nav.scrollLeft +=
        active.getBoundingClientRect().left -
        nav.getBoundingClientRect().left -
        12;
  }, [pathname, job.data?.id]);
  if (job.error && (!job.data || !isConnectionError(job.error)))
    return <ErrorBox error={job.error} />;
  if (!job.data) return <div className="loading">Loading job…</div>;
  const j = job.data;
  const inputTabs = new Set(failedTasks(j).map((task) => taskPage(task.type)));
  const manualPage: Record<string, string> = {
    WAITING_FOR_TRACK_SELECTION: "tracks",
    WAITING_FOR_ENCODE_SELECTION: "crf",
    WAITING_FOR_SCREENSHOT_SELECTION: "screenshots",
    WAITING_FOR_RELEASE_DETAILS: "release",
  };
  if (manualPage[j.state]) inputTabs.add(manualPage[j.state]);
  const needsTracks =
    j.tracks_editable &&
    j.track_analysis_complete &&
    (!j.track_selection || j.subtitle_discovery?.review_required);
  if (needsTracks) inputTabs.add("tracks");
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
        <div className="job-status-badges">
          <Badge>{j.analysis_profile}</Badge>
          <JobStatus job={j} />
          {needsTracks && !j.state.startsWith("WAITING") && (
            <Badge>TRACK SELECTION NEEDED</Badge>
          )}
        </div>
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
      <nav className="tabs" ref={tabNav} aria-label="Job pages">
        {[
          ["", "Overview"],
          ["tracks", "Tracks"],
          ["crf", "CRF analysis"],
          ["encode", "Encode status"],
          ["screenshots", "Screenshots"],
          ["release", "Release"],
          ["artifacts", "Artifacts & logs"],
        ].map(([path, label]) => (
          <NavLink
            key={path}
            end={path === ""}
            to={`/jobs/${id}/${path}`}
            className={({ isActive }) =>
              `${isActive ? "active" : ""} ${inputTabs.has(path) ? "needs-input" : ""}`
            }
            title={inputTabs.has(path) ? "Your input is needed" : undefined}
          >
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
  const review = latestTask({
    ...job,
    tasks: job.tasks.filter((t) => t.type === "review_tracks"),
  });
  const stopped = review && ["FAILED", "CANCELLED"].includes(review.status);
  const active = job.tasks.filter((t) =>
    ["QUEUED", "RUNNING"].includes(t.status),
  );
  return (
    <div className="overview-layout">
      <div className="overview-main">
        {active.map((task) => (
          <TaskCard key={task.id} task={task} />
        ))}
        {failedTasks(job).map((task) => (
          <TaskCard key={task.id} task={task} />
        ))}
        {((job.tracks_editable && !job.track_selection) ||
          job.state === "WAITING_FOR_TRACK_SELECTION") && (
          <Callout
            to="tracks"
            title={
              stopped
                ? "Track review needs attention"
                : job.track_analysis_complete
                  ? "Choose the tracks to preserve"
                  : "Tracks are being reviewed"
            }
            label={
              stopped
                ? "REVIEW STOPPED"
                : job.track_analysis_complete
                  ? "YOUR INPUT IS NEEDED"
                  : "IN PROGRESS"
            }
          >
            {stopped
              ? "Track review stopped. Open Tracks to inspect the error and retry; video processing can continue."
              : job.track_analysis_complete
                ? "Audio comparisons and subtitle descriptions are ready. Confirm your tracks before remuxing."
                : "Audio and subtitle review runs alongside CRF analysis and encoding. Descriptions appear as they finish."}
          </Callout>
        )}
        {job.state === "WAITING_FOR_ENCODE_SELECTION" && (
          <Callout to="crf" title="Your encode decision is ready">
            Choose a CRF or two-pass bitrate to start encoding.
          </Callout>
        )}
        {job.state === "WAITING_FOR_SCREENSHOT_SELECTION" && (
          <Callout to="screenshots" title="Choose your final screenshots">
            Review the recommendations and choose 1–15 comparison pairs.
          </Callout>
        )}
        {job.state === "WAITING_FOR_RELEASE_DETAILS" && (
          <Callout to="release" title="Add your release details">
            Confirm your release details to generate the local files, with
            optional screenshot uploads.
          </Callout>
        )}
        {job.state !== "COMPLETE" &&
          job.state !== "WAITING_FOR_RELEASE_DETAILS" && (
            <Callout
              to="release"
              title={
                job.analysis.release_details
                  ? "Release draft saved"
                  : "Prepare release details anytime"
              }
              label="AVAILABLE ANYTIME"
            >
              Save or edit release details while video processing runs.
            </Callout>
          )}
        {job.state === "COMPLETE" && (
          <Callout to="release" title="Your release is ready" label="COMPLETE">
            Preview the generated text and download the torrent from Release.
          </Callout>
        )}
      </div>
      <div>
        <section className="workflow-panel">
          <h2>Workflow</h2>
          <div className="workflow-status">
            <span>Video pipeline</span>
            <Badge>{job.state}</Badge>
          </div>
          <div className="workflow-status">
            <span>Track selection</span>
            <Badge>
              {job.track_selection
                ? "CONFIRMED"
                : job.track_analysis_complete
                  ? "READY TO SELECT"
                  : stopped
                    ? "STOPPED"
                    : (review?.status ?? "PENDING")}
            </Badge>
          </div>
          <div className="workflow-status">
            <span>Release details</span>
            <Badge>
              {job.analysis.release_details ? "SAVED" : "NOT FILLED"}
            </Badge>
          </div>
          <details className="workflow-details">
            <summary>All workflow steps</summary>
            <div className="timeline">
              {stages
                .filter((s) => s !== "NEW")
                .map((s) => (
                  <div
                    key={s}
                    className={
                      s === job.state
                        ? `current ${s.startsWith("WAITING_FOR_") ? "needs-input" : ""}`
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
          </details>
        </section>
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
                  <small>VIDEO BITRATE</small>
                  <strong>
                    {video.bit_rate
                      ? `${(video.bit_rate / 1000000).toFixed(2)} Mbps`
                      : "Not reported"}
                  </strong>
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
      </div>
    </div>
  );
}
function Callout({
  to,
  title,
  children,
  label = "YOUR INPUT IS NEEDED",
}: {
  to: string;
  title: string;
  children: ReactNode;
  label?: string;
}) {
  return (
    <div
      className={`callout ${["YOUR INPUT IS NEEDED", "REVIEW STOPPED"].includes(label) ? "needs-input" : ""}`}
    >
      <small>{label}</small>
      <h2>{title}</h2>
      <p>{children}</p>
      <Link className="button" to={to}>
        Review & continue →
      </Link>
    </div>
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
        CRF analysis starts after the source scan. Track review runs alongside
        it.
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
        <Badge>{job.encode_config ? "TARGET SAVED" : "CHOOSE A TARGET"}</Badge>
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
      <section
        className={
          job.state === "WAITING_FOR_ENCODE_SELECTION" ? "needs-input" : ""
        }
      >
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
          {editableEncodingTask(job) && (
            <>
              <Link to={`/jobs/${job.id}/encode`}>
                Change the saved encoding target →
              </Link>{" "}
            </>
          )}
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
                <td>{p.average_qp?.toFixed(2) ?? "—"}</td>
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
  const progress = taskProgress(task);
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
        <p
          role="status"
          className={`callout ${pause.status === "Paused" ? "needs-input" : ""}`}
        >
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
            value={progress.indeterminate ? undefined : progress.percent}
            max="100"
          />
          <div className="section-heading">
            <span>{progress.label}</span>
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
                    "indeterminate",
                    "progress_basis",
                    "phase_percentage",
                  ].includes(k),
              )
              .map(([k, v]) => (
                <div key={k}>
                  <small>{readable(k)}</small>
                  <strong>
                    {typeof v === "number"
                      ? k.includes("seconds")
                        ? clock(v)
                        : k.endsWith("bytes")
                          ? `${(v / 1024 / 1024).toFixed(1)} MiB`
                          : Number.isInteger(v)
                            ? v.toLocaleString()
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
          className="needs-input"
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
      ["encode", "validate", "prepare_tracks", "mux"].includes(item.type),
    ),
  });
  return (
    <>
      <EncodeTargetEditor key={job.id} job={job} />
      {task ? (
        <TaskCard task={task} />
      ) : (
        <Empty title="No running encode">
          Choose an encoding target in CRF analysis. You can select tracks while
          the video encodes.
        </Empty>
      )}
      <EncoderSummary job={job} />
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
          {task && task.status !== "SUCCEEDED" && <TaskCard task={task} />}
          <details className="screenshot-settings">
            <summary>Screenshot settings and agent transcript</summary>
            <ScreenshotDecoderSettings key={job.id} job={job} />
            <AgentTranscript key={`agent-${job.id}`} job={job} />
          </details>
        </>
      }
    />
  );
}

function JobTaskLog({ job }: { job: Job }) {
  const [expanded, setExpanded] = useState(false);
  const [selected, setSelected] = useState("");
  const selectedTask = job.tasks.find((task) => task.id === selected);
  const { pathname } = useLocation();
  const task =
    selectedTask ??
    automaticLogTask(job, pathname.split("/").filter(Boolean)[2] ?? "");
  return (
    <section className="task-log" aria-labelledby="task-log-heading">
      <details
        className="log-fold"
        open={expanded}
        onToggle={(event) => setExpanded(event.currentTarget.open)}
      >
        <summary id="task-log-heading">
          Task log {task && <Badge>{task.status}</Badge>}
        </summary>
        {expanded && (
          <>
            <label>
              View task log
              <select
                aria-label="View task log"
                value={selectedTask?.id ?? ""}
                disabled={job.tasks.length === 0}
                onChange={(e) => setSelected(e.target.value)}
              >
                <option value="">This page’s task (automatic)</option>
                {[...job.tasks]
                  .sort((a, b) => b.created_at.localeCompare(a.created_at))
                  .map((t) => (
                    <option key={t.id} value={t.id}>
                      {readable(t.type)} · attempt {t.attempt} ·{" "}
                      {readable(t.status)}
                    </option>
                  ))}
              </select>
            </label>
            {task ? (
              <>
                <small>
                  {readable(task.type)} · attempt {task.attempt}
                  {task.status === "RUNNING" &&
                    " · Live · refreshes every 3 seconds"}
                </small>
                <LogViewer task={task} />
              </>
            ) : (
              <p className="muted">
                No tasks yet. Logs will appear when processing begins.
              </p>
            )}
          </>
        )}
      </details>
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

createRoot(document.getElementById("root")!).render(
  <QueryClientProvider client={client}>
    <BrowserRouter>
      <App />
    </BrowserRouter>
  </QueryClientProvider>,
);
