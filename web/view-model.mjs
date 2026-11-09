const START = new Set(['harness', 'builtin', 'mcp', 'skills', 'memory', 'unknown']);
export function summarize(waves) {
  const main = waves.filter(w => ['primary', 'unknown'].includes(w.kind) && w.capture !== 'gap');
  const first = main[0];
  const startupBlocks = first?.category_totals
    ? Object.entries(first.category_totals).filter(([category]) => START.has(category)).map(([, estimated_tokens]) => ({ estimated_tokens }))
    : first?.blocks.filter(b => START.has(b.category)) ?? [];
  const unknown = startupBlocks.some(b => b.estimated_tokens === null);
  const reported = waves.filter(w => w.usage && (w.usage.input_tokens ?? w.usage.prompt_tokens) !== undefined);
  return {
    startup: first ? (unknown ? null : startupBlocks.reduce((n, b) => n + b.estimated_tokens, 0)) : null,
    first: first?.estimated_tokens ?? null,
    latest: main.at(-1)?.estimated_tokens ?? null,
    providerInputSum: reported.reduce((n, w) => n + (w.usage.input_tokens ?? w.usage.prompt_tokens), 0),
    usageReportedCount: reported.length,
  };
}

export function compareBlocks(current, previous) {
  function subtract(a, b) {
    const counts = new Map();
    b.forEach(x => counts.set(x.id, (counts.get(x.id) ?? 0) + 1));
    return a.filter(x => {
      const count = counts.get(x.id) ?? 0;
      if (count) { counts.set(x.id, count - 1); return false; }
      return true;
    });
  }
  return { added: subtract(current, previous), removed: subtract(previous, current) };
}

export function tileCounts(blocks) {
  const groups = new Map();
  let unknown = false;
  blocks.forEach(b => {
    if (b.estimated_tokens === null) { unknown = true; return; }
    groups.set(b.category, (groups.get(b.category) ?? 0) + b.estimated_tokens);
  });
  const total = [...groups.values()].reduce((a, b) => a + b, 0);
  const unit = Math.max(100, Math.ceil(total / Math.max(1, 1600 - groups.size) / 100) * 100);
  return { unit, unknown, tiles: [...groups].map(([category, tokens]) => ({ category, count: Math.ceil(tokens / unit) })) };
}
