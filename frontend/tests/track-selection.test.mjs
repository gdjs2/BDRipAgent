import test from "node:test";
import assert from "node:assert/strict";
import { analysisComplete, suggestedName, unresolvedFlags } from "../src/track-selection.ts";

const track = {track_id: 4, kind: "subtitles", info: {codec_id: "S_HDMV/PGS", base_name: "French PGS", name: "source label", subtitle_detection: {schema_version: 1, status: "inconclusive"}, track_review: {schema_version: 1}, default: false, forced: false, hearing_impaired: null, visual_impaired: false, commentary: false}};

test("completed reviews remain ready across schema versions and inconclusive findings", () => {
  const job = {analysis: {track_review_version: 1}, tracks: [track]};
  assert.equal(analysisComplete(job), true);
  assert.equal(analysisComplete({...job, track_analysis_complete: false}), false);
  assert.equal(analysisComplete({...job, tracks: [{...track, info: {...track.info, track_review: null}}]}), false);
});

test("unknown flags need a choice and explicit false overrides survive", () => {
  assert.deepEqual(unresolvedFlags(track), ["hearing_impaired"]);
  assert.deepEqual(unresolvedFlags(track, {hearing_impaired: false}), []);
  assert.equal(suggestedName(track, {hearing_impaired: true, forced: true}), "French PGS SDH Forced");
  assert.equal(suggestedName(track, {hearing_impaired: false}), "French PGS");
});

test("saved order survives source enumeration and newly discovered tracks", async () => {
  const {orderedTracks} = await import('../src/track-selection.ts');
  const tracks = [1,2,3].map(track_id => ({track_id,kind:'audio'}));
  assert.deepEqual(orderedTracks([...tracks,{track_id:4,kind:'subtitles'}], 'audio', [3,1,99,3,4]).map(t=>t.track_id), [3,1,2]);
  assert.deepEqual(orderedTracks(tracks,'subtitles',[1]), []);
});


test("initial order follows source positions even when rows arrive shuffled", async () => {
  const {orderedTracks} = await import('../src/track-selection.ts');
  const tracks = [
    {track_id:2,kind:'audio',info:{source_order:3}},
    {track_id:9,kind:'subtitles',info:{source_order:2}},
    {track_id:4,kind:'audio',info:{source_order:1}},
    {track_id:1,kind:'subtitles',info:{source_order:4}},
  ];
  assert.deepEqual(orderedTracks(tracks,'audio').map(t=>t.track_id), [4,2]);
  assert.deepEqual(orderedTracks(tracks,'subtitles').map(t=>t.track_id), [9,1]);
  assert.deepEqual(orderedTracks(tracks,'subtitles',[1,9]).map(t=>t.track_id), [1,9]);
  assert.deepEqual(orderedTracks([{track_id:9,kind:'audio'},{track_id:4,kind:'audio'}],'audio').map(t=>t.track_id), [4,9]);
});
