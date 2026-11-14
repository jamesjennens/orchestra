"""Site look system (kittrial-5bb.523): chips, tables, the rail column.

The source checks pin the CSS and markup classes the browser uses, and run
everywhere. The measured checks drive a real headless Chromium (found via
ORCHESTRA_WEB_TEST_BROWSER, else the usual chromium/chrome names on PATH;
the subprocess inherits the environment, so an LD_LIBRARY_PATH the browser
needs travels with it) over the repo's own app.css and a static harness page
carrying the views' exact table markup and the longest real chip labels, at
1280, 900 and 375 px. They verify, as rendered bounding boxes, that a state
chip never runs into the neighbour cell, is never clipped by the table
wrapper, stays inside its own cell, that every cell of a row shares one
bottom edge (the divider draws on the row line), that the phone layout
stacks without sideways page scroll, and that the rail column paints the
page's full height. They skip when no browser binary is findable.
"""
import html
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CSS = ROOT / 'web' / 'css' / 'app.css'

BROWSER_NAMES = ('chromium', 'chromium-browser', 'google-chrome', 'google-chrome-stable',
                 'chrome-headless-shell')


def find_browser():
    env = os.environ.get('ORCHESTRA_WEB_TEST_BROWSER')
    if env:
        return env if os.path.isfile(env) else None
    for name in BROWSER_NAMES:
        found = shutil.which(name)
        if found:
            return found
    return None


# The harness mirrors what project.js and work.js emit: the fixed table, the
# state chip classes of ui.js's review/status chips with their longest real
# labels, and the who-acts cell beside the state column.
ROWS = [
    ('Fix invoice amounts on the order portal checkout page', 'chip accent', 'Awaiting review',
     'Piper · claim or comment', 'Nov 6, 01:12'),
    ('Publish the design tokens as a pinned reference for later slices', 'chip ok', 'Approved · not integrated',
     'Maya (owner)', '5h ago'),
    ('Rework the summary strip so the counts read left to right', 'chip warn', 'Changes requested',
     'Otto · revise and resend', '4h ago'),
    ('Archive the winter campaign records', 'chip crit', 'Cannot be read',
     'anyone', '1d ago'),
    ('Open the second review round for the CLI contract', 'chip', 'Open',
     'Rae · claim or comment', '2d ago'),
]


def harness_page(css_uri):
    def row(title, chip_class, label, who, updated):
        return (
            f'<tr><td class="task-title-cell"><div class="task-list-title">{title}</div></td>'
            f'<td class="task-state-column"><span class="{chip_class}">{label}</span></td>'
            f'<td class="who-acts-cell task-next-column"><span class="who-acts">{who}</span></td>'
            f'<td class="hide-narrow muted num task-updated-column">{updated}</td></tr>'
        )
    head = (
        '<tr><th class="task-title-column">Task</th><th class="task-state-column">State</th>'
        '<th class="task-next-column">Who acts next</th>'
        '<th class="hide-narrow num task-updated-column">Updated</th></tr>'
    )
    body = ''.join(row(*r) for r in ROWS)
    return f'''<!doctype html><html><head><meta charset="utf-8">
<link rel="stylesheet" href="{css_uri}">
</head><body>
<div class="shell"><nav class="rail"><div class="brand">Kit</div></nav>
<main class="main"><div class="table-wrap">
<table class="stack-narrow task-list-project"><thead>{head}</thead><tbody>{body}</tbody></table>
<table class="task-list-work" style="margin-top:16px"><thead>{head}</thead><tbody>{body}</tbody></table>
</div></main></div>
<script>
const out = {{ chips: [], rows: [], shellBg: getComputedStyle(document.querySelector('.shell')).backgroundImage }};
document.querySelectorAll('table').forEach((table) => {{
  const wrap = table.closest('.table-wrap');
  const wrapRect = wrap.getBoundingClientRect();
  const stacked = getComputedStyle(table.tHead).display === 'none';
  if (!stacked) {{
    Array.from(table.rows).forEach((row) => {{
      const bottoms = Array.from(row.cells).map((c) => c.getBoundingClientRect().bottom);
      out.rows.push({{ table: table.className, spread: Math.max(...bottoms) - Math.min(...bottoms) }});
    }});
  }}
  table.querySelectorAll('td.task-state-column').forEach((cell) => {{
    const cellRect = cell.getBoundingClientRect();
    const sibRect = cell.nextElementSibling.getBoundingClientRect();
    cell.querySelectorAll('.chip').forEach((chip) => {{
      const r = chip.getBoundingClientRect();
      out.chips.push({{
        label: chip.textContent.trim(), stacked,
        beyondCell: r.right - cellRect.right,
        intoNeighbour: r.right - sibRect.left,
        pastWrap: r.right - wrapRect.right,
        pageScroll: document.documentElement.scrollWidth - document.documentElement.clientWidth,
      }});
    }});
  }});
}});
document.title = 'MEASURE' + JSON.stringify(out);
</script>
</body></html>'''


class LookSourcePins(unittest.TestCase):
    """The shipped rules, pinned as source (runs everywhere)."""

    @classmethod
    def read(cls, rel):
        return (ROOT / 'web' / rel).read_text()

    def test_chip_wraps_instead_of_running_into_the_neighbour(self):
        css = CSS.read_text()
        body = css[css.index('.chip {'):css.index('.chip::before')]
        self.assertIn('white-space: normal; max-width: 100%; overflow-wrap: anywhere;', body)
        self.assertNotIn('nowrap', body)
        self.assertIn('flex: none;', css[css.index('.chip::before'):css.index('.chip.ok')])

    def test_state_column_fits_and_cells_stay_cells(self):
        css = CSS.read_text()
        wide = css[css.index('@media (min-width: 761px) {\n  /* no min-width'):css.index('/* numbers and times')]
        self.assertIn('min-width: 0;', wide)
        self.assertIn('width: 168px;', wide)
        # the r1 mistake: a grid cell in the wide layout drew the divider mid-row
        self.assertNotIn('task-state-column { display: grid; gap', css)

    def test_numbers_and_times_right_align(self):
        css = CSS.read_text()
        self.assertIn('th.num, td.num { text-align: right; }', css)
        self.assertIn('th.task-updated-column, td.task-updated-column { text-align: right; }', css)

    def test_updated_columns_carry_the_num_class(self):
        for f in ('js/views/project.js', 'js/views/work.js'):
            src = self.read(f)
            self.assertIn("class: 'hide-narrow num task-updated-column'", src)
            self.assertIn("class: 'hide-narrow muted num task-updated-column'", src)

    def test_both_tables_stack_at_phone_width(self):
        css = CSS.read_text()
        phone = css[css.index('@media (max-width: 760px)'):]
        self.assertIn('.task-list-work thead { display: none; }', phone)
        self.assertIn('.task-list-work td.task-title-cell { grid-column: 1 / -1; }', phone)
        self.assertIn('.task-list-project td.task-title-cell, .task-list-project td.task-state-column,', phone)

    def test_rail_column_paints_the_full_page_height(self):
        css = CSS.read_text()
        block = css[css.index('the rail is sticky'):css.index('.brand {')]
        self.assertIn('linear-gradient(90deg,', block)
        self.assertIn('calc(var(--rail) - 1px)', block)
        self.assertIn('@media (min-width: 761px)', block)

    def test_tokens_and_the_one_colour_fix(self):
        css = CSS.read_text()
        self.assertIn('--t-2xs: 11px;', css)
        self.assertIn('--t-2xl: 24px;', css)
        self.assertIn('--warn: #9a5c00;', css)
        self.assertIn('--s-1: 4px;', css)
        self.assertIn('.agent-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(min(340px, 100%), 1fr));',
                      css)


@unittest.skipUnless(find_browser(), 'no headless chromium found (set ORCHESTRA_WEB_TEST_BROWSER)')
class LookMeasuredCases(unittest.TestCase):
    """The same rules, verified as rendered boxes at 1280, 900 and 375 px."""

    def measure(self, width, height):
        browser = find_browser()
        with tempfile.TemporaryDirectory() as tmp:
            page = Path(tmp) / 'harness.html'
            page.write_text(harness_page(CSS.as_uri()), encoding='utf-8')
            proc = subprocess.run(
                [browser, '--headless', '--no-sandbox', '--disable-gpu',
                 f'--window-size={width},{height}', '--virtual-time-budget=1500',
                 '--dump-dom', page.as_uri()],
                capture_output=True, text=True, timeout=120)
            self.assertEqual(0, proc.returncode, proc.stderr)
            m = re.search(r'<title>(MEASURE.*?)</title>', proc.stdout, re.S)
            self.assertIsNotNone(m, 'measurement script did not run: ' + proc.stdout[:400])
            return json.loads(html.unescape(m.group(1))[len('MEASURE'):])

    def assertChipsClean(self, data, stacked_expected):
        self.assertGreater(len(data['chips']), 0)
        for chip in data['chips']:
            self.assertEqual(stacked_expected, chip['stacked'], chip)
            self.assertLessEqual(round(chip['beyondCell'], 1), 0.5, chip)
            self.assertLessEqual(round(chip['pageScroll'], 1), 0.5, chip)
            if stacked_expected:
                continue  # stacked cells sit beside each other by design
            self.assertLessEqual(round(chip['intoNeighbour'], 1), -0.5, chip)
            self.assertLessEqual(round(chip['pastWrap'], 1), 0.5, chip)

    def test_1280(self):
        data = self.measure(1280, 800)
        self.assertChipsClean(data, stacked_expected=False)
        for row in data['rows']:
            self.assertLessEqual(round(row['spread'], 1), 0.5, row)
        self.assertIn('linear-gradient', data['shellBg'])

    def test_900(self):
        data = self.measure(900, 700)
        self.assertChipsClean(data, stacked_expected=False)
        for row in data['rows']:
            self.assertLessEqual(round(row['spread'], 1), 0.5, row)

    def test_375(self):
        data = self.measure(375, 812)
        self.assertChipsClean(data, stacked_expected=True)


if __name__ == '__main__':
    unittest.main()
