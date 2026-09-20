import test from "node:test";
import assert from "node:assert/strict";
import { displayCommand, outputDimensions, recordedEncodingCommand } from "../src/encoding-configuration.ts";

const command = ["HandBrakeCLI", "-i", "/source/Movie's $(literal) name.mkv", "-e", "x265_10bit", "--encoder-preset", "slower"];
const encode = {id: "encode", type: "encode", created_at: "2026-09-20T10:00:00Z", attempt: 1, command_json: [command]};

test("the encoding command comes from its own attempt, not later mux work", () => {
  const result = recordedEncodingCommand({tasks: [encode, {type: "mux", created_at: "2026-09-20T11:00:00Z", command_json: [["mkvmerge"]]}]});
  assert.equal(result.task.id, "encode");
  assert.deepEqual(result.argv, command);
  const retry = {id: "retry", type: "encode", created_at: "2026-09-20T12:00:00Z", command_json: []};
  assert.equal(recordedEncodingCommand({tasks: [encode, retry]}).argv, undefined);
});

test("command display quotes literal shell characters and empty values", () => {
  const shown = displayCommand(["HandBrakeCLI", "Movie's $(literal).mkv", ""]);
  assert.ok(shown.includes("'Movie'\\''s $(literal).mkv'"));
  assert.ok(shown.endsWith("''"));
});

test("dimensions apply explicit crop and respect smoke mode", () => {
  const job = {analysis: {video: {width: 1920, height: 1080}, crop: {top: 104, bottom: 104, left: 0, right: 0}}};
  assert.deepEqual(outputDimensions(job), {width: 1920, height: 872});
  assert.deepEqual(outputDimensions({...job, encode_config: {data: {execution_mode: "smoke"}}}), {width: 1920, height: 1080});
  assert.equal(outputDimensions({analysis: {video: job.analysis.video}}), null);
});
