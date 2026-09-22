import { useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, clock } from "./api";
import type { Artifact, Job } from "./types";

type MediaTrack = {
  index: number;
  kind: "video" | "audio" | "subtitle";
  codec: string;
  name: string;
  language: string;
  default: boolean;
  supported: boolean;
  width?: number;
  height?: number;
  channels?: number;
};
type MediaInfo = {
  filename: string;
  version: string;
  duration: number;
  tracks: MediaTrack[];
  text: string;
  truncated: boolean;
  chapters: { start: number; title: string }[];
};

export function FinalMedia({ job }: { job: Job }) {
  const movies = [...job.artifacts]
    .reverse()
    .filter(
      (file) =>
        ["RELEASE_MEDIA", "FINAL_MKV", "SMOKE_TEST_MKV"].includes(
          file.artifact_type,
        ) && !file.info?.backup,
    );
  const [selected, setSelected] = useState("");
  const current =
    movies.find((file) => file.id === selected) ??
    movies.find((file) => file.artifact_type === "RELEASE_MEDIA") ??
    movies[0];
  if (!current) return null;
  return (
    <section className="final-media">
      <div className="section-heading">
        <h2>Final video</h2>
        <span className="muted">Preview & media information</span>
      </div>
      {movies.length > 1 && (
        <label>
          Video file
          <select
            value={current.id}
            onChange={(e) => setSelected(e.target.value)}
          >
            {movies.map((file) => (
              <option key={file.id} value={file.id}>
                {file.path.split("/").pop()} ·{" "}
                {file.artifact_type === "RELEASE_MEDIA"
                  ? "Release copy"
                  : "Remuxed video"}
              </option>
            ))}
          </select>
        </label>
      )}
      <MediaDetails key={current.id} file={current} />
    </section>
  );
}

function MediaDetails({ file }: { file: Artifact }) {
  const [playing, setPlaying] = useState(false);
  const [showInfo, setShowInfo] = useState(false);
  const info = useQuery({
    queryKey: ["media-info", file.id, file.size],
    queryFn: () => api<MediaInfo>(`/artifacts/${file.id}/media-info`),
    enabled: playing || showInfo,
    staleTime: 300000,
    retry: false,
  });
  return (
    <>
      <p className="artifact-path">{file.path.split("/").pop()}</p>
      <p className="muted">
        Compatibility preview, up to 720p with stereo audio. Selected subtitles
        are rendered into the preview. The downloadable MKV retains its original
        quality and tracks.
      </p>
      <div className="screenshot-actions">
        <button type="button" onClick={() => setPlaying((value) => !value)}>
          {playing ? "Close video preview" : "Open video preview"}
        </button>
        <a className="button secondary" href={`/api/artifacts/${file.id}`}>
          Download MKV
        </a>
      </div>
      {(playing || showInfo) && info.isPending && (
        <p role="status">Reading video tracks and MediaInfo…</p>
      )}
      {info.error && (
        <p className="error" role="alert">
          {info.error.message}{" "}
          <button className="secondary" onClick={() => info.refetch()}>
            Retry media information
          </button>
        </p>
      )}
      {playing && info.data && <Player file={file} info={info.data} />}
      <details
        className="media-info-text"
        open={showInfo}
        onToggle={(event) => setShowInfo(event.currentTarget.open)}
      >
        <summary>MediaInfo · text report</summary>
        {info.data && (
          <>
            <pre tabIndex={0} aria-label="MediaInfo text report">
              {info.data.text}
            </pre>
            {info.data.truncated && (
              <p className="muted">Report limited to 1 MiB.</p>
            )}
          </>
        )}
      </details>
    </>
  );
}

function Player({ file, info }: { file: Artifact; info: MediaInfo }) {
  const initial = (kind: string) =>
    info.tracks.find((t) => t.kind === kind && t.default)?.index ??
    info.tracks.find((t) => t.kind === kind)?.index ??
    -1;
  const [video, setVideo] = useState(initial("video"));
  const [audio, setAudio] = useState(initial("audio"));
  const [subtitle, setSubtitle] = useState(-1);
  const [retry, setRetry] = useState(0);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const ref = useRef<HTMLVideoElement>(null);
  const position = useRef(0);
  const resume = useRef(false);
  const speed = useRef(1);
  useEffect(() => {
    const element = ref.current!;
    let disposed = false;
    let hls: import("hls.js").default | undefined;
    const query = new URLSearchParams({
      video: String(video),
      audio: String(audio),
      subtitle: String(subtitle),
      v: info.version,
    });
    const url = `/api/artifacts/${file.id}/playback/index.m3u8?${query}`;
    setError("");
    setLoading(true);
    const loaded = () => {
      if (disposed) return;
      if (position.current) element.currentTime = position.current;
      element.playbackRate = speed.current;
      if (resume.current) void element.play().catch(() => {});
      setLoading(false);
    };
    const play = () => hls?.startLoad(element.currentTime);
    const pause = () => hls?.stopLoad();
    const seek = () => hls?.startLoad(element.currentTime);
    element.addEventListener("loadedmetadata", loaded);
    element.addEventListener("play", play);
    element.addEventListener("pause", pause);
    element.addEventListener("seeking", seek);
    void import("hls.js")
      .then(({ default: Hls }) => {
        if (disposed) return;
        if (Hls.isSupported()) {
          hls = new Hls({
            maxBufferLength: 12,
            maxMaxBufferLength: 18,
            backBufferLength: 12,
            maxBufferSize: 8 * 1024 * 1024,
            startPosition: position.current,
          });
          hls.on(Hls.Events.ERROR, (_, data) => {
            if (data.fatal && !disposed) {
              setError(
                "Video preview could not load. Retry playback or choose another track.",
              );
              setLoading(false);
            }
          });
          hls.loadSource(url);
          hls.attachMedia(element);
        } else if (element.canPlayType("application/vnd.apple.mpegurl")) {
          element.src = url;
        } else {
          setError(
            "This browser does not support the streaming preview. Use a current browser or download the MKV.",
          );
          setLoading(false);
        }
      })
      .catch(() => {
        if (!disposed) {
          setError("Could not load the video player. Retry playback.");
          setLoading(false);
        }
      });
    return () => {
      if (element.readyState > 0) position.current = element.currentTime;
      resume.current = !element.paused;
      disposed = true;
      element.removeEventListener("loadedmetadata", loaded);
      element.removeEventListener("play", play);
      element.removeEventListener("pause", pause);
      element.removeEventListener("seeking", seek);
      hls?.destroy();
      element.removeAttribute("src");
      element.load();
    };
  }, [file.id, info.version, video, audio, subtitle, retry]);
  return (
    <div className="video-preview">
      <video
        ref={ref}
        controls
        playsInline
        preload="metadata"
        aria-label="Final video preview"
        onWaiting={() => setLoading(true)}
        onPlaying={() => setLoading(false)}
        onCanPlay={() => setLoading(false)}
      />
      {loading && <p role="status">Preparing preview…</p>}
      {error && (
        <p role="alert" className="error">
          {error}{" "}
          <button
            type="button"
            className="secondary"
            onClick={() => setRetry((value) => value + 1)}
          >
            Retry playback
          </button>
        </p>
      )}
      <div className="media-track-controls">
        {(
          [
            ["video", video, setVideo],
            ["audio", audio, setAudio],
            ["subtitle", subtitle, setSubtitle],
          ] as const
        ).map(([kind, selected, update]) => (
          <label key={kind}>
            {kind === "subtitle"
              ? "Subtitles"
              : kind === "audio"
                ? "Audio track"
                : "Video track"}
            <select
              aria-label={
                kind === "subtitle"
                  ? "Subtitles"
                  : kind === "audio"
                    ? "Audio track"
                    : "Video track"
              }
              value={selected}
              onChange={(e) => update(Number(e.target.value))}
            >
              {kind !== "video" && (
                <option value={-1}>
                  {kind === "subtitle" ? "Off" : "No audio"}
                </option>
              )}
              {info.tracks
                .filter((t) => t.kind === kind)
                .map((t) => (
                  <option key={t.index} value={t.index} disabled={!t.supported}>
                    #{t.index} · {t.name}
                    {t.width ? ` · ${t.width}×${t.height}` : ""}
                    {t.channels ? ` · ${t.channels} ch` : ""}
                    {!t.supported ? " (preview unavailable)" : ""}
                  </option>
                ))}
            </select>
          </label>
        ))}
        <label>
          Playback speed
          <select
            aria-label="Playback speed"
            defaultValue="1"
            onChange={(e) => {
              speed.current = Number(e.target.value);
              if (ref.current) ref.current.playbackRate = speed.current;
            }}
          >
            {[0.5, 0.75, 1, 1.25, 1.5, 2].map((speed) => (
              <option key={speed} value={speed}>
                {speed}×
              </option>
            ))}
          </select>
        </label>
        {info.chapters.length > 0 && (
          <label>
            Chapter
            <select
              defaultValue=""
              onChange={(e) => {
                if (ref.current && e.target.value)
                  ref.current.currentTime = Number(e.target.value);
              }}
            >
              <option value="">Jump to chapter…</option>
              {info.chapters.map((chapter, index) => (
                <option key={index} value={chapter.start}>
                  {clock(chapter.start)} · {chapter.title}
                </option>
              ))}
            </select>
          </label>
        )}
      </div>
    </div>
  );
}
