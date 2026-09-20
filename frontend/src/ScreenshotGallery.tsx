import { useState, type ReactNode } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api, clock, readable } from "./api";
import type { Job, Shot } from "./types";

type View = "best" | "shortlist" | "final";
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
      : "best",
  );
  const [openId, setOpenId] = useState<string | null>(null);
  const [draft, setDraft] = useState<number[] | null>(null);
  const items = data.data?.items ?? [];
  const best = items
    .filter((s) => s.info.recommendation_rank)
    .sort((a, b) => a.info.recommendation_rank! - b.info.recommendation_rank!);
  const shortlist = items.filter((s) => s.shortlisted);
  const final = items.filter((s) => s.selected);
  const displayed =
    view === "best" ? best : view === "shortlist" ? shortlist : final;
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
    mutationFn: () => api(`/jobs/${job.id}/screenshots/review`, {}),
    onSuccess: () => {
      query.invalidateQueries({ queryKey: ["job", job.id] });
      setDraft(null);
      setOpenId(null);
      setView("best");
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
  const editing = curate.isPending || choose.isPending || prepare.isPending;
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
        Keep up to 15 best choices, remove any you do not want, or add
        replacements from the full shortlist. Choose 1–15 final pairs. Both
        images in every pair must be B-frames. Frames reserved by the other
        codec, including nearby frames within the required spacing, cannot be
        selected.
      </p>
      {controls}
      {job.state === "WAITING_FOR_RELEASE_DETAILS" && (
        <div className="callout">
          <h3>Your final screenshots are ready</h3>
          <p>
            Add the Chinese name, source, extra description, and tracker to
            generate release files and upload your chosen pairs.
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
            ["best", `Best ${best.length || 15}`],
            ["shortlist", `Shortlist (${shortlist.length})`],
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
      {view === "best" &&
        !best.length &&
        job.state === "COMPLETE" &&
        shortlist.length > 0 && (
          <section>
            <h3>Prepare the best 15 from your saved shortlist</h3>
            <p>
              Your existing final images stay available. The agent will rank the
              saved shortlist and prepare comparison pairs for you to review.
            </p>
            <button disabled={editing} onClick={() => prepare.mutate()}>
              {prepare.isPending ? "Queuing review…" : "Prepare best 15"}
            </button>
          </section>
        )}
      {canChoose && (
        <section className="screenshot-choice-bar">
          <div>
            <h3>{chosen.length} of up to 15 pairs chosen</h3>
            <p>
              Select your favorites in Best {best.length}. Frame spacing and
              B-frame checks still apply.
            </p>
          </div>
          <div className="screenshot-actions">
            <button
              className="secondary"
              disabled={editing}
              onClick={() => prepare.mutate()}
            >
              {prepare.isPending ? "Queuing review…" : "Refresh best 15"}
            </button>
            <button
              className="secondary"
              disabled={editing}
              onClick={() => {
                setDraft(
                  best.filter((s) => !s.reservation).map((s) => s.candidate_id),
                );
                choose.reset();
              }}
            >
              Choose all available
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
      {[data.error, choose.error, prepare.error, curate.error].map(
        (error, index) =>
          error && (
            <p className="error" role="alert" key={index}>
              {error.message}
            </p>
          ),
      )}
      {view === "shortlist" && (
        <p className="muted">
          Up to 40 source frames considered by the agent. Click any frame to
          inspect its full-resolution image. Best choices also have
          source/encode comparisons. Newly added choices get comparison pairs
          when you confirm and render. Remove a best choice first if the list is
          full.
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
              ? "Open Best 15 to choose your final pairs."
              : "Screenshot review follows validation and remuxing."}
          </p>
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
                      editing || best.length >= 15 || Boolean(shot.reservation)
                    }
                    aria-label={`Add candidate ${shot.candidate_id} to best`}
                    onClick={() => editBest(shot.candidate_id, true)}
                  >
                    {best.length >= 15
                      ? "Best list full (15/15)"
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
            {canChoose && open.info.recommendation_rank && (
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
