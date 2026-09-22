import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "./api";
import { editableEncodingTask } from "./encoding-configuration";
import type { Job, RateControl } from "./types";

export function EncodeTargetEditor({ job }: { job: Job }) {
  const query = useQueryClient();
  const [mode, setMode] = useState<RateControl | null>(null);
  const [crf, setCrf] = useState<string | null>(null);
  const [bitrate, setBitrate] = useState<string | null>(null);
  const config = job.encode_config?.data;
  const rateControl = mode ?? config?.rate_control ?? "crf";
  const crfValue = crf ?? String(config?.crf ?? "");
  const bitrateValue =
    bitrate ??
    (config?.bitrate_kbps == null ? "" : String(config.bitrate_kbps / 1000));
  const task = editableEncodingTask(job);
  const completed =
    [...job.tasks]
      .filter((t) => t.type === "encode")
      .sort((a, b) => b.created_at.localeCompare(a.created_at))[0]?.status ===
    "SUCCEEDED";
  const replacement = !task && completed && config?.execution_mode !== "smoke";
  const save = useMutation({
    mutationFn: () =>
      api<Job>(
        replacement
          ? `/jobs/${job.id}/reencode`
          : `/jobs/${job.id}/encode-selection`,
        {
          rate_control: rateControl,
          ...(rateControl === "bitrate"
            ? { bitrate_kbps: Math.round(Number(bitrateValue) * 1000) }
            : { crf: Number(crfValue) }),
        },
        replacement ? "POST" : "PATCH",
      ),
    onSuccess: (updated) => {
      query.setQueryData(["job", job.id], updated);
      setMode(null);
      setCrf(null);
      setBitrate(null);
    },
    onSettled: () => {
      query.invalidateQueries({ queryKey: ["job", job.id] });
      query.invalidateQueries({ queryKey: ["queue"] });
    },
  });
  if ((!task && !replacement) || !config) return null;
  const queued = task?.status === "QUEUED";
  return (
    <section
      aria-label={replacement ? "Re-encode video" : "Adjust encoding target"}
    >
      <h2>{replacement ? "Re-encode video" : "Adjust encoding target"}</h2>
      <p className="muted">
        {replacement
          ? "Queue a replacement with a new CRF or bitrate. Existing video and release files remain available until replaced. Validation, screenshot review and release generation must run again; track selections and release details are retained."
          : queued
            ? "Changes apply when this queued encode starts. Its queue position and hold setting stay the same."
            : "Save your new target, then use Retry this stage below to restart encoding. Saving keeps this job stopped."}
      </p>
      {replacement && !job.reencode?.available && (
        <p role="status">
          {job.reencode?.reason ?? "Re-encoding is unavailable."}
        </p>
      )}
      <form
        className="inline-form encode-target-form"
        onSubmit={(event) => {
          event.preventDefault();
          if (!replacement || job.reencode?.available) save.mutate();
        }}
      >
        <label>
          Rate control
          <select
            aria-label="Rate control"
            value={rateControl}
            disabled={save.isPending}
            onChange={(event) => {
              setMode(event.target.value as RateControl);
              save.reset();
            }}
          >
            <option value="crf">Constant quality (CRF)</option>
            <option value="bitrate">Average bitrate (2-pass)</option>
          </select>
        </label>
        {rateControl === "bitrate" ? (
          <label>
            Target video bitrate (Mbps)
            <input
              type="number"
              required
              min="0.001"
              max="1000"
              step="0.001"
              value={bitrateValue}
              disabled={save.isPending}
              onChange={(event) => {
                setBitrate(event.target.value);
                save.reset();
              }}
            />
          </label>
        ) : (
          <label>
            CRF
            <input
              type="number"
              required
              min={config.profile_snapshot.crf_min}
              max={config.profile_snapshot.crf_max}
              step="0.1"
              value={crfValue}
              disabled={save.isPending}
              onChange={(event) => {
                setCrf(event.target.value);
                save.reset();
              }}
            />
          </label>
        )}
        <button
          disabled={
            save.isPending ||
            (replacement && !job.reencode?.available) ||
            (rateControl === "bitrate" ? bitrateValue : crfValue) === ""
          }
        >
          {save.isPending
            ? "Saving…"
            : replacement
              ? "Queue re-encode"
              : "Save encoding target"}
        </button>
      </form>
      {save.error && (
        <p className="error" role="alert">
          {save.error.message}
        </p>
      )}
      {save.isSuccess && (
        <p role="status">
          {queued
            ? "Target saved for the queued encode."
            : "Target saved. Retry this stage when ready."}
        </p>
      )}
    </section>
  );
}
