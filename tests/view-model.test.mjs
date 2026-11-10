import test from 'node:test';
import assert from 'node:assert/strict';

let summarize, compareBlocks, tileCounts, skillRows;
try { ({ summarize, compareBlocks, tileCounts, skillRows } = await import('../web/view-model.mjs')); }
catch (e) { if (e.code !== 'ERR_MODULE_NOT_FOUND') throw e; }

test('start excludes user, assistant and tool results, unknown is not zero', () => {
  assert.ok(summarize, 'view model is not implemented');
  const result = summarize([{ id: 1, kind: 'primary', estimated_tokens: 20, blocks: [
    { category: 'harness', estimated_tokens: 12 }, { category: 'user', estimated_tokens: 8 }],
    usage: { input_tokens: 23 } }, { id: 2, kind: 'primary', estimated_tokens: null, blocks: [] }]);
  assert.equal(result.startup, 12);
  assert.equal(result.first, 20);
  assert.equal(result.latest, null);
  assert.equal(result.providerInputSum, 23);
  assert.equal(result.usageReportedCount, 1);
});

test('auxiliary requests do not become first main request', () => {
  assert.ok(summarize, 'view model is not implemented');
  const result = summarize([{ kind: 'title', estimated_tokens: 100, blocks: [] },
    { kind: 'primary', estimated_tokens: 7, blocks: [{ category: 'user', estimated_tokens: 7 }] }]);
  assert.equal(result.first, 7);
  assert.equal(result.startup, 0);
});

test('block comparison preserves repeated blocks as a multiset', () => {
  assert.ok(compareBlocks, 'block comparison is not implemented');
  const result = compareBlocks([{ id: 'a' }, { id: 'a' }, { id: 'b' }], [{ id: 'a' }, { id: 'c' }]);
  assert.equal(result.added.length, 2);
  assert.equal(result.removed.length, 1);
});

test('large contexts bound tile count without inventing multimodal sizes', () => {
  assert.ok(tileCounts, 'tile counts are not implemented');
  const result = tileCounts([{ category: 'harness', estimated_tokens: 10000000 },
    { category: 'user', estimated_tokens: null }]);
  assert.ok(result.tiles.reduce((sum, x) => sum + x.count, 0) <= 1600);
  assert.equal(result.unknown, true);
  assert.ok(result.unit > 100);
});

test('skill table joins selected estimates with all-session totals by name and kind', () => {
  assert.ok(skillRows, 'skill row join is not implemented');
  const current = { estimated_tokens: 200, skills: [
    { name: 'review', kind: 'loaded', estimated_tokens: 50, attributions: ['source-metadata'] },
    { name: 'review', kind: 'description', estimated_tokens: 10, attributions: ['format-heuristic'] },
  ] };
  const overview = { skill_totals: [
    { name: 'review', kind: 'loaded', estimated_tokens: 150, request_count: 3, attributions: ['format-heuristic', 'source-metadata'] },
    { name: 'other', kind: 'loaded', estimated_tokens: 90, request_count: 2, attributions: ['format-heuristic'] },
    { name: 'review', kind: 'description', estimated_tokens: 30, request_count: 3, attributions: ['format-heuristic'] },
  ] };
  const rows = skillRows(current, overview);
  assert.equal(rows.length, 3);
  assert.equal(rows[0].selected_tokens, 50);
  assert.equal(rows[0].percent, 25);
  assert.equal(rows[0].session_tokens, 150);
  assert.equal(rows[0].request_count, 3);
  assert.deepEqual(rows[0].attributions, ['format-heuristic', 'source-metadata']);
  assert.equal(rows[1].name, 'other');
  assert.equal(rows[1].selected_present, false);
  assert.equal(rows[1].selected_tokens, null);
  assert.equal(rows[1].percent, null);
  assert.equal(rows[2].kind, 'description');
});

test('skill table does not invent unknown sizes or retain another sessions rows', () => {
  assert.ok(skillRows, 'skill row join is not implemented');
  for (const denominator of [null, 0]) {
    const rows = skillRows({ estimated_tokens: denominator, skills: [
      { name: 'x', kind: 'loaded', estimated_tokens: null, attributions: ['format-heuristic'] },
    ] }, null);
    assert.equal(rows[0].percent, null);
    assert.equal(rows[0].selected_present, true);
    assert.equal(rows[0].selected_tokens, null);
    assert.equal(rows[0].session_tokens, null);
    assert.equal(rows[0].request_count, null);
  }
  assert.deepEqual(skillRows(null, null), []);
  assert.deepEqual(skillRows({ estimated_tokens: 10, skills: [] }, { skill_totals: [] }), []);
  assert.equal(skillRows(null, { skill_totals: [
    { name: '<img src=x onerror=alert(1)>', kind: 'loaded', estimated_tokens: 2, request_count: 1, attributions: [] },
  ] })[0].selected_present, false);
});
