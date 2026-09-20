import test from "node:test";
import assert from "node:assert/strict";
import { encodingPauseState } from "../src/encoding-pause.ts";

const task = {id: "encode", type: "encode", status: "RUNNING", can_pause: true};

test("running and paused encodes expose opposite actions", () => {
  assert.deepEqual(encodingPauseState(task), {visible: true, status: "Running", action: "pause", label: "Pause encoding", disabled: false});
  const paused = encodingPauseState({...task, pause_requested: true, paused_at: "2026-09-20T12:00:00Z"});
  assert.equal(paused.action, "resume");
  assert.equal(paused.label, "Resume encoding");
  assert.equal(paused.status, "Paused");
  assert.equal(paused.disabled, false);
});

test("pending requests wait for worker acknowledgement", () => {
  const pending = encodingPauseState({...task, pause_requested: true});
  assert.equal(pending.status, "Pausing…");
  assert.equal(pending.disabled, true);
  const resumed = encodingPauseState({...task, pause_requested: false, paused_at: "now"});
  assert.equal(resumed.status, "Resuming…");
  assert.equal(resumed.disabled, true);
});

test("finished, queued, other stages and old workers cannot offer pause", () => {
  for (const status of ["QUEUED", "SUCCEEDED", "FAILED", "CANCELLED"]) {
    assert.equal(encodingPauseState({...task, status}), null);
  }
  assert.equal(encodingPauseState({...task, type: "crf_analysis"}), null);
  assert.equal(encodingPauseState({...task, can_pause: false}).visible, false);
  assert.equal(encodingPauseState({...task, cancel_requested: true}).disabled, true);
});
