import { FinalMedia } from "./MediaPreview";
import { useState } from "react";
import { Link } from "react-router-dom";
import { readable, size } from "./api";
import type { Artifact, Job } from "./types";

const groups = ["All files", "Release", "Backups", "Media", "Analysis", "Logs"];
function group(file: Artifact) {
  if (file.info?.backup) return "Backups";
  if (
    file.artifact_type.startsWith("RELEASE_") ||
    ["FINAL_MKV", "SMOKE_TEST_MKV"].includes(file.artifact_type)
  )
    return "Release";
  if (file.artifact_type === "LOG") return "Logs";
  if (/^(AUDIO|SUBTITLE|SCREENSHOT)/.test(file.artifact_type)) return "Media";
  return "Analysis";
}
export function Artifacts({ job }: { job: Job }) {
  const [filter, setFilter] = useState(
    job.artifacts.some((file) => file.artifact_type.startsWith("RELEASE_"))
      ? "Release"
      : "All files",
  );
  const [search, setSearch] = useState("");
  const [limit, setLimit] = useState(20);
  const files = [...job.artifacts]
    .reverse()
    .filter(
      (file) =>
        (filter === "All files" || group(file) === filter) &&
        `${file.path} ${readable(file.artifact_type)}`
          .toLowerCase()
          .includes(search.toLowerCase()),
    );
  return (
    <>
      <FinalMedia key={job.id} job={job} />
      <section className="artifacts-panel">
        <div className="section-heading">
          <h2>Generated artifacts</h2>
          <span>{job.artifacts.length} files</span>
        </div>
        <p>
          Find release files, media, analysis reports, and logs. Read generated
          text in the <Link to={`/jobs/${job.id}/release`}>Release tab</Link>.
        </p>
        <div
          className="artifact-filters"
          role="group"
          aria-label="Artifact categories"
        >
          {groups.map((name) => (
            <button
              type="button"
              key={name}
              className={name === filter ? "" : "secondary"}
              aria-pressed={name === filter}
              onClick={() => {
                setFilter(name);
                setLimit(20);
              }}
            >
              {name}
            </button>
          ))}
        </div>
        <label>
          Find a file
          <input
            type="search"
            value={search}
            onChange={(e) => {
              setSearch(e.target.value);
              setLimit(20);
            }}
            placeholder="Search names or file types"
          />
        </label>
        <div className="artifact-list">
          {files.slice(0, limit).map((file) => (
            <article className="artifact-row" key={file.id}>
              <div className="grow">
                <strong>{file.path.split("/").pop()}</strong>
                <small>
                  {readable(file.artifact_type)} · {size(file.size)}
                </small>
                {file.info?.backup && (
                  <small className="backup-expiry">
                    {file.info.backup.expires_at
                      ? `Backup · expires ${new Date(file.info.backup.expires_at).toLocaleString()}`
                      : "Backup · kept until manually deleted"}
                  </small>
                )}
                <details>
                  <summary>File location</summary>
                  <code>{file.path}</code>
                </details>
              </div>
              <a
                className="artifact-open"
                href={`/api/artifacts/${file.id}`}
                target="_blank"
                rel="noreferrer"
                aria-label={`Open ${file.path.split("/").pop()}`}
              >
                Open ↗
              </a>
            </article>
          ))}
        </div>
        {!files.length && (
          <p className="muted">
            {job.artifacts.length
              ? "No files match this filter."
              : "Files will appear as processing finishes."}
          </p>
        )}
        {files.length > limit && (
          <button className="secondary" onClick={() => setLimit(limit + 20)}>
            Show more ({files.length - limit} remaining)
          </button>
        )}
      </section>
    </>
  );
}
