// An agent's history (kittrial-5bb.365): credentials issued, revoked and extended; the agent
// disabled, enabled or changed; projects given and taken; who did it and when. The server
// decides what each reader may see (the agent's owner sees labels and scopes, a project's
// owners see neither); this only shows what it answered, as text, newest first.
import { h, time } from './dom.js';
import { errorState } from './ui.js';

export function historyRow(row) {
  const parts = [time(row.at), ' · ', row.kind, row.by ? [' by ', row.by] : null];
  if (row.label || row.credential) parts.push(' · credential ', row.label || row.credential);
  if (row.revoked_count != null) parts.push(' · ', String(row.revoked_count), row.revoked_count === 1 ? ' credential revoked' : ' credentials revoked');
  if (row.scopes_before && row.scopes_after && row.kind === 'scopes changed') {
    parts.push(' · ', row.scopes_before.join(', ') || 'none', ' → ', row.scopes_after.join(', ') || 'none');
  }
  return h('li', { class: 'small', 'data-history-kind': row.kind }, parts);
}

// A collapsed "History" block that loads when opened. ``load`` answers the server's history.
export function historyPanel(load, { label = 'History' } = {}) {
  const body = h('div', { class: 'stack' });
  const details = h('details', { class: 'deliver', 'data-agent-history': 'true' }, h('summary', null, label), body);
  let loaded = false;
  async function draw() {
    let data;
    try { data = await load(); } catch (error) { body.replaceChildren(errorState(error, draw)); return; }
    const items = Array.isArray(data.items) ? data.items : [];
    body.replaceChildren(
      items.length ? h('ul', { class: 'agent-history' }, items.map(historyRow)) : h('p', { class: 'small muted' }, 'Nothing recorded.'),
      data.omitted ? h('p', { class: 'small muted' }, `${data.omitted} older events are not shown.`) : null,
      ...(Array.isArray(data.notes) ? data.notes : []).map((note) => h('p', { class: 'small muted', 'data-history-note': 'true' }, note)));
  }
  details.addEventListener('toggle', () => { if (details.open && !loaded) { loaded = true; draw(); } });
  return details;
}
