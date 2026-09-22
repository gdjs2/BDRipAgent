import {
  useEffect,
  useRef,
  useState,
  type FormEvent,
  type ReactNode,
} from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api, readable } from "./api";
import type { Artifact, Config, Job, ReleaseDetails } from "./types";

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
    movie_description: "",
    tracker: "",
    source: "",
    upload_screenshots: false,
    ...job.analysis.release_details,
  });
  const edited = useRef(false);
  const shared = job.analysis.shared_release_details;
  const sharedRevision = shared?.revision ?? 0;
  const [loadedRevision, setLoadedRevision] = useState(sharedRevision);
  const staleShared = edited.current && loadedRevision !== sharedRevision;
  const savedDetails = JSON.stringify(job.analysis.release_details ?? {});
  useEffect(() => {
    if (!edited.current) {
      setLoadedRevision(sharedRevision);
      setDetails({
        chinese_name: "",
        extra_description: "",
        tracker: "",
        source: "",
        upload_screenshots: false,
        movie_description: "",
        ...JSON.parse(savedDetails),
      });
    }
  }, [savedDetails, sharedRevision]);
  const [saved, setSaved] = useState(false);
  const task = [...job.tasks]
    .filter((t) => t.type === "generate_release")
    .sort((a, b) => b.created_at.localeCompare(a.created_at))[0];
  const active = job.tasks.some((t) =>
    ["QUEUED", "RUNNING"].includes(t.status),
  );
  const available = !job.tasks.some(
    (t) =>
      t.type === "generate_release" && ["QUEUED", "RUNNING"].includes(t.status),
  );
  const canGenerate =
    ["WAITING_FOR_RELEASE_DETAILS", "GENERATING_RELEASE", "COMPLETE"].includes(
      job.state,
    ) && !active;
  const result = job.analysis.release_result;
  const submit = useMutation({
    mutationFn: (start: boolean) =>
      api<Job>(
        `/jobs/${job.id}/release`,
        { ...details, shared_revision: loadedRevision },
        start ? "POST" : "PATCH",
      ),
    onSuccess: (updated, start) => {
      edited.current = false;
      setLoadedRevision(updated.analysis.shared_release_details?.revision ?? 0);
      query.setQueryData(["job", job.id], updated);
      setSaved(!start);
    },
    onError: () => query.invalidateQueries({ queryKey: ["job", job.id] }),
  });
  const convert = useMutation({
    mutationFn: (source: string) =>
      api<{ source: string }>("/release/source-description", { source }),
    onSuccess: (result, original) => {
      edited.current = true;
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
  function change(field: keyof ReleaseDetails, value: string | boolean) {
    edited.current = true;
    setDetails({ ...details, [field]: value });
    setSaved(false);
    submit.reset();
    if (field === "source") convert.reset();
  }
  function loadShared() {
    edited.current = false;
    setDetails({ ...details, ...job.analysis.release_details });
    setLoadedRevision(sharedRevision);
    setSaved(false);
    submit.reset();
    convert.reset();
  }
  function generate(event: FormEvent) {
    event.preventDefault();
    if (canGenerate && available && !staleShared) submit.mutate(true);
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
            "RELEASE_ENCODER_INFO",
          ].includes(a.artifact_type),
      )
    : [];
  return (
    <>
      <h2>Prepare your release</h2>
      <p>
        Generate the BBCode, NFO, MD5 checksum, and private torrent. You can
        optionally upload your chosen comparison pairs to TTG.
      </p>
      {job.analysis.remux_revision && canGenerate && !result && (
        <p className="callout needs-input">
          The revised MKV is ready. Generate release files below to create a new
          BBCode, torrent, MD5, and NFO from this version.
        </p>
      )}
      {job.remux?.available && (
        <p className="muted">
          Need different audio or subtitles?{" "}
          <Link to={`/jobs/${job.id}/tracks`}>Edit tracks and remux</Link> using
          the retained encoded video.
        </p>
      )}
      <p className="muted">
        Release information is saved for this source video and shared by all its
        encodes, including future jobs.
        {shared && (
          <>
            {" "}
            Last updated from{" "}
            {shared.source_job_available === false ? (
              <span>{shared.title} (removed job)</span>
            ) : (
              <Link to={`/jobs/${shared.source_job_id}/release`}>
                {shared.title}
              </Link>
            )}
            .
          </>
        )}
      </p>
      {staleShared && (
        <div className="callout needs-input" role="alert">
          <p>
            Release information changed in another encoding. Your unsaved edits
            are kept here. Load the shared information before saving or
            generating files.
          </p>
          <button type="button" className="secondary" onClick={loadShared}>
            Load shared release information
          </button>
        </div>
      )}
      {shared?.snapshot_differs && (result || !available) && (
        <p className="callout needs-input">
          {available
            ? "The existing release files use older information. Generate files again to apply the shared information to this encode."
            : "This release task uses its saved information. The updated shared information can be used the next time you generate files."}
        </p>
      )}
      {!canGenerate && !task && (
        <p className="muted">
          Save your release details now while analysis or encoding runs. File
          generation and uploads become available after choosing and rendering
          final pairs in{" "}
          <Link to={`/jobs/${job.id}/screenshots`}>Screenshots</Link>.
        </p>
      )}
      <section
        className={`release-form ${job.state === "WAITING_FOR_RELEASE_DETAILS" ? "needs-input" : ""}`}
      >
        <details className="release-editor" open={!result}>
          <summary>
            {result
              ? "Edit release details and regenerate files"
              : "Release details"}
          </summary>
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
                Original disc or source release used for this encode. This text
                is saved in the NFO and BBCode. You can paste a dotted release
                name and convert it below, then edit the result.
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
                separator dots with spaces and @ with -, and keeps channel
                numbers such as 5.1 and 7.1.
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
                Movie description (BBCode, optional)
                <textarea
                  maxLength={100000}
                  rows={10}
                  value={details.movie_description}
                  onChange={(e) => change("movie_description", e.target.value)}
                  placeholder="Paste the full movie description here, including BBCode if needed."
                />
                <small>
                  Full description block in the BBCode post. Leave blank to use
                  the generated movie details and synopsis. This is separate
                  from the extra description beside the title.
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
                  HTTP, HTTPS, or UDP. Include your tracker passkey when
                  required.
                </small>
              </label>
              <label className="checkbox">
                <input
                  type="checkbox"
                  disabled={!canGenerate}
                  checked={details.upload_screenshots}
                  onChange={(e) =>
                    change("upload_screenshots", e.target.checked)
                  }
                />
                Upload screenshots to TTG
              </label>
              <p className="muted">
                When unchecked, no screenshots are uploaded and the BBCode
                screenshot section stays empty.
              </p>
              <div className="screenshot-actions">
                <button
                  type="button"
                  className="secondary"
                  disabled={
                    !details.chinese_name.trim() ||
                    !details.tracker.trim() ||
                    !details.source.trim() ||
                    convert.isPending ||
                    staleShared
                  }
                  onClick={() => submit.mutate(false)}
                >
                  Save details
                </button>
                <button
                  type="submit"
                  disabled={
                    !canGenerate ||
                    staleShared ||
                    (details.upload_screenshots &&
                      !config.release?.upload_configured) ||
                    convert.isPending
                  }
                >
                  {submit.isPending
                    ? "Saving…"
                    : details.upload_screenshots
                      ? "Generate files & upload screenshots"
                      : "Generate files"}
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
          {details.upload_screenshots && !config.release?.upload_configured && (
            <p className="attention-text">
              Screenshot uploads need a TTG image-host API token. Set{" "}
              <code>TU_TTG_TOKEN</code> in the server’s <code>.env</code> file,
              then restart the API and worker, or uncheck uploads to generate
              files now.
            </p>
          )}
          <p className="muted">
            Successful image uploads are saved and reused on retry. The torrent
            contains the MKV, NFO, and MD5 files. Preview the release text and
            download the torrent below.
          </p>
        </details>
      </section>
      {task?.status !== "SUCCEEDED" && controls}
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
            {result.upload_screenshots === false
              ? "Screenshots were not uploaded; the BBCode screenshot section is empty."
              : `${result.uploaded_images} screenshots uploaded.`}{" "}
            Torrent pieces verified against the release files.
          </p>
          <div className="screenshot-actions">
            {files
              .filter((a) => a.artifact_type === "RELEASE_TORRENT")
              .map((a) => (
                <a
                  className="button secondary"
                  href={`/api/artifacts/${a.id}`}
                  key={a.id}
                >
                  {readable(a.artifact_type.replace("RELEASE_", ""))} ↓
                </a>
              ))}
          </div>
          <ReleaseTextPreview
            key={result.task_id}
            files={files}
            revision={result.task_id}
          />
          <p className="artifact-path">
            Release folder:{" "}
            <code>
              {result.package_storage ?? "completed"}/
              {result.bundle_path ?? result.package_path}
            </code>
          </p>
          {result.torrent_path && (
            <p className="artifact-path">
              Torrent:{" "}
              <code>
                {result.torrent_storage ?? "torrents"}/{result.torrent_path}
              </code>
            </p>
          )}
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

function ReleaseTextPreview({
  files,
  revision,
}: {
  files: Artifact[];
  revision: string;
}) {
  const texts = files.filter(
    (file) => file.artifact_type !== "RELEASE_TORRENT",
  );
  const [selected, setSelected] = useState("");
  const file =
    texts.find((item) => item.id === selected) ??
    texts.find((item) => item.artifact_type === "RELEASE_BBCODE") ??
    texts[0];
  const preview = useQuery({
    queryKey: ["artifact-preview", file?.id, revision],
    queryFn: () =>
      api<{ filename: string; text: string; truncated: boolean }>(
        `/artifacts/${file!.id}/preview`,
      ),
    enabled: Boolean(file),
  });
  if (!file) return null;
  return (
    <div className="release-text-preview">
      <h3>Text preview</h3>
      <div
        className="screenshot-actions"
        role="group"
        aria-label="Release text files"
      >
        {texts.map((item) => (
          <button
            key={item.id}
            type="button"
            aria-pressed={file.id === item.id}
            className={file.id === item.id ? "" : "secondary"}
            onClick={() => setSelected(item.id)}
          >
            {item.artifact_type === "RELEASE_ENCODER_INFO"
              ? "Encoder notes"
              : item.artifact_type.replace("RELEASE_", "")}
          </button>
        ))}
      </div>
      {preview.isPending && <p role="status">Loading preview…</p>}
      {preview.error && (
        <p className="error" role="alert">
          {preview.error.message}
        </p>
      )}
      {preview.data && (
        <>
          <p className="artifact-path">{preview.data.filename}</p>
          <pre tabIndex={0} aria-label={`${preview.data.filename} preview`}>
            {preview.data.text}
          </pre>
          {preview.data.truncated && (
            <p className="muted">
              Showing the first 1 MiB.{" "}
              <a href={`/api/artifacts/${file.id}`}>Download the full file</a>.
            </p>
          )}
        </>
      )}
    </div>
  );
}
