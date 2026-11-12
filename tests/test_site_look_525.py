"""Site look, fresh tokens (kittrial-5bb.525 slice a): token values and AA contrast.

The static half runs everywhere. It parses web/css/app.css's three token blocks:
light (:root), dark by the system setting, and dark forced by data-theme. It checks
- the fresh-look values on .523's frozen names;
- the eight additions approved at 18:13Z, present in every block;
- that both dark blocks agree;
- the WCAG 2.x contrast of every token pair the design uses (docs/design/WEB_FRESH_LOOK.md §9).
It also pins the few rules slice (a) changes: control edges on --field-line, .chip.info,
ui.js's state tones, and the serif on h1 only.

The measured half drives a real headless Chromium, found as test_site_look_523 finds it
(ORCHESTRA_WEB_TEST_BROWSER, else chromium/chrome on PATH; the subprocess inherits the
environment, so an LD_LIBRARY_PATH the browser needs travels with it). It runs the real
views on the prototype's mock service: a harness page imports web/js/app.js and mock.js,
exactly as web/js/prototype.js does. It visits sign-in, My work, Tasks, a task, Agents,
Reviews and Members & settings at 1280 and 375 px, in light, in dark forced by
data-theme, and in dark from prefers-color-scheme. For every visible text node it
measures the computed colour against the composited background it sits on: 4.5:1, or
3:1 for large text. For every control it measures its edge (or its fill, when the fill
is what marks it) against the background around it: 3:1. Disabled controls are exempt
(WCAG 1.4.3). It skips when no browser binary is findable.
"""
import html
import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

from test_site_look_523 import find_browser

ROOT = Path(__file__).resolve().parent.parent
CSS = ROOT / 'web' / 'css' / 'app.css'
UI = ROOT / 'web' / 'js' / 'ui.js'

LIGHT = {
    'ground': '#f6f5f2', 'surface': '#ffffff', 'surface-2': '#efede8', 'ink': '#1d1c21', 'ink-2': '#3b3942',
    'muted': '#605c67', 'line': '#e6e3dd', 'line-strong': '#d3cfc7', 'accent': '#4b3fc0', 'accent-ink': '#ffffff',
    'accent-soft': '#ecebfb', 'ok': '#1b6b41', 'ok-soft': '#e2f1e7', 'warn': '#9a5c00', 'warn-soft': '#fbefd9',
    'crit': '#b0251d', 'crit-soft': '#fbe7e5', 'idle': '#5f5b66', 'idle-soft': '#eeece8', 'focus': '#4b3fc0',
    'field-line': '#8a8691', 'info': '#0a6178', 'info-soft': '#dff0f5',
}
DARK = {
    'ground': '#121116', 'surface': '#1a1920', 'surface-2': '#24232b', 'ink': '#ecebf0', 'ink-2': '#cac7d1',
    'muted': '#a19daa', 'line': '#2c2a33', 'line-strong': '#3b3943', 'accent': '#aaa4ff', 'accent-ink': '#121116',
    'accent-soft': '#2b2852', 'ok': '#6fcf97', 'ok-soft': '#16301f', 'warn': '#f0b45a', 'warn-soft': '#35270f',
    'crit': '#ff8f85', 'crit-soft': '#3d1b19', 'idle': '#aaa6b2', 'idle-soft': '#2a2930', 'focus': '#c4c0ff',
    'field-line': '#6f6b79', 'info': '#71c7e0', 'info-soft': '#13303a',
}
SIZES = {'radius': '6px', 'rail': '248px', 'radius-lg': '8px', 'measure': '1120px', 's-12': '48px',
         't-3xl': '28px', 't-4xl': '36px', 't-2xs': '11px', 't-2xl': '24px'}
ADDITIONS = ('field-line', 'info', 'info-soft', 'radius-lg', 'measure', 's-12', 't-3xl', 't-4xl')

# (foreground, background, minimum, where) -- WEB_FRESH_LOOK.md §9
PAIRS = [
    ('ink', 'surface', 4.5, 'body text, table cells, headings'),
    ('ink', 'ground', 4.5, 'rail, facts'),
    ('ink-2', 'surface', 4.5, 'who-acts, prose, buttons'),
    ('ink-2', 'ground', 4.5, 'rail links'),
    ('ink-2', 'surface-2', 4.5, 'counts, chip text'),
    ('ink-2', 'idle-soft', 4.5, 'chip text on the neutral chip'),
    ('ink-2', 'info-soft', 4.5, 'chip text on In progress'),
    ('ink-2', 'warn-soft', 4.5, 'chip text on Awaiting review'),
    ('ink-2', 'ok-soft', 4.5, 'chip text on Approved'),
    ('ink-2', 'crit-soft', 4.5, 'chip text on Blocked; error text'),
    ('muted', 'surface', 4.5, 'crumbs, table heads, sub-lines'),
    ('muted', 'ground', 4.5, 'rail labels, empty-state text'),
    ('muted', 'surface-2', 4.5, 'muted text on a hovered row'),
    ('accent', 'surface', 4.5, 'links'),
    ('accent', 'ground', 4.5, 'links on the ground'),
    ('accent-ink', 'accent', 4.5, 'primary button'),
    ('ink', 'accent-soft', 4.5, 'current nav item, avatar'),
    ('idle', 'idle-soft', 4.5, 'chip tone Open'),
    ('info', 'info-soft', 4.5, 'chip tone In progress'),
    ('ok', 'ok-soft', 4.5, 'chip tone Approved'),
    ('warn', 'warn-soft', 4.5, 'chip tone Awaiting review'),
    ('crit', 'crit-soft', 4.5, 'chip tone Blocked'),
    ('ok', 'ground', 4.5, 'fact done'),
    ('warn', 'ground', 4.5, 'fact now'),
    ('ok', 'surface', 4.5, 'ok as text'),
    ('crit', 'surface', 4.5, 'field error, danger button text'),
    ('ink', 'crit-soft', 4.5, 'error title'),
    ('field-line', 'surface', 3.0, 'input, select, button edges (1.4.11)'),
    ('field-line', 'ground', 3.0, 'control edges on the ground'),
    ('focus', 'surface', 3.0, 'focus ring'),
    ('focus', 'ground', 3.0, 'focus ring on the ground'),
    ('accent', 'surface', 3.0, 'current tab, primary button fill'),
]


def luminance(hex_colour):
    r, g, b = (int(hex_colour[i:i + 2], 16) / 255 for i in (1, 3, 5))
    f = lambda c: c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)


def contrast(a, b):
    hi, lo = sorted((luminance(a), luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def token_blocks(css):
    """The three blocks as {name: value}: light, dark by media query, dark by data-theme."""
    light = css[css.index(':root {'):css.index('@media (prefers-color-scheme: dark)')]
    media = re.search(r':root:not\(\[data-theme="light"\]\)\s*\{([^}]*)\}', css).group(1)
    forced = re.search(r':root\[data-theme="dark"\]\s*\{([^}]*)\}', css).group(1)
    parse = lambda block: dict(re.findall(r'--([\w-]+):\s*([^;]+?);', block))
    return parse(light), parse(media), parse(forced)


class FreshTokens(unittest.TestCase):
    """The values on the frozen names, the additions, and their contrast (runs everywhere)."""

    @classmethod
    def setUpClass(cls):
        cls.css = CSS.read_text()
        cls.light, cls.media, cls.forced = token_blocks(cls.css)

    def test_light_values(self):
        for name, value in {**LIGHT, **SIZES}.items():
            self.assertEqual(value, self.light.get(name), '--' + name)

    def test_dark_values_in_both_dark_blocks(self):
        for block in (self.media, self.forced):
            for name, value in DARK.items():
                self.assertEqual(value, block.get(name), '--' + name)
        self.assertEqual(self.media, self.forced, 'the two dark blocks must agree')

    def test_additions_are_in_every_block(self):
        for block in (self.light, self.media, self.forced):
            for name in ADDITIONS:
                self.assertIn(name, block)
        for name in ADDITIONS:
            if name in SIZES:
                self.assertEqual(SIZES[name], self.media[name])

    def test_frozen_pins_of_523_hold(self):
        # .523's pins (test_site_look_523.test_tokens_and_the_one_colour_fix) are kept, not changed.
        for pin in ('--t-2xs: 11px;', '--t-2xl: 24px;', '--warn: #9a5c00;', '--s-1: 4px;'):
            self.assertIn(pin, self.css)

    def test_every_pair_meets_aa_in_both_themes(self):
        # measured on the values app.css actually ships (both dark blocks), not on this file's constants
        failures = []
        for theme, values in (('light', self.light), ('dark', self.media), ('dark (data-theme)', self.forced)):
            for fg, bg, need, where in PAIRS:
                ratio = contrast(values[fg], values[bg])
                if ratio < need:
                    failures.append(f'{theme} {fg} on {bg} ({where}): {ratio:.2f} < {need}')
        self.assertEqual([], failures)

    def test_controls_take_the_field_line_edge(self):
        button = self.css[self.css.index('button, .btn {'):self.css.index('button:hover, .btn:hover')]
        field = self.css[self.css.index('input, select, textarea {'):self.css.index('input[type="checkbox"]')]
        seg = self.css[self.css.index('.seg {'):self.css.index('.seg button {')]
        for rule in (button, field, seg):
            self.assertIn('border: 1px solid var(--field-line);', rule)
        # the danger button's 45% crit mix measured 2.20:1 (light) and 2.60:1 (dark) as an edge
        self.assertIn('button.danger { color: var(--crit); border-color: var(--field-line); }', self.css)

    def test_in_progress_has_its_own_state_colour(self):
        self.assertIn('.chip.info { background: var(--info-soft); } .chip.info::before { background: var(--info); }',
                      self.css)
        ui = UI.read_text()
        self.assertIn("in_progress: ['In progress', 'info']", ui)
        self.assertIn("'awaiting-review': ['Awaiting review', 'warn']", ui)

    def test_the_serif_is_for_the_h1_only(self):
        self.assertIn('h1, h2, h3 { font-family: var(--font);', self.css)
        self.assertIn('h1 { font-family: var(--font-display);', self.css)
        self.assertTrue(self.light['font-display'].endswith('serif') and 'sans' not in self.light['font-display'])


def harness_page(theme):
    js = (ROOT / 'web' / 'js').as_uri()
    set_theme = "document.documentElement.dataset.theme = 'dark';" if theme == 'dark' else ''
    return f'''<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="{CSS.as_uri()}"></head>
<body><div id="app"></div>
<script type="module">
import {{ start }} from '{js}/app.js';
import {{ createMock }} from '{js}/mock.js';
{set_theme}
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const parse = (c) => {{
  let m = c.match(/rgba?\\(([^)]+)\\)/);
  if (m) {{ const p = m[1].split(/[ ,\\/]+/).filter(Boolean).map(Number); return [p[0], p[1], p[2], p.length > 3 ? p[3] : 1]; }}
  m = c.match(/color\\(srgb ([^)]+)\\)/);
  if (m) {{ const p = m[1].split(/[ \\/]+/).filter(Boolean).map(Number); return [p[0] * 255, p[1] * 255, p[2] * 255, p.length > 3 ? p[3] : 1]; }}
  return [0, 0, 0, 0];
}};
const over = (top, under) => {{ const a = top[3]; return [0, 1, 2].map((i) => top[i] * a + under[i] * (1 - a)).concat([1]); }};
const lum = (c) => {{ const f = (v) => {{ v /= 255; return v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; }};
  return 0.2126 * f(c[0]) + 0.7152 * f(c[1]) + 0.0722 * f(c[2]); }};
const ratio = (a, b) => {{ const x = lum(a), y = lum(b); return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05); }};
const hex = (c) => '#' + c.slice(0, 3).map((v) => Math.round(v).toString(16).padStart(2, '0')).join('');
function background(el) {{
  const layers = [];
  for (let e = el; e; e = e.parentElement) {{
    const c = parse(getComputedStyle(e).backgroundColor);
    if (c[3] > 0) layers.push(c);
    if (c[3] >= 0.999) break;
  }}
  let bg = [255, 255, 255, 1];
  if (layers.length && layers[layers.length - 1][3] >= 0.999) bg = layers.pop();
  while (layers.length) bg = over(layers.pop(), bg);
  return bg;
}}
function dimmed(el) {{
  for (let e = el; e; e = e.parentElement) if (parseFloat(getComputedStyle(e).opacity) < 1) return true;
  return false;
}}
function measure(page) {{
  const texts = {{}}, controls = {{}};
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  for (let n = walker.nextNode(); n; n = walker.nextNode()) {{
    if (!n.textContent.trim()) continue;
    const el = n.parentElement;
    if (!el || el.closest('script, style, option, noscript, .proto-bar')) continue;
    if (el.closest(':disabled, [aria-disabled="true"]')) continue;
    const range = document.createRange(); range.selectNodeContents(n);
    const r = range.getBoundingClientRect();
    if (r.width <= 1 || r.height <= 1) continue;
    const cs = getComputedStyle(el);
    if (cs.visibility === 'hidden' || dimmed(el)) continue;
    const bg = background(el);
    const fg = over(parse(cs.color), bg);
    const size = parseFloat(cs.fontSize), weight = parseInt(cs.fontWeight, 10);
    const large = size >= 24 || (size >= 18.66 && weight >= 700);
    const key = hex(fg) + ' on ' + hex(bg) + (large ? ' large' : '');
    const v = ratio(fg, bg);
    if (!texts[key]) texts[key] = {{ ratio: v, need: large ? 3 : 4.5, sample: n.textContent.trim().slice(0, 40), cls: el.className && String(el.className).slice(0, 40), page }};
  }}
  document.querySelectorAll('input:not([type=checkbox]):not([type=radio]):not([type=hidden]), select, textarea, button, .btn, .seg').forEach((el) => {{
    if (el.matches(':disabled') || el.closest('.seg') && el.tagName === 'BUTTON' || el.closest('.proto-bar')) return;
    const r = el.getBoundingClientRect(); if (r.width < 2 || r.height < 2 || dimmed(el)) return;
    const cs = getComputedStyle(el);
    const around = background(el.parentElement);
    const fill = over(parse(cs.backgroundColor), around);
    const edge = parseFloat(cs.borderTopWidth) > 0 ? over(parse(cs.borderTopColor), around) : null;
    const byEdge = edge ? ratio(edge, around) : 1, byFill = ratio(fill, around);
    if (byEdge < 1.01 && byFill < 1.01) return;  // a text-only control (link, ghost): its text carries it
    const v = Math.max(byEdge, byFill);
    const key = (edge ? hex(edge) : 'none') + '/' + hex(fill) + ' on ' + hex(around);
    if (!controls[key]) controls[key] = {{ ratio: v, need: 3, sample: (el.textContent || el.tagName).trim().slice(0, 30), cls: String(el.className).slice(0, 40), page }};
  }});
  return {{ texts, controls }};
}}
const out = {{ pages: [], dark: matchMedia('(prefers-color-scheme: dark)').matches, errors: [] }};
try {{
  const ctx = await start(document.getElementById('app'), {{ transport: createMock({{ extra: null, latency: 5 }}), features: {{ requirements: true }} }});
  await wait(300);
  out.pages.push(['sign-in', measure('sign-in')]);
  const user = await ctx.api.login('priya', 'demo');
  await ctx.signedIn(user); await wait(400);
  const go = async (name, path) => {{ ctx.go(path); await wait(600); out.pages.push([name, measure(name)]); }};
  await go('my-work', '/');
  await go('tasks', '/p/proj_portal');
  const task = document.querySelector('a[href*="/t/"]');
  if (task) await go('task', task.getAttribute('href').replace(/^#/, ''));
  else out.errors.push('no task link on Tasks');
  await go('agents', '/agents');
  await go('reviews', '/p/proj_portal/reviews');
  await go('settings', '/p/proj_portal/settings');
}} catch (e) {{ out.errors.push(String(e && e.stack || e)); }}
document.title = 'MEASURE' + JSON.stringify(out);
</script></body></html>'''


def run_harness(browser, width, height, theme, prefers_dark=False):
    with tempfile.TemporaryDirectory() as tmp:
        page = Path(tmp) / 'aa.html'
        page.write_text(harness_page(theme), encoding='utf-8')
        args = [browser, '--headless', '--no-sandbox', '--disable-gpu', '--allow-file-access-from-files',
                f'--window-size={width},{height}', '--virtual-time-budget=30000', '--dump-dom', page.as_uri()]
        if prefers_dark:
            args.insert(1, '--blink-settings=preferredColorScheme=0')
        proc = subprocess.run(args, capture_output=True, text=True, timeout=180)
    m = re.search(r'<title>(MEASURE.*?)</title>', proc.stdout, re.S)
    if not m:
        raise AssertionError('measurement did not run (rc %s): %s %s' % (proc.returncode, proc.stdout[:300], proc.stderr[-600:]))
    return json.loads(html.unescape(m.group(1))[len('MEASURE'):])


def failures_of(data):
    bad, rows = [], []
    for page, found in data['pages']:
        for kind in ('texts', 'controls'):
            for key, v in found[kind].items():
                rows.append((v['ratio'], kind, key, page, v['sample']))
                if v['ratio'] < v['need'] - 0.005:
                    bad.append('%s %s %s: %.2f < %s (%r, .%s)' % (page, kind, key, v['ratio'], v['need'], v['sample'], v['cls']))
    return bad, sorted(rows)


@unittest.skipUnless(find_browser(), 'no headless chromium found (set ORCHESTRA_WEB_TEST_BROWSER)')
class FreshContrastMeasured(unittest.TestCase):
    """AA re-measured on the built pages, light and dark, at 1280 and 375 px."""

    def check(self, width, height, theme, prefers_dark=False):
        data = run_harness(find_browser(), width, height, theme, prefers_dark)
        self.assertEqual([], data['errors'])
        self.assertEqual(['sign-in', 'my-work', 'tasks', 'task', 'agents', 'reviews', 'settings'],
                         [p for p, _ in data['pages']])
        if prefers_dark:
            self.assertTrue(data['dark'], 'the browser did not report prefers-color-scheme: dark')
        bad, rows = failures_of(data)
        report = os.environ.get('ORCHESTRA_AA_REPORT')
        if report:
            with open(report, 'a', encoding='utf-8') as f:
                label = f'{width} {theme}{" (prefers)" if prefers_dark else ""}'
                for r in [r for r in rows if r[1] == 'texts'][:6] + [r for r in rows if r[1] == 'controls'][:3]:
                    f.write('%s\t%.2f\t%s\t%s\t%s\t%s\n' % (label, r[0], r[1], r[2], r[3], r[4]))
        self.assertGreater(len(rows), 40)
        self.assertEqual([], bad)

    def test_1280_light(self):
        self.check(1280, 800, 'light')

    def test_1280_dark(self):
        self.check(1280, 800, 'dark')

    def test_375_light(self):
        self.check(375, 812, 'light')

    def test_375_dark(self):
        self.check(375, 812, 'dark')

    def test_1280_dark_by_system_setting(self):
        self.check(1280, 800, 'light', prefers_dark=True)

    def test_375_dark_by_system_setting(self):
        self.check(375, 812, 'light', prefers_dark=True)


if __name__ == '__main__':
    unittest.main()
