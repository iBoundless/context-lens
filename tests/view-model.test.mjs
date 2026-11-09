import test from 'node:test';
import assert from 'node:assert/strict';

let summarize, compareBlocks, tileCounts;
try { ({ summarize, compareBlocks, tileCounts } = await import('../web/view-model.mjs')); }
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
