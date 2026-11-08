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


FONT = ('"Segoe UI Variable Text", "Segoe UI", system-ui, -apple-system, "Helvetica Neue", Roboto, "Noto Sans", '
        '"DejaVu Sans", Arial, sans-serif')
MONO = '"Cascadia Mono", "Cascadia Code", ui-monospace, SFMono-Regular, Menlo, Consolas, "DejaVu Sans Mono", monospace'
SHADOW = {'light': '0 1px 2px rgba(29, 28, 33, .05), 0 8px 24px rgba(29, 28, 33, .06)',
          'dark': '0 1px 2px rgba(0, 0, 0, .5), 0 8px 24px rgba(0, 0, 0, .35)'}
# Theme-independent additions both dark blocks repeat by decision (slice a, 18:13Z), so each block lists every addition.
REPEATED_SIZES = {'radius-lg', 'measure', 's-12', 't-3xl', 't-4xl'}
DARK_MEDIA = '@media (prefers-color-scheme: dark)'
DARK_MEDIA_SELECTOR = ':root:not([data-theme="light"])'
DARK_FORCED_SELECTOR = ':root[data-theme="dark"]'


def css_rules(css):
    """Every style rule of a stylesheet in source order, as (media, selectors, [(property, value)]).

    media is None at the top level, else the normalised @media prelude. Comments are dropped;
    @keyframes and other at-rules are skipped. Source order is what the cascade uses between
    rules of one selector, so "the last declaration wins" can be read off this list."""
    css = re.sub(r'/\*.*?\*/', '', css, flags=re.S)
    rules = []

    def walk(text, media):
        pos = 0
        head_re = re.compile(r'\s*([^{}]+?)\s*\{')
        while True:
            m = head_re.match(text, pos)
            if not m:
                return
            head, depth, j = m.group(1).strip(), 1, m.end()
            while depth:
                depth += {'{': 1, '}': -1}.get(text[j], 0)
                j += 1
            body, pos = text[m.end():j - 1], j
            if head.startswith('@media'):
                walk(body, ' '.join(head.split()))
            elif not head.startswith('@'):
                selectors = tuple(' '.join(part.split()) for part in head.split(','))
                decls = [tuple(x.strip() for x in part.split(':', 1)) for part in body.split(';') if ':' in part]
                rules.append((media, selectors, decls))

    walk(css, None)
    return rules


def winning(rules, selector, properties, media=None):
    """The (property, value) that wins for an exact selector among the given properties: the last one declared."""
    found = None
    for rule_media, selectors, decls in rules:
        if rule_media == media and selector in selectors:
            for prop, value in decls:
                if prop in properties:
                    found = (prop, value)
    return found


def tokens(rules, selector, media=None):
    """Custom properties declared on a selector, the last declaration of each winning."""
    out = {}
    for rule_media, selectors, decls in rules:
        if rule_media == media and selector in selectors:
            out.update((prop[2:], value) for prop, value in decls if prop.startswith('--'))
    return out


def token_blocks(css):
    """The tokens as they win: light (:root), and the two dark blocks (system setting, data-theme)."""
    rules = css_rules(css)
    return (tokens(rules, ':root'), tokens(rules, DARK_MEDIA_SELECTOR, DARK_MEDIA),
            tokens(rules, DARK_FORCED_SELECTOR))


class FreshTokens(unittest.TestCase):
    """The values on the frozen names, the additions, and their contrast (runs everywhere).

    Every check reads the value that WINS in the cascade (the last declaration of a token, or
    of a rule's property, in source order), so a later duplicate cannot override a pinned value
    while the first declaration still satisfies the test."""

    @classmethod
    def setUpClass(cls):
        cls.css = CSS.read_text()
        cls.rules = css_rules(cls.css)
        cls.light, cls.media, cls.forced = token_blocks(cls.css)
        # what a dark page resolves: the dark block over the light one
        cls.themes = {'light': cls.light, 'dark': {**cls.light, **cls.media}, 'dark (data-theme)': {**cls.light, **cls.forced}}

    def test_light_values(self):
        for name, value in {**LIGHT, **SIZES}.items():
            self.assertEqual(value, self.light.get(name), '--' + name)

    def test_dark_values_in_both_dark_blocks(self):
        for block in (self.media, self.forced):
            for name, value in DARK.items():
                self.assertEqual(value, block.get(name), '--' + name)
        self.assertEqual(self.media, self.forced, 'the two dark blocks must agree')

    def test_dark_blocks_declare_exactly_the_themed_tokens(self):
        # Should 1 (slice a review): each dark block's KEY SET is pinned. Every token the light block
        # gives a colour or the shadow is re-declared (nothing missing), plus the additions repeated
        # by decision, and nothing else (nothing extra, nothing the light block lacks).
        themed = {name for name, value in self.light.items() if re.fullmatch(r'#[0-9a-fA-F]{6}', value) or name == 'shadow'}
        expected = sorted(themed | REPEATED_SIZES)
        self.assertEqual(expected, sorted(self.media), 'dark block (prefers-color-scheme)')
        self.assertEqual(expected, sorted(self.forced), 'dark block (data-theme)')
        self.assertLessEqual(set(self.media), set(self.light))

    def test_font_mono_and_shadow_in_every_theme(self):
        # Note 3: the stacks and the shadow, as each theme resolves them.
        for theme, values in self.themes.items():
            self.assertEqual(FONT, values['font'], theme)
            self.assertEqual(MONO, values['mono'], theme)
            self.assertEqual(SHADOW['light' if theme == 'light' else 'dark'], values['shadow'], theme)

    def test_additions_are_in_every_block(self):
        for block in (self.light, self.media, self.forced):
            for name in ADDITIONS:
                self.assertIn(name, block)
        for name in ADDITIONS:
            if name in SIZES:
                self.assertEqual(SIZES[name], self.media[name])

    def test_frozen_pins_of_523_hold(self):
        # .523's pins (test_site_look_523.test_tokens_and_the_one_colour_fix), as values that win.
        for name, value in (('t-2xs', '11px'), ('t-2xl', '24px'), ('warn', '#9a5c00'), ('s-1', '4px')):
            self.assertEqual(value, self.light[name], '--' + name)

    def test_every_pair_meets_aa_in_both_themes(self):
        # measured on the values app.css actually ships (both dark blocks), not on this file's constants
        failures = []
        for theme, values in self.themes.items():
            for fg, bg, need, where in PAIRS:
                ratio = contrast(values[fg], values[bg])
                if ratio < need:
                    failures.append(f'{theme} {fg} on {bg} ({where}): {ratio:.2f} < {need}')
        self.assertEqual([], failures)

    def edge(self, selector):
        return winning(self.rules, selector, ('border', 'border-color'))

    def test_controls_take_the_field_line_edge(self):
        for selector in ('button', '.btn', 'input', 'select', 'textarea', '.seg'):
            self.assertEqual(('border', '1px solid var(--field-line)'), self.edge(selector), selector)
        # the danger button's 45% crit mix measured 2.20:1 (light) and 2.60:1 (dark) as an edge
        self.assertEqual(('border-color', 'var(--field-line)'), self.edge('button.danger'))
        self.assertEqual(('color', 'var(--crit)'), winning(self.rules, 'button.danger', ('color',)))

    def test_in_progress_has_its_own_state_colour(self):
        self.assertEqual(('background', 'var(--info-soft)'), winning(self.rules, '.chip.info', ('background', 'background-color')))
        # slice (c): the dot is the chip's own text colour, so In progress reads in --info throughout
        self.assertEqual(('color', 'var(--info)'), winning(self.rules, '.chip.info', ('color',)))
        ui = UI.read_text()
        self.assertIn("in_progress: ['In progress', 'info']", ui)
        self.assertIn("'awaiting-review': ['Awaiting review', 'warn']", ui)

    def test_the_serif_is_for_the_h1_only(self):
        family = ('font-family', 'font')
        self.assertEqual(('font-family', 'var(--font-display)'), winning(self.rules, 'h1', family))
        for selector in ('h2', 'h3'):
            self.assertEqual(('font-family', 'var(--font)'), winning(self.rules, selector, family), selector)
        self.assertTrue(self.light['font-display'].endswith('serif') and 'sans' not in self.light['font-display'])


JS_ROOT = ROOT / 'web' / 'js'
# The one place the accent may still look like a chip: the set-up wizard's "Step N of 6" says
# where the reader is (interaction and position, not a state).
ACCENT_ALLOWED = ("h('span', { class: 'chip accent', 'data-wizard-progress': 'true' })",)


class FreshTables(unittest.TestCase):
    """Slice (c): tables and lists that read at a glance, state colours only for states (runs everywhere)."""

    @classmethod
    def setUpClass(cls):
        cls.css = CSS.read_text()
        cls.rules = css_rules(cls.css)

    def win(self, selector, *properties):
        found = winning(self.rules, selector, properties)
        return found and found[1]

    def test_table_heads_and_rail_labels_are_sentence_case(self):
        for selector in ('th', '.rail-label'):
            self.assertIn(self.win(selector, 'text-transform'), (None, 'none'), selector)
            self.assertIn(self.win(selector, 'letter-spacing'), (None, '0', 'normal'), selector)
            self.assertEqual('var(--t-xs)', self.win(selector, 'font-size', 'font'), selector)
        self.assertEqual('var(--s-2) var(--s-3)', self.win('th', 'padding'))
        self.assertEqual('var(--s-3)', self.win('td', 'padding'))

    def test_chip_text_takes_its_state_colour(self):
        self.assertEqual('var(--idle)', self.win('.chip', 'color'))
        for tone in ('ok', 'warn', 'crit', 'info'):
            self.assertEqual(f'var(--{tone})', self.win(f'.chip.{tone}', 'color'), tone)
            self.assertEqual(f'var(--{tone}-soft)', self.win(f'.chip.{tone}', 'background', 'background-color'), tone)
        self.assertEqual('currentColor', self.win('.chip::before', 'background', 'background-color'))
        self.assertEqual('var(--ink-2)', self.win('.chip.plain', 'color'))
        # every tone on its own soft ground meets AA as 12px text, in both themes (the values that win)
        light, media, forced = token_blocks(self.css)
        for theme, values in (('light', light), ('dark', {**light, **media}), ('dark (data-theme)', {**light, **forced})):
            for tone in ('idle', 'ok', 'warn', 'crit', 'info'):
                self.assertGreaterEqual(contrast(values[tone], values[tone + '-soft']), 4.5, f'{theme} {tone}')

    def test_changes_requested_is_crit(self):
        ui = UI.read_text()
        self.assertIn("'changes-requested': ['Changes requested', 'crit']", ui)

    def test_the_accent_is_never_a_state_tone(self):
        # A chip, a dot or a tone table naming 'accent' is a state drawn in the interaction colour.
        found = []
        for path in sorted(JS_ROOT.rglob('*.js')):
            for number, line in enumerate(path.read_text(encoding='utf-8').splitlines(), 1):
                if re.search(r"'accent'|\bchip accent\b|\bdot accent\b|'(chip|dot) ' \+[^;]*'accent'", line) \
                        and not any(allowed in line for allowed in ACCENT_ALLOWED):
                    found.append(f'{path.relative_to(ROOT)}:{number}: {line.strip()[:120]}')
        self.assertEqual([], found)

    def test_buttons_in_cells_keep_one_line(self):
        for selector in ('td > button', 'td > .btn'):
            self.assertEqual('nowrap', self.win(selector, 'white-space'), selector)

    def test_rows_waiting_on_you_carry_the_bar(self):
        self.assertEqual('inset 3px 0 0 var(--accent)', self.win('tbody tr.mine > td:first-child', 'box-shadow'))
        phone = winning(self.rules, 'table.stack-narrow tr.mine', ('box-shadow',), media='@media (max-width: 760px)')
        self.assertEqual(('box-shadow', 'inset 3px 0 0 var(--accent)'), phone)
        project = (JS_ROOT / 'views' / 'project.js').read_text(encoding='utf-8')
        self.assertIn("class: 'row-link task-row' + (waitsOnYou(ctx, project, t) ? ' mine' : '')", project)

    def test_who_acts_in_a_cell_has_no_arrow(self):
        self.assertEqual('none', self.win('td .who-acts::before', 'content'))


class FreshStates(unittest.TestCase):
    """Slice (e): empty, loading and error states as the cascade resolves them (runs everywhere)."""

    @classmethod
    def setUpClass(cls):
        cls.rules = css_rules(CSS.read_text())

    def win(self, selector, *properties, media=None):
        found = winning(self.rules, selector, properties, media=media)
        return found and found[1]

    def test_the_shimmer_stops_under_reduced_motion(self):
        self.assertTrue(self.win('.sk', 'animation', 'animation-name').startswith('shimmer'))
        self.assertEqual('none', self.win('.sk', 'animation', 'animation-name', media='@media (prefers-reduced-motion: reduce)'))

    def test_an_error_is_the_crit_well_with_readable_text(self):
        self.assertEqual('var(--crit-soft)', self.win('.state.error', 'background', 'background-color'))
        self.assertEqual('4px solid var(--crit)', self.win('.state.error', 'border-left'))
        self.assertEqual('var(--ink-2)', self.win('.state.error .state-text p', 'color'))
        self.assertEqual('var(--ink)', self.win('.state-text strong', 'color'))
        # Try again on the well: its edge is the crit rule (field-line measured 2.99:1 on crit-soft)
        self.assertEqual('var(--crit)', self.win('.state.error button', 'border-color', 'border'))
        light, media, forced = token_blocks(CSS.read_text())
        for theme, values in (('light', light), ('dark', {**light, **media})):
            self.assertGreaterEqual(contrast(values['crit'], values['crit-soft']), 3.0, theme)
        # the pairs it uses meet AA in both themes (ink-2 and ink on crit-soft are in PAIRS too)
        light, media, forced = token_blocks(CSS.read_text())
        for theme, values in (('light', light), ('dark', {**light, **media}), ('dark (data-theme)', {**light, **forced})):
            self.assertGreaterEqual(contrast(values['ink-2'], values['crit-soft']), 4.5, theme)
            self.assertGreaterEqual(contrast(values['ink'], values['crit-soft']), 4.5, theme)

    def test_an_empty_state_is_a_dashed_well(self):
        self.assertEqual('1px dashed var(--line-strong)', self.win('.state', 'border'))
        self.assertEqual('var(--ground)', self.win('.state', 'background', 'background-color'))


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
import {{ errorState, empty, loading }} from '{js}/ui.js';
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
  // slice (c): a button in a table cell keeps its label on one line (count its text's line boxes)
  const cellButtons = [];
  document.querySelectorAll('td > button, td > .btn').forEach((el) => {{
    const r = el.getBoundingClientRect(); if (r.width < 2 || r.height < 2) return;
    const text = Array.from(el.childNodes).find((n) => n.nodeType === 3 && n.textContent.trim());
    if (!text) return;
    const range = document.createRange(); range.selectNodeContents(text);
    const lines = new Set(Array.from(range.getClientRects()).map((x) => Math.round(x.top))).size;
    cellButtons.push({{ text: el.textContent.trim().slice(0, 30), lines }});
  }});
  // r2: the header's action sits flush right, with or without a key; and the tab's own title
  const head = document.querySelector('header.page-head');
  const acts = head ? Array.from(head.children).filter((e) => e.classList.contains('actions')) : [];
  const last = acts.length ? Array.from(acts[0].querySelectorAll('a, button')).pop() : null;
  const headAlign = last ? {{ gap: Math.round(head.getBoundingClientRect().right - last.getBoundingClientRect().right),
    key: Boolean(head.querySelector('[data-page-key]:not([hidden])')) }} : null;
  return {{ texts, controls, cellButtons, headAlign, tab: document.title }};
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
  // r2: a plain task (open, nothing delivered: an action, no key) and People (key and action)
  await go('task-plain', '/p/proj_portal/t/task_port007');
  await go('people', '/p/proj_portal/people');
  await go('agents', '/agents');
  await go('reviews', '/p/proj_portal/reviews');
  await go('settings', '/p/proj_portal/settings');
  // slice (e): the three states, rendered by the real helpers inside the app's own main
  const main = document.getElementById('main');
  main.replaceChildren(
    errorState({{ status: 503, code: 'busy', message: 'The server is busy just now.', requestId: 'req_4f19c2' }}, () => {{}}),
    empty('Nothing to confirm', 'An agent that asks to join one of your projects appears here.'),
    loading('Loading activity…'));
  await wait(50);
  out.pages.push(['states', measure('states')]);
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
        self.assertEqual(['sign-in', 'my-work', 'tasks', 'task', 'task-plain', 'people', 'agents', 'reviews', 'settings', 'states'],
                         [p for p, _ in data['pages']])
        if prefers_dark:
            self.assertTrue(data['dark'], 'the browser did not report prefers-color-scheme: dark')
        bad, rows = failures_of(data)
        # slice (e): the error state's text was measured on its crit-soft well (ink-2 on crit-soft)
        states = dict(data['pages'])['states']['texts']
        well = [key.split(' on ')[1].split()[0] for key, v in states.items() if v['sample'] == 'Could not load this page']
        self.assertEqual(1, len(well), states)
        self.assertTrue(any(key.split(' on ')[1].split()[0] == well[0] and v['sample'] != 'Could not load this page'
                            for key, v in states.items()), 'the error text on the crit-soft well was not measured')
        # r2: the action is flush right on every page that has one, keyed or not (32px short before)
        # (on a phone the action starts its own line, left-aligned, by design)
        aligned = {page: found['headAlign'] for page, found in data['pages'] if found['headAlign']}
        self.assertTrue(any(not a['key'] for a in aligned.values()), 'a page without a key must be measured')
        if width > 760:
            self.assertEqual({}, {page: a for page, a in aligned.items() if abs(a['gap']) > 1})
        # r2: the Tasks tab names the project (the mock's portal project is "Customer portal")
        self.assertEqual('Tasks · Customer portal · Orchestra', dict(data['pages'])['tasks']['tab'])
        buttons = {page: found['cellButtons'] for page, found in data['pages']}
        self.assertTrue(buttons['settings'], 'the members table has its Remove buttons to measure')
        self.assertEqual([], [(page, b) for page, found in buttons.items() for b in found if b['lines'] > 1])
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
