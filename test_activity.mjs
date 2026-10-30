import test from 'node:test';
import assert from 'node:assert/strict';
import { sessionActivity } from './web/activity.mjs';

function observation(frame, events, time = frame * 60) {
  return {frame_id: frame, timestamp_s: time, events: events.map((event, sequence) => ({
    observation_id: frame, sequence, id: `${frame}:${sequence}`, elapsed_s: sequence,
    timestamp: `2026-10-29T00:00:0${sequence}Z`, ...event,
  }))};
}
const evaluation = status => ({kind: 'model', status, action_id: 'model:1', turn: 1});
const result = outcome => ({kind: 'run', action_id: 'run', status: 'finished', outcome});

test('previews and actions share a single chronological stream with session-relative times', () => {
  const rows = sessionActivity([
    observation(0, [evaluation('started'), evaluation('completed'), result('no_photo')]),
    observation(1, [evaluation('started'), evaluation('completed'),
      {kind: 'tool', action_id: 'tool:interval', name: 'set_preview_interval', status: 'completed', previous_seconds: 60, seconds: 10},
      result('preview_interval_set')]),
  ]);
  assert.deepEqual(rows.map(row => row.kind), ['preview', 'model', 'preview', 'model', 'tool']);
  assert.deepEqual(rows.map(row => row.elapsed), [0, 0, 60, 60, 62]);
  assert.equal(new Set(rows.map(row => row.id)).size, rows.length);
  assert.equal(rows[1].noPhoto, true);
  assert.equal(rows.at(-1).event.seconds, 10);
});

test('progress updates revise one stable action instead of adding new rows', () => {
  const started = sessionActivity([observation(0, [evaluation('checking'), evaluation('started')])]);
  const finished = sessionActivity([observation(0, [evaluation('checking'), evaluation('started'), evaluation('completed'), result('no_photo')])]);
  assert.deepEqual(started.map(row => row.id), finished.map(row => row.id));
  assert.notEqual(started[1].revision, finished[1].revision);
  assert.equal(finished[1].event.status, 'completed');
});

test('an unchanged preview interval remains a single skipped tool action', () => {
  const rows = sessionActivity([observation(2, [
    evaluation('completed'),
    {kind: 'tool', action_id: 'tool:interval', name: 'set_preview_interval', status: 'started'},
    {kind: 'tool', action_id: 'tool:interval', name: 'set_preview_interval', status: 'skipped',
      previous_seconds: 15, seconds: 15, changed: false},
    result('preview_interval_unchanged'),
  ])]);
  assert.deepEqual(rows.map(row => row.kind), ['preview', 'model', 'tool']);
  assert.equal(rows[2].event.status, 'skipped');
  assert.equal(rows[2].event.changed, false);
  assert.equal(rows[2].event.seconds, 15);
});

test('capture preparation and downloads update the shoot call on the main thread', () => {
  const rows = sessionActivity([observation(2, [
    {kind: 'tool', action_id: 'tool:photo', name: 'shoot', status: 'started'},
    {kind: 'capture', action_id: 'photo:capture', status: 'started', capture_status: 'shutter_sent'},
    {kind: 'capture', action_id: 'photo:capture', status: 'completed', capture_status: 'downloaded', saved_files: ['moments/0/photo.jpg']},
    {kind: 'tool', action_id: 'tool:photo', name: 'shoot', status: 'completed', capture_status: 'downloaded'},
    result('downloaded'),
  ])]);
  assert.deepEqual(rows.map(row => row.kind), ['preview', 'tool']);
  assert.equal(rows[1].event.name, 'shoot');
  assert.equal(rows[1].capture.capture_status, 'downloaded');
  assert.deepEqual(rows[1].capture.saved_files, ['moments/0/photo.jpg']);
});

test('a failed or skipped photograph is never projected as a successful download', () => {
  const rows = sessionActivity([observation(0, [
    {kind: 'tool', action_id: 'tool:photo', name: 'shoot', status: 'started'},
    {kind: 'capture', action_id: 'photo:capture', status: 'failed', capture_status: 'failed_after_shutter'},
    {kind: 'tool', action_id: 'tool:photo', name: 'shoot', status: 'failed'}, result('error'),
  ])]);
  assert.equal(rows.length, 2);
  assert.equal(rows[1].capture.capture_status, 'failed_after_shutter');
  assert.equal(rows[1].capture.saved_files, undefined);
});

test('observation limits precede later compaction, without nesting either under a preview', () => {
  const rows = sessionActivity([observation(0, [
    evaluation('completed'), result('run_limit'),
    {kind: 'compaction', action_id: 'compaction:0', status: 'started'},
    {kind: 'compaction', action_id: 'compaction:0', status: 'failed'},
  ])]);
  assert.deepEqual(rows.map(row => row.kind), ['preview', 'model', 'outcome', 'compaction']);
  assert.equal(rows.at(-1).event.status, 'failed');
  assert.deepEqual(rows.map(row => row.elapsed), [0, 0, 1, 2]);
});

test('history references survive projection and rebuilding the same report is deterministic', () => {
  const report = [observation(4, [
    {kind: 'tool', action_id: 'tool:history', name: 'get_history', status: 'started'},
    {kind: 'tool', action_id: 'tool:history', name: 'get_history', status: 'completed', frame_ids: [0, 2]},
  ])];
  const original = structuredClone(report);
  assert.deepEqual(sessionActivity(report), sessionActivity(report));
  assert.deepEqual(sessionActivity(report)[1].event.frame_ids, [0, 2]);
  assert.deepEqual(report, original);
});
