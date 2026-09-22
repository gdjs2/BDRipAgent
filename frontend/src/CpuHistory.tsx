import { useEffect, useId, useState } from "react";
import {
  CPU_SAMPLE_MS,
  cpuHistorySlots,
  cpuLoad,
  type CpuHistoryPoint,
} from "./cpu-history";

const levels = Array.from({ length: 20 }, (_, index) => index);
const timeLabel = (time: number) =>
  new Date(time).toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });

export function CpuHistory({ points }: { points: CpuHistoryPoint[] }) {
  const patternId = useId();
  const [now, setNow] = useState(Date.now);
  const [inspected, setInspected] = useState<number | null>(null);
  useEffect(() => {
    const tick = () => setNow(Date.now());
    const timer = window.setInterval(tick, CPU_SAMPLE_MS);
    // Background tabs can miss ticks; catch up as soon as the user returns.
    document.addEventListener("visibilitychange", tick);
    return () => {
      clearInterval(timer);
      document.removeEventListener("visibilitychange", tick);
    };
  }, []);
  const slots = cpuHistorySlots(
    points,
    Math.max(now, points.at(-1)?.time ?? 0),
  );
  const selected = slots.find((slot) => slot.time === inspected);
  const latest = slots.filter((slot) => slot.sample).at(-1)?.sample;
  return (
    <div className="cpu-history">
      <div className="cpu-history-heading">
        <span>Usage history</span>
        <span className="cpu-history-reading" aria-live="off">
          {selected ? (
            <>
              <time>{timeLabel(selected.sample?.time ?? selected.time)}</time>
              <strong
                className={
                  selected.sample
                    ? `cpu-load-${cpuLoad(selected.sample.value)}`
                    : ""
                }
              >
                {selected.sample
                  ? `${selected.sample.value.toFixed(1)}%`
                  : "No sample"}
              </strong>
            </>
          ) : (
            "Last 60 seconds"
          )}
        </span>
      </div>
      <div className="cpu-history-plot">
        <div className="cpu-history-scale" aria-hidden="true">
          <span>100</span>
          <span>50</span>
          <span>0%</span>
        </div>
        <svg
          viewBox="0 0 300 100"
          preserveAspectRatio="none"
          role="img"
          tabIndex={0}
          aria-label={`Overall CPU usage over the last 60 seconds. ${latest ? `Latest recorded sample ${latest.value.toFixed(1)}%.` : "Waiting for samples."} Each bar stacks green blocks from 0 to 30%, amber from 30 to 70%, and coral from 70 to 100%. Use left and right arrow keys to inspect samples.`}
          aria-describedby={selected ? `${patternId}-reading` : undefined}
          onPointerMove={(event) => {
            const rect = event.currentTarget.getBoundingClientRect();
            const index = Math.max(
              0,
              Math.min(
                slots.length - 1,
                Math.floor(
                  ((event.clientX - rect.left) / rect.width) * slots.length,
                ),
              ),
            );
            setInspected(slots[index].time);
          }}
          onPointerLeave={() => setInspected(null)}
          onFocus={() => setInspected(slots.at(-1)!.time)}
          onBlur={() => setInspected(null)}
          onKeyDown={(event) => {
            if (
              !["ArrowLeft", "ArrowRight", "Home", "End", "Escape"].includes(
                event.key,
              )
            )
              return;
            event.preventDefault();
            if (event.key === "Escape") return setInspected(null);
            const current = Math.max(
              0,
              slots.findIndex((slot) => slot.time === inspected),
            );
            const index =
              event.key === "Home"
                ? 0
                : event.key === "End"
                  ? slots.length - 1
                  : Math.max(
                      0,
                      Math.min(
                        slots.length - 1,
                        current + (event.key === "ArrowLeft" ? -1 : 1),
                      ),
                    );
            setInspected(slots[index].time);
          }}
        >
          <defs>
            <pattern
              id={patternId}
              width="10"
              height="5"
              patternUnits="userSpaceOnUse"
            >
              <rect
                x="1"
                y="1.5"
                width="7.5"
                height="3.5"
                rx="0.7"
                className="cpu-history-unlit"
              />
            </pattern>
          </defs>
          <rect width="300" height="100" fill={`url(#${patternId})`} />
          {slots.map((slot, index) => (
            <g
              key={slot.time}
              className="cpu-history-column"
              style={{ transform: `translateX(${index * 10}px)` }}
              data-time={slot.time}
              data-value={slot.sample?.value}
            >
              {slot.time === inspected && (
                <rect width="10" height="100" className="cpu-history-cursor" />
              )}
              {slot.sample &&
                levels.map((level) => {
                  const fill = Math.max(
                    0,
                    Math.min(1, (slot.sample!.value - level * 5) / 5),
                  );
                  if (!fill) return null;
                  const height = fill * 3.5;
                  return (
                    <rect
                      key={level}
                      className={`cpu-history-block cpu-load-${cpuLoad(level * 5)}`}
                      x="1"
                      y={100 - level * 5 - height}
                      width="7.5"
                      height={height}
                      rx="0.7"
                      fill="currentColor"
                    />
                  );
                })}
              {slot.sample?.value === 0 && (
                <path
                  className="cpu-load-low"
                  d="M1 99.5h7.5"
                  stroke="currentColor"
                  strokeWidth="1"
                />
              )}
            </g>
          ))}
        </svg>
      </div>
      <div className="cpu-history-axis" aria-hidden="true">
        <span>60s ago</span>
        <span>30s</span>
        <span>Now</span>
      </div>
      <div className="cpu-history-legend" aria-label="CPU usage color ranges">
        <span>
          <i className="cpu-load-low" />
          0–30%
        </span>
        <span>
          <i className="cpu-load-medium" />
          30–70%
        </span>
        <span>
          <i className="cpu-load-high" />
          70–100%
        </span>
      </div>
      <span className="sr-only" id={`${patternId}-reading`} aria-live="polite">
        {selected &&
          `${timeLabel(selected.sample?.time ?? selected.time)}: ${selected.sample ? `${selected.sample.value.toFixed(1)}% CPU usage` : "No sample recorded"}`}
      </span>
    </div>
  );
}
