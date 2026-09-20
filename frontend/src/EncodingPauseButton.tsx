import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "./api";
import { encodingPauseState, type EncodingPauseTask } from "./encoding-pause";

export function EncodingPauseButton({ task }: { task: EncodingPauseTask }) {
  const query = useQueryClient();
  const pause = encodingPauseState(task);
  const change = useMutation({
    mutationFn: (action: string) => api(`/tasks/${task.id}/${action}`, {}),
    onSettled: () =>
      Promise.all([
        query.invalidateQueries({ queryKey: ["job"] }),
        query.invalidateQueries({ queryKey: ["jobs"] }),
        query.invalidateQueries({ queryKey: ["queue"] }),
      ]),
  });
  if (!pause?.visible) return null;
  return (
    <div className="encoding-pause-control">
      <button
        className="secondary"
        disabled={change.isPending || pause.disabled}
        onClick={() => change.mutate(pause.action)}
      >
        {change.isPending
          ? pause.action === "pause"
            ? "Pausing…"
            : "Resuming…"
          : pause.label}
      </button>
      {change.error && (
        <p className="error" role="alert">
          {change.error.message}
        </p>
      )}
    </div>
  );
}
