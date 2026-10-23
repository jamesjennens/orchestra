// The Files panel on the task page (kittrial-5bb.376, docs/TASK_FILES_DESIGN.md 3d).
// It lists a task's files and offers upload, a digest-checked download and withdrawal over
// the routes of kittrial-5bb.370. Every name and value is the server's or another person's
// text: it goes into the page as text, never as markup, and nothing is opened inline.
// Which controls show is computed here from the project role and the task's assignee; it
// only hides controls. The server decides, and its refusal is shown as its own sentence.

import { h, time, shortSha } from '../dom.js';
import { describe } from '../ui.js';
import { canWrite, canApprove } from './project.js';

const DIRECTIONS = { 'to-worker': 'To the worker', 'from-worker': 'From the worker' };
export const MISMATCH = 'This download did not match its record, so it was not saved.';

export async function sha256Hex(bytes) {
  const digest = await globalThis.crypto.subtle.digest('SHA-256', bytes);
  return Array.from(new Uint8Array(digest), (b) => b.toString(16).padStart(2, '0')).join('');
}

// The name a saved file gets: its last path part, with no control characters.
export function cleanFileName(name) {
  const last = String(name || '').split(/[\\/]/).pop().replace(/[\u0000-\u001f\u007f]/g, '').trim();
  return last && last !== '.' && last !== '..' ? last : 'download';
}

export function saveBytes(name, bytes) {
  const url = URL.createObjectURL(new Blob([bytes], { type: 'application/octet-stream' }));
  const link = h('a', { href: url, download: cleanFileName(name), rel: 'noopener' });
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 0);
}

export function sizeText(bytes) {
  if (!Number.isFinite(bytes)) return '';
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KiB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MiB`;
}

// What the page may offer: upload directions and, per file, withdraw. The actor the page acts
// as is ctx.me.id; the file's author and the task's assignee are compared with it exactly.
export function filesAuthority(ctx, project, task) {
  const me = ctx.me && typeof ctx.me.id === 'string' ? ctx.me.id : null;
  const open = !project.archived;
  const coordinator = open && canApprove(project);
  const assignee = open && canWrite(project) && Boolean(me) && typeof task.assignee === 'string'
    && task.assignee === me && task.status !== 'closed';
  const directions = [];
  if (coordinator) directions.push('to-worker');
  if (assignee) directions.push('from-worker');
  return {
    directions,
    mayWithdraw: (item) => open && canWrite(project)
      && (coordinator || (Boolean(me) && typeof item.by === 'string' && item.by === me)),
  };
}

export function taskFilesPanel(ctx, { pid, tid, task, project }, deps = {}) {
  if (!ctx.api || typeof ctx.api.taskFiles !== 'function') return null;
  const digest = deps.digest || sha256Hex;
  const save = deps.save || saveBytes;
  const authority = filesAuthority(ctx, project, task);
  const status = h('p', { class: 'small', role: 'alert', 'data-files-status': 'true', hidden: true });
  const list = h('ul', { class: 'open-items', 'data-files-list': 'true' });
  const body = h('div', { class: 'panel-body stack' }, status, list);
  let busy = false;

  function say(text) {
    status.textContent = text || '';
    status.hidden = !text;
  }

  async function guarded(work) {
    if (busy) return;
    busy = true;
    say('');
    try { await work(); } catch (error) { say(describe(error)); } finally { busy = false; }
  }

  async function download(item) {
    const got = await ctx.api.downloadTaskFile(pid, tid, item.sha256);
    const actual = await digest(got.bytes);
    const stated = typeof got.digest === 'string' ? got.digest.trim().toLowerCase() : '';
    if (actual !== item.sha256 || actual !== stated) { say(MISMATCH); return; }
    save(item.name, got.bytes);
  }

  function withdrawForm(item) {
    const reason = h('input', { type: 'text', name: 'reason', maxlength: 1000, 'aria-label': 'Reason for withdrawing ' + item.name });
    const form = h('form', { class: 'form', novalidate: true, 'data-withdraw': item.sha256 },
      reason,
      h('button', { type: 'submit' }, 'Withdraw'),
      h('button', { type: 'button', onclick: () => form.remove() }, 'Cancel'));
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      const text = String(reason.value || '').trim();
      if (!text) { say('Say why the file is withdrawn.'); return; }
      await guarded(async () => {
        await ctx.api.withdrawTaskFile(pid, tid, item.sha256, text);
        await load();
      });
    });
    return form;
  }

  function row(item) {
    const entry = h('li', { 'data-file': item.sha256 }, rowParts(item));
    const controls = [];
    if (item.may_download === true && !item.missing) {
      controls.push(h('button', { type: 'button', 'data-download': item.sha256, onclick: () => guarded(() => download(item)) }, 'Download'));
    }
    if (authority.mayWithdraw(item)) {
      controls.push(h('button', { type: 'button', 'data-withdraw-open': item.sha256, onclick: () => {
        if (!entry.querySelector('form')) entry.appendChild(withdrawForm(item));
      } }, 'Withdraw'));
    }
    if (controls.length) entry.appendChild(h('div', { class: 'actions' }, controls));
    return entry;
  }

  function rowParts(item) {
    const mine = ctx.me && typeof item.by === 'string' && item.by === ctx.me.id;
    return [
      h('strong', null, item.name),
      h('div', { class: 'small muted' },
        sizeText(item.bytes), ' · ', DIRECTIONS[item.direction] || item.direction, ' · ',
        mine ? 'you' : item.by, ' · ', time(item.at)),
      h('div', { class: 'small mono', title: item.sha256 }, 'sha256 ', shortSha(item.sha256)),
      item.note ? h('p', { class: 'small prose' }, item.note) : null,
      item.missing ? h('p', { class: 'small muted' }, 'The stored bytes are missing; ask an operator.') : null,
    ];
  }

  async function load() {
    const result = await ctx.api.taskFiles(pid, tid);
    const items = Array.isArray(result && result.items) ? result.items : [];
    list.replaceChildren(...items.map(row));
    if (!items.length) list.append(h('li', { class: 'muted' }, 'No files on this task.'));
  }

  function uploadForm() {
    const choices = authority.directions;
    if (!choices.length) return null;
    const input = h('input', { type: 'file', name: 'file', 'aria-label': 'File to attach' });
    const direction = choices.length > 1
      ? h('select', { name: 'direction', 'aria-label': 'Direction' }, choices.map((d) => h('option', { value: d }, DIRECTIONS[d])))
      : null;
    const form = h('form', { class: 'form', novalidate: true, 'data-upload': 'true' }, input, direction,
      h('button', { type: 'submit' }, choices.length === 1 ? (choices[0] === 'to-worker' ? 'Attach for the worker' : 'Attach from the worker') : 'Attach'));
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      const file = input.files && input.files[0];
      if (!file) { say('Choose a file first.'); return; }
      await guarded(async () => {
        const bytes = new Uint8Array(await file.arrayBuffer());
        const sha256 = await digest(bytes);
        await ctx.api.uploadTaskFile(pid, tid, { name: file.name, direction: direction ? direction.value : choices[0], bytes, sha256 });
        await load();
      });
    });
    return form;
  }

  const form = uploadForm();
  if (form) body.appendChild(form);
  load().catch((error) => say(describe(error)));
  return h('section', { class: 'panel', 'aria-labelledby': 'files-h', 'data-files-panel': 'true' },
    h('div', { class: 'panel-head' }, h('h2', { class: 'small', id: 'files-h' }, 'Files')), body);
}
