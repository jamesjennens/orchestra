// Tiny DOM builder. All text is set through textContent, never innerHTML, so task,
// comment and feedback content written by other people cannot inject markup.

export function h(tag, props, ...children) {
  const el = document.createElement(tag);
  if (props) {
    for (const [key, value] of Object.entries(props)) {
      if (value === undefined || value === null || value === false) continue;
      if (key === 'class') el.className = value;
      else if (key === 'dataset') Object.assign(el.dataset, value);
      else if (key.startsWith('on') && typeof value === 'function') el.addEventListener(key.slice(2), value);
      else if (key === 'value') el.value = value;
      else if (key === 'checked' || key === 'disabled' || key === 'selected' || key === 'hidden') el[key] = Boolean(value);
      else el.setAttribute(key, value === true ? '' : String(value));
    }
  }
  append(el, children);
  return el;
}

function append(el, children) {
  for (const child of children) {
    if (child === null || child === undefined || child === false) continue;
    if (Array.isArray(child)) append(el, child);
    else if (child instanceof Node) el.appendChild(child);
    else el.appendChild(document.createTextNode(String(child)));
  }
}

export function clear(el) {
  while (el.firstChild) el.removeChild(el.firstChild);
  return el;
}

export function mount(el, ...children) {
  clear(el);
  append(el, children);
  return el;
}

const SVG_NS = 'http://www.w3.org/2000/svg';
export function brandMark() {
  // Five staggered bars: parts entering in turn, the score of an orchestra.
  const svg = document.createElementNS(SVG_NS, 'svg');
  svg.setAttribute('viewBox', '0 0 22 22');
  svg.setAttribute('class', 'brand-mark');
  svg.setAttribute('aria-hidden', 'true');
  [[2, 12, 8], [6, 7, 13], [10, 3, 16], [14, 8, 11], [18, 5, 14]].forEach(([x, y, height], i) => {
    const rect = document.createElementNS(SVG_NS, 'rect');
    rect.setAttribute('x', x); rect.setAttribute('y', y);
    rect.setAttribute('width', '2.6'); rect.setAttribute('height', height);
    rect.setAttribute('rx', '1.3');
    rect.setAttribute('fill', i === 2 ? 'var(--accent)' : 'var(--ink-2)');
    svg.appendChild(rect);
  });
  return svg;
}

export function initials(name) {
  const parts = String(name || '?').replace(/[@._-]+/g, ' ').trim().split(/\s+/);
  return ((parts[0] || '?')[0] + (parts.length > 1 ? parts[parts.length - 1][0] : '')).toUpperCase();
}

const RTF = typeof Intl !== 'undefined' && Intl.RelativeTimeFormat ? new Intl.RelativeTimeFormat(undefined, { numeric: 'auto' }) : null;
export function ago(iso) {
  if (!iso) return '';
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return iso;
  const seconds = Math.round((then - Date.now()) / 1000);
  const units = [['year', 31536000], ['month', 2592000], ['week', 604800], ['day', 86400], ['hour', 3600], ['minute', 60]];
  for (const [unit, size] of units) {
    if (Math.abs(seconds) >= size) return RTF ? RTF.format(Math.round(seconds / size), unit) : iso;
  }
  return 'just now';
}

export function time(iso) {
  const el = h('time', { datetime: iso, title: iso ? new Date(iso).toLocaleString() : '' }, ago(iso));
  return el;
}

export function shortSha(sha) {
  return sha ? String(sha).slice(0, 9) : '';
}

let toastHost;
export function toast(message, kind) {
  if (!toastHost) {
    toastHost = h('div', { class: 'toasts', role: 'status', 'aria-live': 'polite' });
    document.body.appendChild(toastHost);
  }
  const el = h('div', { class: 'toast' + (kind ? ' ' + kind : '') }, message);
  toastHost.appendChild(el);
  setTimeout(() => el.remove(), kind === 'crit' ? 7000 : 3500);
}

// In-page confirmation (never window.confirm): resolves true only on explicit confirm.
export function confirmDialog({ title, body, confirmLabel = 'Confirm', danger = false }) {
  return new Promise((resolve) => {
    const dialog = h('dialog', { 'aria-labelledby': 'confirm-title' });
    const done = (value) => { dialog.close(); dialog.remove(); resolve(value); };
    dialog.append(
      h('div', { class: 'dialog-body' }, h('h2', { id: 'confirm-title' }, title), h('p', null, body)),
      h('div', { class: 'dialog-foot' },
        h('button', { type: 'button', onclick: () => done(false) }, 'Cancel'),
        h('button', { type: 'button', class: danger ? 'danger solid' : 'primary', onclick: () => done(true) }, confirmLabel)));
    dialog.addEventListener('cancel', (event) => { event.preventDefault(); done(false); });
    document.body.appendChild(dialog);
    dialog.showModal();
  });
}

// Copies text to the clipboard: the async Clipboard API where the page is a secure
// context, else a temporary off-screen textarea and execCommand('copy'). Resolves
// true when the copy happened. The textarea is removed at once and never persisted.
export async function copyText(text) {
  try {
    if (navigator.clipboard && window.isSecureContext) { await navigator.clipboard.writeText(text); return true; }
  } catch { /* fall back below */ }
  const area = h('textarea', { class: 'visually-hidden', readonly: true, 'aria-hidden': 'true', tabindex: '-1' });
  area.value = text;
  const host = document.querySelector('dialog[open]') || document.body; // a modal dialog is the only focusable layer
  const active = document.activeElement;
  host.appendChild(area);
  area.select();
  let ok = false;
  try { ok = document.execCommand('copy'); } catch { ok = false; }
  area.remove();
  if (active && active.focus) active.focus();
  return ok;
}

// A small button that copies `text` (or `text()`) and briefly says "Copied".
export function copyButton(label, text, { ariaLabel, what } = {}) {
  const button = h('button', { type: 'button', class: 'ghost', 'aria-label': ariaLabel || label }, label);
  button.addEventListener('click', async () => {
    const ok = await copyText(typeof text === 'function' ? text() : text);
    if (ok) {
      button.textContent = 'Copied';
      toast((what || 'Text') + ' copied');
      setTimeout(() => { button.textContent = label; }, 1500);
    } else {
      toast('Copy is blocked here: select the text and press Ctrl+C', 'crit');
    }
  });
  return button;
}

// Shows a one-time secret (reset value, worker credential). It is displayed once and
// never stored by the page.
export function secretDialog({ title, body, secret }) {
  const dialog = h('dialog', { 'aria-labelledby': 'secret-title' });
  const box = h('div', { class: 'secret', id: 'secret-value' }, secret);
  const copy = h('button', {
    type: 'button', onclick: async () => {
      if (await copyText(secret)) { toast('Copied'); return; }
      const range = document.createRange(); range.selectNodeContents(box); const sel = getSelection(); sel.removeAllRanges(); sel.addRange(range); toast('Selected — press Ctrl+C to copy');
    },
  }, 'Copy');
  dialog.append(
    h('div', { class: 'dialog-body' }, h('h2', { id: 'secret-title' }, title), h('p', null, body), box),
    h('div', { class: 'dialog-foot' }, copy, h('button', { type: 'button', class: 'primary', onclick: () => { dialog.close(); dialog.remove(); } }, 'Done')));
  document.body.appendChild(dialog);
  dialog.showModal();
}
