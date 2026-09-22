import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "./api";
import type { DiscoveredSubtitle, Job } from "./types";

function language(code: string) {
  try {
    return new Intl.DisplayNames(["en"], { type: "language" }).of(code) ?? code;
  } catch {
    return code;
  }
}
function SourceLink({
  url,
  children,
}: {
  url: string;
  children: React.ReactNode;
}) {
  return /^https?:\/\//i.test(url) ? (
    <a href={url} target="_blank" rel="noopener noreferrer">
      {children}
    </a>
  ) : (
    <span>{children}</span>
  );
}
export function SubtitleProvenance({ value }: { value: DiscoveredSubtitle }) {
  const a = value.alignment;
  const warnings = [...value.quality.warnings, ...value.review.issues];
  return (
    <div className="subtitle-provenance">
      <h4>
        {value.origin === "upload"
          ? "Reviewed uploaded subtitle"
          : "Agent-found subtitle"}
      </h4>
      <p>
        {value.origin === "upload" ? (
          "Uploaded file · "
        ) : (
          <>
            {value.source_url && (
              <>
                <SourceLink url={value.source_url}>Source page</SourceLink>
                {" · "}
              </>
            )}
            {value.download_url && (
              <>
                <SourceLink url={value.download_url}>
                  Downloaded file
                </SourceLink>
                {" · "}
              </>
            )}
          </>
        )}
        {value.source_filename}
      </p>
      <p>
        <strong>Why this subtitle:</strong> {value.selection_reason}
      </p>
      {value.release && (
        <p>
          <strong>Release:</strong> {value.release}
        </p>
      )}
      {!!value.critical_errors?.length && (
        <div className="needs-input" role="status">
          <strong>PGS generated with critical subtitle issues</strong>
          <p>
            Supported repairs were applied. Uncertain dialogue was retained and
            is listed below.
          </p>
          <ul>
            {value.critical_errors.map((issue, index) => (
              <li key={index}>{issue}</li>
            ))}
          </ul>
        </div>
      )}
      {value.cleanup && (
        <details>
          <summary>
            Single-language cleanup · {value.cleanup.reviewed_cues} cues
            reviewed · {value.cleanup.edited_cues} edits
          </summary>
          <p>
            {value.cleanup.retained_cues} cues retained in{" "}
            {value.cleanup.language}. Cleaned locally to SRT before Subtitle
            Edit PGS conversion.
          </p>
          {value.cleanup.reviews.map((review, index) => (
            <div key={index}>
              <p>{review.explanation}</p>
              {review.edits.map((edit) => (
                <div key={edit.cue_id}>
                  <strong>
                    Cue {edit.cue_id}: {edit.action}
                  </strong>
                  <p>{edit.reason}</p>
                  <pre>{edit.original_text}</pre>
                  <pre>{edit.replacement_text ?? "Removed"}</pre>
                </div>
              ))}
            </div>
          ))}
        </details>
      )}
      <div className="subtitle-alignment-stats">
        <span>
          <small>Timing scale</small>
          <strong>× {a.scale.toFixed(6)}</strong>
        </span>
        <span>
          <small>Displacement</small>
          <strong>
            {a.offset_seconds >= 0 ? "+" : ""}
            {a.offset_seconds.toFixed(3)} s
          </strong>
        </span>
        <span>
          <small>Largest anchor error</small>
          <strong>{(a.max_error_seconds * 1000).toFixed(0)} ms</strong>
        </span>
        <span>
          <small>Source reference</small>
          <strong>
            Track #{a.reference_track_id} · {a.anchor_count} matches
          </strong>
        </span>
      </div>
      <p className="muted">
        {value.converter} · {value.quality.cues} cues ·{" "}
        {value.crop_checked ? "Sup2sup crop checked" : "Crop pending"}.
        Alignment is checked against matched source dialogue throughout the
        movie.
      </p>
      {warnings.length > 0 && (
        <details className="needs-input">
          <summary>Quality notes ({warnings.length})</summary>
          <ul>
            {warnings.map((warning, index) => (
              <li key={index}>{warning}</li>
            ))}
          </ul>
        </details>
      )}
      <details>
        <summary>Matched dialogue and alignment reasoning</summary>
        <p>{value.review.explanation}</p>
        <ul>
          {a.anchors.map((anchor) => (
            <li key={anchor.candidate_id}>
              Cue {anchor.candidate_id} → source {anchor.reference_id}:{" "}
              {anchor.explanation}
            </li>
          ))}
        </ul>
      </details>
    </div>
  );
}

export function SubtitleDiscoveryPanel({ job }: { job: Job }) {
  const query = useQueryClient();
  const state = job.subtitle_discovery;
  const found = job.tracks.filter((track) => track.info.origin === "discovery");
  const remove = useMutation({
    mutationFn: (id: number) =>
      api<Job>(
        `/jobs/${job.id}/subtitles/discovered/${id}`,
        undefined,
        "DELETE",
      ),
    onSuccess: (updated) => {
      query.setQueryData(["job", job.id], updated);
      query.invalidateQueries({ queryKey: ["job"] });
    },
  });
  const [originals, setOriginals] = useState(
    state?.policy.original_languages.join(", ") ?? "",
  );
  const active = state?.active_task;
  const latest = [...job.tasks]
    .filter((task) => task.type === "discover_subtitles")
    .sort((a, b) => b.created_at.localeCompare(a.created_at))[0];
  const refresh = () => query.invalidateQueries({ queryKey: ["job", job.id] });
  const search = useMutation({
    mutationFn: () =>
      api<Job>(`/jobs/${job.id}/subtitles/discover`, {
        enabled: state?.policy.enabled ?? false,
        original_languages: originals.split(/[,\s]+/).filter(Boolean),
      }),
    onSuccess: refresh,
  });
  const cancel = useMutation({
    mutationFn: () => api(`/tasks/${active!.id}/cancel`, {}),
    onSuccess: refresh,
  });
  if (!state || (!state.allowed && !state.report && !active && !found.length))
    return null;
  const report = state.report;
  return (
    <section className="subtitle-discovery-panel">
      <div className="section-heading">
        <div>
          <h3>Find missing subtitles</h3>
          <p className="muted">
            Original language · English · Simplified Chinese · Traditional
            Chinese
          </p>
        </div>
        {active ? (
          <span className="badge running">
            {active.status === "QUEUED"
              ? active.type === "review_uploaded_subtitle"
                ? "Upload review queued"
                : "Search queued"
              : active.type === "review_uploaded_subtitle"
                ? "Reviewing uploaded subtitle"
                : "Searching & aligning"}
          </span>
        ) : report?.needs_review || state.review_required ? (
          <span className="badge needs-input">Needs review</span>
        ) : report?.status === "complete" ? (
          <span className="badge succeeded">Search complete</span>
        ) : null}
      </div>
      <p>
        Find single-language SRT/ASS online, clean the text, and align it to the
        existing source subtitles before preparing PGS locally. New tracks stay
        unselected until you choose them.
      </p>
      <p className="muted">
        Missing:{" "}
        {state.missing.length
          ? state.missing.map(language).join(", ")
          : "none of the known target languages"}
        .
        {state.original_language_unknown &&
          " The movie’s original language still needs verification."}
      </p>
      {state.review_required && !active && (
        <p className="callout needs-input">
          Subtitle tracks have changed. Review the available tracks and confirm
          your track choices again before remuxing, even if you keep them
          unselected.
        </p>
      )}
      <div className="subtitle-discovery-actions">
        <label>
          Original language codes (optional)
          <input
            aria-label="Subtitle search original languages"
            value={originals}
            disabled={!!active}
            onChange={(event) => setOriginals(event.target.value)}
            placeholder="Agent verifies if blank; e.g. ko"
          />
        </label>
        <button
          type="button"
          className="secondary"
          disabled={!state.allowed || !!active || search.isPending}
          onClick={() => search.mutate()}
        >
          {search.isPending
            ? "Queuing…"
            : report
              ? "Search again"
              : "Find missing subtitles"}
        </button>
        {active && (
          <button
            type="button"
            className="secondary danger"
            disabled={cancel.isPending}
            onClick={() => cancel.mutate()}
          >
            {active?.type === "review_uploaded_subtitle"
              ? "Cancel review"
              : "Cancel search"}
          </button>
        )}
      </div>
      {active && (
        <p role="status">
          {active.detail?.phase ?? "Waiting for the subtitle discovery worker"}.
          Video processing can continue.
        </p>
      )}
      {(search.error ||
        cancel.error ||
        (!active && latest?.status === "FAILED" && latest.error_message)) && (
        <p className="error" role="alert">
          {String(search.error ?? cancel.error ?? latest?.error_message)}
        </p>
      )}
      {found.length > 0 && (
        <div className="discovered-track-list">
          <h4>Agent-found subtitle tracks</h4>
          <p className="muted">
            Removal applies to all encodes of this source. Existing videos keep
            their tracks until remuxed; retained subtitle files are preserved.
          </p>
          {found.map((track) => (
            <div className="subtitle-search-result" key={track.track_id}>
              <p>
                <strong>
                  #{track.track_id} · {language(track.info.language)}
                </strong>
                {" · "}
                {track.info.original_filename ?? track.info.name}
              </p>
              <button
                type="button"
                className="secondary danger"
                aria-label={`Remove agent-found track #${track.track_id}`}
                disabled={
                  !state.removal?.allowed || remove.isPending || !!active
                }
                onClick={() => remove.mutate(track.track_id)}
              >
                Remove subtitle track
              </button>
            </div>
          ))}
          {!state.removal?.allowed && state.removal?.reason && (
            <p role="status">{state.removal.reason}</p>
          )}
        </div>
      )}
      {remove.error && (
        <p role="alert" className="error">
          {remove.error.message}
        </p>
      )}
      {remove.isSuccess && (
        <p role="status">
          Subtitle track removed from this source. Remux existing videos to
          update their tracks.
        </p>
      )}
      {report && (
        <details open={!!report.needs_review}>
          <summary>
            Search report · {report.added_tracks.length} subtitle tracks added
            {report.reused ? " · reused for this source" : ""}
          </summary>
          <p>{report.summary}</p>
          {report.original_languages.length > 0 && (
            <p>
              Original languages:{" "}
              {report.original_languages.map(language).join(", ")}.{" "}
              {report.original_language_sources.map((url, index) => (
                <SourceLink key={url} url={url}>
                  Source {index + 1}{" "}
                </SourceLink>
              ))}
            </p>
          )}
          {report.candidates.map((candidate, index) => (
            <article className="subtitle-search-result" key={index}>
              <div>
                <strong>{language(candidate.language)}</strong>
                {" · "}
                <SourceLink url={candidate.source_url}>Source</SourceLink>
                {" · "}
                <span
                  className={
                    candidate.status === "needs_review"
                      ? "needs-input"
                      : "muted"
                  }
                >
                  {candidate.status === "removed"
                    ? "Removed from this source"
                    : candidate.status === "added"
                      ? `Added as track #${candidate.track_id}`
                      : candidate.status === "needs_review"
                        ? "Needs human review"
                        : "Checking"}
                </span>
              </div>
              <p>{candidate.reason}</p>
              {candidate.issues.length > 0 && (
                <ul>
                  {candidate.issues.map((issue, i) => (
                    <li key={i}>{issue}</li>
                  ))}
                </ul>
              )}
            </article>
          ))}
          {!!report.missing?.length && (
            <p className="needs-input">
              Still missing: {report.missing.map(language).join(", ")}. Review
              the source links, try another search, or upload a subtitle you
              have aligned.
            </p>
          )}
          {report.source_job_id !== job.id && (
            <p>
              <a href={`/jobs/${report.source_job_id}/tracks`}>
                Open the source job’s agent transcript
              </a>
            </p>
          )}
        </details>
      )}
    </section>
  );
}
