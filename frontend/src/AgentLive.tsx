import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "./api";
import {
  applyLiveEvent,
  type Conversation,
  type History,
  type LiveEvent,
} from "./agent-live";

function responseText(text: string) {
  try {
    return JSON.stringify(JSON.parse(text), null, 2);
  } catch {
    return text;
  }
}

export function AgentLive() {
  const [runs, setRuns] = useState<Conversation[]>([]);
  const [connection, setConnection] = useState("Connecting");
  const [error, setError] = useState("");
  const [before, setBefore] = useState<number | null>(null);
  const [more, setMore] = useState(false);
  const [loading, setLoading] = useState(false);
  const [following, setFollowing] = useState(true);
  const [retry, setRetry] = useState(0);
  const viewport = useRef<HTMLDivElement>(null);
  const follow = useRef(true);
  const version = useRef(0);
  useEffect(() => {
    let disposed = false;
    let stream: EventSource | undefined;
    const generation = ++version.current;
    setError("");
    setConnection("Connecting");
    api<History>("/agent/history")
      .then((history) => {
        if (disposed) return;
        setRuns(history.events.reduce(applyLiveEvent, []));
        setBefore(history.before_id);
        setMore(history.has_more);
        let cursor = history.cursor;
        stream = new EventSource(`/api/agent/events?after=${cursor}`);
        stream.onopen = () => setConnection("Live");
        stream.onerror = () => setConnection("Reconnecting");
        stream.addEventListener("agent_activity", (raw) => {
          if (disposed || generation !== version.current) return;
          const event = JSON.parse((raw as MessageEvent).data) as LiveEvent;
          if (event.event_id <= cursor) return;
          cursor = event.event_id;
          setRuns((current) => applyLiveEvent(current, event));
        });
      })
      .catch((reason) => {
        if (!disposed) {
          setError(reason.message);
          setConnection("Disconnected");
        }
      });
    return () => {
      disposed = true;
      stream?.close();
    };
  }, [retry]);
  useEffect(() => {
    if (follow.current && viewport.current)
      viewport.current.scrollTop = viewport.current.scrollHeight;
  }, [runs]);

  async function older() {
    if (!before || loading) return;
    setLoading(true);
    setError("");
    follow.current = false;
    setFollowing(false);
    const generation = version.current;
    const height = viewport.current?.scrollHeight ?? 0;
    const top = viewport.current?.scrollTop ?? 0;
    try {
      const history = await api<History>(`/agent/history?before_id=${before}`);
      if (version.current !== generation) return;
      setRuns((current) => {
        const existing = new Set(current.map((run) => run.id));
        return [
          ...history.events
            .reduce(applyLiveEvent, [])
            .filter((run) => !existing.has(run.id)),
          ...current,
        ];
      });
      setBefore(history.before_id);
      setMore(history.has_more);
      requestAnimationFrame(() => {
        if (viewport.current)
          viewport.current.scrollTop =
            top + viewport.current.scrollHeight - height;
      });
    } catch (reason) {
      setError((reason as Error).message);
    } finally {
      setLoading(false);
    }
  }
  const active = [...runs].reverse().find((run) => run.state === "running");
  return (
    <div className="agent-live-page">
      <header>
        <div>
          <div className="eyebrow">SHARED AGENT WORKSPACE</div>
          <h1>Agent Live</h1>
          <p>Every prompt. Every response. One live conversation feed.</p>
          <Link to="/agent/prompts" className="text-button">
            Edit agent prompts →
          </Link>
        </div>
        <span
          className={`agent-live-connection ${connection === "Live" ? "connected" : ""}`}
        >
          <i />
          {connection}
        </span>
      </header>
      <section className="agent-live-window">
        <div className="agent-live-toolbar">
          <div className="agent-live-current">
            <span className="agent-live-avatar">✦</span>
            <div>
              <strong>{active?.stage ?? "Agent conversations"}</strong>
              <small>
                {active
                  ? `${active.title} · ${active.profile}`
                  : "Recent activity · waiting for the next response"}
              </small>
            </div>
          </div>
          <button
            className={following ? "secondary" : ""}
            aria-pressed={following}
            onClick={() => {
              follow.current = true;
              setFollowing(true);
              if (viewport.current)
                viewport.current.scrollTop = viewport.current.scrollHeight;
            }}
          >
            {following ? "Following live" : "↓ Follow live"}
          </button>
        </div>
        {error && (
          <div className="error" role="alert">
            {error}{" "}
            <button
              className="secondary"
              onClick={() => setRetry((value) => value + 1)}
            >
              Reconnect
            </button>
          </div>
        )}
        <div
          className="agent-live-feed"
          ref={viewport}
          tabIndex={0}
          aria-label="Agent conversation history"
          onScroll={() => {
            const el = viewport.current;
            if (!el) return;
            const bottom =
              el.scrollHeight - el.scrollTop - el.clientHeight < 70;
            follow.current = bottom;
            setFollowing(bottom);
          }}
        >
          {more && (
            <button
              className="secondary agent-live-older"
              disabled={loading}
              onClick={older}
            >
              {loading
                ? "Loading conversations…"
                : "Load earlier conversations"}
            </button>
          )}
          {!runs.length && (
            <div className="agent-live-empty">
              <span>✦</span>
              <h2>
                {connection === "Connecting"
                  ? "Opening the conversation…"
                  : "Ready when the agent is"}
              </h2>
              <p>
                Subtitle reviews, screenshot selection and track analysis will
                appear here automatically.
              </p>
            </div>
          )}
          {runs.map((run) => (
            <article className="agent-live-conversation" key={run.id}>
              <div className="agent-live-divider">
                <span>{run.stage}</span>
                <time dateTime={run.created_at}>
                  {new Date(run.created_at).toLocaleString()}
                </time>
              </div>
              <div className="agent-live-context">
                <Link to={`/jobs/${run.job_id}`}>{run.title}</Link>
                <span>{run.profile}</span>
                <span className={`agent-live-state ${run.state}`}>
                  {run.state === "running"
                    ? "Responding"
                    : run.state === "error"
                      ? "Needs attention"
                      : "Complete"}
                </span>
              </div>
              <div className="agent-live-prompt">
                <div className="agent-live-role">
                  PROMPT <span>Application → agent</span>
                  {run.prompt_key && (
                    <Link
                      to={`/agent/prompts?task=${encodeURIComponent(run.prompt_key)}`}
                    >
                      Edit instructions · revision {run.prompt_revision ?? 0}
                    </Link>
                  )}
                </div>
                <pre>
                  {run.prompt.length > 420
                    ? run.prompt.slice(0, 420) + "…"
                    : run.prompt || "Loading prompt…"}
                </pre>
                {run.prompt.length > 420 && (
                  <details>
                    <summary>
                      Read full prompt · {run.prompt.length.toLocaleString()}{" "}
                      characters
                    </summary>
                    <pre>{run.prompt}</pre>
                  </details>
                )}
                {!!run.images.length && (
                  <small>
                    {run.images.length} image attachments ·{" "}
                    {run.images.join(", ")}
                  </small>
                )}
              </div>
              <div className="agent-live-response">
                <div className="agent-live-role">
                  ✦ AGENT{" "}
                  <span>
                    {run.state === "running"
                      ? "Streaming response"
                      : "Response"}
                  </span>
                </div>
                {run.messages.map((message) => (
                  <pre key={message.id}>{responseText(message.text)}</pre>
                ))}
                {!run.messages.length && (
                  <p className="muted">
                    {run.state === "running"
                      ? "The agent is reviewing the prompt…"
                      : "No response text was recorded."}
                  </p>
                )}
                <div className={`agent-live-status ${run.state}`} role="status">
                  {run.state === "running" && <i />}
                  {run.status}
                </div>
              </div>
            </article>
          ))}
        </div>
        <div className="agent-live-footer">
          <span>Prompts and responses are saved automatically.</span>
          <span>{runs.length} conversations loaded</span>
        </div>
      </section>
    </div>
  );
}
