"""The Files panel on the task page (kittrial-5bb.376), run as a Node page test."""
import json
import shutil
import sys
import unittest
from pathlib import Path

KIT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(KIT), str(KIT / 'tests')]


class TaskFilesPanelTests(unittest.TestCase):
    def test_the_panel_lists_gates_downloads_and_refuses_as_text(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('Node unavailable; the page is not exercised')
        from test_http_web import run_node_module
        done = run_node_module(self, node, 'await import(process.argv[1])',
                               (KIT / 'tests' / 'web_task_files.mjs').as_uri(),
                               (KIT / 'tests' / 'web_dom_shim.mjs').as_uri(),
                               (KIT / 'web' / 'js' / 'views' / 'taskFiles.js').as_uri(),
                               (KIT / 'web' / 'js' / 'api.js').as_uri())
        self.assertEqual(0, done.returncode, done.stderr[-3500:])
        proof = json.loads(done.stdout.strip().splitlines()[-1])
        self.assertTrue(proof['success'])
        self.assertEqual(9, proof['tested'])

    def test_the_task_page_mounts_the_panel_and_never_opens_a_file_inline(self):
        task = (KIT / 'web' / 'js' / 'views' / 'task.js').read_text(encoding='utf-8')
        self.assertIn("taskFilesPanel(ctx, { pid, tid, task: t, project })", task)
        panel = (KIT / 'web' / 'js' / 'views' / 'taskFiles.js').read_text(encoding='utf-8')
        for banned in ('innerHTML', 'window.open', 'location.href', 'target: \'_blank\'', 'iframe'):
            with self.subTest(banned=banned):
                self.assertNotIn(banned, panel)


if __name__ == '__main__':
    unittest.main()
