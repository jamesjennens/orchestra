// Runs web/js/views/proposals.js under Node against a real http_service (started by
// tests/test_http_proposals.py ScreenBehaviourCase). There is no browser: the shared DOM
// shim (tests/web_dom_shim.mjs) gives dom.js and ui.js what they use. Prints one JSON
// object of observations; the Python test asserts on it.
//
// This file used to carry a second copy of the shim. It now imports the shared one, so the
// browser-shaped childNodes/children (kittrial-5bb.342) are the same in every screen test.
//
// argv: api.js URL, proposals.js URL, base URL, project id, the key of an escalated proposal,
// the key of a submitted one, then name=token=userId triples.
const [apiUrl, proposalsUrl, base, project, escalated, fresh, ...people] = process.argv.slice(2);

await import(new URL('./web_dom_shim.mjs', import.meta.url));

const { createApi } = await import(apiUrl);
const screens = await import(proposalsUrl);

const sent = [];            // every request the page made: [method, path, idempotency key]
const fault = { next503: 0, next401: 0 };
function person(spec) {
  const [name, token, id] = spec.split('=');
  const transport = async (method, path, headers, body) => {
    sent.push([method, path, headers['Idempotency-Key'] || null]);
    if (method !== 'GET' && fault.next401 > 0) { fault.next401 -= 1; return { status: 401, data: { error: { code: 'unauthenticated', message: 'x' } } }; }
    const response = await fetch(base + path, { method, headers: { ...headers, Authorization: 'Bearer ' + token }, body });
    const text = await response.text();
    let data = null;
    try { data = text ? JSON.parse(text) : null; } catch { data = null; }
    if (method !== 'GET' && fault.next503 > 0) {
      // The write reached the server and committed; the page is told the outcome is unknown.
      fault.next503 -= 1;
      return { status: 503, data: { error: { code: 'uncertain', message: 'The operation may have committed' } } };
    }
    return { status: response.status, data };
  };
  const seen = { went: [], rendered: 0, lost: 0 };
  const ctx = { api: createApi(transport), me: { id }, href: (path) => '#' + path, go: (path) => seen.went.push(path),
    render: () => { seen.rendered += 1; }, sessionLost: () => { seen.lost += 1; }, query: new Map() };
  return [name, { ctx, seen }];
}
const who = Object.fromEntries(people.map(person));
const out = {};
const buttons = (root, label) => root.all((e) => e.tagName === 'BUTTON' && e.textContent === label);
const control = (root, id) => root.querySelector('#' + id);

// 1. The queue with more than 100 proposals: the newest is on the first page, the count
//    is the server's, and "Show older" pages to the end.
{
  const { ctx } = who.blair;
  const panel = await screens.queuePanel(ctx, project);
  const rows = () => panel.all((e) => e.tagName === 'TR' && e.className === 'row-link').length;
  out.queue = { text: panel.textContent.slice(0, 400), count: panel.querySelector('#h-proposals').textContent,
    hasNewest: panel.textContent.includes('The newest proposal of all'), rows: [rows()] };
  for (let i = 0; i < 5; i += 1) {
    const more = buttons(panel, 'Show older').filter((b) => !b.parentNode.hidden)[0];
    if (!more) break;
    await more.dispatch('click');
    out.queue.rows.push(rows());
  }
  out.queue.moreLeft = buttons(panel, 'Show older').filter((b) => !b.parentNode.hidden).length;
  const strip = await screens.myStrip(who.alex.ctx, project);
  out.strip = strip.textContent;
}

// 2. Submit again after an uncertain answer: the same Idempotency-Key, one proposal.
{
  const { ctx, seen } = who.alex;
  const before = (await ctx.api.proposals(project, { limit: 1 })).total;
  const section = screens.proposeForm(ctx, project);
  const form = section.querySelector('form');
  control(form, 'text').value = 'Submitted once, pressed twice.';
  fault.next503 = 1;
  const from = sent.length;
  await form.dispatch('submit');
  const first = { message: control(form, 'text-error').textContent, went: seen.went.length };
  await form.dispatch('submit');
  const posts = sent.slice(from).filter(([method]) => method === 'POST');
  out.retry = { first, went: seen.went.slice(), keys: posts.map((p) => p[2]),
    added: (await ctx.api.proposals(project, { limit: 1 })).total - before };
  // Different content is a different intent: a new key.
  control(form, 'text').value = 'A different proposal.';
  const again = sent.length;
  await form.dispatch('submit');
  out.retry.otherKey = sent.slice(again).filter(([method]) => method === 'POST')[0][2];
}

// 2b. The owner-decision form is an owner's (kittrial-5bb.261): a coordinator, who triages,
//     opens the escalated proposal and is told who decides; an owner gets the form.
if (who.cora) {
  const forms = (panel) => panel.all((e) => e.tagName === 'FORM').length;
  const coordinator = await screens.detailPanel(who.cora.ctx, project, escalated);
  const owner = await screens.detailPanel(who.dana.ctx, project, escalated);
  const triage = await screens.detailPanel(who.cora.ctx, project, fresh);
  out.decide = { coordinator: { forms: forms(coordinator), text: coordinator.textContent },
    owner: { forms: forms(owner), heading: owner.textContent.includes('Owner decision') },
    triage: { forms: forms(triage), heading: triage.textContent.includes('Triage') },
    reads: [(await who.cora.ctx.api.proposal(project, escalated)).can_decide, (await who.dana.ctx.api.proposal(project, escalated)).can_decide] };
}

// 3. A rule refusal shows the server's reason; a stale read says to reload.
{
  const { ctx, seen } = who.dana;
  const panel = await screens.detailPanel(ctx, project, escalated);
  const form = panel.all((e) => e.tagName === 'FORM')[0];
  control(form, 'decision_id').value = 'alpha-nope';
  await form.dispatch('submit');
  out.refusal = { message: control(form, 'to_state-error').textContent, rendered: seen.rendered };

  const blair = who.blair;
  const stale = await screens.detailPanel(blair.ctx, project, fresh);
  const view = await blair.ctx.api.proposal(project, fresh);
  await who.dana.ctx.api.disposeProposal(project, fresh, { previous: view.disposition_comment_id, proposal_sha256: view.sha256, to_state: 'under-review' });
  const staleForm = stale.all((e) => e.tagName === 'FORM')[0];
  await staleForm.dispatch('submit');
  out.stale = { message: control(staleForm, 'to_state-error').textContent };
}

// 4. Smaller things: a bad key never reaches a request; a lost session goes to sign-in.
{
  const { ctx, seen } = who.alex;
  const from = sent.length;
  const bad = await screens.detailPanel(ctx, project, '--help');
  out.badKey = { text: bad.textContent, requests: sent.length - from };
  const section = screens.proposeForm(ctx, project);
  const form = section.querySelector('form');
  control(form, 'text').value = 'After the session ended.';
  fault.next401 = 1;
  await form.dispatch('submit');
  out.lost = { lost: seen.lost };
  const outsider = await screens.queuePanel(who.erin.ctx, project);
  out.outsider = outsider.textContent;
}

// 5. "Who decides": only configured deciders who are owners here are offered; the note
//    names the others without a raw account id. (The proposal is under review by now.)
{
  const { ctx } = who.blair;
  const panel = await screens.detailPanel(ctx, project, fresh);
  const form = panel.all((e) => e.tagName === 'FORM')[0];
  const select = control(form, 'to_state');
  select.value = 'escalated-to-owner';
  await select.dispatch('change');
  const owner = control(form, 'owner_identity');
  out.deciders = { options: owner.all((e) => e.tagName === 'OPTION').map((o) => [o.value, o.textContent]),
    note: control(form, 'owner_identity-hint').textContent };
}

console.log(JSON.stringify(out));
process.exit(0);
