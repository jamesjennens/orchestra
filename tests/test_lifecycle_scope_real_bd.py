"""The live per-task scope read and its refusal against a REAL pinned bd/Dolt.

Opt-in exactly like the other ``*_real_bd`` modules: it needs ``ORCHESTRA_BD_BIN``
(or ``bd`` on PATH) with a sibling ``dolt``, and skips without one. It drives the
endpoint, so it exercises the bounded per-task read the endpoint's host runner
serves for a one-fact write (kittrial-5bb.289 item 2) - the path no synthetic
double can prove - and the refusal sentence that names both scopes and the view's
newest record (item 1).

DELIVERED WITHOUT A RUN: bd and dolt are not on the worker host's PATH, so every
test here skipped there (``no pinned native bd (set ORCHESTRA_BD_BIN)``). Run it
where a pinned bd 1.2.2 and dolt 2.2.0 are available.
"""
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

KIT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(KIT), str(KIT / 'tests')]
import test_bd_label_aliases as fixture
from requirements import content_hash

endpoint = fixture.endpoint


@unittest.skipIf(endpoint is None, 'endpoint imports fcntl (POSIX-only)')
@unittest.skipIf(fixture.BD is None, 'no pinned native bd (set ORCHESTRA_BD_BIN)')
class LiveScopeRealBdTests(fixture.RealBdLabelAliasTests):
    """A one-fact write reads the task live and refuses a moved scope with reasons."""

    # The parent fixture's own label-spelling tests are covered by that module; this
    # module only needs its scratch server-mode tracker.
    test_create_label_spellings_refuse_every_reserved_value = None
    test_update_label_spellings_refuse_every_reserved_value = None
    test_ordinary_labels_still_reach_real_bd = None

    def fact(self, task, dimension, value, scope, operation, actor='op'):
        return dict(schema_version=1, operation_id=operation, task=task, dimension=dimension,
                    value=value, scope=dict(scope), actor=actor,
                    evidence=['synthetic live-scope proof'], provenance='performed')

    def send(self, payload, actor='op'):
        return endpoint.execute(self.root, dict(project='pp', actor=actor, action='lifecycle',
                                                args=[json.dumps(payload)]))

    def task(self):
        made = self.bd('create', 'synthetic live-scope target', '--json')
        self.assertEqual(made.returncode, 0, made.stderr)
        return json.loads(made.stdout)['id']

    def test_a_moved_scope_refuses_with_both_scopes_and_no_full_export(self):
        task = self.task()
        older = dict(source_commit='1' * 40, integration_commit='3' * 40,
                     release_id='synthetic-old-release', environment='staging')
        newer = dict(source_commit='2' * 40, integration_commit='4' * 40,
                     release_id='synthetic-new-release', environment='production')
        first = self.send(self.fact(task, 'lifecycle-scope', content_hash(older), older,
                                    'live-scope-old'))
        self.assertEqual(first['returncode'], 0, first)
        second = self.send(self.fact(task, 'lifecycle-scope', content_hash(newer), newer,
                                     'live-scope-new'))
        self.assertEqual(second['returncode'], 0, second)
        # Trace the native processes of the refused write: the live read must not
        # fall back to a full `export --all` (item 2), and a real bd must answer
        # the bounded `show`/`list --parent`/`list --desc-contains` reads.
        exports = []
        real = subprocess.run

        def traced(argv, **kwargs):
            if isinstance(argv, (list, tuple)) and 'export' in argv and '--all' in argv:
                exports.append(list(argv))
            return real(argv, **kwargs)

        with mock.patch.object(endpoint.subprocess, 'run', traced):
            refused = self.send(self.fact(task, 'tested', 'passed', older, 'live-tested-old'))
        self.assertEqual(exports, [], 'a one-fact write must not pay for a full export')
        self.assertNotEqual(refused['returncode'], 0, refused)
        message = (refused.get('stderr') or '') + (refused.get('stdout') or '')
        self.assertIn('set matching lifecycle scope before recording facts', message)
        self.assertIn('synthetic-new-release', message, 'the refusal must name the compared scope')
        self.assertIn('production', message, 'the refusal must name the compared scope')
        self.assertIn('synthetic-old-release', message, 'the refusal must name the payload scope')
        self.assertIn('staging', message, 'the refusal must name the payload scope')
        self.assertRegex(message, r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}',
                         "the refusal must name the view's newest record time")
        # The same fact under the CURRENT scope is accepted.
        accepted = self.send(self.fact(task, 'tested', 'passed', newer, 'live-tested-new'))
        self.assertEqual(accepted['returncode'], 0, accepted)


if __name__ == '__main__':
    unittest.main()
