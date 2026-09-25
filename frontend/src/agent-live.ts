import {
  applyAgentEvent,
  type AgentEvent,
  type AgentInvocation,
} from "./agent-transcript.ts";

export type LiveEvent = AgentEvent & {
  prompt_key?: string;
  prompt_revision?: number;
  event_id: number;
  job_id: string;
  task_id: string;
  title: string;
  year: number;
  profile: string;
  task_status: string;
  created_at: string;
  error?: string;
  reason?: string;
  agent_queue_state?: string;
};
export type Conversation = AgentInvocation & {
  prompt_key?: string;
  prompt_revision?: number;
  job_id: string;
  task_id: string;
  title: string;
  profile: string;
  created_at: string;
  event_id: number;
  state: "running" | "complete" | "error";
};
export type History = {
  events: LiveEvent[];
  cursor: number;
  before_id: number | null;
  has_more: boolean;
};

export function applyLiveEvent(
  runs: Conversation[],
  event: LiveEvent,
): Conversation[] {
  if (
    event.type === "task_end" ||
    (event.type === "error" && !event.invocation_id)
  ) {
    return runs.map((run) =>
      run.task_id === event.task_id && run.state === "running"
        ? {
            ...run,
            state: event.task_status === "SUCCEEDED" ? "complete" : "error",
            status:
              event.error ?? event.text ?? event.reason ?? event.task_status,
          }
        : run,
    );
  }
  if (!event.invocation_id) return runs;
  const id = `${event.task_id}:${event.invocation_id}`;
  const previous = runs.find((run) => run.id === id);
  const parsed = applyAgentEvent(previous ? [previous] : [], {
    ...event,
    invocation_id: id,
  })[0];
  const terminal = !["QUEUED", "RUNNING"].includes(event.task_status);
  const state =
    event.type === "error"
      ? "error"
      : event.type === "complete"
        ? "complete"
        : terminal
          ? event.task_status === "SUCCEEDED"
            ? "complete"
            : "error"
          : (previous?.state ?? "running");
  const run: Conversation = {
    ...parsed,
    prompt_key: event.prompt_key ?? previous?.prompt_key,
    prompt_revision: event.prompt_revision ?? previous?.prompt_revision,
    job_id: event.job_id,
    task_id: event.task_id,
    title: `${event.title}${event.year ? ` (${event.year})` : ""}`,
    profile: event.profile,
    created_at: previous?.created_at ?? event.created_at,
    event_id: previous?.event_id ?? event.event_id,
    state,
  };
  return previous
    ? runs.map((item) => (item.id === id ? run : item))
    : [...runs, run];
}
