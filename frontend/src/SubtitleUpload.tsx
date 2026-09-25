import { useEffect, useRef, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api, uploadSubtitle } from "./api";
import { SubtitleReviewGuidance } from "./SubtitleReviewGuidance";
import type { Job } from "./types";

export function SubtitleUpload({
  jobId,
  disabled,
  imports = [],
}: {
  jobId: string;
  disabled: boolean;
  imports?: Job["subtitle_uploads"];
}) {
  const query = useQueryClient();
  const input = useRef<HTMLInputElement>(null);
  const [file, setFile] = useState<File | null>(null);
  const [code, setCode] = useState("");
  const [sdh, setSdh] = useState("");
  const [language, setLanguage] = useState<{
    code: string;
    language_name: string;
  } | null>(null);
  const [languageError, setLanguageError] = useState("");
  const [progress, setProgress] = useState<number | null>(null);
  useEffect(() => {
    let current = true;
    setLanguage(null);
    setLanguageError("");
    if (!code.trim()) return;
    const timer = setTimeout(() => {
      api<{ code: string; language_name: string }>(
        `/languages/verify?code=${encodeURIComponent(code.trim())}`,
      )
        .then((value) => {
          if (current) setLanguage(value);
        })
        .catch((error) => {
          if (current) setLanguageError(error.message);
        });
    }, 350);
    return () => {
      current = false;
      clearTimeout(timer);
    };
  }, [code]);
  const upload = useMutation({
    mutationFn: () =>
      uploadSubtitle(jobId, file!, language!.code, sdh === "true", setProgress),
    onSuccess: () => {
      setFile(null);
      if (input.current) input.current.value = "";
      return query.invalidateQueries({ queryKey: ["job", jobId] });
    },
  });
  const retry = useMutation({
    mutationFn: (taskId: string) => api(`/tasks/${taskId}/retry`, {}),
    onSuccess: () => query.invalidateQueries({ queryKey: ["job", jobId] }),
  });
  const textUpload = !!file && !file.name.toLowerCase().endsWith(".sup");
  const limit = file?.name.toLowerCase().endsWith(".sup") ? 128 : 16;
  const fileError =
    file &&
    (!/\.(srt|ass|ssa|sup)$/i.test(file.name)
      ? "Choose an SRT, ASS, SSA, or PGS .sup file."
      : file.size > limit * 1024 * 1024
        ? `This file exceeds the ${limit} MB limit.`
        : !file.size
          ? "This file is empty."
          : "");
  const busy = disabled || upload.isPending;
  return (
    <details className="subtitle-upload">
      <summary>
        Add subtitles from a file{" "}
        <span className="muted">SRT · ASS · SSA · PGS</span>
        {imports.some((item) =>
          ["FAILED", "CANCELLED"].includes(item.status),
        ) && <span className="badge needs-input">Upload needs attention</span>}
      </summary>
      <p className="muted">
        Uploads are shared across encodes of this source and retained for future
        remuxes. SRT/ASS/SSA files are reviewed for language, SDH and text
        issues, aligned to source subtitles, converted to PGS and cropped before
        they become selectable. PGS uploads retain their supplied timing and
        layout.
      </p>
      <form
        onSubmit={(event) => {
          event.preventDefault();
          if (!busy && file && !fileError && language && (sdh || textUpload)) {
            setProgress(0);
            upload.mutate();
          }
        }}
      >
        <div className="subtitle-upload-fields">
          <label>
            Subtitle file
            <input
              ref={input}
              type="file"
              accept=".srt,.ass,.ssa,.sup"
              disabled={busy}
              onChange={(event) => {
                setFile(event.target.files?.[0] ?? null);
                upload.reset();
              }}
            />
            <small>
              Text: UTF-8 or UTF-16, up to 16 MB. PGS: up to 128 MB.
            </small>
          </label>
          <label>
            Subtitle language code
            <input
              value={code}
              disabled={busy}
              placeholder="e.g. en, ja, zh-Hant"
              maxLength={64}
              spellCheck={false}
              onChange={(event) => {
                setCode(event.target.value);
                setLanguage(null);
              }}
            />
            <small aria-live="polite">
              {language
                ? `${language.language_name} (${language.code})`
                : languageError ||
                  (code.trim()
                    ? "Verifying language…"
                    : "Language name appears after verification.")}
            </small>
          </label>
          <label>
            SDH / hearing impaired
            <select
              value={sdh}
              disabled={busy}
              onChange={(event) => setSdh(event.target.value)}
            >
              <option value="">
                {textUpload ? "Agent will determine SDH" : "Choose…"}
              </option>
              <option value="false">No — dialogue subtitles</option>
              <option value="true">Yes — includes accessibility cues</option>
            </select>
          </label>
        </div>
        <div className="subtitle-upload-actions">
          <button
            type="submit"
            disabled={
              busy || !file || !!fileError || !language || (!sdh && !textUpload)
            }
          >
            {upload.isPending ? "Uploading…" : "Upload subtitle"}
          </button>
          <small className="muted">
            Text subtitles appear as selectable PGS tracks after review
            succeeds.
          </small>
        </div>
        {upload.isPending && (
          <div role="status">
            <progress
              max={100}
              value={progress !== null && progress < 100 ? progress : undefined}
              aria-label="Subtitle upload progress"
            />
            <small>
              {progress === 100
                ? "Validating subtitle file…"
                : progress === null
                  ? "Uploading file…"
                  : `Uploading file · ${Math.round(progress)}%`}
            </small>
          </div>
        )}
        {(fileError || upload.error) && (
          <p className="error" role="alert">
            {fileError || upload.error?.message}
          </p>
        )}
        {upload.isSuccess && (
          <p role="status">
            {upload.data.duplicate
              ? "This subtitle is already available below."
              : upload.data.queued
                ? "Upload queued for review, alignment and PGS conversion."
                : "Subtitle added. Select it below to include it in the video."}
          </p>
        )}
      </form>
      {imports.length > 0 && (
        <div className="subtitle-imports">
          <h4>Uploaded subtitle reviews</h4>
          {imports.map((item) => (
            <div
              key={item.id}
              className={`callout ${["FAILED", "CANCELLED"].includes(item.status) ? "needs-input" : ""}`}
            >
              <strong>{item.filename}</strong>{" "}
              <span
                className={`badge ${["FAILED", "CANCELLED"].includes(item.status) ? "needs-input" : item.status === "SUCCEEDED" ? "succeeded" : "running"}`}
              >
                {item.status}
              </span>
              <p>
                {item.error ||
                  item.detail?.phase ||
                  "Waiting for subtitle review"}
              </p>
              <a
                href={`/api/jobs/${jobId}/subtitles/uploads/${item.id}/download`}
                download
              >
                Download uploaded file
              </a>
              {["FAILED", "CANCELLED"].includes(item.status) && (
                <button
                  className="secondary"
                  disabled={disabled || retry.isPending}
                  onClick={() => retry.mutate(item.task_id)}
                >
                  Retry review
                </button>
              )}
              <SubtitleReviewGuidance
                key={item.task_id}
                taskId={item.task_id}
                disabled={disabled}
                canContinue={["FAILED", "CANCELLED"].includes(item.status)}
              />
            </div>
          ))}
          {retry.error && <p className="error">{retry.error.message}</p>}
        </div>
      )}
    </details>
  );
}
