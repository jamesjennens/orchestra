// Prototype entry: the real interface running against the in-memory mock service
// with synthetic sample data. Nothing leaves the browser; reloading resets it.
import { start } from './app.js';
import { createMock } from './mock.js';
import { h } from './dom.js';

const PEOPLE = [
  ['morgan', 'Morgan Ellis', 'superuser'],
  ['priya', 'Priya Raman', 'project owner'],
  ['tomasz', 'Tomasz Nowak', 'contributor'],
  ['kestrel', 'Kestrel (agent of Tomasz)', 'agent'],
  ['lena', 'Lena Fischer', 'viewer'],
];

let ctx;
async function signInAs(username) {
  try { await ctx.api.logout(); } catch { /* not signed in */ }
  const user = await ctx.api.login(username, 'demo');
  await ctx.signedIn(user);
}

const loginHint = () => h('div', { class: 'stack' },
  h('div', { class: 'banner info' }, 'Prototype with sample data. Every password is “demo”. Pick a person to see what they can do.'),
  h('div', { class: 'demo-accounts' }, PEOPLE.map(([username, name, role]) =>
    h('button', { type: 'button', onclick: () => signInAs(username) }, name, h('span', { class: 'role' }, role)))));

const banner = (c) => {
  const select = h('select', { id: 'proto-person', 'aria-label': 'Signed in as', onchange: (e) => signInAs(e.target.value) },
    PEOPLE.map(([username, name, role]) => h('option', { value: username, selected: c.me && c.me.username === username }, `${name} — ${role}`)));
  return h('div', { class: 'proto-bar', role: 'note' },
    h('span', null, h('strong', null, 'Prototype'), ' · sample data only, resets on reload · server rules (roles, conflicts, retries) are simulated'),
    h('span', { class: 'toolbar' }, h('label', { for: 'proto-person' }, 'View as'), select));
};

// A private copy may ship data/harness.json (a real project's requirements); the
// public repository never contains it.
async function extraData() {
  try { const r = await fetch('data/harness.json', { cache: 'no-store' }); return r.ok ? await r.json() : null; } catch { return null; }
}

extraData().then((extra) => start(document.getElementById('app'), { transport: createMock({ extra }), loginHint, banner, features: { requirements: true } })).then((c) => { ctx = c; });
