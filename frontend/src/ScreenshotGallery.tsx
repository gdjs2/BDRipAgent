import { useEffect, useState, type ReactNode } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api, clock, readable } from "./api";
import type { Job, Shot } from "./types";

type View = "best" | "shortlist" | "new" | "final";
type GalleryData = {
  candidates: number;
  shortlisted: number;
  recommended: number;
  final: number;
  items: Shot[];
};

export function ScreenshotGallery({
  job,
  controls,
}: {
  job: Job;
  controls: ReactNode;
}) {
  const query = useQueryClient();
  const target = job.screenshot_policy.best_count ?? 30;
  const strategy = job.screenshot_policy.strategy ?? "local";
  const [bestCount, setBestCount] = useState(target);
  useEffect(() => setBestCount(target), [target, job.id]);
  const validCount =
    Number.isInteger(bestCount) && bestCount >= 2 && bestCount <= 40;
  const reviewing = job.tasks.some(
    (task) =>
      [
        "generate_candidates",
        "select_screenshots",
        "sync_screenshots",
      ].includes(task.type) && task.status === "RUNNING",
  );
  const saveCount = useMutation({
    mutationFn: () =>
      api<Job>(
        `/jobs/${job.id}/screenshots/best-count`,
        { best_count: bestCount },
        "PATCH",
      ),
    onSuccess: (updated) => query.setQueryData(["job", job.id], updated),
  });
  const saveStrategy = useMutation({
    mutationFn: (strategy: "local" | "agent") =>
      api<Job>(`/jobs/${job.id}/screenshots/strategy`, { strategy }, "PATCH"),
    onSuccess: (updated) => query.setQueryData(["job", job.id], updated),
  });
  const data = useQuery({
    queryKey: ["screenshots", job.id],
    queryFn: () => api<GalleryData>(`/jobs/${job.id}/screenshots`),
    refetchInterval: 5000,
  });
  const [view, setView] = useState<View>(
    ["WAITING_FOR_RELEASE_DETAILS", "GENERATING_RELEASE", "COMPLETE"].includes(
      job.state,
    )
      ? "final"
      : strategy === "local"
        ? "shortlist"
        : "best",
  );
  const [openId, setOpenId] = useState<string | null>(null);
  const [draft, setDraft] = useState<number[] | null>(null);
  const items = data.data?.items ?? [];
  const best = items
    .filter((s) => s.info.recommendation_rank)
    .sort((a, b) => a.info.recommendation_rank! - b.info.recommendation_rank!);
  const newCandidates = new Set(
    job.analysis.screenshot_more?.candidate_ids ?? [],
  );
  const shortlist = items
    .filter((s) => s.shortlisted)
    .sort((a, b) => a.info.source_frame_number - b.info.source_frame_number);
  const latestBatch = shortlist.filter((s) =>
    newCandidates.has(s.candidate_id),
  );
  const final = items.filter((s) => s.selected);
  const displayed =
    view === "best"
      ? best
      : view === "shortlist"
        ? shortlist
        : view === "new"
          ? latestBatch
          : final;
  const chosen = (
    draft ?? best.filter((s) => s.selected).map((s) => s.candidate_id)
  ).filter((id) => best.some((s) => s.candidate_id === id && !s.reservation));
  const canEdit = [
    "WAITING_FOR_SCREENSHOT_SELECTION",
    "WAITING_FOR_RELEASE_DETAILS",
    "COMPLETE",
  ].includes(job.state);
  const canChoose = canEdit && best.length > 0;
  const imageURL = (path?: string) => {
    const artifact = path && job.artifacts.find((a) => a.path === path);
    return artifact ? `/api/artifacts/${artifact.id}` : undefined;
  };
  const choose = useMutation({
    mutationFn: () =>
      api<Job>(`/jobs/${job.id}/screenshots/selection`, {
        candidate_ids: chosen,
      }),
    onSettled: () => query.invalidateQueries({ queryKey: ["screenshots"] }),
    onSuccess: (updated) => {
      query.setQueryData(["job", job.id], updated);
      query.invalidateQueries({ queryKey: ["screenshots", job.id] });
      setDraft(null);
      setOpenId(null);
      setView("final");
    },
  });
  const prepare = useMutation({
    mutationFn: (resample: boolean) =>
      api(`/jobs/${job.id}/screenshots/review`, {
        best_count: bestCount,
        strategy,
        resample,
      }),
    onSuccess: () => {
      query.invalidateQueries({ queryKey: ["job", job.id] });
      setDraft(null);
      setOpenId(null);
      setView(strategy === "local" ? "shortlist" : "best");
    },
  });
  const more = useMutation({
    mutationFn: () => api(`/jobs/${job.id}/screenshots/more`, { count: 15 }),
    onSuccess: () => {
      query.invalidateQueries({ queryKey: ["job", job.id] });
      setOpenId(null);
      setView("new");
    },
  });
  const curate = useMutation({
    onMutate: () => query.cancelQueries({ queryKey: ["screenshots", job.id] }),
    mutationFn: (candidate_ids: number[]) =>
      api<GalleryData>(
        `/jobs/${job.id}/screenshots/best`,
        { candidate_ids },
        "PATCH",
      ),
    onSuccess: (updated) => {
      query.setQueryData(["screenshots", job.id], updated);
      setDraft(
        chosen.filter((id) =>
          updated.items.some(
            (shot) => shot.candidate_id === id && shot.info.recommendation_rank,
          ),
        ),
      );
      choose.reset();
    },
  });
  const editing =
    curate.isPending ||
    choose.isPending ||
    prepare.isPending ||
    more.isPending ||
    saveCount.isPending ||
    saveStrategy.isPending;
  function editBest(id: number, add: boolean) {
    const ids = best.map((s) => s.candidate_id);
    curate.mutate(add ? [...ids, id] : ids.filter((value) => value !== id));
  }
  function toggle(id: number) {
    setDraft(
      chosen.includes(id)
        ? chosen.filter((value) => value !== id)
        : [...chosen, id],
    );
    choose.reset();
  }
  const open = items.find((s) => s.id === openId);
  const comparisons =
    open &&
    (view === "final"
      ? open.info.comparisons
      : (open.info.review_comparisons ?? open.info.comparisons));
  return (
    <div className="screenshot-review">
      <h2>Screenshot review</h2>
      <p>
        Keep up to 40 best choices, remove any you do not want, or add
        replacements from the full shortlist. Choose 1–15 final pairs. Both
        images in every pair must be B-frames. Frames reserved by the other
        codec, including nearby frames within the required spacing, cannot be
        selected.
      </p>
      <p className="muted">
        Sampling is shared by all jobs using this source. New batches appear in
        each encode’s shortlist in source frame order after its frame checks.
        Best and final choices stay separate for each encode.
      </p>
      {controls}
      <section className="screenshot-best-settings">
        <label>
          Screenshot selection strategy
          <select
            aria-label="Screenshot selection strategy"
            value={strategy}
            disabled={reviewing || editing}
            onChange={(e) =>
              saveStrategy.mutate(e.target.value as "local" | "agent")
            }
          >
            <option value="local">
              Local filtering · 200 scene samples (default)
            </option>
            <option value="agent">Agent review · adaptive sampling</option>
          </select>
        </label>
        <p className="muted">
          {strategy === "local"
            ? "Sample 200 regions across the movie and filter them locally. Best starts empty: open Shortlist and use Add to best for each frame you want. Local checks cannot judge spoilers or reliably identify credits. No agent is used."
            : "The agent visually reviews candidates and samples fresh windows if fewer than the requested best choices are suitable. Default: 30 choices; up to four sampling rounds and two hours."}
        </p>
        {saveStrategy.error && (
          <p className="error">{saveStrategy.error.message}</p>
        )}
        {strategy === "agent" && (
          <>
            <label>
              Best screenshot candidates
              <input
                type="number"
                min="2"
                max="40"
                step="1"
                required
                value={bestCount}
                disabled={reviewing || editing}
                onChange={(e) => {
                  setBestCount(Number(e.target.value));
                  saveCount.reset();
                }}
              />
            </label>
            <button
              className="secondary"
              disabled={
                reviewing || editing || !validCount || bestCount === target
              }
              onClick={() => saveCount.mutate()}
            >
              {saveCount.isPending ? "Saving…" : "Save candidate count"}
            </button>
            <p className="muted">
              Rank 2–40 best choices per encode, then choose your final pairs
              (for example, 7 from each codec). Refresh reuses prepared
              candidates. Sample again explores new moments across the movie. If
              too few suitable frames remain, the review reports the shortfall.
            </p>
          </>
        )}
        {reviewing && (
          <p role="status">
            Screenshot processing is running. Settings can be changed when it
            finishes or is cancelled.
          </p>
        )}
        {saveCount.isSuccess && (
          <p role="status">Candidate count saved for the next review.</p>
        )}
      </section>
      {more.error && <p className="error">{more.error.message}</p>}
      {job.analysis.screenshot_more && (
        <p
          className={
            job.analysis.screenshot_more.warning
              ? "callout needs-input"
              : "muted"
          }
        >
          {job.analysis.screenshot_more.warning ??
            `Added ${job.analysis.screenshot_more.added} screenshots to Shortlist. Add any you like to Best.`}
        </p>
      )}
      {job.analysis.screenshot_review?.warning && (
        <p className="callout needs-input">
          {job.analysis.screenshot_review.warning}
        </p>
      )}
      {(canEdit ||
        [
          "SCREENSHOT_AGENT_SELECTION",
          "SCREENSHOT_CANDIDATE_GENERATION",
        ].includes(job.state)) && (
        <div className="screenshot-actions">
          <button
            className="secondary"
            disabled={
              editing ||
              reviewing ||
              !validCount ||
              job.tasks.some(
                (t) => t.status === "QUEUED" || t.status === "RUNNING",
              )
            }
            onClick={() => more.mutate()}
          >
            {more.isPending
              ? "Queuing more screenshots…"
              : "Find 15 more screenshots"}
          </button>
          <p className="muted">
            Sample new moments and append up to 15 quality-checked frames. Your
            Best list and final choices are kept.
          </p>
          {!canEdit && (
            <button
              className="secondary"
              disabled={
                editing ||
                reviewing ||
                !validCount ||
                job.tasks.some(
                  (t) => t.status === "QUEUED" || t.status === "RUNNING",
                )
              }
              onClick={() => prepare.mutate(false)}
            >
              Retry with selected strategy
            </button>
          )}
        </div>
      )}
      {job.state === "WAITING_FOR_RELEASE_DETAILS" && (
        <div className="callout needs-input">
          <h3>Your final screenshots are ready</h3>
          <p>
            Add the Chinese name, source, extra description, and tracker to
            generate release files, with optional screenshot uploads.
          </p>
          <Link className="button" to={`/jobs/${job.id}/release`}>
            Continue to release →
          </Link>
        </div>
      )}
      <div
        className="screenshot-views"
        role="group"
        aria-label="Screenshot views"
      >
        {(
          [
            ["best", `Best ${best.length}`],
            ["shortlist", `Shortlist (${shortlist.length})`],
            ["new", `New batch (${latestBatch.length})`],
            ["final", `Final (${final.length})`],
          ] as [View, string][]
        ).map(([value, label]) => (
          <button
            key={value}
            className={view === value ? "" : "secondary"}
            aria-pressed={view === value}
            onClick={() => setView(value)}
          >
            {label}
          </button>
        ))}
      </div>
      {strategy === "local" && view === "best" && !best.length && (
        <section className="callout needs-input">
          <h3>Your Best list is empty</h3>
          <p>
            Open Shortlist and use Add to best to collect the frames you like.
          </p>
          <button className="secondary" onClick={() => setView("shortlist")}>
            Browse shortlist
          </button>
        </section>
      )}
      {strategy === "agent" &&
        view === "best" &&
        !best.length &&
        job.state === "COMPLETE" &&
        shortlist.length > 0 && (
          <section>
            <h3>Prepare the best {bestCount} from your saved shortlist</h3>
            <p>
              Your existing final images stay available. Rank the saved
              candidates using your chosen strategy and prepare comparison pairs
              for review.
            </p>
            <button
              disabled={editing || !validCount}
              onClick={() => prepare.mutate(false)}
            >
              {prepare.isPending
                ? "Queuing review…"
                : `Prepare best ${bestCount}`}
            </button>
          </section>
        )}
      {canChoose && view === "best" && (
        <section
          className={`screenshot-choice-bar ${job.state === "WAITING_FOR_SCREENSHOT_SELECTION" ? "needs-input" : ""}`}
        >
          <div>
            <h3>{chosen.length} of up to 15 pairs chosen</h3>
            <p>
              Select your favorites in Best {best.length}. Frame spacing and
              B-frame checks still apply.
            </p>
          </div>
          <div className="screenshot-actions">
            {strategy === "agent" && (
              <button
                className="secondary"
                disabled={editing || !validCount}
                onClick={() => prepare.mutate(false)}
              >
                {prepare.isPending
                  ? "Queuing review…"
                  : `Refresh best ${bestCount}`}
              </button>
            )}
            <button
              className="secondary"
              disabled={editing}
              onClick={() => {
                setDraft(
                  best
                    .filter((s) => !s.reservation)
                    .slice(0, 15)
                    .map((s) => s.candidate_id),
                );
                choose.reset();
              }}
            >
              Choose up to 15 available
            </button>
            <button
              className="secondary"
              disabled={editing}
              onClick={() => {
                setDraft([]);
                choose.reset();
              }}
            >
              Clear choices
            </button>
            <button
              disabled={!chosen.length || chosen.length > 15 || editing}
              onClick={() => choose.mutate()}
            >
              {choose.isPending
                ? "Saving…"
                : `Confirm ${chosen.length} & render`}
            </button>
          </div>
        </section>
      )}
      {[
        data.error,
        choose.error,
        prepare.error,
        curate.error,
        saveCount.error,
      ].map(
        (error, index) =>
          error && (
            <p className="error" role="alert" key={index}>
              {error.message}
            </p>
          ),
      )}
      {view === "shortlist" && (
        <p className="muted">
          Browse the available quality-checked source frames and use Add to best
          for your favorites. New batches appear first. Click a frame for its
          full-resolution preview; comparison pairs are rendered when you
          confirm your final choices. Best can hold up to 40 frames.
        </p>
      )}
      {!displayed.length && (
        <div className="empty">
          <h3>
            {view === "final"
              ? "No final pairs chosen yet"
              : "Review images will appear here"}
          </h3>
          <p>
            {job.state === "WAITING_FOR_SCREENSHOT_SELECTION"
              ? "Open Best to choose your final pairs."
              : "Screenshot review follows validation and remuxing."}
          </p>
        </div>
      )}
      <section
        aria-label={
          view === "new" ? "New screenshot batch" : "Screenshot gallery"
        }
        className={view === "new" ? "screenshot-new-batch" : undefined}
      >
        {view === "new" && (
          <div className="section-heading">
            <div>
              <h3>Latest added screenshots</h3>
              <p className="muted">
                {latestBatch.length
                  ? "Only the latest additions are shown here, in frame order. Add your favorites to Best; they also remain in the full shortlist."
                  : reviewing || more.isPending
                    ? "Preparing additional frames. They will appear here when the batch is ready."
                    : "No additional batch yet. Use Find 15 more screenshots to explore more frames."}
              </p>
            </div>
            <button className="secondary" onClick={() => setView("best")}>
              Open Best ({best.length})
            </button>
          </div>
        )}
        <div className="gallery">
          {displayed.map((shot) => (
            <div
              key={shot.id}
              className={`review-tile ${chosen.includes(shot.candidate_id) && view === "best" ? "chosen" : ""}`}
            >
              <button className="shot-card" onClick={() => setOpenId(shot.id)}>
                <img
                  loading="lazy"
                  src={imageURL(
                    shot.info.thumbnail ??
                      shot.info.comparisons?.src ??
                      shot.info.path,
                  )}
                  alt={`Source frame ${shot.info.source_frame_number}`}
                />
                <div>
                  <small>
                    {newCandidates.has(shot.candidate_id) && "NEW BATCH · "}
                    {shot.info.recommendation_rank &&
                      `BEST #${shot.info.recommendation_rank} · `}
                    {clock(shot.info.timeline_seconds)} · FRAME{" "}
                    {shot.info.source_frame_number}
                  </small>
                  <h3>
                    Candidate #{shot.candidate_id}
                    {shot.info.category
                      ? ` · ${readable(shot.info.category)}`
                      : ""}
                  </h3>
                  <p>{shot.info.reason ?? "Source shortlist candidate"}</p>
                </div>
              </button>
              {shot.reservation && (
                <p className="muted" role="status">
                  Reserved by {shot.reservation.codec}: frame{" "}
                  {shot.reservation.frame_number}. Keep at least{" "}
                  {shot.reservation.spacing_seconds} seconds apart.
                </p>
              )}
              {canEdit && view !== "final" && (
                <div className="screenshot-curation">
                  {shot.info.recommendation_rank ? (
                    <button
                      className="secondary"
                      disabled={editing}
                      aria-label={`Remove candidate ${shot.candidate_id} from best`}
                      onClick={() => editBest(shot.candidate_id, false)}
                    >
                      Remove from best
                    </button>
                  ) : (
                    <button
                      className="secondary"
                      disabled={
                        editing ||
                        best.length >= 40 ||
                        Boolean(shot.reservation)
                      }
                      aria-label={`Add candidate ${shot.candidate_id} to best`}
                      onClick={() => editBest(shot.candidate_id, true)}
                    >
                      {best.length >= 40
                        ? "Best list full (40/40)"
                        : "Add to best"}
                    </button>
                  )}
                </div>
              )}
              {view === "best" && canChoose && (
                <label className="screenshot-pick">
                  <input
                    type="checkbox"
                    aria-label={`Choose candidate ${shot.candidate_id}`}
                    checked={chosen.includes(shot.candidate_id)}
                    disabled={editing || Boolean(shot.reservation)}
                    onChange={() => toggle(shot.candidate_id)}
                  />
                  Choose this pair
                </label>
              )}
            </div>
          ))}
        </div>
      </section>
      {open && (
        <div
          className="modal"
          role="dialog"
          aria-modal="true"
          aria-label="Screenshot comparison"
          onClick={() => setOpenId(null)}
        >
          <div
            className="modal-inner"
            onClick={(event) => event.stopPropagation()}
          >
            <div className="section-heading">
              <h2>
                Candidate #{open.candidate_id} · Frame{" "}
                {open.info.source_frame_number} ·{" "}
                {clock(open.info.timeline_seconds)}
              </h2>
              <button className="secondary" onClick={() => setOpenId(null)}>
                Close ×
              </button>
            </div>
            <div className={comparisons ? "two-column" : ""}>
              {(comparisons
                ? (["src", "encode"] as const)
                : (["src"] as const)
              ).map((type) => {
                const path = comparisons?.[type] ?? open.info.path;
                return (
                  <div key={type}>
                    <h3>
                      {type === "src"
                        ? "Source"
                        : job.analysis.smoke_test
                          ? "Smoke test — source reused"
                          : job.release_name}
                    </h3>
                    <a href={imageURL(path)} target="_blank" rel="noreferrer">
                      <img
                        className="comparison"
                        src={imageURL(path)}
                        alt={
                          type === "src"
                            ? "Full-resolution source"
                            : "Full-resolution encode"
                        }
                      />
                    </a>
                  </div>
                );
              })}
            </div>
            {!comparisons && (
              <p className="muted">
                Source shortlist preview. Add this frame to your best list and
                confirm your choices to render its comparison pair.
              </p>
            )}
            <p>{open.info.reason}</p>
            <small>
              Source PTS: {open.info.source_pts_seconds.toFixed(6)} seconds ·
              B-frames verified in both videos
            </small>
            {canChoose && view === "best" && open.info.recommendation_rank && (
              <label className="screenshot-pick">
                <input
                  type="checkbox"
                  aria-label={`Choose candidate ${open.candidate_id} in preview`}
                  checked={chosen.includes(open.candidate_id)}
                  disabled={editing || Boolean(open.reservation)}
                  onChange={() => toggle(open.candidate_id)}
                />
                Choose this pair
              </label>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
