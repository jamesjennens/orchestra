"""The sign-in page (kittrial-5bb.525 slice d): what the split frame holds.

tests/web_sign_in_page.mjs renders web/js/views/auth.js `login` and `redeem` under Node (the
DOM shim). This module asserts, one rule per test:
- each page has exactly one h1, and its form keeps the field ids the password tests and the
  focus rule rely on (username and password; the reset page's account);
- the mark is the new tile: aria-hidden, four rects (the tile and three bars), the tile in the accent;
- every SVG is decorative (aria-hidden) and nothing loads from elsewhere (no external href or src);
- "Have a reset code?" sits on the Password label's line, after the input in tab order, and opens the reset page.
Skips when Node is not installed.
"""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

KIT = Path(__file__).resolve().parent.parent
_RUN = {}


def observed(case):
    if 'data' not in _RUN:
        node = shutil.which('node')
        if not node:
            case.skipTest('node is not installed')
        proc = subprocess.run([node, str(KIT / 'tests' / 'web_sign_in_page.mjs'),
                               (KIT / 'tests' / 'web_dom_shim.mjs').as_uri(),
                               (KIT / 'web' / 'js' / 'views' / 'auth.js').as_uri()],
                              capture_output=True, text=True, timeout=120)
        case.assertEqual(0, proc.returncode, proc.stderr[-2000:])
        _RUN['data'] = json.loads(proc.stdout.strip().splitlines()[-1])
    return _RUN['data']


class SignInPage(unittest.TestCase):

    def test_one_h1_and_the_fields_keep_their_ids(self):
        data = observed(self)
        self.assertEqual(['Sign in'], data['login']['h1'])
        self.assertEqual(1, data['login']['forms'])
        self.assertEqual([['username', 'text'], ['password', 'password']], data['login']['inputs'])
        self.assertIsNotNone(data['redeem'], 'the reset link did not open the reset page')
        self.assertEqual(['Set a new password'], data['redeem']['h1'])
        self.assertEqual('account', data['redeem']['inputs'][0][0])

    def test_the_mark_is_the_tile_with_three_bars(self):
        for page in ('login', 'redeem'):
            self.assertIsNotNone(observed(self)[page], page + ' was not rendered')
            marks = observed(self)[page]['marks']
            self.assertTrue(marks, page)
            for mark in marks:
                self.assertEqual({'hidden': 'true', 'rects': 4, 'tile': 'var(--accent)'}, mark, page)

    def test_decoration_is_hidden_and_nothing_loads_from_elsewhere(self):
        for page in ('login', 'redeem'):
            data = observed(self)[page]
            self.assertIsNotNone(data, page + ' was not rendered')
            self.assertEqual(0, data['svgsNotHidden'], page)
            self.assertEqual([], data['external'], page)
            self.assertEqual(['About Orchestra'], data['side'], page)

    def test_the_reset_code_link_sits_beside_the_password_label(self):
        reset = observed(self)['login']['reset']
        self.assertEqual(1, len(reset))
        self.assertEqual('Have a reset code?', reset[0]['text'])
        self.assertEqual(['field', 'with-aside'], reset[0]['field'].split())
        self.assertEqual([['password', 'Password']], reset[0]['label'])
        # keyboard order: Tab reaches the password box before the link
        self.assertEqual(['label', 'input:password', 'reset'], reset[0]['order'])



# kittrial-5bb.525 r2 (d): the phone fold, measured in a real headless Chromium (found as test_site_look_523
# finds it). The real login view and app.css: at 375 px the brand side is not displayed and the card's own
# mark is; at 1280 px the reverse. Kills the reviewer's MD1 (the phone rule's `.auth-side { display: none }`).
def fold_page():
    js = (KIT / 'web' / 'js').as_uri()
    css = (KIT / 'web' / 'css' / 'app.css').as_uri()
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><link rel="stylesheet" href="{css}"></head>
<body><div id="app"></div><script type="module">
import {{ login }} from '{js}/views/auth.js';
const out = {{}};
try {{
  const root = document.getElementById('app');
  root.replaceChildren(login({{ options: {{}}, api: {{}}, root }}));
  await new Promise((r) => setTimeout(r, 50));
  const box = (sel) => {{ const el = document.querySelector(sel); if (!el) return null; const r = el.getBoundingClientRect();
    return {{ display: getComputedStyle(el).display, width: Math.round(r.width), height: Math.round(r.height) }}; }};
  out.side = box('.auth-side');
  out.cardMark = box('.auth-card-brand');
  out.cardMarkSvg = box('.auth-card-brand .brand-mark');
  out.scroll = document.documentElement.scrollWidth - document.documentElement.clientWidth;
}} catch (e) {{ out.error = String(e && e.stack || e); }}
document.title = 'MEASURE' + JSON.stringify(out);
</script></body></html>"""


def measure_fold(width, height):
    import html as html_lib, re, tempfile
    from test_site_look_523 import find_browser
    with tempfile.TemporaryDirectory() as tmp:
        page = Path(tmp) / 'fold.html'
        page.write_text(fold_page(), encoding='utf-8')
        proc = subprocess.run([find_browser(), '--headless', '--no-sandbox', '--disable-gpu', '--allow-file-access-from-files',
                               f'--window-size={width},{height}', '--virtual-time-budget=5000', '--dump-dom', page.as_uri()],
                              capture_output=True, text=True, timeout=120)
    m = re.search(r'<title>(MEASURE.*?)</title>', proc.stdout, re.S)
    if not m:
        raise AssertionError('measurement did not run: ' + proc.stderr[-600:])
    return json.loads(html_lib.unescape(m.group(1))[len('MEASURE'):])


def _browser():
    from test_site_look_523 import find_browser
    return find_browser()


@unittest.skipUnless(_browser(), 'no headless chromium found (set ORCHESTRA_WEB_TEST_BROWSER)')
class SignInPhoneFold(unittest.TestCase):

    def test_the_brand_side_folds_away_on_a_phone(self):
        data = measure_fold(375, 812)
        self.assertNotIn('error', data, data.get('error'))
        self.assertEqual('none', data['side']['display'], data)
        self.assertEqual((0, 0), (data['side']['width'], data['side']['height']))
        self.assertNotEqual('none', data['cardMark']['display'], data)
        self.assertGreater(data['cardMarkSvg']['width'], 0, data)
        self.assertLessEqual(data['scroll'], 0, 'the sign-in page scrolls sideways at 375 px')

    def test_on_a_wide_screen_the_brand_side_shows_and_the_card_mark_does_not(self):
        data = measure_fold(1280, 800)
        self.assertNotIn('error', data, data.get('error'))
        self.assertNotEqual('none', data['side']['display'], data)
        self.assertGreater(data['side']['width'], 300, data)
        self.assertEqual('none', data['cardMark']['display'], data)


if __name__ == '__main__':
    unittest.main()
