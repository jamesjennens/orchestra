// The sign-in page (kittrial-5bb.525 slice d), run under Node with tests/web_dom_shim.mjs:
// web/js/views/auth.js `login` and `redeem` in the split frame. Prints one JSON object of
// what each page holds; tests/test_sign_in_page.py asserts on it.
//
// argv: shim URL, auth.js URL.
const [shimUrl, authUrl] = process.argv.slice(2);
await import(shimUrl);
globalThis.window = globalThis.window || { isSecureContext: false };
const auth = await import(authUrl);

const root = document.createElement('div');
const ctx = { me: null, options: {}, api: {}, root, href: (path) => '#' + path, go() {} };

function seen(page) {
  const svgs = page.all((e) => e.tagName === 'SVG');
  const marks = svgs.filter((e) => String(e.attributes.class || '').split(' ').includes('brand-mark'));
  const external = page.all((e) => ['href', 'src', 'srcset', 'action', 'xlink:href'].some((name) => {
    const value = e.attributes[name];
    return value !== undefined && /^(https?:)?\/\/|^[a-z][a-z0-9+.-]*:/i.test(String(value).trim()) && !String(value).startsWith('#');
  })).map((e) => e.tagName);
  const forms = page.all((e) => e.tagName === 'FORM');
  const resetButtons = page.all((e) => e.tagName === 'BUTTON' && e.attributes['data-reset-code'] !== undefined);
  return {
    h1: page.all((e) => e.tagName === 'H1').map((e) => e.textContent),
    forms: forms.length,
    inputs: forms.length ? forms[0].all((e) => e.tagName === 'INPUT').map((e) => [e.attributes.id, e.attributes.type || 'text']) : [],
    marks: marks.map((m) => ({ hidden: m.attributes['aria-hidden'], rects: m.all((e) => e.tagName === 'RECT').length,
      tile: (m.all((e) => e.tagName === 'RECT')[0] || { attributes: {} }).attributes.fill })),
    svgsNotHidden: svgs.filter((e) => e.attributes['aria-hidden'] !== 'true').length,
    external,
    side: page.all((e) => e.tagName === 'ASIDE').map((e) => e.attributes['aria-label']),
    // the reset link sits in the password field, drawn on its label's line, and comes after the input
    // in the document so Tab reaches the password box first
    reset: resetButtons.map((b) => {
      const kids = Array.from(b.parentNode.childNodes || b.parentNode.children || []);
      return { text: b.textContent, field: String(b.parentNode.attributes.class || b.parentNode.className),
        label: b.parentNode.all((e) => e.tagName === 'LABEL').map((l) => [l.attributes.for, l.textContent]),
        order: kids.map((k) => k.tagName === 'INPUT' ? 'input:' + k.attributes.id : k.tagName === 'LABEL' ? 'label' : k === b ? 'reset' : null).filter(Boolean) };
    }),
    _reset: resetButtons[0] || null,
  };
}

const out = {};
const login = seen(auth.login(ctx));
const resetButton = login._reset; delete login._reset;
out.login = login;
// Following the link opens the reset page in the same frame.
if (resetButton) await resetButton.dispatch('click');
const redeemPage = root.firstChild;
const redeem = redeemPage ? seen(redeemPage) : null;
if (redeem) delete redeem._reset;
out.redeem = redeem;
console.log(JSON.stringify(out));
