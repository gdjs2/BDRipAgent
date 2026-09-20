import type { Point } from "./types.ts";

export type BitrateModel = {
  minMbps: number;
  maxMbps: number;
  d: number;
  e: number;
  qp: { a: number; b: number } | null;
};

export type BitrateEstimate = {
  bitrateMbps: number;
  qp: number | null;
  crf: number | null;
  extrapolated: boolean;
  message: string | null;
};

// Same two-point fit as BDRip_Scripts' crf/model.py: QP = a + b*CRF,
// ln(video Mbps) = d + e*CRF. Refit saved measurements for older jobs too.
export function fitBitrateModel(samples: Point[]): BitrateModel | null {
  const low = samples.find((p) => p.crf === 13);
  const high = samples.find((p) => p.crf === 20);
  if (
    !low ||
    !high ||
    ![low.bitrate_kbps, high.bitrate_kbps].every(
      (n) => Number.isFinite(n) && n > 0,
    )
  )
    return null;
  const r0 = low.bitrate_kbps / 1000;
  const r1 = high.bitrate_kbps / 1000;
  const e = (Math.log(r1) - Math.log(r0)) / 7;
  const hasQP =
    low.average_qp !== null &&
    high.average_qp !== null &&
    Number.isFinite(low.average_qp) &&
    Number.isFinite(high.average_qp);
  const b = hasQP ? (high.average_qp! - low.average_qp!) / 7 : 0;
  return {
    minMbps: Math.min(r0, r1),
    maxMbps: Math.max(r0, r1),
    d: Math.log(r0) - e * 13,
    e,
    qp: hasQP ? { a: low.average_qp! - b * 13, b } : null,
  };
}

export function predictAtBitrate(
  model: BitrateModel | null,
  bitrateMbps: number,
): BitrateEstimate | null {
  if (!model || !Number.isFinite(bitrateMbps) || bitrateMbps <= 0) return null;
  const result: BitrateEstimate = {
    bitrateMbps,
    qp: null,
    crf: null,
    extrapolated: false,
    message: null,
  };
  if (Math.abs(model.e) <= 1e-12) {
    return {
      ...result,
      message:
        "Equal measured bitrates: a unique QP or CRF cannot be predicted.",
    };
  }
  const crf = (Math.log(bitrateMbps) - model.d) / model.e;
  if (!Number.isFinite(crf))
    return { ...result, message: "Estimate outside the numerical range." };
  const qp = model.qp ? model.qp.a + model.qp.b * crf : null;
  return {
    ...result,
    crf,
    qp: qp !== null && Number.isFinite(qp) ? qp : null,
    extrapolated: crf < 13 - 1e-9 || crf > 20 + 1e-9,
    message:
      qp === null
        ? "B-frame QP is unavailable at one or both measured CRFs."
        : null,
  };
}

export function bitrateCurve(model: BitrateModel | null): number[][] {
  if (!model) return [];
  return Array.from({ length: 201 }, (_, index) => {
    const rate =
      model.minMbps + ((model.maxMbps - model.minMbps) * index) / 200;
    const estimate = predictAtBitrate(model, rate);
    return estimate?.qp !== null && estimate?.qp !== undefined
      ? [rate, estimate.qp]
      : [];
  }).filter((point) => point.length > 0);
}
