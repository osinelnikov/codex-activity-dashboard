// Behavioral checks without browser dependencies. Rendering is checked separately.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function deferred() {
  let resolve;
  const promise = new Promise(done => { resolve = done; });
  return { promise, resolve };
}
function detail(id, tokens = 10, complete = true) {
  return { id, title: id, cwd: '/demo', tokens: { total_tokens: tokens },
    loading_progress: { complete, bytes_read: 1, file_size: 2 }, freshness_seconds: 0,
    turns: {}, active_tools: [], tool_stats: {}, agents: [], skills: [], files_changed: [], log: [] };
}
function response(value) { return { ok: true, json: async () => value }; }
const elements = new Map();
function element(id) {
  if (!elements.has(id)) elements.set(id, {
    value: '', textContent: '', dataset: {}, classList: { toggle() {} },
    addEventListener() {}, click() {}, showModal() {}, close() {},
    set innerHTML(value) {
      this.html = value;
      if (id === 'session') this.value = /value="([^"]*)"/.exec(value)?.[1] || '';
    },
    get innerHTML() { return this.html || ''; }
  });
  return elements.get(id);
}
let fetcher = async url => url.startsWith('/api/sessions') ? response([{ id: 'a', title: 'a' }]) : response(detail('a'));
const downloads = [];
const context = {
  document: { getElementById: element, querySelectorAll: () => [], addEventListener() {},
    createElement: () => ({ click() { downloads.push(this.href); } }) },
  fetch: url => fetcher(url), localStorage: { getItem() {}, setItem() {}, removeItem() {} },
  URL: { createObjectURL: blob => blob, revokeObjectURL() {} }, Blob,
  setTimeout: (fn, delay) => { if (delay <= 20) queueMicrotask(fn); }, clearTimeout() {},
  setInterval() {}, window: { print() {} }
};
const file = process.env.DASHBOARD_APP_SOURCE || 'static/app.js';
const code = fs.readFileSync(file, 'utf8').replace(/\}\)\(\);\s*$/, 'globalThis.api = { loadSession, loadSessions, downloadReport, get current() { return current; } }; })();');
vm.runInNewContext(code, context);
const settle = () => new Promise(resolve => setImmediate(resolve));

(async () => {
  await settle();
  assert.equal(context.api.current.id, 'a');
  const oldDetail = deferred();
  fetcher = async url => url.endsWith('/a') ? { ok: true, json: () => oldDetail.promise } : response(detail('b'));
  const pending = context.api.loadSession('a');
  await settle();
  element('session').value = 'b';
  await context.api.loadSession('b');
  oldDetail.resolve(detail('a'));
  await pending;
  assert.equal(context.api.current.id, 'b', 'late JSON must not replace selected session');

  const oldList = deferred();
  fetcher = async url => url.includes('old') ? { ok: true, json: () => oldList.promise }
    : url.startsWith('/api/sessions') ? response([{ id: 'b', title: 'b' }]) : response(detail('b'));
  const pendingList = context.api.loadSessions('old');
  await settle();
  await context.api.loadSessions('new');
  oldList.resolve([{ id: 'a', title: 'a' }]);
  await pendingList;
  assert.equal(element('session').value, 'b', 'late JSON list must not undo newer search');

  fetcher = async () => response([]);
  await context.api.loadSessions('none');
  assert.equal(context.api.current, null, 'empty search must not retain exportable old session');
  assert.equal(element('title').textContent, 'No sessions found');

  fetcher = async () => { throw new Error('offline'); };
  element('session').value = 'a';
  await context.api.loadSession('a');
  assert.match(element('connection').textContent, /API UNAVAILABLE/);
  fetcher = async () => response(detail('a'));
  await context.api.loadSession('a');
  assert.equal(element('connection').textContent, 'LOCAL · READ ONLY');

  // Export refreshes the selected journal instead of reusing an old complete snapshot.
  element('scope').value = 'current'; element('format').value = 'json';
  const exportData = deferred();
  fetcher = async () => ({ ok: true, json: () => exportData.promise });
  const exporting = context.api.downloadReport();
  await settle();
  fetcher = async () => response(detail('b'));
  element('session').value = 'b';
  await context.api.loadSession('b');
  exportData.resolve(detail('a', 99));
  await exporting;
  const exported = JSON.parse(await downloads.at(-1).text());
  assert.equal(exported[0].id, 'a'); assert.equal(exported[0].tokens.total_tokens, 99);

  element('scope').value = 'all'; element('format').value = 'md';
  fetcher = async url => url.startsWith('/api/sessions') ? response([{ id: 'a' }])
    : response({ ...detail('a', 7, false), title: '<img src=x>', files_changed: ['[x](https://example.test)'],
      tool_stats: { 'review-tool': { count: 2, errors: 1, average_seconds: 1 } },
      agents: [{ agent_id: 'agent1', parent_id: 'a', model: 'test-model' }], compactions: 3,
      limits: { observed_at: 'now', primary: { used_percent: 25 } } });
  await context.api.downloadReport();
  const md = await downloads.at(-1).text();
  assert.match(md, /summary is provisional/);
  assert.doesNotMatch(md, /<img/);
  assert.doesNotMatch(md, /\[x\]\(https:/);
  assert.match(md, /## Timing/); assert.match(md, /2 calls/); assert.match(md, /agent1/);
  assert.ok(md.includes('used\\_percent')); assert.match(md, /Compactions: 3/);

  const previousDownloads = downloads.length;
  fetcher = async () => ({ ok: false });
  await element('download').onclick();
  assert.equal(downloads.length, previousDownloads);
  assert.match(element('connection').textContent, /EXPORT ERROR/);
  console.log('PASS: delayed detail/list JSON, empty search, network failure/recovery, fresh consistent export, partial Markdown, export failure');
})().catch(error => { console.error(error); process.exitCode = 1; });
