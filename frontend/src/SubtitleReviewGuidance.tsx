import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "./api";

type Message = {
  id: number;
  message: string;
  created_at: string;
  recheck_completed: boolean;
};

export function SubtitleReviewGuidance({
  taskId,
  disabled = false,
  canContinue = true,
}: {
  taskId: string;
  disabled?: boolean;
  canContinue?: boolean;
}) {
  const query = useQueryClient();
  const storageKey = `subtitle-guidance:${taskId}`;
  const [message, setMessage] = useState(() => {
    try {
      return sessionStorage.getItem(storageKey) ?? "";
    } catch {
      return "";
    }
  });
  const [recheck, setRecheck] = useState(false);
  const history = useQuery({
    queryKey: ["subtitle-guidance", taskId],
    queryFn: () => api<Message[]>(`/tasks/${taskId}/subtitle-guidance`),
  });
  const followup = useMutation({
    mutationFn: () =>
      api(`/tasks/${taskId}/continue-subtitle-review`, {
        message: message.trim(),
        recheck_completed: recheck,
      }),
    onSuccess: async () => {
      setMessage("");
      try {
        sessionStorage.removeItem(storageKey);
      } catch {
        /* Storage is optional. */
      }
      await query.invalidateQueries({ queryKey: ["job"] });
      await query.invalidateQueries({ queryKey: ["subtitle-guidance"] });
    },
  });
  return (
    <div className="subtitle-review-guidance">
      {!!history.data?.length && (
        <details>
          <summary>Your review guidance ({history.data.length})</summary>
          {history.data.map((item) => (
            <blockquote key={item.id}>
              <small>
                {new Date(item.created_at).toLocaleString()} ·{" "}
                {item.recheck_completed
                  ? "Re-review all cues"
                  : "Continue unfinished work"}
              </small>
              <p>{item.message}</p>
            </blockquote>
          ))}
        </details>
      )}
      {canContinue && (
        <details open={!!message} className="review-guidance-editor">
          <summary>Add guidance & continue review</summary>
          <p className="muted">
            Explain how to resolve the reported problem. The agent receives your
            message, earlier guidance and the previous review. Completed batches
            are kept by default.
          </p>
          <label htmlFor={`subtitle-guidance-${taskId}`}>
            Message to the subtitle agent
          </label>
          <textarea
            id={`subtitle-guidance-${taskId}`}
            value={message}
            maxLength={8000}
            rows={4}
            placeholder="For example: keep minor timing differences within the supported tolerance, and use the source English dialogue to repair damaged Chinese lines."
            disabled={disabled || followup.isPending}
            onChange={(event) => {
              setMessage(event.target.value);
              try {
                sessionStorage.setItem(storageKey, event.target.value);
              } catch {
                /* Storage is optional. */
              }
            }}
          />
          <label className="review-guidance-recheck">
            <input
              type="checkbox"
              checked={recheck}
              disabled={disabled || followup.isPending}
              onChange={(event) => setRecheck(event.target.checked)}
            />
            Re-review all cues with this guidance
          </label>
          <div className="review-guidance-actions">
            <button
              type="button"
              disabled={disabled || followup.isPending || !message.trim()}
              onClick={() => followup.mutate()}
            >
              {followup.isPending
                ? "Queuing continuation…"
                : "Continue with guidance"}
            </button>
            <small className="muted">
              {message.length.toLocaleString()} / 8,000
            </small>
          </div>
          {followup.error && (
            <p className="error" role="alert">
              {followup.error.message}
            </p>
          )}
          {followup.isSuccess && (
            <p role="status">Review queued with your guidance.</p>
          )}
        </details>
      )}
    </div>
  );
}
