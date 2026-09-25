import { useEffect, useRef, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "./api";
import {
  SubtitleDiscoveryPanel,
  SubtitleProvenance,
} from "./SubtitleDiscovery";
import { SubtitleUpload } from "./SubtitleUpload";
import { AgentTranscript } from "./AgentTranscript";
import { useTrackOrdering } from "./useTrackOrdering";
import type { Job, Track, TrackFlag } from "./types";
import {
  analysisComplete,
  hasAnalysisAttempt,
  suggestedName,
  trackFlagLabels,
  unresolvedFlags,
} from "./track-selection";

function movieLanguageCodes(job: Job) {
  return (
    job.original_languages?.map((item) => item.code) ??
    job.subtitle_discovery?.policy.original_languages ??
    []
  );
}

function languageBase(code: string) {
  try {
    return new Intl.Locale(code).language;
  } catch {
    return undefined;
  }
}

function sharedChoiceKey(job: Job) {
  return JSON.stringify({
    selection: job.track_selection,
    originals: movieLanguageCodes(job),
    revision: job.shared_track_selection?.revision ?? 0,
    applied: job.shared_track_selection?.applied_revision ?? 0,
  });
}

export function Tracks({ job }: { job: Job }) {
  const query = useQueryClient();
  const [originals, setOriginals] = useState(
    movieLanguageCodes(job).join(", "),
  );
  const [audio, setAudio] = useState<number[]>(
    job.track_selection?.audio_track_ids ?? [],
  );
  const [subs, setSubs] = useState<number[]>(
    job.track_selection?.subtitle_track_ids ?? [],
  );
  const [remuxEditing, setRemuxEditing] = useState(false);
  const [languages, setLanguages] = useState<Record<number, string>>({});
  const [verifiedLanguages, setVerifiedLanguages] = useState<
    Record<
      number,
      {
        input: string;
        code: string;
        language_name: string;
        track_name: string;
        base_name: string;
      }
    >
  >({});
  const [languageErrors, setLanguageErrors] = useState<Record<number, string>>(
    {},
  );
  const verifyLanguage = useMutation({
    mutationFn: ({ id, input }: { id: number; input: string }) =>
      api<{
        code: string;
        language_name: string;
        track_name: string;
        base_name: string;
      }>(
        `/jobs/${job.id}/tracks/${id}/language?code=${encodeURIComponent(input)}`,
      ),
    onSuccess: (result, { id, input }) => {
      setVerifiedLanguages((current) => ({
        ...current,
        [id]: { ...result, input },
      }));
      setLanguageErrors((current) => ({ ...current, [id]: "" }));
    },
    onError: (error, { id }) =>
      setLanguageErrors((current) => ({ ...current, [id]: String(error) })),
  });
  const [names, setNames] = useState<Record<number, string>>({});
  const [flags, setFlags] = useState<
    Record<number, Partial<Record<TrackFlag, boolean>>>
  >({});
  const [expanded, setExpanded] = useState<Record<number, boolean>>({});
  const started = useRef(new Set<string>());
  const [dirty, setDirty] = useState(false);
  const syncedChoices = useRef(sharedChoiceKey(job));
  const [loadedRevision, setLoadedRevision] = useState(
    job.shared_track_selection?.revision ?? 0,
  );
  const waiting =
    job.tracks_editable ??
    [
      "RUNNING_CRF_ANALYSIS",
      "WAITING_FOR_ENCODE_SELECTION",
      "ENCODING",
      "VALIDATING_ENCODE",
      "WAITING_FOR_TRACK_SELECTION",
    ].includes(job.state);
  const complete = analysisComplete(job);
  const attempted = hasAnalysisAttempt(job);
  const refresh = () =>
    Promise.all([
      query.invalidateQueries({ queryKey: ["job"] }),
      query.invalidateQueries({ queryKey: ["jobs"] }),
      query.invalidateQueries({ queryKey: ["queue"] }),
    ]);
  const ensure = useMutation({
    mutationFn: () => api(`/jobs/${job.id}/tracks/analyze`, {}),
    onSuccess: refresh,
  });
  useEffect(() => {
    if (
      waiting &&
      !complete &&
      !attempted &&
      !job.track_selection &&
      !started.current.has(job.id)
    ) {
      started.current.add(job.id);
      ensure.mutate();
    }
  }, [
    job.id,
    waiting,
    complete,
    attempted,
    job.track_selection,
    ensure.mutate,
  ]);
  function currentSuggestion(track: Track) {
    const verified = verifiedLanguages[track.track_id];
    return suggestedName(
      verified &&
        verified.input === (languages[track.track_id] ?? track.info.language)
        ? {
            ...track,
            info: {
              ...track.info,
              base_name: verified.base_name,
              suggested_name: verified.track_name,
            },
          }
        : track,
      flags[track.track_id],
    );
  }
  const selected = [...audio, ...subs];
  const editableChoices =
    waiting || (remuxEditing && job.remux?.available === true);
  const enabled =
    editableChoices &&
    complete &&
    !ensure.isPending &&
    !job.subtitle_discovery?.active_task;
  const save = useMutation({
    mutationFn: () =>
      api<Job>(
        `/jobs/${job.id}/${remuxEditing ? "remux" : "tracks/selection"}`,
        {
          shared_revision: loadedRevision,
          original_languages: originals.split(/[,\s]+/).filter(Boolean),
          audio_track_ids: ordering.groups.audio
            .filter((t) => audio.includes(t.track_id))
            .map((t) => t.track_id),
          subtitle_track_ids: ordering.groups.subtitles
            .filter((t) => subs.includes(t.track_id))
            .map((t) => t.track_id),
          track_languages: Object.fromEntries(
            selected
              .filter((id) => languages[id] !== undefined)
              .map((id) => [id, verifiedLanguages[id]?.code ?? languages[id]]),
          ),
          track_names: Object.fromEntries(
            job.tracks
              .filter((t) => selected.includes(t.track_id))
              .map((t) => [
                t.track_id,
                names[t.track_id] ??
                  t.info.name_override ??
                  currentSuggestion(t),
              ]),
          ),
          track_flags: Object.fromEntries(
            job.tracks
              .filter((t) => selected.includes(t.track_id))
              .map((t) => [
                t.track_id,
                Object.fromEntries(
                  trackFlagLabels.map(([key]) => [
                    key,
                    flags[t.track_id]?.[key] ?? t.info[key],
                  ]),
                ),
              ]),
          ),
        },
      ),
    onSuccess: (updated) => {
      setRemuxEditing(false);
      setOriginals(movieLanguageCodes(updated).join(", "));
      setLanguages({});
      setVerifiedLanguages({});
      setLanguageErrors({});
      syncedChoices.current = sharedChoiceKey(updated);
      query.setQueryData(["job", job.id], updated);
      setDirty(false);
      setNames({});
      setFlags({});
      setAudio(updated.track_selection?.audio_track_ids ?? []);
      setSubs(updated.track_selection?.subtitle_track_ids ?? []);
      setLoadedRevision(updated.shared_track_selection?.revision ?? 0);
      ordering.markSaved();
      return refresh();
    },
  });
  const ordering = useTrackOrdering(
    job.tracks,
    job.track_selection,
    enabled && !save.isPending,
  );
  const remoteChoices = sharedChoiceKey(job);
  const resetOrder = ordering.reset;
  useEffect(() => {
    if (
      dirty ||
      ordering.changed ||
      save.isPending ||
      syncedChoices.current === remoteChoices
    )
      return;
    syncedChoices.current = remoteChoices;
    const current = JSON.parse(remoteChoices);
    setOriginals(current.originals.join(", "));
    setAudio(current.selection?.audio_track_ids ?? []);
    setSubs(current.selection?.subtitle_track_ids ?? []);
    setNames({});
    setFlags({});
    setLanguages({});
    setVerifiedLanguages({});
    setLanguageErrors({});
    setLoadedRevision(current.revision);
    resetOrder(current.selection);
  }, [remoteChoices, dirty, ordering.changed, save.isPending, resetOrder]);
  const staleChoices =
    (dirty || ordering.changed) &&
    loadedRevision !== (job.shared_track_selection?.revision ?? 0);
  function loadSharedChoices() {
    setOriginals(movieLanguageCodes(job).join(", "));
    syncedChoices.current = remoteChoices;
    setAudio(job.track_selection?.audio_track_ids ?? []);
    setSubs(job.track_selection?.subtitle_track_ids ?? []);
    setNames({});
    setFlags({});
    setLanguages({});
    setVerifiedLanguages({});
    setLanguageErrors({});
    setDirty(false);
    setLoadedRevision(job.shared_track_selection?.revision ?? 0);
    ordering.reset(job.track_selection);
    save.reset();
  }
  const selectedTracks = job.tracks.filter((t) =>
    selected.includes(t.track_id),
  );
  const unknown = selectedTracks.some(
    (t) => unresolvedFlags(t, flags[t.track_id]).length > 0,
  );
  const unknownLanguage = selectedTracks.some(
    (t) =>
      t.info.subtitle_detection?.language_confident === false &&
      !t.info.language_override &&
      !(
        languages[t.track_id] !== undefined &&
        verifiedLanguages[t.track_id]?.input === languages[t.track_id]
      ),
  );
  const unverifiedLanguage = selected.some(
    (id) =>
      languages[id] !== undefined &&
      verifiedLanguages[id]?.input !== languages[id],
  );
  const invalidName = selected.some(
    (id) => names[id] !== undefined && !names[id].trim(),
  );
  const active = job.tasks.find(
    (t) =>
      ["analyze", "review_tracks"].includes(t.type) &&
      ["QUEUED", "RUNNING"].includes(t.status),
  );
  const reviewAttempt = [...job.tasks]
    .filter((t) => ["review_tracks", "analyze"].includes(t.type))
    .sort((a, b) => b.created_at.localeCompare(a.created_at))[0];
  const failedReview =
    reviewAttempt && ["FAILED", "CANCELLED"].includes(reviewAttempt.status);
  const retry = useMutation({
    mutationFn: () => api(`/tasks/${reviewAttempt!.id}/retry`, {}),
    onSuccess: refresh,
  });
  const toggle = (track: Track) => {
    setDirty(true);
    const update = track.kind === "audio" ? setAudio : setSubs;
    update((current) =>
      current.includes(track.track_id)
        ? current.filter((id) => id !== track.track_id)
        : [...current, track.track_id],
    );
    if (
      !selected.includes(track.track_id) &&
      unresolvedFlags(track, flags[track.track_id]).length
    ) {
      setExpanded((current) => ({ ...current, [track.track_id]: true }));
    }
  };
  return (
    <div className="track-selection">
      {job.remux?.available && (
        <section className={remuxEditing ? "callout needs-input" : "callout"}>
          <h3>
            {remuxEditing
              ? "Revise the final tracks"
              : "Need to change the finished video’s tracks?"}
          </h3>
          <p>
            Reuse the encoded video and retained tracks. After remuxing,
            regenerate BBCode, torrent, MD5, and NFO in the Release tab.
            Replaced videos are deleted after the new video succeeds. Other old
            release files{" "}
            {job.remux.backup_days
              ? `are kept for ${job.remux.backup_days} days after replacement`
              : "are kept until manually deleted"}
            .
          </p>
          <button
            type="button"
            className="secondary"
            onClick={() => {
              if (remuxEditing) loadSharedChoices();
              setRemuxEditing(!remuxEditing);
            }}
          >
            {remuxEditing ? "Cancel track revision" : "Edit tracks & remux"}
          </button>
        </section>
      )}
      {job.state === "COMPLETE" && job.remux && !job.remux.available && (
        <p className="muted">Remux unavailable: {job.remux.reason}</p>
      )}
      {job.analysis.shared_track_analysis?.reused && (
        <p className="muted">
          Audio and subtitle analysis reused from the same source.
        </p>
      )}
      <p className="muted">
        Track choices, order, names, flags, and language codes are shared across
        encodes of this source. Finished videos remux automatically; busy jobs
        apply the changes after their current work finishes.
      </p>
      {job.shared_track_selection?.pending && (
        <p className={job.shared_track_selection.error ? "error" : "muted"}>
          {job.shared_track_selection.error ??
            (job.shared_track_selection.remux_pending
              ? "Showing the latest shared choices. Automatic remux is pending; the current video still uses its previous tracks."
              : "Shared choices will apply automatically when this track review finishes.")}
        </p>
      )}
      {staleChoices && (
        <div className="callout needs-input" role="alert">
          <p>
            Track choices changed in another encoding. Your unsaved edits are
            still shown.
          </p>
          <button
            type="button"
            className="secondary"
            onClick={loadSharedChoices}
          >
            Load shared choices
          </button>
        </div>
      )}
      {(waiting || job.remux?.available || !!job.subtitle_uploads?.length) && (
        <SubtitleUpload
          jobId={job.id}
          imports={job.subtitle_uploads}
          disabled={
            save.isPending ||
            !!job.subtitle_discovery?.active_task ||
            !(waiting || job.remux?.available)
          }
        />
      )}
      <SubtitleDiscoveryPanel job={job} />
      <div className="section-heading">
        <div>
          <h2>Choose your tracks</h2>
          <p>
            Select audio and subtitles. Open a row to edit its name, flags, or
            review the evidence. You can save or change your choices while the
            video encodes; they are needed before remux preparation begins.
          </p>
          <p id="track-order-help">
            Drag ⠿ to reorder within each group, or focus a handle and use the
            arrow keys. Output: video → audio → subtitles in the order shown.
            Confirm to save.
          </p>
        </div>
        <span
          className={`badge ${(complete && waiting && (!job.track_selection || ordering.changed || job.subtitle_discovery?.review_required)) || failedReview ? "needs-input" : complete ? "succeeded" : "running"}`}
        >
          {complete
            ? job.subtitle_discovery?.review_required
              ? "REVIEW NEW SUBTITLES"
              : ordering.changed
                ? "ORDER NOT SAVED"
                : job.track_selection
                  ? "CONFIRMED"
                  : "READY TO SELECT"
            : failedReview
              ? "REVIEW STOPPED"
              : "ANALYZING"}
        </span>
      </div>
      {!complete && (
        <p
          className={`callout ${failedReview ? "needs-input" : ""}`}
          role="status"
        >
          {active
            ? active.progress_detail.phase || "Preparing and reviewing tracks…"
            : failedReview
              ? "Track review stopped. You can retry it while video processing continues."
              : waiting
                ? "Track review is starting automatically…"
                : "Track analysis is paused or stopped. Check the task on Overview to continue."}{" "}
          Completed descriptions appear below as they become available.
        </p>
      )}
      {!complete && failedReview && waiting && (
        <div className="callout needs-input">
          <details className="review-error">
            <summary>Track review error details</summary>
            <pre>{reviewAttempt.error_message}</pre>
          </details>
          <button disabled={retry.isPending} onClick={() => retry.mutate()}>
            Retry track review
          </button>
          {retry.error && <p className="error">{retry.error.message}</p>}
        </div>
      )}
      {ensure.error && (
        <p className="error" role="alert">
          {String(ensure.error)}
        </p>
      )}
      {job.analysis.audio_comparison && (
        <section className="audio-comparison">
          <div className="section-heading">
            <h3>Audio differences</h3>
            <span
              className={`badge ${job.analysis.audio_comparison.status === "sampling" ? "running" : job.analysis.audio_comparison.status === "resolved" ? "succeeded" : "needs-input"}`}
            >
              {job.analysis.audio_comparison.status === "sampling"
                ? "Gathering more evidence"
                : job.analysis.audio_comparison.status === "resolved"
                  ? "Comparison ready"
                  : "Manual review needed"}
            </span>
          </div>
          <p>{job.analysis.audio_comparison.summary}</p>
          <div className="audio-differences">
            {job.analysis.audio_comparison.distinctions.map((item) => (
              <div key={item.track_id}>
                <button
                  className="text-button"
                  onClick={() =>
                    setExpanded((current) => ({
                      ...current,
                      [item.track_id]: true,
                    }))
                  }
                >
                  Track #{item.track_id}
                </button>
                <p>{item.difference}</p>
                <small>
                  {item.compared_with.length
                    ? `Compared with ${item.compared_with.map((id) => `#${id}`).join(", ")}`
                    : "Only audio track"}
                  {!item.resolved && " · Uncertain"}
                </small>
              </div>
            ))}
          </div>
          {job.analysis.audio_comparison.question && (
            <p
              className={
                job.analysis.audio_comparison.status === "sampling"
                  ? "muted"
                  : "attention-text"
              }
            >
              {job.analysis.audio_comparison.status === "sampling"
                ? "Next question: "
                : "Needs your review: "}
              {job.analysis.audio_comparison.question}
            </p>
          )}
          {job.analysis.audio_comparison.stop_reason && (
            <p className="muted">{job.analysis.audio_comparison.stop_reason}</p>
          )}
          <small>
            {job.analysis.audio_comparison.rounds}
            {job.analysis.audio_comparison.max_rounds
              ? ` / ${job.analysis.audio_comparison.max_rounds}`
              : ""}{" "}
            comparison rounds · Content conclusions apply to sampled intervals.
          </small>
        </section>
      )}
      <div className="movie-languages">
        <label htmlFor="movie-original-languages">
          Movie’s original language codes
        </label>
        <input
          id="movie-original-languages"
          value={originals}
          disabled={!enabled || save.isPending}
          placeholder="e.g. en, ko, zh or yue"
          onChange={(event) => {
            setOriginals(event.target.value);
            setDirty(true);
          }}
          aria-describedby="original-language-help"
        />
        <p className="muted" id="original-language-help">
          Video always gets the Original language flag. Audio and subtitles get
          it when their language matches. Saved with your track choices for all
          encodes of this source.
          {job.original_languages?.length
            ? ` Verified codes: ${job.original_languages.map((item) => `${item.name} (${item.code})`).join(", ")}.`
            : ""}
        </p>
        {!originals.trim() && (
          <p className="attention-text">
            Set the movie’s original language to identify matching audio and
            subtitles. Source flags are not used to guess it.
          </p>
        )}
        <span className="track-chip original-language">
          Video · Original language
        </span>
        <small className="muted">
          {" "}
          Flags shown below apply on the next remux.
        </small>
      </div>
      <p className="sr-only" role="status" aria-live="polite">
        {ordering.announcement}
      </p>
      {(["audio", "subtitles"] as const).map((kind) => (
        <section className="track-list" key={kind} data-track-kind={kind}>
          <div className="track-list-heading">
            <h3>{kind === "audio" ? "Audio" : "Subtitles"}</h3>
            <small>
              {(kind === "audio" ? audio : subs).length} selected ·{" "}
              {job.tracks.filter((t) => t.kind === kind).length} available
            </small>
          </div>
          {ordering.groups[kind].map((track) => {
            const id = track.track_id,
              info = track.info;
            const editable = enabled && info.extractable && !save.isPending;
            const overrides = editableChoices ? flags[id] : undefined;
            const effective = { ...info, ...overrides };
            const verified =
              verifiedLanguages[id]?.input === (languages[id] ?? info.language)
                ? verifiedLanguages[id]
                : undefined;
            const suggested = suggestedName(
              verified
                ? {
                    ...track,
                    info: {
                      ...info,
                      base_name: verified.base_name,
                      suggested_name: verified.track_name,
                    },
                  }
                : track,
              overrides,
            );
            const name = editableChoices
              ? (names[id] ?? info.name_override ?? suggested)
              : (info.mux_name ?? suggested);
            const base = languageBase(verified?.code ?? info.language);
            const original =
              base &&
              originals
                .split(/[,\s]+/)
                .filter(Boolean)
                .some((code) => languageBase(code) === base);
            const missing = unresolvedFlags(track, overrides);
            const detection = info.subtitle_detection;
            const description =
              info.track_review?.description ?? detection?.explanation;
            const checked = selected.includes(id);
            return (
              <article
                className={`track-row ${checked ? "selected" : ""} ${!info.extractable ? "unsupported" : ""} ${ordering.dragging === id ? "dragging" : ""} ${ordering.drop?.id === id ? (ordering.drop.after ? "drop-after" : "drop-before") : ""}`}
                key={id}
                data-track-id={id}
                data-track-kind={kind}
              >
                <div className="track-summary">
                  <button
                    type="button"
                    className="secondary track-drag-handle"
                    {...ordering.handleProps(track)}
                  >
                    <span aria-hidden="true">⠿</span>
                  </button>
                  <input
                    id={`include-track-${id}`}
                    type="checkbox"
                    disabled={!editable}
                    checked={checked}
                    onChange={() => toggle(track)}
                  />
                  <div className="track-summary-text">
                    <label
                      className="track-title"
                      htmlFor={`include-track-${id}`}
                    >
                      {name}
                    </label>
                    {info.discovery?.requires_attention && (
                      <span className="track-chip uncertain">
                        PGS ready · Critical issues in report
                      </span>
                    )}
                    {info.origin === "discovery" && (
                      <span className="track-chip discovered-subtitle">
                        Agent-found PGS · Aligned & checked
                      </span>
                    )}
                    {info.origin === "upload" && (
                      <small className="muted upload-filename">
                        {info.discovery
                          ? "Uploaded PGS · Aligned & checked"
                          : "Uploaded"}{" "}
                        · {info.original_filename}
                      </small>
                    )}
                    <div className="track-meta">
                      {checked && (
                        <span className="track-chip">
                          {kind === "audio" ? "Audio" : "Subtitle"}{" "}
                          {ordering.groups[kind]
                            .filter((t) => selected.includes(t.track_id))
                            .findIndex((t) => t.track_id === id) + 1}
                        </span>
                      )}
                      <span>
                        #{id} ·{" "}
                        {verified?.language_name ??
                          info.language_name ??
                          info.language}{" "}
                        ({verified?.code ?? info.language}) · {info.codec}
                      </span>
                      {original && (
                        <span
                          className="track-chip original-language"
                          title="Language matches the movie’s original language; set automatically during remux"
                        >
                          Original language
                        </span>
                      )}
                      {trackFlagLabels
                        .filter(([key]) => effective[key])
                        .map(([key, label]) => (
                          <span className="track-chip" key={key}>
                            {label}
                          </span>
                        ))}
                      {complete && missing.length > 0 && info.extractable && (
                        <span className="track-chip uncertain">
                          {missing.length}{" "}
                          {missing.length === 1 ? "flag" : "flags"} to review
                        </span>
                      )}
                      {!complete && (
                        <span className="track-chip">
                          {info.track_review
                            ? "Reviewed"
                            : detection
                              ? "Language reviewed"
                              : "Preparing"}
                        </span>
                      )}
                      {detection?.language_confident === false &&
                        !info.language_override &&
                        !verified && (
                          <span className="track-chip uncertain">
                            Language uncertain
                          </span>
                        )}
                      {!info.extractable && (
                        <span className="track-chip">Unsupported codec</span>
                      )}
                    </div>
                    {description && (
                      <p className="track-description">{description}</p>
                    )}
                  </div>
                  <button
                    className="secondary track-expand"
                    aria-expanded={!!expanded[id]}
                    aria-controls={`track-details-${id}`}
                    onClick={() =>
                      setExpanded((current) => ({
                        ...current,
                        [id]: !current[id],
                      }))
                    }
                  >
                    {expanded[id] ? "Close" : "Details & edit"}
                  </button>
                </div>
                {expanded[id] && (
                  <div className="track-details" id={`track-details-${id}`}>
                    {info.extractable && (
                      <div className="track-edit-grid">
                        <div className="track-name-field">
                          <label htmlFor={`track-name-${id}`}>
                            Final MKV track name
                          </label>
                          <input
                            id={`track-name-${id}`}
                            value={name}
                            maxLength={255}
                            disabled={!editable}
                            onChange={(e) => {
                              setDirty(true);
                              setNames((current) => ({
                                ...current,
                                [id]: e.target.value,
                              }));
                            }}
                          />
                          {editable && name !== suggested && (
                            <button
                              className="text-button"
                              onClick={() => {
                                setDirty(true);
                                setNames((current) => ({
                                  ...current,
                                  [id]: suggested,
                                }));
                              }}
                            >
                              Use suggested name
                            </button>
                          )}
                        </div>
                        <div className="track-language-field">
                          <label htmlFor={`track-language-${id}`}>
                            Language code
                          </label>
                          <div className="track-language-input">
                            <input
                              id={`track-language-${id}`}
                              value={languages[id] ?? info.language}
                              maxLength={64}
                              disabled={!editable}
                              placeholder="en, ja, zh-Hans, yue-Hant"
                              aria-describedby={`track-language-help-${id}`}
                              aria-invalid={!!languageErrors[id]}
                              onChange={(e) => {
                                setDirty(true);
                                setLanguages((current) => ({
                                  ...current,
                                  [id]: e.target.value,
                                }));
                                setLanguageErrors((current) => ({
                                  ...current,
                                  [id]: "",
                                }));
                              }}
                              onBlur={() => {
                                if (languages[id] !== undefined)
                                  verifyLanguage.mutate({
                                    id,
                                    input: languages[id],
                                  });
                              }}
                            />
                            <button
                              type="button"
                              className="secondary"
                              disabled={!editable || verifyLanguage.isPending}
                              onClick={() =>
                                verifyLanguage.mutate({
                                  id,
                                  input: languages[id] ?? info.language,
                                })
                              }
                            >
                              Verify
                            </button>
                          </div>
                          <small
                            id={`track-language-help-${id}`}
                            className={languageErrors[id] ? "error" : "muted"}
                          >
                            {languageErrors[id] ||
                              (verified
                                ? `Verified: ${verified.code} · ${verified.language_name}`
                                : info.language_override
                                  ? `Manual code: ${info.language_override}`
                                  : `${info.language_name ?? info.language} · Script and region are optional.`)}
                          </small>
                        </div>
                        <fieldset className="track-flags" disabled={!editable}>
                          <legend>Flags</legend>
                          <div className="track-flag-grid">
                            {trackFlagLabels.map(([key, label]) => (
                              <label key={key}>
                                {label}
                                <select
                                  className={
                                    effective[key] == null && editable
                                      ? "needs-input"
                                      : ""
                                  }
                                  aria-label={`${label} for track ${id}`}
                                  value={
                                    effective[key] == null
                                      ? "unknown"
                                      : String(effective[key])
                                  }
                                  onChange={(e) => {
                                    setDirty(true);
                                    setFlags((current) => ({
                                      ...current,
                                      [id]: {
                                        ...current[id],
                                        [key]: e.target.value === "true",
                                      },
                                    }));
                                  }}
                                >
                                  <option value="unknown" disabled>
                                    Choose…
                                  </option>
                                  <option value="true">Yes</option>
                                  <option value="false">No</option>
                                </select>
                              </label>
                            ))}
                          </div>
                        </fieldset>
                      </div>
                    )}
                    <p className="track-downloads">
                      <a
                        href={`/api/jobs/${job.id}/tracks/${id}/download`}
                        download
                      >
                        Download track
                      </a>
                      {info.discovery && (
                        <>
                          {" · "}
                          <a
                            href={`/api/jobs/${job.id}/tracks/${id}/download?variant=original`}
                            download
                          >
                            Original subtitle
                          </a>
                          {" · "}
                          <a
                            href={`/api/jobs/${job.id}/tracks/${id}/download?variant=cleaned`}
                            download
                          >
                            Cleaned SRT
                          </a>
                        </>
                      )}
                    </p>
                    {description && <p>{description}</p>}
                    {info.discovery && (
                      <SubtitleProvenance value={info.discovery} />
                    )}
                    {info.track_review && (
                      <p className="muted">
                        {info.track_review.flag_explanation} · Confidence:{" "}
                        {info.track_review.confidence}
                      </p>
                    )}
                    {detection && (
                      <div className="track-evidence">
                        <strong>Subtitle content</strong>
                        <p>{detection.explanation}</p>
                        <small>
                          {detection.method} · {detection.sampled_cues}/
                          {detection.unique_cues} distinct cues · SDH:{" "}
                          {detection.hearing_impaired == null
                            ? "uncertain"
                            : detection.hearing_impaired
                              ? "yes"
                              : "no"}
                        </small>
                      </div>
                    )}
                    <small>
                      {[
                        info.channel_layout,
                        info.bit_depth && `${info.bit_depth}-bit`,
                        info.sample_rate && `${info.sample_rate} Hz`,
                        info.bitrate && `${info.bitrate} bps`,
                        (info.source_subtitle_metadata?.name || info.name) &&
                          `Source label: ${info.source_subtitle_metadata?.name || info.name}`,
                      ]
                        .filter(Boolean)
                        .join(" · ")}
                    </small>
                    {info.audio_analysis && (
                      <details className="track-evidence">
                        <summary>
                          Local audio evidence ·{" "}
                          {info.audio_analysis.sampled_seconds ?? 0}s sampled
                        </summary>
                        <p>{info.audio_analysis.method}</p>
                        {info.audio_analysis.limitations.map((text) => (
                          <p key={text}>{text}</p>
                        ))}
                        {info.audio_analysis.samples.map((sample) => (
                          <div key={sample.id}>
                            <small>
                              Sample {sample.id} ·{" "}
                              {sample.start_seconds.toFixed(1)}s ·{" "}
                              {sample.duration_seconds.toFixed(1)}s duration{" "}
                              {sample.detected_language &&
                                `· ${sample.detected_language} (${Math.round((sample.language_probability ?? 0) * 100)}%)`}
                            </small>
                            <p>
                              {sample.segments
                                ?.map((segment) => segment.text)
                                .join(" ") || "No speech transcript available."}
                            </p>
                          </div>
                        ))}
                      </details>
                    )}
                  </div>
                )}
              </article>
            );
          })}
          {job.tracks.every((t) => t.kind !== kind) && (
            <p className="muted">
              {complete ? "No tracks available." : "Reading the source…"}
            </p>
          )}
        </section>
      ))}
      <div
        className={`track-confirm ${enabled && (!job.track_selection || ordering.changed || job.subtitle_discovery?.review_required) ? "needs-input" : ""}`}
      >
        <div>
          <strong>
            {audio.length} audio · {subs.length} subtitles
          </strong>
          {ordering.changed && (
            <p className="attention-text">
              Track order changed. Confirm your choices to save.
            </p>
          )}
          {unknown && (
            <p className="error">
              Choose the unknown flags in the selected rows.
            </p>
          )}
          {unknownLanguage && (
            <p className="error">
              Verify a language code for uncertain tracks, or skip them.
            </p>
          )}
          {unverifiedLanguage && (
            <p className="error">
              Verify the changed language codes before saving.
            </p>
          )}
          {invalidName && (
            <p className="error">Selected tracks need a nonempty name.</p>
          )}
        </div>
        <button
          disabled={
            !enabled ||
            staleChoices ||
            ordering.dragging !== null ||
            save.isPending ||
            unknown ||
            unknownLanguage ||
            unverifiedLanguage ||
            invalidName
          }
          onClick={() => save.mutate()}
        >
          {save.isPending
            ? "Saving…"
            : remuxEditing
              ? "Save choices & remux"
              : job.track_selection
                ? "Update track choices"
                : "Confirm tracks →"}
        </button>
      </div>
      {save.isSuccess && !ordering.changed && (
        <p role="status">
          Track choices saved for all encodes of this source that have not
          started preparation for remux.
        </p>
      )}
      {save.error && (
        <p className="error" role="alert">
          {String(save.error)}
        </p>
      )}
      <AgentTranscript job={job} subtitles />
    </div>
  );
}
