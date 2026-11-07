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


if __name__ == '__main__':
    unittest.main()
