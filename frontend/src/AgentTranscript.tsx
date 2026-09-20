import { useEffect, useState } from "react";
import { applyAgentEvent, type AgentInvocation } from "./agent-transcript";
import type { Job } from "./types";

export function AgentTranscript({
  job,
  subtitles = false,
}: {
  job: Job;
  subtitles?: boolean;
}) {
  const tasks = job.tasks
    .filter((task) =>
      subtitles
        ? ["analyze", "prepare_tracks", "mux"].includes(task.type)
        : task.type === "select_screenshots",
    )
    .sort((a, b) => b.created_at.localeCompare(a.created_at));
  const [selected, setSelected] = useState("");
  const task = tasks.find((item) => item.id === selected) ?? tasks[0];
  const [runs, setRuns] = useState<AgentInvocation[]>([]);
  const [connection, setConnection] = useState("");
  useEffect(() => {
    setRuns([]);
    setConnection("");
    if (!task) return;
    const stream = new EventSource(`/api/tasks/${task.id}/agent-events`);
    let cursor = 0;
    stream.onopen = () => setConnection("");
    stream.onerror = () =>
      setConnection("Reconnecting to the agent transcript…");
    stream.addEventListener("agent_output", (raw) => {
      const event = raw as MessageEvent;
      const id = Number(event.lastEventId);
      if (id <= cursor) return;
      cursor = id;
      const data = JSON.parse(event.data);
      if (data.type === "error" && !data.invocation_id)
        setConnection(data.text);
      setRuns((current) => applyAgentEvent(current, data));
    });
    stream.addEventListener("terminal", () => stream.close());
    return () => stream.close();
  }, [task?.id]);
  if (!task) return null;
  return (
    <section className="agent-transcript">
      <h2>{subtitles ? "Track review agent" : "Screenshot agent"}</h2>
      <label>
        {subtitles ? "Processing attempt" : "Selection attempt"}
        <select
          value={task.id}
          onChange={(event) => setSelected(event.target.value)}
        >
          {tasks.map((item) => (
            <option key={item.id} value={item.id}>
              {subtitles
                ? `${item.type === "analyze" ? "Analysis" : item.type === "mux" ? "Mux" : "Preparation"} · `
                : ""}
              Attempt {item.attempt} ·{" "}
              {new Date(item.created_at).toLocaleString()} · {item.status}
            </option>
          ))}
        </select>
      </label>
      {connection && <p role="status">{connection}</p>}
      {!runs.length && (
        <p className="muted">
          {["RUNNING", "QUEUED"].includes(task.status)
            ? "Waiting for the agent prompt…"
            : subtitles
              ? "No agent review was recorded. Clear content decisions use program checks."
              : "No transcript was recorded for this attempt."}
        </p>
      )}
      {runs.map((run, index) => (
        <article key={run.id}>
          <h3>
            {subtitles
              ? run.stage
              : run.stage === "review"
                ? "Review"
                : "Shortlist"}{" "}
            · Request {index + 1}
          </h3>
          <p className="muted" role="status">
            {run.status}
          </p>
          <details>
            <summary>Prompt · {run.images.length} attached images</summary>
            <pre>{run.prompt}</pre>
            <p className="muted">{run.images.join(", ")}</p>
          </details>
          <h4>Agent response</h4>
          {run.messages.length ? (
            run.messages.map((message) => (
              <pre key={message.id}>{message.text}</pre>
            ))
          ) : (
            <p className="muted">Waiting for response…</p>
          )}
        </article>
      ))}
    </section>
  );
}
