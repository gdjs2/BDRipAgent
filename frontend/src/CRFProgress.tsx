import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import type { Task } from "./types";

export function CRFProgress({ task }: { task: Task }) {
  const detail = task.progress_detail;
  const running = task.status === "RUNNING";
  const queued = task.status === "QUEUED";
  const succeeded = task.status === "SUCCEEDED";
  const number = (key: string) => {
    const value = detail[key];
    return typeof value === "number" && Number.isFinite(value) ? value : 0;
  };
  const [now, setNow] = useState(Date.now);
  useEffect(() => {
    if (!running) return;
    setNow(Date.now());
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [running, task.id]);
  const started = Date.parse(task.started_at ?? "");
  const finished = Date.parse(task.finished_at ?? "");
  const elapsed = Math.max(
    0,
    Math.floor(
      Number.isFinite(started) && (running || Number.isFinite(finished))
        ? ((running ? now : finished) - started) / 1000
        : number("elapsed_seconds"),
    ),
  );
  const elapsedLabel = [
    Math.floor(elapsed / 3600),
    Math.floor(elapsed / 60) % 60,
    elapsed % 60,
  ]
    .map((part) => String(part).padStart(2, "0"))
    .join(":");
  const total = number("total");
  const completed = number("completed");
  const indeterminate = running && !total;
  const encoding = running && detail.stage === "encoding" && completed < total;
  const percent = succeeded ? 100 : Math.min(99, Math.max(0, task.progress));
  const message = queued
    ? task.held
      ? "On hold in the queue"
      : "Waiting for a worker slot"
    : succeeded
      ? "Analysis complete"
      : task.status === "FAILED"
        ? "Analysis failed"
        : task.status === "CANCELLED"
          ? "Analysis cancelled"
          : task.cancel_requested
            ? "Stopping the active analysis…"
            : detail.state === "complete"
              ? "Saving analysis results…"
              : typeof detail.message === "string"
                ? detail.message
                : "Starting CRF Studio…";
  return (
    <div className="crf-progress">
      <div className="crf-progress-heading">
        <span role="status">{message}</span>
        <strong>
          {indeterminate
            ? "Preparing…"
            : queued
              ? "Queued"
              : `${percent.toFixed(0)}%`}
        </strong>
      </div>
      <progress
        aria-label="CRF analysis progress"
        value={indeterminate ? undefined : percent}
        max={100}
      />
      <div className="crf-progress-summary">
        <span>
          {total > 0
            ? `${completed} / ${total} encodes complete`
            : queued
              ? "Analysis will start automatically when released and a slot is available."
              : running
                ? "Preparing the sample plan and encoder settings."
                : ""}
        </span>
        <small>Attempt {task.attempt}</small>
      </div>
      {queued ? (
        <p>
          <Link to="/queue">Manage queue →</Link>
        </p>
      ) : (
        <div className="metrics crf-progress-metrics">
          {encoding && (
            <>
              <div>
                <small>Current encode</small>
                <strong>
                  {detail.codec} · CRF {detail.crf}
                </strong>
              </div>
              <div>
                <small>Sample</small>
                <strong>
                  {number("sample_index")} / {number("samples_per_endpoint")}
                </strong>
              </div>
            </>
          )}
          <div>
            <small>Elapsed time</small>
            <strong>{elapsedLabel}</strong>
          </div>
          {encoding && (
            <div>
              <small>Frames submitted</small>
              <strong>{number("frames").toLocaleString()}</strong>
            </div>
          )}
        </div>
      )}
      {encoding && (
        <div className="crf-sample-progress">
          <div className="crf-progress-summary">
            <span>
              {detail.flushing
                ? "Flushing encoder — finishing the current sample"
                : "Current sample"}
            </span>
            <span>{Math.floor(number("sample_fraction") * 100)}%</span>
          </div>
          <progress
            aria-label="Current sample progress"
            value={number("sample_fraction") * 100}
            max={100}
          />
        </div>
      )}
    </div>
  );
}
