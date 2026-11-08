// The empty, loading and error states (kittrial-5bb.525 slice e): web/js/ui.js `empty`,
// `loading` and `errorState`, run under Node with tests/web_dom_shim.mjs. Prints one JSON
// object of their roles, text and retry; tests/test_web_states.py asserts on it.
//
// argv: shim URL, ui.js URL.
const [shimUrl, uiUrl] = process.argv.slice(2);
await import(shimUrl);
globalThis.window = globalThis.window || { isSecureContext: false };
const ui = await import(uiUrl);
const { ApiError } = await import(new URL('api.js', uiUrl).href);

const cls = (e) => String(e.attributes.class || e.className || '').split(/\s+/).filter(Boolean);
const by = (root, name) => root.all((e) => cls(e).includes(name));
const icons = (root) => root.all((e) => e.tagName === 'SVG').map((s) => s.attributes['aria-hidden']);
function shape(node) {
  return {
    classes: cls(node), role: node.attributes.role || null, state: node.attributes['data-state'] || null,
    busy: node.attributes['aria-busy'] || null, text: node.textContent,
    title: (node.all((e) => e.tagName === 'STRONG')[0] || { textContent: null }).textContent,
    paragraphs: node.all((e) => e.tagName === 'P').map((p) => [cls(p).join(' '), p.textContent]),
    buttons: node.all((e) => e.tagName === 'BUTTON').map((b) => b.textContent),
    act: by(node, 'state-act').map((e) => e.textContent), icons: icons(node),
  };
}

const out = {};
out.empty = shape(ui.empty('Nothing to confirm', 'An agent that asks to join appears here.'));
const go = document.createElement('a'); go.textContent = 'Add an agent';
out.emptyWithAction = shape(ui.empty('No agents yet', 'Add one for each assistant you run.', go));
out.emptyNoBody = shape(ui.empty('No tasks yet'));
const load = ui.loading('Loading your agents…');
out.loading = { ...shape(load), hidden: by(load, 'visually-hidden').map((e) => e.textContent),
  skeleton: by(load, 'skeleton').map((e) => e.attributes['aria-hidden']), rows: by(load, 'sk-row').length };
out.loadingDefault = by(ui.loading(), 'visually-hidden').map((e) => e.textContent);

let retried = 0;
const failure = { status: 503, code: 'busy', message: 'The server is busy just now.', requestId: 'req_4f19c2' };
const err = ui.errorState(failure, () => { retried += 1; });
out.error = shape(err);
await err.all((e) => e.tagName === 'BUTTON')[0].dispatch('click');
out.retried = retried;
out.error404 = shape(ui.errorState({ status: 404, message: 'No such task.' }));
out.error403 = shape(ui.errorState({ status: 403, message: 'Not a member.' }));
// e r2: failed READS, built as api.js builds them. The network one carries the transport's own write-time
// sentence (api.js fetchTransport), which a read must not repeat.
const sentence = (error) => shape(ui.errorState(error, () => {})).paragraphs.map((p) => p[1]);
out.read503Said = sentence(new ApiError(503, 'unavailable', 'The tracker did not answer in time.', null, 'req_1'));
out.read502Bare = sentence(new ApiError(502, undefined, undefined, null, null));
out.read504Code = sentence(new ApiError(504, 'timeout', undefined, null, null));
out.readNetwork = sentence(new ApiError(0, 'network', 'The server could not be reached. Your change may not have been saved; retry to check.', null, null));
out.writeStill = ui.describe(new ApiError(503, 'busy', 'busy', null, null));
console.log(JSON.stringify(out));
