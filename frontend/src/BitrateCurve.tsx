import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import * as echarts from "echarts/core";
import { LineChart, ScatterChart } from "echarts/charts";
import {
  GridComponent,
  LegendComponent,
  MarkLineComponent,
} from "echarts/components";
import { CanvasRenderer } from "echarts/renderers";
import { bitrateCurve, fitBitrateModel, predictAtBitrate } from "./crf-model";
import type { Point } from "./types";

echarts.use([
  LineChart,
  ScatterChart,
  GridComponent,
  LegendComponent,
  MarkLineComponent,
  CanvasRenderer,
]);

function readPin(key: string): number | null {
  try {
    const raw = sessionStorage.getItem(key);
    const value = raw === null ? NaN : Number(raw);
    return Number.isFinite(value) && value > 0 && value <= 100000
      ? value
      : null;
  } catch {
    return null;
  }
}

export function BitrateCurve({
  jobId,
  samples,
  canChoose,
  crfMin,
  crfMax,
  choose,
  chooseBitrate,
}: {
  jobId: string;
  samples: Point[];
  canChoose: boolean;
  crfMin: number;
  crfMax: number;
  choose: (crf: number) => void;
  chooseBitrate: (bitrateKbps: number) => void;
}) {
  const pinKey = `bdrip.crf.pin:${jobId}`;
  const model = useMemo(() => fitBitrateModel(samples), [samples]);
  const [pinned, setPinned] = useState<number | null>(() => readPin(pinKey));
  const [hover, setHover] = useState<number | null>(null);
  const [input, setInput] = useState(() =>
    pinned === null ? "" : String(Number(pinned.toFixed(6))),
  );
  const [inputError, setInputError] = useState("");
  const element = useRef<HTMLDivElement>(null);
  const chart = useRef<echarts.EChartsType | null>(null);
  const pinnedRef = useRef(pinned);
  const pin = useCallback(
    (value: number | null) => {
      pinnedRef.current = value;
      setPinned(value);
      setHover(null);
      setInputError("");
      if (value !== null) setInput(String(Number(value.toFixed(6))));
      try {
        if (value === null) sessionStorage.removeItem(pinKey);
        else sessionStorage.setItem(pinKey, String(value));
      } catch {
        /* Pinning still works when browser storage is unavailable. */
      }
    },
    [pinKey],
  );
  const estimate = predictAtBitrate(model, pinned ?? hover ?? NaN);
  const selectedCRF =
    estimate?.crf === null || estimate?.crf === undefined
      ? null
      : Number(estimate.crf.toFixed(1));
  const inProfileRange =
    selectedCRF !== null && selectedCRF >= crfMin && selectedCRF <= crfMax;
  const selectedBitrate = pinned === null ? 0 : Math.round(pinned * 1000);
  const validBitrate = selectedBitrate >= 1 && selectedBitrate <= 1_000_000;
  const measured = useMemo(
    () =>
      samples
        .filter((p) => p.average_qp !== null && Number.isFinite(p.average_qp))
        .map((p) => [p.bitrate_kbps / 1000, p.average_qp!, p.crf]),
    [samples],
  );
  const curve = useMemo(() => bitrateCurve(model), [model]);

  useEffect(() => {
    const instance = echarts.init(element.current!);
    chart.current = instance;
    const rateAt = (event: { offsetX: number; offsetY: number }) => {
      const pixel = [event.offsetX, event.offsetY];
      if (!instance.containPixel({ gridIndex: 0 }, pixel)) return null;
      const [rate] = instance.convertFromPixel(
        { gridIndex: 0 },
        pixel,
      ) as number[];
      return Number.isFinite(rate) && rate > 0 ? rate : null;
    };
    const zr = instance.getZr();
    zr.on("mousemove", (event) => {
      if (pinnedRef.current === null) setHover(rateAt(event));
    });
    zr.on("globalout", () => {
      if (pinnedRef.current === null) setHover(null);
    });
    zr.on("click", (event) => {
      const rate = rateAt(event);
      if (rate !== null && model) pin(rate);
    });
    const observer = new ResizeObserver(() => instance.resize());
    observer.observe(element.current!);
    return () => {
      observer.disconnect();
      instance.dispose();
      chart.current = null;
    };
  }, [model, pin]);

  // Keep axes stable while hovering; a typed pin may expand the view.
  useEffect(() => {
    const pinnedQP = predictAtBitrate(model, pinned ?? NaN)?.qp;
    const qps = measured.map((p) => p[1]);
    if (pinnedQP !== null && pinnedQP !== undefined) qps.push(pinnedQP);
    const minQP = qps.length ? Math.min(...qps) : 0;
    const maxQP = qps.length ? Math.max(...qps) : 51;
    const padding = Math.max(1, (maxQP - minQP) * 0.15);
    chart.current?.setOption({
      animation: false,
      backgroundColor: "transparent",
      textStyle: { color: "#969eaa" },
      grid: { left: 65, right: 22, top: 46, bottom: 60 },
      legend: {
        top: 4,
        bottom: "auto",
        data: ["Measured", "Predicted"],
        textStyle: { color: "#aeb4bd" },
      },
      xAxis: {
        type: "value",
        name: "Video bitrate (Mbps)",
        nameLocation: "middle",
        nameGap: 36,
        nameTextStyle: { color: "#aeb4bd" },
        min: 0,
        max: Math.max(model?.maxMbps ?? 20, pinned ?? 0) * 1.12,
        axisLabel: {
          color: "#aeb4bd",
          hideOverlap: true,
          showMaxLabel: false,
          formatter: (value: number) => String(Number(value.toFixed(3))),
        },
        splitLine: { lineStyle: { color: "#272b31" } },
      },
      yAxis: {
        type: "value",
        name: "Average B-frame QP",
        nameLocation: "middle",
        nameGap: 43,
        nameTextStyle: { color: "#aeb4bd" },
        axisLabel: { color: "#aeb4bd" },
        min: Math.floor(minQP - padding),
        max: Math.ceil(maxQP + padding),
        splitLine: { lineStyle: { color: "#272b31" } },
      },
      series: [
        {
          id: "predicted",
          name: "Predicted",
          type: "line",
          data: curve,
          showSymbol: false,
          lineStyle: { color: "#eabc74", width: 3 },
          itemStyle: { color: "#eabc74" },
          silent: true,
        },
        {
          id: "measured",
          name: "Measured",
          type: "scatter",
          data: measured,
          symbolSize: 11,
          itemStyle: {
            color: "#75b4a5",
            borderColor: "#101214",
            borderWidth: 2,
          },
          label: {
            show: true,
            formatter: (p: { value: number[] }) => `CRF ${p.value[2]}`,
            position: "top",
            color: "#b9d8d0",
          },
          silent: true,
        },
        {
          id: "cursor",
          type: "scatter",
          data: [],
          symbolSize: 13,
          symbol: "diamond",
          itemStyle: {
            color: "#fff0cd",
            borderColor: "#eabc74",
            borderWidth: 2,
          },
          z: 10,
          silent: true,
        },
      ],
    });
  }, [model, curve, measured, pinned]);

  useEffect(() => {
    chart.current?.setOption({
      series: [
        {
          id: "cursor",
          data:
            estimate?.qp !== null && estimate?.qp !== undefined
              ? [[estimate.bitrateMbps, estimate.qp]]
              : [],
          markLine: {
            symbol: "none",
            silent: true,
            label: { show: false },
            lineStyle: {
              color: "#b9a889",
              type: pinned === null ? "dashed" : "solid",
              width: 1,
            },
            data: estimate
              ? [
                  { xAxis: estimate.bitrateMbps },
                  ...(estimate.qp === null ? [] : [{ yAxis: estimate.qp }]),
                ]
              : [],
          },
        },
      ],
    });
  }, [estimate?.bitrateMbps, estimate?.qp, pinned, model]);

  return (
    <section
      className="bitrate-panel"
      onKeyDown={(event) => {
        if (event.key === "Escape") pin(null);
      }}
    >
      <h2>Bitrate → B-frame QP</h2>
      <p className="muted">
        Move over the plot to estimate QP. Click or tap to pin a bitrate.
        Right-click or use Unpin to release it.
      </p>
      <div className="bitrate-plot">
        <div
          ref={element}
          className="bitrate-chart"
          role="img"
          aria-label="Video bitrate in Mbps versus average B-frame QP, with measured CRF 13 and 20 points and a predicted curve"
          onContextMenu={(event) => {
            event.preventDefault();
            pin(null);
          }}
        />
        {estimate && (
          <div
            className="bitrate-tooltip"
            aria-hidden="true"
            style={
              estimate.bitrateMbps >
              Math.max(model?.maxMbps ?? 20, pinned ?? 0) * 0.56
                ? { left: 65 }
                : { right: 22 }
            }
          >
            <strong>
              {pinned === null ? "Estimate" : "Pinned"} ·{" "}
              {estimate.bitrateMbps.toFixed(3)} Mbps
            </strong>
            <span>B-frame QP: {estimate.qp?.toFixed(3) ?? "unavailable"}</span>
            <span>CRF ≈ {estimate.crf?.toFixed(3) ?? "unavailable"}</span>
            {estimate.extrapolated && <small>Extrapolation</small>}
          </div>
        )}
      </div>
      <form
        className="inline-form bitrate-controls"
        onSubmit={(event) => {
          event.preventDefault();
          const value = Number(input);
          if (!Number.isFinite(value) || value <= 0 || value > 100000) {
            setInputError(
              "Enter a bitrate greater than 0 and at most 100000 Mbps.",
            );
            return;
          }
          pin(value);
        }}
      >
        <label>
          Video bitrate (Mbps)
          <input
            type="number"
            step="any"
            min="0.000001"
            max="100000"
            required
            value={input}
            onChange={(event) => {
              setInput(event.target.value);
              setInputError("");
            }}
            placeholder="e.g. 8"
          />
        </label>
        <button disabled={!model}>Pin bitrate</button>
        <button
          type="button"
          className="secondary"
          disabled={pinned === null}
          onClick={() => pin(null)}
        >
          Unpin
        </button>
      </form>
      {inputError && (
        <p className="error" role="alert">
          {inputError}
        </p>
      )}
      <div
        className={`bitrate-readout ${pinned === null ? "" : "pinned"}`}
        aria-live={pinned === null ? "off" : "polite"}
      >
        <div className="bitrate-status">
          {pinned !== null
            ? "Pinned bitrate"
            : estimate
              ? "Hover estimate"
              : "Choose a bitrate"}
          {estimate?.extrapolated && (
            <span className="badge">Extrapolated</span>
          )}
        </div>
        <div className="bitrate-values">
          <div>
            <small>Video bitrate</small>
            <strong data-testid="estimate-bitrate">
              {estimate ? `${estimate.bitrateMbps.toFixed(3)} Mbps` : "—"}
            </strong>
          </div>
          <div>
            <small>Predicted B-frame QP</small>
            <strong data-testid="estimate-qp">
              {estimate ? (estimate.qp?.toFixed(3) ?? "Unavailable") : "—"}
            </strong>
          </div>
          <div>
            <small>Approximate CRF</small>
            <strong data-testid="estimate-crf">
              {estimate?.crf?.toFixed(3) ?? "—"}
            </strong>
          </div>
        </div>
        {estimate?.message && <p>{estimate.message}</p>}
        {estimate?.extrapolated && (
          <p>Outside the measured bitrate range; this is an extrapolation.</p>
        )}
        {pinned !== null && (
          <div className="encode-target-actions">
            <button
              type="button"
              className="secondary"
              disabled={!canChoose || !inProfileRange}
              onClick={() => {
                if (selectedCRF !== null && inProfileRange) choose(selectedCRF);
              }}
            >
              {selectedCRF === null
                ? "Use this CRF"
                : `Use CRF ${selectedCRF.toFixed(1)}`}
            </button>
            <button
              type="button"
              className="secondary"
              disabled={!canChoose || !validBitrate}
              onClick={() => {
                if (validBitrate) chooseBitrate(selectedBitrate);
              }}
            >
              Use bitrate for 2-pass
            </button>
          </div>
        )}
        {pinned !== null && !validBitrate && (
          <p>Two-pass targets must be between 0.001 and 1000 Mbps.</p>
        )}
        {pinned !== null && selectedCRF !== null && !inProfileRange && (
          <p>
            This profile accepts CRF {crfMin}–{crfMax}.
          </p>
        )}
      </div>
      <p className="muted">
        Markers are sample means at CRF 13 and 20. The line uses BDRip_Scripts’
        logarithmic bitrate model.
        {model &&
          ` Measured range: ${model.minMbps.toFixed(3)}–${model.maxMbps.toFixed(3)} Mbps.`}
        {(!model || !model.qp) &&
          " B-frame QP measurements are unavailable; the QP curve cannot be drawn."}
        {model &&
          Math.abs(model.e) <= 1e-12 &&
          " Equal measured bitrates do not define a unique QP curve."}
      </p>
    </section>
  );
}
