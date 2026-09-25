import { useQuery } from "@tanstack/react-query";
import { api } from "./api";
import type { Job } from "./types";

type EncodeSizes = {
  encoded_bytes: number | null;
  source_bytes: number | null;
  percent_of_source: number | null;
  encoding_skipped: boolean;
};

function fileSize(bytes: number | null) {
  if (bytes === null) return "Unavailable";
  if (bytes < 1024) return `${bytes} B`;
  const exponent = Math.min(4, Math.floor(Math.log(bytes) / Math.log(1024)));
  return `${(bytes / 1024 ** exponent).toFixed(2)} ${["B", "KiB", "MiB", "GiB", "TiB"][exponent]}`;
}

export function EncoderSummary({ job }: { job: Job }) {
  const task = [...job.tasks]
    .filter((t) => t.type === "encode")
    .sort((a, b) => b.created_at.localeCompare(a.created_at))[0];
  const ready = task?.status === "SUCCEEDED";
  const query = useQuery({
    queryKey: ["encoder-info", job.id, task?.id],
    queryFn: () =>
      api<{ filename: string; text: string; sizes?: EncodeSizes }>(
        `/jobs/${job.id}/encoder-info`,
      ),
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
          {query.data.sizes && !query.data.sizes.encoding_skipped && (
            <>
              <dl className="encode-sizes" aria-label="Encoding file sizes">
                <div>
                  <dt>Encoded video</dt>
                  <dd
                    title={`${query.data.sizes.encoded_bytes ?? "Unknown"} bytes`}
                  >
                    {fileSize(query.data.sizes.encoded_bytes)}
                  </dd>
                </div>
                <div>
                  <dt>Original source file</dt>
                  <dd
                    title={`${query.data.sizes.source_bytes ?? "Unknown"} bytes`}
                  >
                    {fileSize(query.data.sizes.source_bytes)}
                  </dd>
                </div>
                <div>
                  <dt>Size ratio</dt>
                  <dd>
                    {query.data.sizes.percent_of_source === null
                      ? "Unavailable"
                      : `${query.data.sizes.percent_of_source.toFixed(1)}% of original`}
                  </dd>
                </div>
              </dl>
              <p className="muted">
                The encoded size is video only. The original file includes its
                audio and subtitles.
              </p>
            </>
          )}
          <p className="artifact-path">{query.data.filename}</p>
          <pre tabIndex={0} aria-label="Encoder information">
            {query.data.text}
          </pre>
        </>
      )}
    </section>
  );
}
