"""kittrial-5bb.455: the Add-an-agent form's "Runs in" field is a label, not a fixed choice."""
import json
import shutil
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'tests'))
from test_http_web import run_node_module  # noqa: E402


class CreateFormTests(unittest.TestCase):
    def test_runs_in_is_empty_with_examples_and_says_it_is_only_a_label(self):
        node = shutil.which('node')
        if not node:
            print('NOTE: CreateFormTests was SKIPPED: node is not installed.', file=sys.stderr)
            self.skipTest('node is not installed')
        web = ROOT / 'web' / 'js'
        done = run_node_module(self, node, 'await import(process.argv[1])',
                               (ROOT / 'tests' / 'web_agent_create_form.mjs').as_uri(),
                               (ROOT / 'tests' / 'web_dom_shim.mjs').as_uri(),
                               (web / 'views' / 'agents.js').as_uri())
        self.assertEqual(0, done.returncode, done.stderr[-3000:])
        seen = json.loads(done.stdout.strip().splitlines()[-1])
        self.assertEqual('', seen['toolValue'])
        self.assertNotEqual('', seen['toolPlaceholder'])
        for example in ('GitHub Copilot in VS Code', 'Claude Code', 'Codex', 'Linux'):
            self.assertIn(example, seen['toolHint'])
        self.assertIn('label', seen['toolHint'])
        self.assertIn('any agent', seen['dirHint'])
        self.assertEqual('', seen['sentTool'])          # nothing depends on the old default


if __name__ == '__main__':
    unittest.main()
