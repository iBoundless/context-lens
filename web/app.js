import { summarize, compareBlocks, tileCounts, skillRows } from '/assets/view-model.mjs';

const $ = id => document.getElementById(id);
const fmt = n => n === null || n === undefined ? '?' : n.toLocaleString('ru-RU');
const layers = {
  harness: ['Харнес', '#969e90', 'Системные инструкции и окружение'],
  builtin: ['Инструменты', '#379ce9', 'Определения функций и встроенных инструментов'],
  mcp: ['MCP-инструменты', '#a18bf0', 'По имени инструмента, эвристическая атрибуция'],
  skills: ['Навыки', '#ed743d', 'По меткам источника или стандартным тегам, включая описания'],
  memory: ['AGENTS.md и память', '#23ab7d', 'Только при доступной атрибуции источника'],
  user: ['Пользователь', '#d5ab29', 'Сообщения и вложения'],
  assistant: ['Ассистент', '#5eaaad', 'Ответы, рассуждения и вызовы инструментов'],
  tool: ['Результаты инструментов', '#b58e63', 'Данные, возвращённые инструментами'],
  unknown: ['Не определено', '#8b8192', 'Неизвестные или размещённые у провайдера инструменты'],
};
let client = 'opencode', sessionId = '', sessions = [], waves = [], view = 'start';
let waveId = null, selectedCategory = null, source = null, revision = 0, pending = false;
let overview = null, paging = false;
let sessionEpoch = 0;
const fullWaves = new Map(), loadingWaves = new Set();

function el(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
}

function error(message) { $('error').textContent = message; $('error').hidden = !message; }

async function api(path, options = {}) {
  const response = await fetch(path, { cache: 'no-store', ...options,
    headers: { 'Content-Type': 'application/json', ...options.headers } });
  if (response.status === 401) {
    $('login').hidden = false; $('workspace').hidden = true;
    source?.close(); source = null;
    throw new Error('Нужен токен локального сборщика.');
  }
  if (!response.ok) throw new Error('Ошибка сборщика: HTTP ' + response.status);
  return response.json();
}

function query() { return new URLSearchParams({ client, session_id: sessionId }); }

function resetSessionView() {
  sessionEpoch++;
  waveId = null; selectedCategory = null; overview = null; waves = [];
  fullWaves.clear(); loadingWaves.clear(); paging = false;
}

async function refresh() {
  const myRevision = ++revision;
  const list = await api('/api/sessions');
  if (myRevision !== revision) return;
  sessions = list;
  const available = list.filter(s => s.client === client);
  if (!available.some(s => s.session_id === sessionId)) {
    sessionId = available[0]?.session_id ?? ''; resetSessionView();
  }
  const select = $('sessions'); select.replaceChildren();
  if (!available.length) select.append(new Option('Нет обнаруженных сессий', ''));
  available.forEach(s => select.append(new Option(s.session_id + (s.recording ? ' · запись' : ' · пауза'), s.session_id)));
  select.value = sessionId;
  const [loaded, info] = sessionId ? await Promise.all([
    api('/api/waves?' + query()), api('/api/overview?' + query()),
  ]) : [[], null];
  if (myRevision !== revision) return;
  waves = loaded;
  overview = info;
  const session = sessions.find(s => s.client === client && s.session_id === sessionId);
  $('record').disabled = !sessionId;
  $('delete').disabled = !sessionId;
  $('record').textContent = session?.recording ? 'Остановить запись' : 'Включить запись';
  $('record').classList.toggle('accent', Boolean(session?.recording));
  $('workspace').hidden = false; $('login').hidden = true;
  render();
}

function startLive() {
  source?.close(); source = new EventSource('/api/live');
  source.addEventListener('ready', () => {
    $('connection').textContent = '● Подключён · обновления включены';
    refresh().catch(e => error(e.message));
  });
  source.addEventListener('changed', () => {
    if (pending) return;
    pending = true;
    setTimeout(() => { pending = false; refresh().catch(e => error(e.message)); }, 120);
  });
  source.onerror = () => { $('connection').textContent = 'Переподключение...'; };
}

async function login(token) {
  await api('/api/login', { method: 'POST', body: JSON.stringify({ token }) });
  $('token').value = ''; error('');
  await refresh(); startLive();
}

function render() {
  const stats = summarize(overview ? [overview.first, overview.latest].filter(Boolean) : waves);
  if (overview) {
    stats.providerInputSum = overview.provider_input_sum;
    stats.usageReportedCount = overview.usage_reported_count;
  }
  for (const key of ['startup', 'first', 'latest']) $(key).textContent = fmt(stats[key]);
  $('count').textContent = fmt(overview?.total ?? waves.length);
  $('usage').textContent = stats.usageReportedCount
    ? 'Σ входных по usage: ' + fmt(stats.providerInputSum) + ' · отчётов: ' + stats.usageReportedCount
    : 'usage провайдера пока нет';
  const first = overview?.first ?? waves.find(w => ['primary', 'unknown'].includes(w.kind) && w.capture !== 'gap');
  let current = view === 'start' ? first : waves.find(w => w.id === waveId) ?? waves.at(-1);
  if (current && view === 'waves') waveId = current.id;
  $('empty').hidden = Boolean(current); $('visual').hidden = !current;
  $('wave-controls').hidden = view !== 'waves' || !waves.length;
  $('blocks').replaceChildren(); $('cells').replaceChildren(); $('categories').replaceChildren();
  $('raw').hidden = !current; $('raw').open = false; $('raw-content').textContent = '';
  if (view === 'waves') renderWaves();
   if (!current) { $('coverage').textContent = 'Нет данных'; renderSkills(null); return; }
  const saved = fullWaves.get(current.id);
  if (saved) current = { ...saved, ...current, blocks: saved.blocks };
  else if (!loadingWaves.has(current.id)) {
    const id = current.id, epoch = sessionEpoch; loadingWaves.add(id);
    api('/api/wave/' + id).then(full => {
      if (epoch !== sessionEpoch) return;
      fullWaves.set(id, full);
      while (fullWaves.size > 4) fullWaves.delete(fullWaves.keys().next().value);
      render();
    }).catch(e => { if (epoch === sessionEpoch) error(e.message); })
      .finally(() => { if (epoch === sessionEpoch) loadingWaves.delete(id); });
  }
  renderSkills(current);
  const index = waves.findIndex(w => w.id === current.id);
  $('title').textContent = view === 'start' ? 'Из чего состоит первый захваченный запрос' : 'Запрос ' + current.id + ' / состав контекста';
  const incomplete = current.capture !== 'complete';
  $('coverage').textContent = incomplete ? 'Захват: ' + current.capture : current.context_completeness === 'client-payload'
    ? 'Клиентский payload захвачен' : 'Полный контекст неизвестен';
  const visual = tileCounts(current.blocks);
  visual.tiles.forEach(group => {
    const layer = layers[group.category] ?? layers.unknown;
    for (let i = 0; i < group.count; i++) {
      const tile = el('span', 'cell' + (selectedCategory && selectedCategory !== group.category ? ' dim' : ''));
      tile.style.setProperty('--color', layer[1]); tile.title = layer[0]; $('cells').append(tile);
    }
  });
  $('caption').textContent = 'Одна клетка ≈ ' + fmt(visual.unit) + ' токенов. Категории округлены вверх.' +
    (visual.unknown ? ' Размер вложений неизвестен и не показан клетками.' : '') + ' Нажмите категорию для выделения.';
  const grouped = new Map();
  current.blocks.forEach(block => {
    const entry = grouped.get(block.category) ?? { count: 0, tokens: 0 };
    entry.count++;
    entry.tokens = entry.tokens === null || block.estimated_tokens === null ? null : entry.tokens + block.estimated_tokens;
    grouped.set(block.category, entry);
  });
  grouped.forEach((group, key) => {
    const layer = layers[key] ?? layers.unknown;
    const row = el('button', 'row' + (selectedCategory === key ? ' selected' : ''));
    const swatch = el('span', 'swatch'); swatch.style.setProperty('--color', layer[1]);
    const name = el('span', 'name', layer[0]); name.append(el('span', 'desc', group.count + ' блоков · ' + layer[2]));
    const percent = group.tokens === null || !current.estimated_tokens ? '?' : (100 * group.tokens / current.estimated_tokens).toFixed(1) + '%';
    row.append(swatch, name, el('span', 'num', fmt(group.tokens)), el('span', 'num muted', percent));
    row.onclick = () => { selectedCategory = selectedCategory === key ? null : key; render(); };
    $('categories').append(row);
  });
  $('total').textContent = fmt(current.estimated_tokens);
  const usage = current.usage;
  $('wave-info').textContent = [new Date(current.received * 1000).toLocaleString('ru-RU'), current.transport,
    current.kind, current.model, 'ID: ' + current.request_id,
    usage ? 'Usage провайдера: ' + JSON.stringify(usage) : 'Usage отсутствует'].filter(Boolean).join(' · ');
  const warnings = [...current.warnings];
  if (overview?.storage_errors) warnings.push('Ошибок хранилища: ' + overview.storage_errors + '. Возможны пропуски захвата.');
  if (incomplete) warnings.push('Тело захвачено не полностью. Не интерпретируйте сумму как полный контекст.');
  if (view === 'start' && current.blocks.some(b => ['assistant', 'tool'].includes(b.category)))
    warnings.push('Запись началась после начала истории. Стартовые слои не равны контексту до первого сообщения.');
  if (index > 0) {
    const diff = compareBlocks(current.blocks, waves[index - 1].blocks);
    warnings.push('Изменение видимых блоков: +' + diff.added.length + ' / -' + diff.removed.length + '. Сжатие определяется только при kind=compaction.');
  }
  $('warnings').hidden = !warnings.length; $('warnings').textContent = warnings.join(' · ');
  renderBlocks(current.blocks.filter(b => !selectedCategory || b.category === selectedCategory));
  $('raw').ontoggle = async () => {
    if (!$('raw').open || $('raw-content').textContent) return;
    try { $('raw-content').textContent = JSON.stringify(await api('/api/request/' + current.id), null, 2); }
    catch (e) { error(e.message); }
  };
}

function renderSkills(current) {
  const container = $('skills'); container.replaceChildren();
  const rows = skillRows(current, overview);
  const coverage = overview?.skill_coverage;
  $('skill-coverage').textContent = coverage
    ? 'Сохранённых запросов: ' + fmt(coverage.request_count) + ' · gap: ' + fmt(coverage.gap_count)
      + ' · неполных: ' + fmt(coverage.incomplete_request_count)
      + ' · с неизвестным серверным состоянием: ' + fmt(coverage.server_state_unknown_count)
    : 'Статистика сессии ещё не загружена';
  if (!rows.length) {
    container.append(el('p', 'caption', 'Скиллы не распознаны в сохранённых данных. Это не означает нулевой расход.'));
    return;
  }
  const table = el('table', 'block-table skill-table'), head = el('thead'), heading = el('tr');
  ['Скилл / источник', 'Тип текста', '≈ В запросе', 'Доля запроса', '≈ За сессию', 'Запросов'].forEach(label => heading.append(el('th', '', label)));
  head.append(heading); table.append(head);
  const body = el('tbody');
  rows.forEach(skill => {
    const row = el('tr'), name = el('td', '', skill.name);
    name.append(el('div', 'caption', skill.attributions.map(value => value === 'source-metadata'
      ? 'метка источника' : value === 'format-heuristic' ? 'по формату (эвристика)' : value).join(' · ')));
    row.append(name, el('td', '', skill.kind === 'loaded' ? 'Полный текст' : 'Описание'),
      el('td', 'num', skill.selected_present ? fmt(skill.selected_tokens) : 'нет в захвате'),
      el('td', 'num', skill.percent === null ? '?' : skill.percent.toFixed(1) + '%'),
      el('td', 'num', fmt(skill.session_tokens)), el('td', 'num', fmt(skill.request_count)));
    body.append(row);
  });
  table.append(body); container.append(table);
}

function renderWaves() {
  const container = $('wave-controls'); container.replaceChildren();
  const strip = el('div', 'strip');
  const chosen = waves.findIndex(w => w.id === waveId);
  const left = Math.max(0, (chosen < 0 ? waves.length - 1 : chosen) - 30);
  const visible = waves.slice(left, left + 70);
  const max = Math.max(1, ...visible.map(w => w.estimated_tokens ?? 0));
  visible.forEach(w => {
    const button = el('button', (w.id === waveId ? 'active ' : '') + (w.capture === 'gap' ? 'gap' : ''));
    button.style.height = (16 + 55 * (w.estimated_tokens ?? 0) / max) + 'px';
    button.title = 'Волна ' + (waves.indexOf(w) + 1) + ' · ≈' + fmt(w.estimated_tokens);
    button.setAttribute('aria-label', button.title);
    button.onclick = () => { waveId = w.id; selectedCategory = null; render(); };
    strip.append(button);
  });
  const pager = el('div', 'pager');
  const older = el('button', '', 'Более ранние');
  older.disabled = paging || waves.length < 100;
  older.onclick = async () => {
    const epoch = sessionEpoch;
    paging = true; older.disabled = true;
    try {
       const previousPage = await api('/api/waves?' + query() + '&before=' + waves[0].id);
       if (epoch !== sessionEpoch) return;
      waves = [...previousPage, ...waves].slice(0, 200);
      if (previousPage.length) waveId = previousPage.at(-1).id;
      else older.textContent = 'Начало истории';
    } catch (e) { if (epoch === sessionEpoch) error(e.message); }
    finally { if (epoch === sessionEpoch) { paging = false; render(); } }
  };
  const previous = el('button', '', '←'); previous.setAttribute('aria-label', 'Предыдущая волна');
  const next = el('button', '', '→'); next.setAttribute('aria-label', 'Следующая волна');
  previous.disabled = chosen <= 0; next.disabled = chosen >= waves.length - 1;
  previous.onclick = () => { waveId = waves[chosen - 1].id; render(); };
  next.onclick = () => { waveId = waves[chosen + 1].id; render(); };
  pager.append(older, previous, el('span', '', 'Показано ' + waves.length + ' / всего ' + (overview?.total ?? waves.length)), next);
  container.append(strip, pager);
}

function renderBlocks(blocks) {
  const table = el('table', 'block-table');
  const heading = el('tr'); ['Слой', 'Источник / содержимое', '≈ Токены'].forEach(t => heading.append(el('th', '', t)));
  const head = el('thead'); head.append(heading); table.append(head);
  const body = el('tbody');
  // Bound DOM work; every omitted block remains available in the raw request.
  blocks.slice(0, 300).forEach(block => {
    const row = el('tr'); row.append(el('td', '', (layers[block.category] ?? layers.unknown)[0]));
    const cell = el('td'); const details = el('details');
    details.append(el('summary', 'block-source', block.source + ' · ' + block.attribution));
    const content = el('pre');
    details.ontoggle = () => { if (details.open) content.textContent = block.content ?? 'Содержимое загружается...'; };
    details.append(content); cell.append(details); row.append(cell, el('td', 'num', fmt(block.estimated_tokens))); body.append(row);
  });
  table.append(body); $('blocks').append(table);
  if (blocks.length > 300) $('blocks').append(el('p', 'caption', 'Показаны первые 300 блоков; остальные доступны в исходных данных.'));
}

for (const name of ['opencode', 'codex']) $(name).onclick = () => {
  client = name; sessionId = ''; resetSessionView(); render();
  $('opencode').classList.toggle('active', name === 'opencode'); $('codex').classList.toggle('active', name === 'codex');
  refresh().catch(e => error(e.message));
};
$('sessions').onchange = () => { sessionId = $('sessions').value; resetSessionView(); render(); refresh().catch(e => error(e.message)); };
for (const name of ['start', 'waves']) $(name).onclick = () => {
  view = name; selectedCategory = null;
  $('start').classList.toggle('active', view === 'start'); $('waves').classList.toggle('active', view === 'waves'); render();
};
$('record').onclick = async () => {
  const enabled = !sessions.find(s => s.client === client && s.session_id === sessionId)?.recording;
  if (enabled && !confirm('Сохранять полные тела новых запросов этой сессии? В них могут быть секреты, личные данные и исходный код. Хранение 7 суток.')) return;
  try { await api('/api/record', { method: 'POST', body: JSON.stringify({ client, session_id: sessionId, enabled }) }); await refresh(); }
  catch (e) { error(e.message); }
};
$('delete').onclick = async () => {
  if (!confirm('Удалить все захваченные данные выбранной сессии и выключить запись?')) return;
  try { await api('/api/session?' + query(), { method: 'DELETE' }); waveId = null; await refresh(); }
  catch (e) { error(e.message); }
};
$('login-form').onsubmit = event => { event.preventDefault(); login($('token').value.trim()).catch(e => error(e.message)); };
const privateToken = new URLSearchParams(location.hash.slice(1)).get('token');
if (privateToken) { history.replaceState(null, '', location.pathname); login(privateToken).catch(e => error(e.message)); }
else refresh().then(startLive).catch(e => { if (!e.message.includes('Нужен токен')) error(e.message); });
