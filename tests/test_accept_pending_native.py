"""Receipt-aware acceptance through HTTP, forced-command wire and the host CLI."""
import datetime
import json
import os
import secrets
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import http_client
from requirements import canonical_bytes
import test_bd_label_aliases as fixture
import test_web_merge_slot as web_fixture
from test_coordinator_rules_endpoint import RulesHelpers

KIT = Path(__file__).resolve().parents[1]


@unittest.skipIf(fixture.endpoint is None, 'native acceptance endpoint requires POSIX')
@unittest.skipIf(fixture.BD is None, 'native acceptance requires pinned ORCHESTRA_BD_BIN')
class NativeAcceptPendingTests(RulesHelpers, unittest.TestCase):
    web = web_fixture.NativeWebSlotTests.web

    def start_web(self):
        self.harness, token = self.web()
        self.caller = http_client.Client('http://127.0.0.1:%d' % self.harness.port).use_credential(token)
        self.deployment(operators=['ops'], coordinator_writes=True, independent_approval=False)
        (self.project / '.sessions.json').write_text(json.dumps(
            dict(schema_version=1, records={}, owners={'ops': 'lane:accept-pending'})))

    def proposal(self, kind, key):
        from test_web_records import _valid_ref_propose, _valid_cap_propose
        body = _valid_ref_propose(key) if kind == 'reference' else _valid_cap_propose(key)
        if kind == 'reference':
            body['review_by'] = (datetime.datetime.now(datetime.timezone.utc).date()
                                 + datetime.timedelta(days=90)).isoformat()
            return self.caller.propose_reference('pp', body, key='propose-' + key)
        body['aliases'] = []
        return self.caller.propose_capability('pp', body, key='propose-' + key)

    def payload(self, key, revision, digest, identity):
        return dict(schema_version=1, operation='accept', operation_id=identity, key=key,
                    revision=revision, record_sha256=digest, acceptance_state='accepted',
                    acceptance=dict(decision_id='native-accept-decision', owners=['person:maintainer'],
                                    approvers=['person:maintainer'], policy='any-owner',
                                    evidence='Synthetic native acceptance regression'))

    def call(self, route, kind, payload, batch=False, actor='ops'):
        if route == 'http':
            body = dict(expected_revision=payload['revision'], record_sha256=payload['record_sha256'],
                        decision_id=payload['acceptance']['decision_id'], evidence=payload['acceptance']['evidence'])
            method = self.caller.accept_reference if kind == 'reference' else self.caller.accept_capability
            try:
                return dict(code=200, body=method('pp', payload['key'], body, key=payload['operation_id']))
            except http_client.HttpApiError as error:
                return dict(code=error.status, error=str(error))
        import client
        command = 'reference-apply' if kind == 'reference' else 'capability-apply'
        body = payload
        if batch:
            body = dict(schema_version=1, operation_id=payload['operation_id'],
                        acceptance_state='accepted', acceptance=payload['acceptance'],
                        items=[dict(key=payload['key'], revision=payload['revision'],
                                    record_sha256=payload['record_sha256'])])
        attachment = self.root / 'accept-pending-request.json'
        attachment.write_text(json.dumps(body))
        if route == 'admin':
            reply = subprocess.run([sys.executable, str(KIT / 'admin.py'), '--root', str(self.root),
                                    command, 'pp', '--actor', actor, '--file', str(attachment)],
                                   capture_output=True, text=True, timeout=240)
            answer = dict(code=reply.returncode, stdout=reply.stdout, error=reply.stderr)
        else:
            target = str(KIT / 'endpoint.py')
            wire = client._wire('pp', 'ops', [command, '--file', str(attachment)], 'coordinator', None)
            reply = subprocess.run([sys.executable, str(KIT / 'ssh_forced_command.py'),
                                    '--root', str(self.root), '--endpoint', target,
                                    '--python', sys.executable, '--project', 'pp',
                                    '--principal', 'lane:accept-pending'], input=wire,
                                   capture_output=True, text=True, timeout=240,
                                   env=dict(os.environ, SSH_ORIGINAL_COMMAND=target))
            self.assertEqual(reply.returncode, 0, reply.stderr)
            result = json.loads(reply.stdout)
            answer = dict(code=result['returncode'], stdout=result['stdout'], error=result['stderr'])
        if answer.get('stdout', '').lstrip().startswith('{'):
            answer['body'] = json.loads(answer['stdout'])
        return answer

    def success(self, answer, route, revision, batch):
        self.assertEqual(answer['code'], 200 if route == 'http' else 0, answer)
        body = answer['body']
        if batch:
            self.assertTrue(body['complete'], body)
            body = body['items'][0]
        self.assertEqual(body['revision'], revision, body)
        return body

    def refusal(self, answer, route, batch):
        if batch:
            self.assertEqual(answer['code'], 0, answer)
            self.assertFalse(answer['body']['complete'], answer)
            item = answer['body']['items'][0]
            self.assertEqual(item['result'], 'refused', item)
            detail = item['reason']
        else:
            self.assertEqual(answer['code'], {'http': 409, 'ssh': 2, 'admin': 1}[route], answer)
            detail = answer['error']
        self.assertIn('already accepted; nothing waits to be accepted', detail)

    def revision(self, kind, key, number):
        import reference_records
        import capability_records
        module = reference_records if kind == 'reference' else capability_records
        row = next(row for row in self.rows() if module.key_label(key) in (row.get('labels') or []))
        return module.existing_revisions(row)[number]

    def remove_acceptance_receipts(self, kind, native_id):
        import reference_records
        import capability_records
        module = reference_records if kind == 'reference' else capability_records
        removed = []
        for path in (self.project / module.JOURNAL).glob('*.json'):
            receipt = json.loads(path.read_text())
            if receipt.get('operation') == 'accept' and receipt.get('id') == native_id:
                path.unlink()
                removed.append(path.name)
        self.assertTrue(removed, 'The lost-receipt test must remove the actual fixture accept receipt')

    def routes(self):
        return [('http', False), ('ssh', False), ('admin', False), ('ssh', True), ('admin', True)]

    def test_each_route_refuses_fresh_reaccept_and_preserves_replay_and_pending_draft(self):
        self.start_web()
        for route, batch in self.routes():
            for kind in ('reference', 'capability'):
                with self.subTest(route=route, batch=batch, kind=kind):
                    key = 'pending.' + secrets.token_hex(8)
                    draft = self.proposal(kind, key)
                    original = self.payload(key, 1, draft['sha256'], 'first-' + key)
                    self.success(self.call(route, kind, original, batch), route, 2, batch)
                    before = canonical_bytes(self.rows())
                    accepted = self.revision(kind, key, 2)
                    fresh = self.payload(key, 2, accepted['sha256'], 'fresh-' + key)
                    self.refusal(self.call(route, kind, fresh, batch), route, batch)
                    self.assertEqual(canonical_bytes(self.rows()), before)
                    self.refusal(self.call(route, kind, dict(original, operation_id='consumed-' + key), batch), route, batch)
                    self.assertEqual(canonical_bytes(self.rows()), before)
                    self.success(self.call(route, kind, original, batch), route, 2, batch)
                    self.assertEqual(canonical_bytes(self.rows()), before)
                    from test_web_records import _valid_ref_propose, _valid_cap_propose
                    body = _valid_ref_propose(key) if kind == 'reference' else _valid_cap_propose(key)
                    if kind == 'reference':
                        body.update(statement='Genuine pending draft', review_by=(datetime.datetime.now(datetime.timezone.utc).date() + datetime.timedelta(days=90)).isoformat())
                    else:
                        body.update(summary='Genuine pending draft', aliases=[])
                    body.update(expected_revision=2, expected_sha256=accepted['sha256'])
                    revise = self.caller.revise_reference if kind == 'reference' else self.caller.revise_capability
                    revised = revise('pp', key, body, key='revise-' + key)
                    self.assertEqual(revised['revision'], 3)
                    pending = self.revision(kind, key, 3)
                    self.success(self.call(route, kind, self.payload(key, 3, pending['sha256'], 'pending-' + key), batch), route, 4, batch)
                    before = canonical_bytes(self.rows())
                    self.success(self.call(route, kind, original, batch), route, 2, batch)
                    self.assertEqual(canonical_bytes(self.rows()), before)

    def test_each_route_receipt_lost_replay_is_native_byte_identical_noop(self):
        self.start_web()
        for route, batch in self.routes():
            for kind in ('reference', 'capability'):
                for fresh in (False, True):
                    with self.subTest(route=route, batch=batch, kind=kind, fresh=fresh):
                        key = 'lost.' + secrets.token_hex(8)
                        draft = self.proposal(kind, key)
                        original = self.payload(key, 1, draft['sha256'], 'first-' + key)
                        accepted = self.success(self.call(route, kind, original, batch), route, 2, batch)
                        self.remove_acceptance_receipts(kind, accepted['native_id'])
                        before = canonical_bytes(self.rows())
                        retry = dict(original, operation_id='fresh-' + key) if fresh else original
                        result = self.success(self.call(route, kind, retry, batch), route, 2, batch)
                        self.assertEqual(result['native_id'], accepted['native_id'])
                        self.assertEqual(canonical_bytes(self.rows()), before)

    def test_cli_and_http_repair_inert_direct_and_newest_accepted_revisions(self):
        self.start_web()
        from test_web_records import _valid_ref_propose, _valid_cap_propose
        for route in ('admin', 'http'):
            for kind in ('reference', 'capability'):
                for direct in (True, False):
                    with self.subTest(route=route, kind=kind, direct=direct):
                        self.deployment(operators=['ops', 'former'], coordinator_writes=True,
                                        independent_approval=False)
                        key = 'repair.' + secrets.token_hex(8)
                        if direct:
                            body = _valid_ref_propose(key) if kind == 'reference' else _valid_cap_propose(key)
                            if kind == 'reference':
                                body['review_by'] = (datetime.datetime.now(datetime.timezone.utc).date()
                                                     + datetime.timedelta(days=90)).isoformat()
                            else:
                                body['aliases'] = []
                            body.update(schema_version=1, operation='draft', operation_id='direct-' + key,
                                        acceptance_state='accepted', acceptance=self.payload(key, 1, '', '')['acceptance'])
                            self.success(self.call('admin', kind, body, actor='former'), 'admin', 1, False)
                            revision = 1
                        else:
                            draft = self.proposal(kind, key)
                            body = self.payload(key, 1, draft['sha256'], 'first-' + key)
                            self.success(self.call('admin', kind, body, actor='former'), 'admin', 2, False)
                            revision = 2
                        self.deployment(operators=['ops'], coordinator_writes=True, independent_approval=False)
                        get = self.caller.get_reference if kind == 'reference' else self.caller.get_capability
                        inert = get('pp', key)
                        self.assertEqual(inert['state'], 'draft-only', inert)
                        self.assertTrue(inert['acceptance_inert'], inert)
                        before = self.rows()
                        accepted = self.revision(kind, key, revision)
                        repair = self.payload(key, revision, accepted['sha256'], 'repair-' + key)
                        self.success(self.call(route, kind, repair), route, revision + 1, False)
                        live = get('pp', key)
                        self.assertEqual(live['state'], 'accepted', live)
                        self.assertFalse(live['acceptance_inert'], live)
                        after = canonical_bytes(self.rows())
                        self.assertNotEqual(after, canonical_bytes(before))
                        self.success(self.call(route, kind, repair), route, revision + 1, False)
                        self.assertEqual(canonical_bytes(self.rows()), after)
                        latest = self.revision(kind, key, revision + 1)
                        self.refusal(self.call(route, kind, self.payload(
                            key, revision + 1, latest['sha256'], 'live-again-' + key)), route, False)
                        self.assertEqual(canonical_bytes(self.rows()), after)

    def test_http_refuses_fresh_live_accepted_and_consumed_draft(self):
        self.start_web()
        for kind in ('reference', 'capability'):
            with self.subTest(kind=kind):
                key = 'http.refusal.' + secrets.token_hex(8)
                draft = self.proposal(kind, key)
                original = self.payload(key, 1, draft['sha256'], 'first-' + key)
                self.success(self.call('http', kind, original), 'http', 2, False)
                accepted = self.revision(kind, key, 2)
                before = canonical_bytes(self.rows())
                for revision, digest in ((1, draft['sha256']), (2, accepted['sha256'])):
                    fresh = self.payload(key, revision, digest, 'fresh-' + key + '-' + str(revision))
                    self.refusal(self.call('http', kind, fresh), 'http', False)
                    self.assertEqual(canonical_bytes(self.rows()), before)
                self.success(self.call('http', kind, original), 'http', 2, False)
                self.assertEqual(canonical_bytes(self.rows()), before)

    def test_web_verify_remains_a_separate_attestation_and_exact_retry(self):
        self.start_web()
        from test_web_records import _valid_cap_results
        key = 'verify.' + secrets.token_hex(8)
        draft = self.proposal('capability', key)
        original = self.payload(key, 1, draft['sha256'], 'first-' + key)
        self.success(self.call('http', 'capability', original), 'http', 2, False)
        accepted = self.revision('capability', key, 2)
        before = self.rows()
        body = dict(revision=2, record_sha256=accepted['sha256'], commit='e' * 40,
                    passed=True, results=_valid_cap_results())
        result = self.caller.verify_capability('pp', key, body, key='verify-' + key)
        self.assertEqual(result['revision'], 2)
        after = self.rows()
        def entries(rows):
            return [c for r in rows for c in (r.get('comments') or [])
                    if c.get('text', '').startswith(('Kind: capability-entry-', 'Kind: capability-acceptance-'))]
        self.assertEqual(entries(before), entries(after))
        stable = canonical_bytes(after)
        self.caller.verify_capability('pp', key, body, key='verify-' + key)
        self.assertEqual(canonical_bytes(self.rows()), stable)
        with self.assertRaises(http_client.HttpApiError) as conflict:
            self.caller.verify_capability('pp', key, dict(body, passed=False), key='verify-' + key)
        self.assertEqual(conflict.exception.status, 409)
        self.assertEqual(canonical_bytes(self.rows()), stable)


if __name__ == '__main__':
    unittest.main()
