import { useState, type FormEvent, type ReactNode } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api, readable } from "./api";
import type { Config, Job, ReleaseDetails } from "./types";

export function Release({
  job,
  config,
  controls,
}: {
  job: Job;
  config: Config;
  controls: ReactNode;
}) {
  const query = useQueryClient();
  const [details, setDetails] = useState<ReleaseDetails>({
    chinese_name: "",
    extra_description: "",
    tracker: "",
    source: "",
    ...job.analysis.release_details,
  });
  const [saved, setSaved] = useState(false);
  const task = [...job.tasks]
    .filter((t) => t.type === "generate_release")
    .sort((a, b) => b.created_at.localeCompare(a.created_at))[0];
  const active = job.tasks.some((t) =>
    ["QUEUED", "RUNNING"].includes(t.status),
  );
  const available =
    ["WAITING_FOR_RELEASE_DETAILS", "GENERATING_RELEASE", "COMPLETE"].includes(
      job.state,
    ) && !active;
  const result = job.analysis.release_result;
  const submit = useMutation({
    mutationFn: (start: boolean) =>
      api<Job>(`/jobs/${job.id}/release`, details, start ? "POST" : "PATCH"),
    onSuccess: (updated, start) => {
      query.setQueryData(["job", job.id], updated);
      setSaved(!start);
    },
  });
  const convert = useMutation({
    mutationFn: (source: string) =>
      api<{ source: string }>("/release/source-description", { source }),
    onSuccess: (result, original) => {
      // Preserve any newer input if the user types while the request is pending.
      setDetails((current) =>
        current.source === original
          ? { ...current, source: result.source }
          : current,
      );
      setSaved(false);
      submit.reset();
    },
  });
  function change(field: keyof ReleaseDetails, value: string) {
    setDetails({ ...details, [field]: value });
    setSaved(false);
    submit.reset();
    if (field === "source") convert.reset();
  }
  function generate(event: FormEvent) {
    event.preventDefault();
    submit.mutate(true);
  }
  const files = result
    ? job.artifacts.filter(
        (a) =>
          result.artifacts.some(
            (r) => r.path === a.path && r.kind === a.artifact_type,
          ) &&
          [
            "RELEASE_BBCODE",
            "RELEASE_NFO",
            "RELEASE_MD5",
            "RELEASE_TORRENT",
          ].includes(a.artifact_type),
      )
    : [];
  return (
    <>
      <h2>Prepare your release</h2>
      <p>
        Enter the release details, upload your chosen comparison pairs to TTG,
        and generate the BBCode, NFO, MD5 checksum, and private torrent.
      </p>
      {!available && !task && (
        <p className="muted">
          Choose and render your final pairs in{" "}
          <Link to={`/jobs/${job.id}/screenshots`}>Screenshots</Link> to unlock
          this stage.
        </p>
      )}
      <section>
        <form onSubmit={generate}>
          <fieldset
            disabled={!available || submit.isPending || convert.isPending}
          >
            <label>
              Chinese name
              <input
                required
                maxLength={300}
                value={details.chinese_name}
                onChange={(e) => change("chinese_name", e.target.value)}
                placeholder="电影中文名"
              />
            </label>
            <label>
              Source
              <input
                required
                maxLength={1000}
                value={details.source}
                onChange={(e) => change("source", e.target.value)}
                placeholder="1080p Blu-ray AVC DTS-HD MA 5.1-GROUP"
                aria-describedby="release-source-help"
              />
            </label>
            <p id="release-source-help" className="muted">
              Original disc or source release used for this encode. This text is
              saved in the NFO and BBCode. You can paste a dotted release name
              and convert it below, then edit the result.
            </p>
            <button
              type="button"
              className="secondary"
              disabled={!details.source.trim() || convert.isPending}
              onClick={() => convert.mutate(details.source)}
            >
              {convert.isPending ? "Converting…" : "Convert dotted name"}
            </button>
            <p className="muted">
              Conversion removes the dotted movie title/year prefix, replaces
              separator dots with spaces and @ with -, and keeps channel numbers
              such as 5.1 and 7.1.
            </p>
            {convert.error && (
              <p className="error" role="alert">
                {convert.error.message}
              </p>
            )}
            <label>
              Extra description (optional)
              <textarea
                maxLength={6000}
                rows={3}
                value={details.extra_description}
                onChange={(e) => change("extra_description", e.target.value)}
                placeholder="国英双语 · 内封中字"
              />
              <small>
                Added beside the Chinese name in the release post heading.
              </small>
            </label>
            <label>
              Tracker announce URL
              <input
                required
                type="text"
                maxLength={4096}
                autoComplete="off"
                spellCheck={false}
                value={details.tracker}
                onChange={(e) => change("tracker", e.target.value)}
                placeholder="https://tracker.example/announce?passkey=…"
              />
              <small>
                HTTP, HTTPS, or UDP. Include your tracker passkey when required.
              </small>
            </label>
            <div className="screenshot-actions">
              <button
                type="button"
                className="secondary"
                disabled={
                  !details.chinese_name.trim() ||
                  !details.tracker.trim() ||
                  !details.source.trim() ||
                  convert.isPending
                }
                onClick={() => submit.mutate(false)}
              >
                Save details
              </button>
              <button
                type="submit"
                disabled={
                  !config.release?.upload_configured || convert.isPending
                }
              >
                {submit.isPending
                  ? "Saving…"
                  : "Generate files & upload screenshots"}
              </button>
            </div>
          </fieldset>
          {saved && <p role="status">Release details saved.</p>}
          {submit.error && (
            <p className="error" role="alert">
              {submit.error.message}
            </p>
          )}
        </form>
        {!config.release?.upload_configured && (
          <p className="muted">
            Screenshot uploads need a TTG image-host API token. Set{" "}
            <code>TU_TTG_TOKEN</code> in the server’s <code>.env</code> file,
            then restart the API and worker. You can save your release details
            now.
          </p>
        )}
        <p className="muted">
          Successful image uploads are saved and reused on retry. The torrent
          contains the MKV, NFO, and MD5 files. Download the finished files
          below.
        </p>
      </section>
      {controls}
      {task && ["QUEUED", "RUNNING"].includes(task.status) && (
        <section role="status">
          <h3>
            {String(task.progress_detail.phase ?? "Release generation queued")}
          </h3>
          {typeof task.progress_detail.uploaded === "number" && (
            <p>
              {task.progress_detail.uploaded} of{" "}
              {task.progress_detail.total_images} screenshots uploaded
            </p>
          )}
          {typeof task.progress_detail.completed_pieces === "number" && (
            <p>
              {task.progress_detail.completed_pieces} of{" "}
              {task.progress_detail.total_pieces} torrent pieces
            </p>
          )}
        </section>
      )}
      {result && (
        <section>
          <h2>Release files ready</h2>
          <p>
            {result.uploaded_images} screenshots uploaded. Torrent pieces
            verified against the release files.
          </p>
          <div className="screenshot-actions">
            {files.map((a) => (
              <a
                className="button secondary"
                href={`/api/artifacts/${a.id}`}
                key={a.id}
              >
                {readable(a.artifact_type.replace("RELEASE_", ""))} ↓
              </a>
            ))}
          </div>
          <p className="artifact-path">
            Release folder: <code>completed/{result.package_path}</code>
          </p>
          <p className="artifact-path">
            Info hash: <code>{result.infohash}</code>
          </p>
          {result.warnings.map((warning) => (
            <p className="muted" key={warning}>
              {warning}
            </p>
          ))}
        </section>
      )}
    </>
  );
}
