export type EncodingPauseTask = {
  id: string;
  type: string;
  status: string;
  can_pause?: boolean;
  pause_requested?: boolean;
  paused_at?: string | null;
  cancel_requested?: boolean;
};

export function encodingPauseState(task: EncodingPauseTask) {
  if (task.type !== "encode" || task.status !== "RUNNING") return null;
  const requested = Boolean(task.pause_requested);
  const paused = Boolean(task.paused_at);
  return {
    visible: Boolean(task.can_pause),
    status: task.cancel_requested
      ? "Cancelling…"
      : requested
        ? paused
          ? "Paused"
          : "Pausing…"
        : paused
          ? "Resuming…"
          : "Running",
    action: requested ? "resume" : "pause",
    label: requested
      ? paused
        ? "Resume encoding"
        : "Pausing…"
      : paused
        ? "Resuming…"
        : "Pause encoding",
    disabled:
      !task.can_pause || Boolean(task.cancel_requested) || requested !== paused,
  };
}
