import test from "node:test";
import assert from "node:assert/strict";
import { cpuHistorySlots, cpuLoad } from "../src/cpu-history.ts";

test("CPU history keeps its time scale, leaves gaps, and expires old samples", () => {
  const slots = cpuHistorySlots([
    { time: 1000, value: 90 },
    { time: 4000, value: 20 },
    { time: 59000, value: 0 },
    { time: 61000, value: 68 },
    { time: 65000, value: 99 },
  ], 61000);
  assert.equal(slots.length, 30);
  assert.equal(slots[0].time, 2000);
  assert.equal(slots[0].sample, undefined);
  assert.equal(slots[1].sample.value, 20);
  assert.equal(slots[2].sample, undefined);
  assert.equal(slots[28].sample.value, 0, "Zero utilization is a real sample");
  assert.equal(slots[29].sample.value, 68);
  assert.ok(cpuHistorySlots([{ time: 61000, value: 68 }], 124000).every(s => !s.sample));
});

test("CPU history uses the latest actual sample per slot, ignoring invalid data", () => {
  const slots = cpuHistorySlots([
    { time: 60300, value: 31 },
    { time: 60100, value: 29 },
    { time: NaN, value: 90 },
    { time: 60900, value: NaN },
  ], 61000);
  assert.equal(slots.at(-1).sample.value, 31);
  assert.equal(slots.filter(s => s.sample).length, 1);
});

test("CPU history thresholds use the actual value, not its rounded label", () => {
  assert.deepEqual([0, 29.99, 30, 69.99, 70, 100].map(cpuLoad), ["low", "low", "medium", "medium", "high", "high"]);
});
