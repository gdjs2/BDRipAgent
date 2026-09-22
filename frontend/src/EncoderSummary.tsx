import { useQuery } from "@tanstack/react-query";
import { api } from "./api";
import type { Job } from "./types";

export function EncoderSummary({ job }: { job: Job }) {
  const task = [...job.tasks]
    .filter((t) => t.type === "encode")
    .sort((a, b) => b.created_at.localeCompare(a.created_at))[0];
  const ready = task?.status === "SUCCEEDED";
  const query = useQuery({
    queryKey: ["encoder-info", job.id, task?.id],
    queryFn: () =>
      api<{ filename: string; text: string }>(`/jobs/${job.id}/encoder-info`),
    enabled: ready,
    retry: false,
    staleTime: Infinity,
  });
  if (!ready) return null;
  return (
    <section className="encoder-summary">
      <h2>Encoder information</h2>
      {query.isPending && <p role="status">Reading encoder summary…</p>}
      {query.error && (
        <p className="error">
          {query.error.message}{" "}
          <button className="secondary" onClick={() => query.refetch()}>
            Retry
          </button>
        </p>
      )}
      {query.data && (
        <>
          <p className="artifact-path">{query.data.filename}</p>
          <pre tabIndex={0} aria-label="Encoder information">
            {query.data.text}
          </pre>
        </>
      )}
    </section>
  );
}
