export const CPU_SAMPLE_MS = 2000;
export const CPU_HISTORY_SLOTS = 30;
export type CpuHistoryPoint = { time: number; value: number };

export function cpuLoad(value: number) {
  return value < 30 ? "low" : value < 70 ? "medium" : "high";
}

// Keep a real time axis: a delayed poll leaves a gap, never a made-up sample.
export function cpuHistorySlots(points: CpuHistoryPoint[], now: number) {
  const end = Math.floor(now / CPU_SAMPLE_MS) * CPU_SAMPLE_MS;
  const start = end - (CPU_HISTORY_SLOTS - 1) * CPU_SAMPLE_MS;
  const slots = Array.from({ length: CPU_HISTORY_SLOTS }, (_, index) => ({
    time: start + index * CPU_SAMPLE_MS,
    sample: undefined as CpuHistoryPoint | undefined,
  }));
  for (const point of points) {
    if (!Number.isFinite(point.time) || !Number.isFinite(point.value)) continue;
    const index = Math.floor((point.time - start) / CPU_SAMPLE_MS);
    if (index < 0 || index >= slots.length) continue;
    const previous = slots[index].sample;
    if (!previous || point.time > previous.time) slots[index].sample = point;
  }
  return slots;
}
