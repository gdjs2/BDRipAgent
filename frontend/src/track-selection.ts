import type { Job, Track, TrackFlag } from "./types";

export const trackFlagLabels: [TrackFlag, string][] = [
  ["default", "Default"],
  ["forced", "Forced"],
  ["hearing_impaired", "SDH"],
  ["visual_impaired", "Audio description"],
  ["commentary", "Commentary"],
];

export function analysisComplete(job: Job): boolean {
  // Older API responses can still contain a completed review. Confidence and
  // schema upgrades are not reasons to start the same analysis again.
  return (
    job.track_analysis_complete ??
    (job.analysis.track_review_version === 1 &&
      job.tracks.every(
        (t) =>
          ["upload", "discovery"].includes(t.info.origin ?? "") ||
          (Boolean(t.info.track_review) &&
            (t.info.codec_id !== "S_HDMV/PGS" ||
              Boolean(t.info.subtitle_detection))),
      ))
  );
}

export function hasAnalysisAttempt(job: Job): boolean {
  return job.tasks.some(
    (task) =>
      task.type === "review_tracks" ||
      (task.type === "analyze" && task.status !== "SUCCEEDED"),
  );
}

export function unresolvedFlags(
  track: Track,
  overrides: Partial<Record<TrackFlag, boolean>> = {},
): TrackFlag[] {
  return trackFlagLabels
    .map(([key]) => key)
    .filter((key) => (overrides[key] ?? track.info[key]) == null);
}

export function suggestedName(
  track: Track,
  overrides: Partial<Record<TrackFlag, boolean>> = {},
): string {
  const info = { ...track.info, ...overrides };
  return track.kind === "subtitles" && info.base_name
    ? [info.base_name, info.hearing_impaired && "SDH", info.forced && "Forced"]
        .filter(Boolean)
        .join(" ")
    : (info.suggested_name ?? info.mux_name ?? info.name ?? "Unnamed track");
}

// Preserve saved/custom order while newly discovered tracks arrive during analysis.
export function orderedTracks(
  tracks: Track[],
  kind: string,
  preferred: number[] = [],
): Track[] {
  const available = tracks
    .filter((track) => track.kind === kind)
    .sort(
      (a, b) =>
        (a.info?.source_order ?? a.track_id) -
        (b.info?.source_order ?? b.track_id),
    );
  const byId = new Map(available.map((track) => [track.track_id, track]));
  return [
    ...new Set([...preferred, ...available.map((track) => track.track_id)]),
  ]
    .map((id) => byId.get(id))
    .filter((track): track is Track => Boolean(track));
}

export function moveTrack(
  ids: number[],
  source: number,
  target: number,
  after: boolean,
): number[] {
  if (source === target || !ids.includes(source) || !ids.includes(target))
    return ids;
  const result = ids.filter((id) => id !== source);
  result.splice(result.indexOf(target) + Number(after), 0, source);
  return result;
}
