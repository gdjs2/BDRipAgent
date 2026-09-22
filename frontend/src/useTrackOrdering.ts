import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type KeyboardEvent,
  type PointerEvent,
} from "react";
import type { Job, Track } from "./types";
import { orderedTracks, moveTrack } from "./track-selection";

type Drag = {
  pointerId: number;
  id: number;
  kind: string;
  x: number;
  y: number;
  startX: number;
  startY: number;
  active: boolean;
};
type Drop = { id: number; after: boolean };

export function useTrackOrdering(
  tracks: Track[],
  selection: Job["track_selection"],
  enabled: boolean,
) {
  const [order, setOrder] = useState<Record<string, number[]>>(() => ({
    audio: selection?.audio_track_ids ?? [],
    subtitles: selection?.subtitle_track_ids ?? [],
  }));
  const [changed, setChanged] = useState(false);
  const [announcement, setAnnouncement] = useState("");
  const pointer = useRef<Drag | null>(null);
  const target = useRef<Drop | null>(null);
  const [dragging, setDragging] = useState<number | null>(null);
  const [drop, setDrop] = useState<Drop | null>(null);
  const groups = {
    audio: orderedTracks(tracks, "audio", order.audio),
    subtitles: orderedTracks(tracks, "subtitles", order.subtitles),
  };

  function cancel() {
    pointer.current = null;
    target.current = null;
    setDragging(null);
    setDrop(null);
  }
  function locateTarget() {
    const drag = pointer.current;
    if (!drag?.active) return;
    const row = document
      .elementFromPoint(drag.x, drag.y)
      ?.closest<HTMLElement>("[data-track-id]");
    const id = row ? Number(row.dataset.trackId) : null;
    const next =
      row && row.dataset.trackKind === drag.kind && id !== drag.id
        ? {
            id: id!,
            after:
              drag.y >
              row.getBoundingClientRect().top +
                row.getBoundingClientRect().height / 2,
          }
        : null;
    target.current = next;
    setDrop((previous) =>
      previous?.id === next?.id && previous?.after === next?.after
        ? previous
        : next,
    );
  }
  useEffect(() => {
    if (!enabled) cancel();
  }, [enabled]);
  useEffect(() => {
    if (dragging === null) return;
    let frame: number;
    const scroll = () => {
      const drag = pointer.current;
      if (!drag?.active) return;
      const distance =
        drag.y < 70
          ? -Math.min(16, (70 - drag.y) / 4)
          : drag.y > innerHeight - 70
            ? Math.min(16, (drag.y - innerHeight + 70) / 4)
            : 0;
      if (distance) {
        window.scrollBy(0, distance);
        locateTarget();
      }
      frame = requestAnimationFrame(scroll);
    };
    frame = requestAnimationFrame(scroll);
    return () => cancelAnimationFrame(frame);
  }, [dragging]);

  function move(track: Track, destination: Drop) {
    if (!enabled) return;
    const current = orderedTracks(tracks, track.kind, order[track.kind]).map(
      (t) => t.track_id,
    );
    const next = moveTrack(
      current,
      track.track_id,
      destination.id,
      destination.after,
    );
    if (next.every((id, index) => id === current[index])) return;
    setOrder((value) => ({ ...value, [track.kind]: next }));
    setChanged(true);
    setAnnouncement(
      `${track.kind === "audio" ? "Audio" : "Subtitle"} track #${track.track_id} moved to position ${next.indexOf(track.track_id) + 1}. Confirm your track choices to save the order.`,
    );
  }
  function handleProps(track: Track) {
    return {
      disabled: !enabled || !track.info.extractable,
      "aria-label": `Reorder ${track.kind === "audio" ? "audio" : "subtitle"} track ${track.track_id}`,
      "aria-describedby": "track-order-help",
      title: "Drag to reorder, or use the Up and Down arrow keys",
      onPointerDown(event: PointerEvent<HTMLButtonElement>) {
        if (!enabled || !event.isPrimary || event.button !== 0) return;
        event.preventDefault();
        event.currentTarget.focus();
        event.currentTarget.setPointerCapture(event.pointerId);
        pointer.current = {
          pointerId: event.pointerId,
          id: track.track_id,
          kind: track.kind,
          x: event.clientX,
          y: event.clientY,
          startX: event.clientX,
          startY: event.clientY,
          active: false,
        };
      },
      onPointerMove(event: PointerEvent<HTMLButtonElement>) {
        const drag = pointer.current;
        if (
          !drag ||
          drag.id !== track.track_id ||
          drag.pointerId !== event.pointerId
        )
          return;
        drag.x = event.clientX;
        drag.y = event.clientY;
        if (
          !drag.active &&
          Math.hypot(drag.x - drag.startX, drag.y - drag.startY) < 5
        )
          return;
        drag.active = true;
        setDragging(drag.id);
        locateTarget();
      },
      onPointerUp(event: PointerEvent<HTMLButtonElement>) {
        if (pointer.current?.pointerId !== event.pointerId) return;
        if (pointer.current.active) {
          pointer.current.x = event.clientX;
          pointer.current.y = event.clientY;
          locateTarget();
          if (target.current) move(track, target.current);
        }
        cancel();
        if (event.currentTarget.hasPointerCapture(event.pointerId))
          event.currentTarget.releasePointerCapture(event.pointerId);
      },
      onPointerCancel: cancel,
      onLostPointerCapture: cancel,
      onKeyDown(event: KeyboardEvent<HTMLButtonElement>) {
        if (event.key === "Escape") {
          cancel();
          return;
        }
        if (
          !enabled ||
          !["ArrowUp", "ArrowDown", "Home", "End"].includes(event.key)
        )
          return;
        event.preventDefault();
        cancel();
        const group = orderedTracks(tracks, track.kind, order[track.kind]);
        const index = group.findIndex((t) => t.track_id === track.track_id);
        const after = ["ArrowDown", "End"].includes(event.key);
        const nextIndex =
          event.key === "Home"
            ? 0
            : event.key === "End"
              ? group.length - 1
              : index + (after ? 1 : -1);
        if (group[nextIndex])
          move(track, { id: group[nextIndex].track_id, after });
      },
    };
  }
  const reset = useCallback((saved: Job["track_selection"]) => {
    setOrder({
      audio: saved?.audio_track_ids ?? [],
      subtitles: saved?.subtitle_track_ids ?? [],
    });
    setChanged(false);
    setAnnouncement("");
  }, []);
  return {
    reset,
    groups,
    changed,
    announcement,
    dragging,
    drop,
    handleProps,
    markSaved: () => {
      setChanged(false);
      setAnnouncement("Track order saved.");
    },
  };
}
