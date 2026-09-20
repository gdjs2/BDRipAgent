import { useEffect, useId, useRef, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api } from "./api";
import type { Job } from "./types";

export function RemoveJob({ job }: { job: Job }) {
  const [open, setOpen] = useState(false);
  const dialog = useRef<HTMLDialogElement>(null);
  const titleId = useId();
  const query = useQueryClient();
  const running = job.tasks.some((task) => task.status === "RUNNING");
  const remove = useMutation({
    mutationFn: () =>
      api(
        `/jobs/${job.id}?confirm=${encodeURIComponent(job.id)}`,
        undefined,
        "DELETE",
      ),
    onSuccess: () => {
      setOpen(false);
      query.invalidateQueries({ queryKey: ["jobs"] });
      query.invalidateQueries({ queryKey: ["queue"] });
      query.removeQueries({ queryKey: ["job", job.id] });
    },
  });
  useEffect(() => {
    if (open) dialog.current?.showModal();
    else dialog.current?.close();
  }, [open]);
  return (
    <>
      <button
        className="secondary"
        aria-label={`Remove ${job.title}`}
        onClick={() => {
          remove.reset();
          setOpen(true);
        }}
      >
        Remove
      </button>
      <dialog
        ref={dialog}
        className="remove-dialog"
        aria-labelledby={titleId}
        onCancel={(event) => {
          if (remove.isPending) event.preventDefault();
          else setOpen(false);
        }}
      >
        <h2 id={titleId}>Remove {job.title}?</h2>
        <p>
          {job.analysis_profile} · {job.year}
        </p>
        <p>
          This removes the job from the list and cancels its queued work. Source
          files and generated files are retained.
        </p>
        {running && (
          <p>
            Cancel the running task and wait for it to stop before removing this
            job. <Link to={`/jobs/${job.id}`}>Open job →</Link>
          </p>
        )}
        {remove.error && (
          <p className="error" role="alert">
            {remove.error.message}
          </p>
        )}
        <div className="dialog-actions">
          <button
            autoFocus
            className="secondary"
            disabled={remove.isPending}
            onClick={() => setOpen(false)}
          >
            Keep job
          </button>
          <button
            disabled={running || remove.isPending}
            onClick={() => remove.mutate()}
          >
            {remove.isPending ? "Removing…" : "Remove job"}
          </button>
        </div>
      </dialog>
    </>
  );
}
