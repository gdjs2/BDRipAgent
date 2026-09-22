import {
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type PointerEvent,
} from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "./api";
import { CpuHistory } from "./CpuHistory";
import { cpuLoad, type CpuHistoryPoint } from "./cpu-history";

type CpuSample = {
  available: boolean;
  percent: number | null;
  cores: { id: number; percent: number | null }[];
  logical_cores: number;
  frequency_mhz?: number | null;
  load_average?: {
    one_minute: number;
    five_minutes: number;
    fifteen_minutes: number;
  } | null;
  sampled_at: string | null;
};
const percent = (value: number | null | undefined) =>
  value == null ? "—" : `${Math.round(value)}%`;

export function CpuMonitor() {
  const [expanded, setExpanded] = useState(() => {
    try {
      return localStorage.getItem("cpu-monitor-expanded") === "true";
    } catch {
      return false;
    }
  });
  const [position, setPosition] = useState({ right: 20, bottom: 20 });
  const panel = useRef<HTMLDivElement>(null);
  const drag = useRef<{
    id: number;
    x: number;
    y: number;
    right: number;
    bottom: number;
  } | null>(null);
  const [history, setHistory] = useState<CpuHistoryPoint[]>([]);
  const stats = useQuery({
    queryKey: ["system-cpu"],
    queryFn: () => api<CpuSample>("/system/cpu"),
    refetchInterval: 2000,
    refetchIntervalInBackground: false,
  });
  const sample = stats.data;
  const stale =
    !!sample?.sampled_at && Date.now() - Date.parse(sample.sampled_at) > 8000;
  const available = sample?.available === true && !stats.error && !stale;
  const value = available ? sample.percent : null;
  const load = !stats.error && !stale ? sample?.load_average : null;
  const frequency = !stats.error && !stale ? sample?.frequency_mhz : null;
  const status = stats.isPending
    ? "Connecting…"
    : stale
      ? "Updates paused"
      : !available
        ? "CPU data unavailable"
        : value == null
          ? "Measuring…"
          : "Live · 2s updates";
  useEffect(() => {
    if (!available || sample?.percent == null || !sample.sampled_at) return;
    const point = {
      time: Date.parse(sample.sampled_at),
      value: sample.percent,
    };
    if (!Number.isFinite(point.time) || !Number.isFinite(point.value)) return;
    setHistory((current) =>
      (current.at(-1)?.time ?? 0) >= point.time
        ? current
        : [
            ...current
              .filter((entry) => entry.time > point.time - 60000)
              .slice(-59),
            point,
          ],
    );
  }, [available, sample?.percent, sample?.sampled_at]);
  useEffect(() => {
    try {
      localStorage.setItem("cpu-monitor-expanded", String(expanded));
    } catch {
      /* Storage may be disabled. */
    }
  }, [expanded]);
  function bounded(right: number, bottom: number) {
    const rect = panel.current?.getBoundingClientRect();
    return {
      right: Math.max(
        8,
        Math.min(right, innerWidth - (rect?.width ?? 180) - 8),
      ),
      bottom: Math.max(
        8,
        Math.min(bottom, innerHeight - (rect?.height ?? 48) - 8),
      ),
    };
  }
  useLayoutEffect(() => {
    const resize = () =>
      setPosition((current) => bounded(current.right, current.bottom));
    resize();
    const observer = new ResizeObserver(resize);
    if (panel.current) observer.observe(panel.current);
    window.addEventListener("resize", resize);
    return () => {
      observer.disconnect();
      window.removeEventListener("resize", resize);
    };
  }, [expanded]);
  function move(event: PointerEvent<HTMLButtonElement>) {
    const start = drag.current;
    if (start?.id !== event.pointerId) return;
    setPosition(
      bounded(
        start.right - (event.clientX - start.x),
        start.bottom - (event.clientY - start.y),
      ),
    );
  }
  return (
    <div
      ref={panel}
      className={`cpu-monitor ${expanded ? "is-expanded" : "is-folded"}`}
      style={position}
      role="region"
      aria-label="CPU monitor"
    >
      <div className="cpu-monitor-bar">
        <button
          type="button"
          className="cpu-move"
          aria-label="Move CPU monitor"
          title="Drag to move; use arrow keys to reposition"
          onPointerDown={(event) => {
            if (event.button !== 0 || !event.isPrimary) return;
            event.currentTarget.setPointerCapture(event.pointerId);
            drag.current = {
              id: event.pointerId,
              x: event.clientX,
              y: event.clientY,
              ...position,
            };
          }}
          onPointerMove={move}
          onPointerUp={() => {
            drag.current = null;
          }}
          onPointerCancel={() => {
            drag.current = null;
          }}
          onLostPointerCapture={() => {
            drag.current = null;
          }}
          onKeyDown={(event) => {
            const steps: Record<string, [number, number]> = {
              ArrowLeft: [20, 0],
              ArrowRight: [-20, 0],
              ArrowUp: [0, 20],
              ArrowDown: [0, -20],
            };
            if (steps[event.key]) {
              event.preventDefault();
              const [right, bottom] = steps[event.key];
              setPosition((current) =>
                bounded(current.right + right, current.bottom + bottom),
              );
            }
          }}
        >
          <svg width="16" height="16" viewBox="0 0 16 16" aria-hidden="true">
            <path
              d="M3 4h10M3 8h10M3 12h10"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.5"
            />
          </svg>
        </button>
        <button
          type="button"
          className="cpu-toggle"
          aria-expanded={expanded}
          aria-controls="cpu-monitor-body"
          aria-label={expanded ? "Collapse CPU monitor" : "Expand CPU monitor"}
          onClick={() => setExpanded(!expanded)}
        >
          <span className={`cpu-live-dot ${available ? "is-live" : ""}`} />
          <span>CPU</span>
          <strong>{percent(value)}</strong>
          <span className="cpu-chevron" aria-hidden="true">
            {expanded ? "⌄" : "⌃"}
          </span>
        </button>
      </div>
      {expanded && (
        <div id="cpu-monitor-body" className="cpu-monitor-body">
          <div className="cpu-overview">
            <div>
              <span>Server CPU</span>
              <strong>{percent(value)}</strong>
            </div>
            <small>
              {available ? `${sample.logical_cores} logical cores` : status}
            </small>
          </div>
          <div
            className={`cpu-total-track ${value == null ? "" : `cpu-load-${cpuLoad(value)}`}`}
            role="meter"
            aria-label="Overall CPU utilization"
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuenow={value ?? undefined}
            aria-valuetext={percent(value)}
          >
            <i style={{ width: `${value ?? 0}%` }} />
          </div>
          <div
            className="cpu-frequency"
            aria-label="Average CPU frequency"
            title="Current frequency averaged across the logical cores reported by the host."
          >
            <span>Average frequency</span>
            <strong>
              {frequency != null &&
              Number.isFinite(frequency) &&
              frequency > 0 ? (
                <>
                  {(frequency / 1000).toFixed(2)} <small>GHz</small>
                </>
              ) : (
                "Unavailable"
              )}
            </strong>
          </div>
          <div
            className="cpu-load-average"
            role="group"
            aria-label="System load averages"
          >
            <div className="cpu-load-heading">
              <span>Load average</span>
              <small title="Average number of runnable tasks or tasks waiting for I/O. A load near the logical core count can indicate full capacity.">
                {available ? `${sample.logical_cores} cores` : "System load"}
              </small>
            </div>
            <dl>
              {(
                [
                  ["1 min", load?.one_minute],
                  ["5 min", load?.five_minutes],
                  ["15 min", load?.fifteen_minutes],
                ] as const
              ).map(([label, average]) => (
                <div key={label}>
                  <dt>{label}</dt>
                  <dd>
                    {average != null && Number.isFinite(average)
                      ? average.toFixed(2)
                      : "—"}
                  </dd>
                </div>
              ))}
            </dl>
          </div>
          <CpuHistory points={history} />
          {available && (
            <div className="cpu-core-grid" aria-label="Per-core utilization">
              {sample.cores.map((core) => (
                <div
                  className="cpu-core"
                  key={core.id}
                  role="meter"
                  aria-label={`CPU ${core.id}`}
                  aria-valuemin={0}
                  aria-valuemax={100}
                  aria-valuenow={core.percent ?? undefined}
                  aria-valuetext={percent(core.percent)}
                  title={`CPU ${core.id}: ${percent(core.percent)}`}
                >
                  <span>{core.id}</span>
                  <strong>{percent(core.percent)}</strong>
                  <div>
                    <i style={{ width: `${core.percent ?? 0}%` }} />
                  </div>
                </div>
              ))}
            </div>
          )}
          <div className="cpu-monitor-foot">
            <span>{status}</span>
            <span>Whole server</span>
          </div>
        </div>
      )}
    </div>
  );
}
