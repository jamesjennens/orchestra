import assert from 'node:assert/strict';
import { webcrypto } from 'node:crypto';
const [shim, moduleUrl, apiUrl] = process.argv.slice(2);
await import(shim);
if (!globalThis.crypto || !globalThis.crypto.subtle) globalThis.crypto = webcrypto;
const { taskFilesPanel, filesAuthority, sha256Hex, MISMATCH, cleanFileName } = await import(moduleUrl);
const { createApi } = await import(apiUrl);

const text = (s) => Buffer.from(s);
const ME = 'user-1';
const GOOD = text('the good bytes');
const GOOD_SHA = await sha256Hex(GOOD);

function file(name, content) {
  return { name, arrayBuffer: async () => content.buffer.slice(content.byteOffset, content.byteOffset + content.byteLength) };
}
function item(extra = {}) {
  return { sha256: GOOD_SHA, name: 'notes.txt', bytes: GOOD.length, media_type: 'application/octet-stream',
    direction: 'to-worker', by: 'coord-1', at: '2026-10-22T10:00:00Z', note: null, may_download: true, ...extra };
}
function makeCtx(items, overrides = {}) {
  const calls = { list: 0, download: [], upload: [], withdraw: [] };
  const api = {
    taskFiles: async () => { calls.list += 1; return { task: 't-1', items: overrides.items ? overrides.items() : items, total: items.length, may_download: true }; },
    downloadTaskFile: async (pid, tid, sha) => { calls.download.push(sha); return overrides.download ? overrides.download() : { bytes: GOOD, digest: GOOD_SHA }; },
    uploadTaskFile: async (pid, tid, body) => { calls.upload.push(body); if (overrides.upload) return overrides.upload(); return { ok: true }; },
    withdrawTaskFile: async (pid, tid, sha, reason) => { calls.withdraw.push({ sha, reason }); if (overrides.withdraw) return overrides.withdraw(); return { ok: true }; },
  };
  return { ctx: { api, me: { id: overrides.me === undefined ? ME : overrides.me } }, calls };
}
const one = (root, attr, value) => root.all((e) => attr in e.attributes && (value === undefined || e.attributes[attr] === value))[0] || null;
const settle = () => new Promise((resolve) => setTimeout(resolve, 0));
const buttons = (panel, label) => panel.all((e) => e.tagName === 'BUTTON' && e.textContent === label);
let tested = 0;

// 1. A coordinator sees the list, newest first, as text, with every control.
{
  const items = [item({ name: 'second.txt', sha256: 'b'.repeat(64), by: ME, at: '2026-10-22T11:00:00Z', note: 'for the worker' }),
    item({ name: 'first.txt', sha256: 'a'.repeat(64), may_download: true })];
  const { ctx, calls } = makeCtx(items);
  const panel = taskFilesPanel(ctx, { pid: 'p', tid: 't-1', task: { assignee: 'agent-1', status: 'in_progress' }, project: { role: 'coordinator' } }, { save() {} });
  await settle();
  assert.equal(calls.list, 1);
  const rows = panel.all((e) => e.attributes['data-file']);
  assert.deepEqual(rows.map((r) => r.attributes['data-file']), ['b'.repeat(64), 'a'.repeat(64)]);
  assert.match(rows[0].textContent, /second\.txt/);
  assert.match(rows[0].textContent, /14 B/);
  assert.match(rows[0].textContent, /To the worker/);
  assert.match(rows[0].textContent, /you/);
  assert.match(rows[0].textContent, /sha256 bbbbbbbbb/);
  assert.match(rows[0].textContent, /for the worker/);
  assert.match(rows[1].textContent, /coord-1/);
  assert.equal(buttons(panel, 'Download').length, 2);
  assert.equal(buttons(panel, 'Withdraw').length, 2);
  assert.ok(one(panel, 'data-upload', 'true'));
  tested++;
}

// 2. A viewer sees the list and no control; a file the list does not allow has no download.
{
  const { ctx } = makeCtx([item({ may_download: false, by: ME })]);
  const panel = taskFilesPanel(ctx, { pid: 'p', tid: 't-1', task: { assignee: ME, status: 'in_progress' }, project: { role: 'viewer' } });
  await settle();
  assert.equal(panel.all((e) => e.attributes['data-file']).length, 1);
  assert.equal(one(panel, 'data-upload', 'true'), null);
  assert.equal(buttons(panel, 'Download').length, 0);
  assert.equal(buttons(panel, 'Withdraw').length, 0);
  tested++;
}

// 3. The assignee attaches only from the worker and withdraws only their own file.
{
  const { ctx } = makeCtx([item({ by: ME, sha256: 'c'.repeat(64) }), item({ by: 'coord-1', sha256: 'd'.repeat(64) })]);
  const panel = taskFilesPanel(ctx, { pid: 'p', tid: 't-1', task: { assignee: ME, status: 'in_progress' }, project: { role: 'contributor' } });
  await settle();
  assert.ok(one(panel, 'data-upload', 'true'));
  assert.equal(panel.querySelector('select'), null);
  assert.match(one(panel, 'data-upload', 'true').textContent, /Attach from the worker/);
  assert.equal(buttons(panel, 'Withdraw').length, 1);
  const own = panel.all((e) => e.attributes['data-withdraw-open']);
  assert.equal(own[0].attributes['data-withdraw-open'], 'c'.repeat(64));
  tested++;
}

// 4. The actor match is exact; a closed task or an archived project offers no attach.
{
  const base = { role: 'contributor' };
  const ctx = { me: { id: 'user-1' } };
  const exact = filesAuthority(ctx, base, { assignee: 'user-1', status: 'in_progress' });
  assert.deepEqual(exact.directions, ['from-worker']);
  assert.equal(exact.mayWithdraw({ by: 'user-1' }), true);
  for (const by of ['USER-1', 'user-10', ' user-1', 'user-1 ', '', null, undefined, 1]) assert.equal(exact.mayWithdraw({ by }), false, String(by));
  assert.deepEqual(filesAuthority(ctx, base, { assignee: 'user-10', status: 'in_progress' }).directions, []);
  assert.deepEqual(filesAuthority(ctx, base, { assignee: 'user-1', status: 'closed' }).directions, []);
  assert.deepEqual(filesAuthority({ me: null }, base, { assignee: null, status: 'open' }).directions, []);
  assert.deepEqual(filesAuthority({ me: { id: null } }, base, { assignee: null, status: 'open' }).directions, []);
  const archived = filesAuthority(ctx, { role: 'coordinator', archived: true }, { assignee: 'user-1', status: 'in_progress' });
  assert.deepEqual(archived.directions, []);
  assert.equal(archived.mayWithdraw({ by: 'user-1' }), false);
  assert.deepEqual(filesAuthority(ctx, { role: 'owner' }, { assignee: 'user-1', status: 'in_progress' }).directions, ['to-worker', 'from-worker']);
  tested++;
}

// 5. A matching download is saved under the cleaned name; a digest that does not match saves nothing.
{
  const saved = [];
  const project = { role: 'coordinator' };
  const task = { assignee: 'agent-1', status: 'in_progress' };
  for (const [download, saves] of [
    [() => ({ bytes: GOOD, digest: GOOD_SHA }), 1],
    [() => ({ bytes: text('other bytes'), digest: GOOD_SHA }), 0],
    [() => ({ bytes: GOOD, digest: 'f'.repeat(64) }), 0],
    [() => ({ bytes: GOOD, digest: null }), 0],
  ]) {
    saved.length = 0;
    const { ctx } = makeCtx([item({ name: '../evil\\name.txt' })], { download });
    const panel = taskFilesPanel(ctx, { pid: 'p', tid: 't-1', task, project }, { save: (name, bytes) => saved.push({ name, bytes }) });
    await settle();
    await buttons(panel, 'Download')[0].dispatch('click');
    assert.equal(saved.length, saves);
    const status = one(panel, 'data-files-status', 'true');
    if (saves) { assert.equal(status.hidden, true); assert.equal(saved[0].name, '../evil\\name.txt'); }
    else { assert.equal(status.hidden, false); assert.equal(status.textContent, MISMATCH); }
  }
  assert.equal(cleanFileName('../evil\\name.txt'), 'name.txt');
  assert.equal(cleanFileName('..'), 'download');
  assert.equal(cleanFileName('a\u0000b\nc.txt'), 'abc.txt');
  tested++;
}

// 6. A hostile name is text: no element is made from it.
{
  const hostile = '<img src=x onerror=alert(1)><script>boom()</script>';
  const { ctx } = makeCtx([item({ name: hostile, note: '<b>bold</b>', by: '<i>who</i>' })]);
  const panel = taskFilesPanel(ctx, { pid: 'p', tid: 't-1', task: { assignee: 'a', status: 'open' }, project: { role: 'coordinator' } });
  await settle();
  for (const tag of ['IMG', 'SCRIPT', 'B', 'I']) assert.equal(panel.all((e) => e.tagName === tag).length, 0, tag);
  assert.ok(panel.textContent.includes(hostile));
  assert.ok(panel.textContent.includes('<b>bold</b>'));
  tested++;
}

// 7. A refusal (403) from upload and from withdraw shows its one sentence and changes nothing.
{
  const refusal = Object.assign(new Error('Only the task\'s current assignee attaches files from the worker, and only while the task is open.'), { status: 403 });
  const { ctx, calls } = makeCtx([item({ by: ME })], { upload() { throw refusal; }, withdraw() { throw refusal; } });
  const panel = taskFilesPanel(ctx, { pid: 'p', tid: 't-1', task: { assignee: ME, status: 'in_progress' }, project: { role: 'contributor' } });
  await settle();
  const before = panel.querySelector('ul').textContent;
  const form = one(panel, 'data-upload', 'true');
  form.querySelector('input').files = [file('x.bin', text('abc'))];
  await form.dispatch('submit');
  const status = one(panel, 'data-files-status', 'true');
  assert.equal(status.hidden, false);
  assert.equal(status.textContent, refusal.message);
  assert.equal(calls.list, 1);
  assert.equal(panel.querySelector('ul').textContent, before);
  assert.equal(calls.upload.length, 1);
  assert.equal(calls.upload[0].direction, 'from-worker');
  assert.equal(calls.upload[0].sha256, await sha256Hex(text('abc')));
  status.textContent = ''; status.hidden = true;
  await buttons(panel, 'Withdraw')[0].dispatch('click');
  const wform = one(panel, 'data-withdraw');
  wform.querySelector('input').value = 'wrong file';
  await wform.dispatch('submit');
  assert.equal(status.textContent, refusal.message);
  assert.equal(calls.list, 1);
  assert.equal(panel.querySelector('ul').textContent.includes('notes.txt'), true);
  tested++;
}

// 8. A good upload or withdrawal reloads the list; withdrawing needs a reason.
{
  let items = [item({ by: ME })];
  const { ctx, calls } = makeCtx(items, { items: () => items });
  const panel = taskFilesPanel(ctx, { pid: 'p', tid: 't-1', task: { assignee: ME, status: 'open' }, project: { role: 'owner' } });
  await settle();
  const form = one(panel, 'data-upload', 'true');
  assert.ok(form.querySelector('select'));
  form.querySelector('input').files = [file('new.txt', text('zzz'))];
  form.querySelector('select').value = 'to-worker';
  await form.dispatch('submit');
  assert.equal(calls.list, 2);
  assert.deepEqual(calls.upload.map((u) => [u.name, u.direction]), [['new.txt', 'to-worker']]);
  await buttons(panel, 'Withdraw')[0].dispatch('click');
  const wform = one(panel, 'data-withdraw');
  await wform.dispatch('submit');
  assert.equal(calls.withdraw.length, 0);
  assert.match(one(panel, 'data-files-status', 'true').textContent, /Say why/);
  wform.querySelector('input').value = 'superseded';
  items = [];
  await wform.dispatch('submit');
  assert.deepEqual(calls.withdraw, [{ sha: GOOD_SHA, reason: 'superseded' }]);
  assert.equal(calls.list, 3);
  assert.match(panel.querySelector('ul').textContent, /No files/);
  tested++;
}

// 9. The api sends the bytes raw with the digest, the CSRF token and a key kept across an uncertain retry.
{
  const binary = [];
  const json = [];
  let lost = true;
  const api = createApi(async (method, path, headers, body) => {
    json.push({ method, path, headers, body });
    if (path === '/v1/sessions') return { status: 200, data: { user: { id: ME }, session: { csrf_token: 'csrf-1' } } };
    return { status: 200, data: { ok: true } };
  }, async (method, path, headers, body) => {
    binary.push({ method, path, headers, body });
    if (method === 'GET') return { status: 200, bytes: GOOD, digest: GOOD_SHA };
    if (lost) { lost = false; return { status: 503, data: { error: { code: 'uncertain', message: 'unknown' } } }; }
    return { status: 201, data: { sha256: GOOD_SHA } };
  });
  await api.login('u', 'p');
  const args = { name: 'a b.txt', direction: 'from-worker', bytes: GOOD, sha256: GOOD_SHA };
  await assert.rejects(api.uploadTaskFile('p', 't-1', args), (e) => e.status === 503);
  await api.uploadTaskFile('p', 't-1', args);
  const [first, second] = binary;
  assert.equal(first.method, 'PUT');
  assert.equal(first.path, '/v1/projects/p/tasks/t-1/files?name=a+b.txt&direction=from-worker');
  assert.equal(first.body, GOOD);
  assert.equal(first.headers['Content-Type'], 'application/octet-stream');
  assert.equal(first.headers['X-Orchestra-SHA256'], GOOD_SHA);
  assert.equal(first.headers['X-CSRF-Token'], 'csrf-1');
  assert.ok(first.headers['Idempotency-Key']);
  assert.equal(first.headers['Idempotency-Key'], second.headers['Idempotency-Key']);
  const got = await api.downloadTaskFile('p', 't-1', GOOD_SHA);
  assert.equal(binary[2].method, 'GET');
  assert.equal(binary[2].headers['X-CSRF-Token'], undefined);
  assert.deepEqual([got.bytes, got.digest], [GOOD, GOOD_SHA]);
  await api.withdrawTaskFile('p', 't-1', GOOD_SHA, 'why');
  const withdraw = json[json.length - 1];
  assert.equal(withdraw.path, `/v1/projects/p/tasks/t-1/files/${GOOD_SHA}/withdraw`);
  assert.equal(withdraw.headers['X-CSRF-Token'], 'csrf-1');
  assert.deepEqual(JSON.parse(withdraw.body), { reason: 'why' });
  tested++;
}
console.log(JSON.stringify({ tested, success: true }));
