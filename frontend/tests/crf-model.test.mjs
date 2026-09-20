import assert from 'node:assert/strict';
import { test } from 'node:test';
import { bitrateCurve, fitBitrateModel, predictAtBitrate } from '../src/crf-model.ts';

const samples = [
  { crf: 13, bitrate_kbps: 20000, average_qp: 20 },
  { crf: 20, bitrate_kbps: 5000, average_qp: 27 },
];
const model = fitBitrateModel(samples);
function close(actual, expected) { assert.ok(Math.abs(actual - expected) < 1e-9, `${actual} != ${expected}`); }

test('bitrate inversion matches both measured anchors and the logarithmic midpoint', () => {
  for (const [rate, qp, crf] of [[20, 20, 13], [5, 27, 20], [10, 23.5, 16.5]]) {
    const estimate = predictAtBitrate(model, rate);
    close(estimate.qp, qp); close(estimate.crf, crf);
    assert.equal(estimate.extrapolated, false);
  }
});

test('fractional bitrates use the continuous model instead of snapping to integer CRF', () => {
  const crf = 17.25;
  const rate = 20 * Math.exp(Math.log(5 / 20) * (crf - 13) / 7);
  const estimate = predictAtBitrate(model, rate);
  close(estimate.crf, crf); close(estimate.qp, 24.25);
  const curve = bitrateCurve(model);
  assert.equal(curve.length, 201);
  close(curve[0][0], 5); close(curve[0][1], 27);
  close(curve.at(-1)[0], 20); close(curve.at(-1)[1], 20);
});

test('extrapolations are flagged without clamping their bitrate or CRF', () => {
  const estimate = predictAtBitrate(model, 40);
  close(estimate.qp, 16.5); close(estimate.crf, 9.5);
  assert.equal(estimate.extrapolated, true);
});

test('equal endpoint bitrates do not produce a fabricated QP or CRF', () => {
  const equal = fitBitrateModel(samples.map(p => ({ ...p, bitrate_kbps: 5000 })));
  const estimate = predictAtBitrate(equal, 5);
  assert.equal(estimate.qp, null); assert.equal(estimate.crf, null);
  assert.match(estimate.message, /Equal measured bitrates/);
  assert.deepEqual(bitrateCurve(equal), []);
});

test('missing B-frame measurements preserve bitrate to CRF prediction', () => {
  const missing = fitBitrateModel([samples[0], { ...samples[1], average_qp: null }]);
  const estimate = predictAtBitrate(missing, 10);
  close(estimate.crf, 16.5); assert.equal(estimate.qp, null);
  assert.match(estimate.message, /unavailable/);
  assert.deepEqual(bitrateCurve(missing), []);
});

test('incomplete measurements and invalid target bitrates are rejected', () => {
  assert.equal(fitBitrateModel([samples[0]]), null);
  assert.equal(fitBitrateModel([{ ...samples[0], bitrate_kbps: 0 }, samples[1]]), null);
  for (const rate of [0, -1, NaN, Infinity]) assert.equal(predictAtBitrate(model, rate), null);
  assert.equal(predictAtBitrate(null, 5), null);
});
