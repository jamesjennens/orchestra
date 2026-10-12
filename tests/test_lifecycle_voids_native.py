"""Pinned native correction and bound-principal raw-write proofs in disposable data."""
import copy
import json
import unittest

import test_bd_label_aliases as fixture
import lifecycle
import lifecycle_voids as voids
from requirements import content_hash


class NativeVoidTests(fixture.RealBdLabelAliasTests):
    # Reuse the disposable real-bd fixture, not its unrelated label-regression methods.
    test_create_label_spellings_refuse_every_reserved_value = None
    test_update_label_spellings_refuse_every_reserved_value = None
    test_ordinary_labels_still_reach_real_bd = None

    def make_fact(self):
        reply = self.bd('create', 'synthetic VOID target', '--json')
        self.assertEqual(reply.returncode, 0, reply.stderr)
        task = json.loads(reply.stdout)['id']
        scope = dict(source_commit='a' * 40, integration_commit='b' * 40,
                     release_id='', environment='synthetic-VOID')
        for dimension, value in [('lifecycle-scope', content_hash(scope)), ('integrated', 'passed')]:
            payload = dict(schema_version=1, operation_id='native-' + task + '-' + dimension,
                           task=task, dimension=dimension, value=value, scope=scope,
                           actor='op', evidence=['synthetic native proof'], provenance='performed')
            result = fixture.endpoint.execute(self.root, dict(project='pp', actor='op', action='lifecycle',
                                                              args=[json.dumps(payload)]))
            self.assertEqual(result['returncode'], 0, result)
        native_rows = self.export_rows()
        target = next(row for row in native_rows if (lifecycle.native_event(row) or {}).get('task') == task
                      and (lifecycle.native_event(row) or {}).get('dimension') == 'integrated')
        return task, target

    def test_host_admin_gate_actual_native_void_exact_retry_and_linked_retraction(self):
        import admin
        task, target = self.make_fact()
        marker = self.root / 'deployment.private.json'
        before = marker.read_bytes()
        cfg = json.loads(before); cfg['operators'] = ['op']
        marker.write_text(json.dumps(cfg))
        try:
            before_rows = self.export_rows()
            with self.assertRaisesRegex(ValueError, 'is off'):
                admin.lifecycle_correction_command(self.root, 'pp', 'void', target['id'], 'op',
                                                   'Synthetic gate proof.', ['synthetic native test'])
            self.assertEqual(self.export_rows(), before_rows)
            self.assertFalse((self.project / voids.DIRECTORY).exists())
            admin.lifecycle_void_switch(self.root, 'on', 'op')
            answer = admin.lifecycle_correction_command(self.root, 'pp', 'void', target['id'], 'op',
                         'Synthetic gate proof.', ['synthetic native test'], 'admin-void-' + task)
            retry = admin.lifecycle_correction_command(self.root, 'pp', 'void', target['id'], 'op',
                         'Synthetic gate proof.', ['synthetic native test'], answer['operation_id'])
            self.assertEqual(retry['comment_id'], answer['comment_id']); self.assertTrue(retry['reconciled'])
            self.assertEqual(voids.context(self.export_rows(), voids.read_journal(self.project))['reasons'],
                             {target['id']: 'voided'})
            with self.assertRaisesRegex(ValueError, 'currently listed operator'):
                admin.lifecycle_correction_command(self.root, 'pp', 'retract', answer['operation_id'],
                         'worker', 'Synthetic refusal.', ['synthetic native test'])
            # Replacement does not undo issued evidence; the current operator
            # can retract it using the actual host CLI and pinned native store.
            cfg = json.loads(marker.read_bytes()); cfg['operators'] = ['new-op']; marker.write_text(json.dumps(cfg))
            self.assertEqual(voids.context(self.export_rows(), voids.read_journal(self.project))['reasons'],
                             {target['id']: 'voided'})
            with self.assertRaisesRegex(ValueError, 'currently listed operator'):
                admin.lifecycle_correction_command(self.root, 'pp', 'retract', answer['operation_id'], 'op',
                             'Synthetic retraction.', ['synthetic native test'], 'admin-retract-' + task)
            import subprocess, sys
            result = subprocess.run([sys.executable, admin.__file__, '--root', str(self.root),
                'lifecycle-event-retract', 'pp', answer['operation_id'], '--actor', 'new-op',
                '--reason', 'Synthetic retraction.', '--evidence', 'synthetic native test',
                '--operation-id', 'admin-retract-' + task], env=admin.environment(self.root),
                capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)['operation'], 'retract')
            self.assertEqual(voids.context(self.export_rows(), voids.read_journal(self.project))['reasons'], {})
            self.assertEqual(next(row for row in self.export_rows() if row['id'] == target['id']), target)
        finally:
            marker.write_bytes(before)

    def test_real_native_comment_and_journal_bind_void_and_retraction_without_changing_event(self):
        task, target = self.make_fact()
        before = copy.deepcopy(target)
        payload = dict(schema_version=1, operation='void', operation_id='native-void-' + task,
                       actor='op', reason='Synthetic operator correction.', evidence=['synthetic independent review'],
                       binding=voids.event_binding(target), target_void=None)
        def native(argv):
            actor = json.loads(argv[3][len(voids.PREFIX):])['actor'] if argv[:2] == ['comments', 'add'] else 'op'
            result = self.bd(*argv, actor=actor)
            self.assertEqual(result.returncode, 0, result.stderr)
            return result.stdout
        answer = voids.apply(self.export_rows(), 'op', payload, native, self.project,
                             operators=['op'], enabled=True)
        self.assertFalse(answer['reconciled'])
        after = self.export_rows()
        self.assertEqual(next(row for row in after if row['id'] == target['id']), before)
        captured = voids.snapshot(after, self.project)
        state = next(row for row in lifecycle.project_facts(captured) if row['id'] == task)
        self.assertEqual((state['facts']['integrated']['value'], state['facts']['integrated']['reason']),
                         ('unknown', 'voided'))
        retry = voids.apply(after, 'op', payload, native, self.project, operators=['op'], enabled=True)
        self.assertTrue(retry['reconciled'])
        # Replacement does not undo the existing record; only its linked current-operator retraction does.
        self.assertEqual(next(row for row in lifecycle.project_facts(voids.snapshot(self.export_rows(), self.project))
                              if row['id'] == task)['facts']['integrated']['value'], 'unknown')
        retract = dict(payload, operation='retract', operation_id='native-retract-' + task,
                       actor='replacement', target_void=payload['operation_id'])
        voids.apply(self.export_rows(), 'replacement', retract, native, self.project,
                    operators=['replacement'], enabled=True)
        recovered = next(row for row in lifecycle.project_facts(voids.snapshot(self.export_rows(), self.project))
                         if row['id'] == task)
        self.assertEqual(recovered['facts']['integrated']['value'], 'passed')
        self.assertNotIn('reason', recovered['facts']['integrated'])

    def test_bound_operator_raw_exemption_other_principal_and_unreadable_registry(self):
        task, target = self.make_fact()
        marker = self.root / 'deployment.private.json'
        cfg = json.loads(marker.read_text()); cfg['operators'] = ['op']; marker.write_text(json.dumps(cfg))
        registry = self.project / '.sessions.json'
        registry.write_text(json.dumps(dict(schema_version=1, records={}, owners={
            'op': 'person:owner', 'bound': 'person:owner', 'other': 'person:other'})))
        refused = ['update', target['id'], '--title', 'synthetic changed lifecycle title', '--json']
        before = self.export_rows()
        with self.assertRaisesRegex(ValueError, 'lifecycle action'):
            self.raw(refused, actor='other')
        self.assertEqual(self.export_rows(), before)
        registry.write_text('{')
        with self.assertRaisesRegex(ValueError, 'session registry cannot be read'):
            self.raw(refused, actor='bound')
        self.assertEqual(self.export_rows(), before)
        registry.write_text(json.dumps(dict(schema_version=1, records={}, owners={
            'op': 'person:owner', 'bound': 'person:owner', 'other': 'person:other'})))
        allowed = self.raw(refused, actor='bound')
        self.assertEqual(allowed['returncode'], 0, allowed)

    def test_raw_native_comment_cannot_forge_correction_envelope(self):
        task, target = self.make_fact()
        before = self.export_rows()
        with self.assertRaisesRegex(ValueError, 'lifecycle correction'):
            self.raw(['comments', 'add', task, voids.PREFIX + '{}', '--json'], actor='other')
        self.assertEqual(self.export_rows(), before)


if __name__ == '__main__':
    unittest.main()
