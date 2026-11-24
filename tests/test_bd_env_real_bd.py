"""Real bd evidence for narrow beads.role suppression (kittrial-5bb.531 bug 4).

Opt in with ``ORCHESTRA_BD_BIN`` (or ``bd`` on PATH), reusing the scratch
server-mode fixture in ``tests/test_bd_label_aliases.py``. Skipped cleanly
where ``bd`` or POSIX endpoint imports are absent.
"""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import admin
import native
import test_bd_label_aliases as rb


@unittest.skipIf(rb.endpoint is None, 'real bd role env test needs POSIX endpoint imports')
@unittest.skipIf(rb.BD is None, 'real bd role env test needs ORCHESTRA_BD_BIN or bd on PATH')
class RealBdRoleEnvTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        base = rb.RealBdLabelAliasTests
        base.setUpClass()
        for name in ('_tmp', '_signals', 'root', 'bd_path', 'port', 'project', 'server'):
            setattr(cls, name, getattr(base, name))

    @classmethod
    def tearDownClass(cls):
        rb.RealBdLabelAliasTests.tearDownClass()

    def test_native_run_and_endpoint_ref_list_suppress_beads_role_warning(self):
        base_env = admin.environment(self.root)
        self.assertNotIn('GIT_CONFIG_COUNT', base_env)

        completed = native.run(
            native.argv(self.root, self.project, 'op', ['list', '--label', 'reference', '--json']),
            base_env,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stderr, '', 'expected empty stderr under native.run')
        self.assertIn(completed.stdout.strip(), ('[]', 'null'))

        reply = rb.endpoint.execute(
            self.root,
            {'action': 'ref', 'project': 'pp', 'actor': 'op', 'args': ['list']},
        )
        self.assertEqual(reply['returncode'], 0, reply)
        self.assertEqual(reply['stderr'], '', reply)
        data = json.loads(reply['stdout'])
        self.assertIsInstance(data, dict)
        self.assertEqual(data.get('schema_version'), 1)

        raw = rb.endpoint.execute(
            self.root,
            {'action': 'bd', 'project': 'pp', 'actor': 'op', 'args': ['list', '--label', 'reference', '--json']},
        )
        self.assertEqual(raw['returncode'], 0, raw)
        self.assertEqual(raw['stderr'], '', raw)


if __name__ == '__main__':
    unittest.main()
