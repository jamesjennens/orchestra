// Runs web/js/agentHistory.js under Node (kittrial-5bb.365): the History block loads once when
// opened, lists the rows newest first as text (a hostile label stays text), and shows the
// omitted count and the server's notes. tests/test_http_agents.py asserts on the JSON it prints.
//
// argv: shim URL, agentHistory.js URL.
const [shimUrl, historyUrl] = process.argv.slice(2);
await import(shimUrl);
const { historyPanel } = await import(historyUrl);

const HOSTILE = '<img src=x onerror="window.PWNED=1">';
let loads = 0;
const answer = {
  items: [
    { at: '2026-10-21T10:00:00Z', kind: 'disabled', action: 'agents.disable', by: 'Alex', revoked_count: 3 },
    { at: '2026-10-21T09:00:00Z', kind: 'credential revoked', action: 'agents.credentials.revoked', by: 'Alex',
      credential: 'cred_1', label: HOSTILE },
    { at: '2026-10-21T08:00:00Z', kind: 'scopes changed', action: 'agents.credentials.issued', by: 'Alex',
      credential: 'cred_2', scopes_before: ['tasks'], scopes_after: ['tasks', 'reviews'] },
  ],
  omitted: 4,
  notes: ['Older history is no longer kept.'],
};
const panel = historyPanel(async () => { loads += 1; return answer; });
const settle = async () => { for (let i = 0; i < 20; i += 1) await new Promise((resolve) => setTimeout(resolve, 5)); };
const before = panel.textContent;
panel.open = true;
await panel.dispatch('toggle');
await settle();
await panel.dispatch('toggle');                 // opened again: not loaded twice
await settle();
const rows = panel.all((e) => e.getAttribute && e.getAttribute('data-history-kind') !== null);
console.log(JSON.stringify({
  loadedOnceWhenOpened: loads === 1 && before.indexOf('disabled') === -1,
  rowsInOrder: rows.map((e) => e.getAttribute('data-history-kind')),
  disableSaysCount: rows[0].textContent.indexOf('3 credentials revoked') !== -1,
  hostileLabelIsText: rows[1].textContent.indexOf(HOSTILE) !== -1 && panel.all((e) => e.tagName === 'IMG').length === 0,
  scopesShown: rows[2].textContent.indexOf('tasks → tasks, reviews') !== -1,
  omittedShown: panel.textContent.indexOf('4 older events are not shown.') !== -1,
  noteShown: panel.all((e) => e.getAttribute && e.getAttribute('data-history-note') === 'true').length === 1,
  pwned: Boolean(globalThis.PWNED),
}));
