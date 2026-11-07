"""Empty, loading and error states (kittrial-5bb.525 slice e): roles, text and retry.

tests/web_states.mjs builds web/js/ui.js `empty`, `loading` and `errorState` under Node (the
DOM shim). One rule per test:
- empty: a quiet well (no live role), its title, its sentence, its action, a hidden icon;
- loading: role=status and aria-busy, its words for screen readers only, a hidden skeleton;
- error: role=alert, the title by status, the server's reason, the request id, and Try again
  that calls the retry (and no button without one).
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
        proc = subprocess.run([node, str(KIT / 'tests' / 'web_states.mjs'), (KIT / 'tests' / 'web_dom_shim.mjs').as_uri(),
                               (KIT / 'web' / 'js' / 'ui.js').as_uri()], capture_output=True, text=True, timeout=120)
        case.assertEqual(0, proc.returncode, proc.stderr[-2000:])
        _RUN['data'] = json.loads(proc.stdout.strip().splitlines()[-1])
    return _RUN['data']


class WebStates(unittest.TestCase):

    def test_empty_is_a_quiet_well_with_title_sentence_and_action(self):
        data = observed(self)
        empty = data['empty']
        self.assertEqual((['state'], None, 'empty'), (empty['classes'], empty['role'], empty['state']))
        self.assertEqual('Nothing to confirm', empty['title'])
        self.assertEqual([['', 'An agent that asks to join appears here.']], empty['paragraphs'])
        self.assertEqual(['true'], empty['icons'])
        self.assertEqual([], empty['act'])
        self.assertEqual(['Add an agent'], data['emptyWithAction']['act'])
        self.assertEqual(('No tasks yet', []), (data['emptyNoBody']['title'], data['emptyNoBody']['paragraphs']))

    def test_loading_is_announced_and_shows_a_skeleton(self):
        data = observed(self)
        load = data['loading']
        self.assertEqual(('status', 'true', 'loading'), (load['role'], load['busy'], load['state']))
        self.assertEqual(['Loading your agents…'], load['hidden'])
        self.assertEqual('Loading your agents…', load['text'])   # the only words: the skeleton has none
        self.assertEqual(['true'], load['skeleton'])
        self.assertEqual(3, load['rows'])
        self.assertEqual(['Loading…'], data['loadingDefault'])

    def test_error_says_what_failed_why_and_offers_try_again(self):
        data = observed(self)
        err = data['error']
        self.assertEqual((['state', 'error'], 'alert', 'error'), (err['classes'], err['role'], err['state']))
        self.assertEqual('Could not load this page', err['title'])
        self.assertEqual([['', data['error']['describe']], ['state-req mono', 'Request req_4f19c2']], err['paragraphs'])
        self.assertEqual(['Try again'], err['buttons'])
        self.assertEqual(['true'], err['icons'])
        self.assertEqual(1, data['retried'])
        self.assertEqual(('Not found', []), (data['error404']['title'], data['error404']['buttons']))
        self.assertEqual('No access', data['error403']['title'])

    def test_the_agents_page_loads_with_the_skeleton(self):
        # agents.js:199 was a 6rem text box ("Loading your agents…"); the cards now replace the
        # skeleton in the same place, still marked data-agents-loading (web_agent_create_form pins it).
        source = (KIT / 'web' / 'js' / 'views' / 'agents.js').read_text(encoding='utf-8')
        self.assertIn("h('div', { class: 'panel', 'data-agents-loading': '' }, h('div', { class: 'panel-body' }, loading('Loading your agents…')))", source)
        self.assertNotIn('min-height: 6rem', source)


if __name__ == '__main__':
    unittest.main()
