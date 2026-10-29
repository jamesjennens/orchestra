"""kittrial-5bb.444: the agent's own first check, reported (design COORDINATOR_ONBOARDING_DESIGN §3.2 C2, §5.3).

The agent reports, with its own credential, whether its saved certificate matches this server's, whether the
project is in its own list, and whose AGENT.md it has (POST /v1/agents/me/checks). The newest 5 are kept on its
record, at most 10 a minute are taken, and step C2 of the hand-over walkthrough shows the newest about the
project, labelled with the agent and the time. It is the agent's claim: it grants nothing, no authority check
reads it and no other step changes with it. The coordinator AGENT.md carries the one call that reports it.
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
sys.path.insert(0, str(KIT / 'tests'))
import admin
import test_coordinator_grant
from http_auth import Service, Store, now_iso
from test_coordinator_agent_md import BUNDLE, node_json


def body(project, matches=True, listed=True, role='coordinator'):
    return {'project': project, 'certificate': {'sha256_matches': matches}, 'first_read': {'project_listed': listed},
            'agent_md': {'role': role}}


class CheckCase(test_coordinator_grant.Grants):
    """olive and oscar own Alpha, carl contributes, cora coordinates and has the agent Heron."""

    def setUp(self):
        super().setUp()
        self.clock = [time.time()]
        self.service._now = lambda: self.clock[0]

    def later(self, seconds=61):
        self.clock[0] += seconds

    def report(self, token=None, payload=None, **values):
        return self.request('POST', '/v1/agents/me/checks', body(self.project, **values) if payload is None else payload,
                            token=token or self.agent)

    def events(self, outcome='committed'):
        with self.service.store.lock:
            return [e for e in self.service.state['audit'] if e['action'] == 'agents.self_check' and e['outcome'] == outcome]

    def kept(self, agent_id=None):
        with self.service.store.lock:
            return list(self.service.state['agents'][agent_id or self.agent_id].get('self_checks') or [])

    def walkthrough(self, token=None, person=None):
        query = '?person=%s' % person if person else ''
        answer = self.request('GET', '/v1/projects/%s/handover%s' % (self.project, query), token=token or self.tokens['olive'])
        self.assertEqual(200, answer.status, answer.data)
        return {s['id']: s for s in answer.data['steps']}

    def c2(self, token=None, person=None):
        return self.walkthrough(token, person or self.ids['cora'])['self-check']


class RouteTests(CheckCase):

    def test_only_the_agents_own_credential_reports_and_it_lands_on_its_own_record(self):
        made = self.report()
        self.assertEqual(201, made.status, made.data)
        self.assertEqual(made.data['report'], {'project': self.project, 'sha256_matches': True, 'project_listed': True,
                                               'role': 'coordinator', 'reported_at': now_iso(self.clock[0])})
        self.assertEqual(403, self.report(token=self.tokens['cora']).status)     # the person, by session
        self.assertEqual(401, self.request('POST', '/v1/agents/me/checks', body(self.project)).status)
        _, kite = self.agent_with_id('cora', 'Kite')
        other = self.request('POST', '/v1/agents/me/checks', body('elsewhere'),
                             token=self.request('POST', '/v1/agents/%s/credentials' % kite, {},
                                                token=self.tokens['cora']).data['credential']['secret'])
        self.assertEqual(201, other.status, other.data)
        self.assertEqual([self.project], [r['project'] for r in self.kept()])  # Kite's report is on Kite only
        self.assertEqual(['elsewhere'], [r['project'] for r in self.kept(kite)])

    def test_every_field_and_value_is_checked(self):
        bad = [
            None, [], 'text',
            {'certificate': {'sha256_matches': True}, 'first_read': {'project_listed': True}, 'agent_md': {'role': 'worker'}},
            dict(body(self.project), extra=1),
            dict(body(self.project), certificate={'sha256_matches': True, 'path': 'C:/x.crt'}),
            dict(body(self.project), certificate={}),
            dict(body(self.project), certificate='yes'),
            dict(body(self.project), project=7),
            dict(body(self.project), project='not an id!'),
            body(self.project, matches='yes'), body(self.project, matches=1),
            body(self.project, listed=None), body(self.project, listed=0),
            body(self.project, role='boss'), body(self.project, role=None),
        ]
        for payload in bad:
            self.later()
            refused = self.report(payload=payload) if payload is not None else self.request(
                'POST', '/v1/agents/me/checks', None, token=self.agent)
            self.assertEqual(422, refused.status, (payload, refused.data))
        self.assertEqual([], self.kept())
        self.assertEqual([], self.events())

    def test_the_time_is_the_servers_and_an_at_field_is_refused(self):
        refused = self.report(payload=dict(body(self.project), at='2000-01-01T00:00:00Z'))
        self.assertEqual(422, refused.status)
        self.assertEqual(['at'], refused.data['error']['detail']['unsupported'])
        self.later(5)
        self.assertEqual(now_iso(self.clock[0]), self.report().data['report']['reported_at'])

    def test_null_says_the_server_has_no_certificate_of_its_own(self):
        made = self.report(matches=None)
        self.assertEqual((201, None), (made.status, made.data['report']['sha256_matches']))

    def test_the_project_need_not_be_the_agents_and_the_answer_does_not_say_whether_it_exists(self):
        real = self.report(listed=False)
        self.later()
        missing = self.report(payload=body('proj_nowhere000', listed=False))
        self.assertEqual((201, 201), (real.status, missing.status))
        self.assertEqual(set(real.data), set(missing.data))
        self.assertEqual(set(real.data['report']), set(missing.data['report']))

    def test_the_newest_five_are_kept_newest_first(self):
        for n in range(7):
            self.later()
            self.assertEqual(201, self.report(payload=body('p%d' % n)).status)
        self.assertEqual(['p6', 'p5', 'p4', 'p3', 'p2'], [r['project'] for r in self.kept()])

    def test_the_eleventh_report_in_a_minute_is_429_with_retry_after(self):
        for _ in range(10):
            self.assertEqual(201, self.report().status)
        refused = self.report()
        self.assertEqual((429, 'rate_limited'), (refused.status, refused.data['error']['code']))
        self.assertEqual('60', refused.headers.get('retry-after'))
        self.assertEqual(10, len(self.events()))                 # one committed event per kept report, no more
        self.later(30)
        self.assertEqual(429, self.report().status)
        self.assertEqual('30', self.report().headers.get('retry-after'))
        self.later(31)
        self.assertEqual(201, self.report().status)

    def test_refused_bodies_count_too_and_each_agent_has_its_own_count(self):
        for _ in range(10):
            self.assertEqual(422, self.report(payload={'nonsense': True}).status)
        self.assertEqual(429, self.report().status)
        kite_secret, _ = self.agent_with_id('cora', 'Kite')
        self.assertEqual(201, self.report(token=kite_secret).status)

    def test_one_committed_audit_event_per_report_with_the_results_and_no_text(self):
        self.report(listed=False, role='worker')
        events = self.events()
        self.assertEqual(1, len(events))
        self.assertEqual({k: events[0][k] for k in ('agent', 'project', 'sha256_matches', 'project_listed', 'role')},
                         {'agent': self.agent_id, 'project': self.project, 'sha256_matches': True,
                          'project_listed': False, 'role': 'worker'})

    def test_no_agent_read_shows_the_reports(self):
        self.report()
        reads = [self.request('GET', '/v1/agents/me', token=self.agent),
                 self.request('GET', '/v1/agents/%s' % self.agent_id, token=self.tokens['cora']),
                 self.request('GET', '/v1/agents', token=self.tokens['cora']),
                 self.request('GET', '/v1/projects/%s/agents' % self.project, token=self.tokens['olive'])]
        for read in reads:
            self.assertEqual(200, read.status, read.data)
            self.assertNotIn('self_checks', json.dumps(read.data))
            self.assertNotIn('sha256_matches', json.dumps(read.data))


class StepTests(CheckCase):

    def test_c2_waits_for_the_grant_then_asks_the_agent_to_report(self):
        self.assertEqual(('waiting', 'Waits for an effective grant (step 3).'), (self.c2()['state'], self.c2()['detail']))
        self.assertEqual(200, self.grant().status)
        self.assertEqual(('todo', 'Ask Heron to run its first check (it is the first step in its AGENT.md).'),
                         (self.c2()['state'], self.c2()['detail']))

    def test_a_passing_report_after_the_grant_is_done_and_labelled_for_each_reader(self):
        self.assertEqual(200, self.grant().status)
        self.later(60)
        at = now_iso(self.clock[0])
        self.assertEqual(201, self.report().status)
        label = "Reported by Heron at %s. This is the agent's own report; it unlocks nothing." % at
        for token in (self.tokens['olive'], self.tokens['cora'], self.agent):
            got = self.walkthrough(token, self.ids['cora'] if token != self.agent else None)['self-check']
            self.assertEqual('done', got['state'])
            self.assertTrue(got['detail'].endswith(label), got['detail'])
            self.assertIn('the certificate matches', got['detail'])

    def test_only_a_report_after_the_grant_counts_and_the_same_second_is_not_after(self):
        self.assertEqual(201, self.report().status)          # before the grant
        self.later(60)
        self.assertEqual(200, self.grant().status)
        self.assertEqual(("todo", "Heron's last check was before the grant: ask it to run its first check again."),
                         (self.c2()['state'], self.c2()['detail']))
        self.later(1)
        self.assertEqual(200, self.grant(method='DELETE').status)
        self.assertEqual(200, self.grant().status)           # granted again, in the same second as the report
        self.assertEqual(201, self.report().status)
        self.assertEqual('todo', self.c2()['state'])
        self.later(1)
        self.assertEqual(201, self.report().status)
        self.assertEqual('done', self.c2()['state'])

    def test_each_failing_item_is_said_in_plain_words_and_points_to_the_set_up(self):
        self.assertEqual(200, self.grant().status)
        cases = [({'matches': False}, "the certificate file does not match this server's: save it from the set-up again"),
                 ({'listed': False}, "this project is not in Heron's own list of projects"),
                 ({'role': 'worker'}, "its AGENT.md is a worker's: open the set-up for Heron again and replace it"),
                 ({'role': 'unknown'}, 'it could not tell whose AGENT.md it has: open the set-up for Heron again')]
        for values, words in cases:
            self.later()
            self.assertEqual(201, self.report(**values).status)
            got = self.c2()
            self.assertEqual(('failed', {'page': 'agent-setup', 'route': None}), (got['state'], got['fix']), values)
            self.assertIn(words, got['detail'])
            self.assertIn('Reported by Heron at', got['detail'])

    def test_no_certificate_of_its_own_is_not_a_failure(self):
        self.assertEqual(200, self.grant().status)
        self.later()
        self.assertEqual(201, self.report(matches=None).status)
        got = self.c2()
        self.assertEqual('done', got['state'])
        self.assertIn('this server has no certificate of its own to compare', got['detail'])

    def test_a_report_about_another_project_is_not_shown_here(self):
        self.assertEqual(200, self.grant().status)
        self.later()
        self.assertEqual(201, self.report(payload=body('elsewhere')).status)
        self.assertEqual('todo', self.c2()['state'])

    def test_another_coordinator_sees_it_hidden_and_reads_no_report(self):
        self.assertEqual(200, self.grant().status)
        self.later()
        self.assertEqual(201, self.report().status)
        self.assertEqual(200, self.member('carl', 'coordinator').status)
        answer = self.request('GET', '/v1/projects/%s/handover?person=%s' % (self.project, self.ids['cora']),
                              token=self.tokens['carl'])
        got = {s['id']: s for s in answer.data['steps']}['self-check']
        self.assertEqual(('hidden', 'Only an owner of this project, or cora, sees what their agent reported.'),
                         (got['state'], got['detail']))
        self.assertNotIn('Reported by', json.dumps(answer.data))

    def test_a_report_unlocks_nothing_and_changes_no_other_step(self):
        def others():
            return {k: (v['state'], v['detail']) for k, v in self.walkthrough(person=self.ids['cora']).items()
                    if k != 'self-check'}
        refused = self.request('PUT', '/v1/projects/%s/handover/note' % self.project,
                               {'text': 'The release branch is r7.', 'for_person': self.ids['cora']}, token=self.agent,
                               key='self-check-note-key-000001')
        before = others()
        self.assertEqual(201, self.report().status)          # a passing report, without a grant
        self.assertEqual(before, others())
        again = self.request('PUT', '/v1/projects/%s/handover/note' % self.project,
                             {'text': 'The release branch is r7.', 'for_person': self.ids['cora']}, token=self.agent,
                             key='self-check-note-key-000002')
        self.assertEqual((403, 403), (refused.status, again.status))   # still no coordinator's authority
        self.assertEqual(200, self.grant().status)
        # Any request of the agent reaches the server, which is what C1 reads (last_seen_at); so the agent reaches it
        # first, and the reports below change nothing else.
        self.later()
        self.assertEqual(200, self.request('GET', '/v1/agents/me', token=self.agent).status)
        granted = others()
        for values in ({'role': 'worker', 'listed': False, 'matches': False}, {}):
            self.later()
            self.assertEqual(201, self.report(**values).status)
            self.assertEqual(granted, others())                # C1 included: it never reads the report


class BackupTests(unittest.TestCase):
    """The HTTP-state pair carries the reports; an older kit restoring it keeps them; damage reads as none."""

    REPORT = {'project': 'alpha', 'sha256_matches': True, 'project_listed': True, 'role': 'coordinator',
              'reported_at': '2026-01-01T00:00:00Z'}

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root / 'backups').mkdir()
        store = Store(str(self.root / 'http-state.json'))
        Service.bootstrap_superuser(store, 'admin', 'admin-password-1')
        with store.lock:
            store.state['agents']['agt_heron'] = {'id': 'agt_heron', 'name': 'Heron', 'owner': 'usr_cora',
                                                 'projects': ['alpha'], 'enabled': True,
                                                 'self_checks': [dict(self.REPORT)]}
            store.save()

    def test_backup_and_restore_carry_the_reports(self):
        self.assertEqual(admin.backup_http_state(self.root)['status'], 'complete')
        (self.root / 'http-state.json').unlink()
        admin.restore_http_state(self.root)
        service = Service(Store(str(self.root / 'http-state.json')))
        self.assertEqual([self.REPORT], service._self_checks(service.state['agents']['agt_heron']))

    def test_a_damaged_report_reads_as_none(self):
        service = Service(Store(str(self.root / 'http-state.json')))
        agent = service.state['agents']['agt_heron']
        agent['self_checks'] = ['junk', dict(self.REPORT, role='boss'), dict(self.REPORT, project_listed='yes'), 7]
        self.assertIsNone(service._self_check_for(agent, 'alpha'))
        agent['self_checks'] = 'not a list'
        self.assertEqual([], service._self_checks(agent))

    def test_an_older_kit_restores_the_pair_and_keeps_the_reports(self):
        previous = os.environ.get('ORCHESTRA_POINTER_PREVIOUS_KIT')
        if not previous:
            self.skipTest('ORCHESTRA_POINTER_PREVIOUS_KIT not supplied; the previous release is not exercised')
        admin.backup_http_state(self.root)
        (self.root / 'http-state.json').unlink()
        script = '''import json,sys
from pathlib import Path
import admin, http_auth
root=Path(sys.argv[1])
print(admin.restore_http_state(root))
store=http_auth.Store(str(root/'http-state.json'))
service=http_auth.Service(store)
with store.lock:
    store.save()
kept=json.loads((root/'http-state.json').read_text(encoding='utf-8'))
assert kept['agents']['agt_heron']['self_checks'][0]['role']=='coordinator', kept['agents']['agt_heron']
print('older kit restored the pair and kept the reports')
'''
        done = subprocess.run([sys.executable, '-c', script, str(self.root)], cwd=previous, text=True,
                              capture_output=True, encoding='utf-8')
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn('kept the reports', done.stdout)


class AgentGuideTests(unittest.TestCase):
    """The coordinator AGENT.md says what to check and carries the one call that reports it."""

    def guide(self, coordinates):
        return node_json(self, ("const p = m.secretlessPayload(%s, 'https://office.example:7443');"
                                "console.log(JSON.stringify({guide: m.agentGuide(p), cert: m.agentGuide(p, { hasCertificate: true }),"
                                " file: p.secretFile, body: m.FIRST_CHECK_BODY}))") % (BUNDLE % coordinates))

    def test_the_coordinator_agent_md_reports_its_first_check_with_curl_k_and_no_secret(self):
        got = self.guide("[{ project: 'alpha', effective: true }]")
        for guide in (got['guide'], got['cert']):
            lines = guide.split('\n')
            self.assertLess(lines.index('## Your first check'), lines.index('## Steps'))
            windows = next(line for line in lines if line.startswith('- Windows (PowerShell): \'{"project"'))
            posix = next(line for line in lines if line.startswith("- macOS/Linux: printf '%s' '{\"project\""))
            self.assertIn('-K "%s"' % got['file']['powershell'], windows)
            self.assertIn('-K %s' % got['file']['posix'], posix)
            for line in (windows, posix):
                self.assertIn("/v1/agents/me/checks'", line)
                self.assertIn('-X POST', line)
                self.assertNotIn('Authorization', line)
                self.assertNotIn('Bearer', line)
        self.assertIn('so send sha256_matches null', got['guide'])
        self.assertIn('/v1/service/certificate serves (sha256_matches true or false)', got['cert'])
        self.assertNotIn("'", got['body'])                     # it sits inside single quotes in both shells

    def test_a_worker_agent_md_has_no_first_check(self):
        got = self.guide('[]')
        self.assertNotIn('## Your first check', got['guide'])
        self.assertNotIn('/v1/agents/me/checks', got['guide'])


class ExampleBodyTests(CheckCase):

    def test_the_example_body_in_the_agent_md_is_what_the_route_takes(self):
        example = node_json(self, 'console.log(JSON.stringify({body: m.FIRST_CHECK_BODY}))')['body']
        made = self.report(payload=json.loads(example.replace('PROJECT', self.project)))
        self.assertEqual(201, made.status, made.data)


if __name__ == '__main__':
    unittest.main()
