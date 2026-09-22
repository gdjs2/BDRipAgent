import type { Task } from "./types";

/** Measured work and unknown-duration activity share the same display policy. */
export function taskProgress(task: Task) {
  const detail = task.progress_detail;
  const running = task.status === "RUNNING";
  const queued = task.status === "QUEUED";
  const paused = Boolean(task.paused_at);
  // Tasks started by older workers can have synthetic 99% reports stored in DB.
  const legacyUnknown =
    !detail.progress_basis &&
    task.progress >= 99 &&
    ["analyze", "review_tracks", "prepare_tracks", "generate_release"].includes(
      task.type,
    ) &&
    typeof detail.tool_percentage !== "number";
  const indeterminate =
    running &&
    !paused &&
    (detail.indeterminate === true ||
      legacyUnknown ||
      (typeof detail.tool === "string" &&
        typeof detail.tool_percentage !== "number"));
  const percent =
    task.status === "SUCCEEDED"
      ? 100
      : Math.min(
          99.9,
          Math.max(0, Number.isFinite(task.progress) ? task.progress : 0),
        );
  return {
    indeterminate,
    percent,
    label: queued
      ? task.held
        ? "On hold"
        : "Queued"
      : indeterminate
        ? "Working…"
        : `${percent.toFixed(1)}%`,
  };
}
