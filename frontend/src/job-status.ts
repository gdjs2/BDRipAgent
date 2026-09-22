import type { Job, Task } from "./types";

export function latestAttempts(tasks: Task[]): Task[] {
  const byType = new Map<string, Task>();
  for (const task of [...tasks].sort((a, b) =>
    b.created_at.localeCompare(a.created_at),
  )) {
    if (!byType.has(task.type)) byType.set(task.type, task);
  }
  return [...byType.values()];
}

export function failedTasks(job: Job): Task[] {
  return latestAttempts(job.tasks).filter((task) => task.status === "FAILED");
}

export function automaticLogTask(job: Job, tab: string): Task | undefined {
  const types: Record<string, string[]> = {
    tracks: ["review_tracks", "analyze"],
    crf: ["crf_analysis"],
    encode: ["encode", "validate", "prepare_tracks", "mux"],
    screenshots: [
      "generate_candidates",
      "select_screenshots",
      "render_screenshots",
    ],
    release: ["generate_release"],
  };
  const sorted = [...job.tasks].sort((a, b) =>
    b.created_at.localeCompare(a.created_at),
  );
  const relevant = sorted.filter((task) => types[tab]?.includes(task.type));
  return (
    relevant[0] ?? sorted.find((task) => task.status === "RUNNING") ?? sorted[0]
  );
}

export function taskPage(type: string): string {
  if (
    [
      "analyze",
      "review_tracks",
      "discover_subtitles",
      "review_uploaded_subtitle",
    ].includes(type)
  )
    return "tracks";
  if (type === "crf_analysis") return "crf";
  if (
    [
      "generate_candidates",
      "select_screenshots",
      "render_screenshots",
    ].includes(type)
  )
    return "screenshots";
  if (type === "generate_release") return "release";
  return "encode";
}

const stageTasks: Record<string, [string, string]> = {
  ANALYZING_SOURCE: ["analyze", "Source analysis"],
  ANALYZING_TRACKS: ["review_tracks", "Track analysis"],
  RUNNING_CRF_ANALYSIS: ["crf_analysis", "CRF analysis"],
  ENCODING: ["encode", "Encoding"],
  VALIDATING_ENCODE: ["validate", "Encode validation"],
  PREPARING_TRACKS: ["prepare_tracks", "Track preparation"],
  REMUXING: ["mux", "Remuxing"],
  SCREENSHOT_CANDIDATE_GENERATION: [
    "generate_candidates",
    "Screenshot sampling",
  ],
  SCREENSHOT_AGENT_SELECTION: ["select_screenshots", "Screenshot review"],
  SCREENSHOT_RENDERING: ["render_screenshots", "Screenshot rendering"],
  GENERATING_RELEASE: ["generate_release", "Release generation"],
};

type JobStatus = {
  label: string;
  tone: "queued" | "running" | "needs-input" | "succeeded" | "neutral";
  detail?: string;
};

/** A pipeline stage describes the next operation, not whether a worker has started it. */
export function jobStatus(job: Job, queuePaused = false): JobStatus {
  const latest = latestAttempts(job.tasks);
  const completed = (type: string) =>
    latest.some((t) => t.type === type && t.status === "SUCCEEDED");
  if (job.state.startsWith("WAITING")) {
    return {
      label: job.state.toLowerCase().replaceAll("_", " "),
      tone: "needs-input",
      detail:
        job.state === "WAITING_FOR_ENCODE_SELECTION" &&
        completed("crf_analysis")
          ? "CRF analysis complete · Choose encode settings to continue"
          : undefined,
    };
  }
  if (job.state === "COMPLETE") return { label: "Complete", tone: "succeeded" };
  const stage = stageTasks[job.state];
  if (!stage)
    return {
      label: job.state.toLowerCase().replaceAll("_", " "),
      tone: "neutral",
    };
  const [type, name] = stage;
  // Track review runs independently; it must not hide a queued pipeline task.
  const task = latest.find((t) => t.type === type);
  if (!task)
    return {
      label: `${name} pending`,
      tone: "neutral",
      detail: "Waiting for task status",
    };
  if (task.status === "QUEUED") {
    if (task.held)
      return {
        label: `${name} on hold`,
        tone: "needs-input",
        detail: "Release this task in the queue to continue",
      };
    if (queuePaused)
      return {
        label: `${name} queued`,
        tone: "needs-input",
        detail: "Queue paused · Resume the queue to start waiting tasks",
      };
    if (type === "generate_release")
      return {
        label: `${name} queued`,
        tone: "queued",
        detail: "Waiting for a release-generation slot · One release at a time",
      };
    const pool =
      type === "encode"
        ? "an encoding"
        : type === "crf_analysis"
          ? "a CRF analysis"
          : "an other-task";
    return {
      label: `${name} queued`,
      tone: "queued",
      detail: `${type === "encode" && completed("crf_analysis") ? "CRF analysis complete · " : ""}Waiting for ${pool} slot`,
    };
  }
  if (task.status === "RUNNING") {
    if (task.cancel_requested)
      return {
        label: `${name} stopping`,
        tone: "running",
        detail: "Cancellation requested",
      };
    if (task.paused_at)
      return {
        label: `${name} ${task.pause_requested ? "paused" : "resuming"}`,
        tone: task.pause_requested ? "needs-input" : "running",
      };
    if (task.pause_requested)
      return { label: `${name} pausing`, tone: "running" };
    if (type === "crf_analysis" && task.progress_detail?.state === "complete") {
      return { label: "Saving CRF results", tone: "running" };
    }
    return { label: `${name} running`, tone: "running" };
  }
  if (task.status === "SUCCEEDED")
    return {
      label: `${name} complete`,
      tone: "succeeded",
      detail: "Preparing the next stage",
    };
  return {
    label: `${name} ${task.status.toLowerCase()}`,
    tone: "needs-input",
    detail: "Review this task to continue",
  };
}
