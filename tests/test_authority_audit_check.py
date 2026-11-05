"""tools/authority_audit_check.py (kittrial-5bb.487): grants no committed audit event explains."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT / 'tools'))
import authority_audit_check as tool


def event(at, action, user, project, outcome='committed'):
    return {'time': at, 'action': action, 'user_id': user, 'project_id': project, 'outcome': outcome}


class AuditCheckTests(unittest.TestCase):
    def state(self):
        return {
            'schema_version': tool.SCHEMA_VERSION,
            'users': {'usr_root': {'superuser': True}, 'usr_o': {}, 'usr_x': {}},
            'coordinator_grants': {'alpha': {
                'agent_a': {'granted_by': 'usr_o', 'granted_at': '2026-10-31T10:00:00Z'},
                'agent_b': {'granted_by': 'usr_o', 'granted_at': '2026-10-31T11:00:00Z'},
                'agent_c': {'granted_by': 'usr_o', 'granted_at': '2026-10-01T00:00:00Z'}}},
            'release_grants': {},
            'agent_approval': {'alpha': {'mode': 'by-agent', 'set_by': 'usr_o', 'set_at': '2026-10-31T10:05:00Z'}},
            'credentials': {'cred_1': {'user_id': 'usr_o', 'project_id': 'alpha', 'created_at': '2026-10-31T10:10:00Z'}},
            'audit': [event('2026-10-30T00:00:00Z', 'login', 'usr_o', None),
                      dict(event('2026-10-31T10:00:01Z', 'agents.coordinator.grant', 'usr_o', 'alpha'),
                           reason='agent agent_a'),
                      # agent_b's grant: only a failed attempt of another account was audited
                      dict(event('2026-10-31T11:00:00Z', 'agents.coordinator.grant', 'usr_x', 'alpha'),
                           reason='agent agent_b'),
                      event('2026-10-31T10:05:00Z', 'projects.agent_approval', 'usr_o', 'alpha'),
                      event('2026-10-31T10:10:00Z', 'credentials.issue', 'usr_o', 'alpha', outcome='rejected')]}

    def test_an_unaudited_grant_is_listed_and_an_audited_one_is_not(self):
        report = tool.check(self.state())
        unexplained = {(row['kind'], row['subject']) for row in report['unexplained']}
        self.assertEqual(unexplained, {('coordinator grant', 'agent_b'), ('credential', 'cred_1')})
        self.assertEqual([row['subject'] for row in report['unchecked']], ['agent_c'])
        self.assertEqual(report['audit_oldest'], '2026-10-30T00:00:00Z')

    def test_the_command_reads_a_file_changes_nothing_and_says_so_in_its_status(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'state.json'
            text = json.dumps(self.state())
            path.write_text(text, encoding='utf-8')
            self.assertEqual(1, tool.main([str(path), '--json']))
            self.assertEqual(text, path.read_text(encoding='utf-8'))
            clean = self.state()
            clean['coordinator_grants']['alpha'].pop('agent_b')
            clean['credentials'] = {}
            path.write_text(json.dumps(clean), encoding='utf-8')
            self.assertEqual(0, tool.main([str(path)]))

    # review of kittrial-5bb.487 F3 and the GLM review's notes
    def test_an_agent_credential_a_superuser_issued_for_another_owner_is_explained_by_its_id(self):
        state = self.state()
        state['coordinator_grants'] = {}
        state['agent_approval'] = {}
        state['credentials'] = {
            'cred_a': {'user_id': 'usr_o', 'agent_id': 'agt_1', 'created_at': '2026-10-31T12:00:00Z'},
            'cred_b': {'user_id': 'usr_o', 'agent_id': 'agt_2', 'created_at': '2026-10-31T12:00:00Z'}}
        state['audit'] += [
            # issued by an account that was a superuser then and is not now: the id still explains it
            dict(event('2026-10-31T12:00:00Z', 'agents.credentials.issued', 'usr_former', None),
                 agent_id='agt_1', target_credential_id='cred_a'),
            # names another credential: explains neither, even by the owner
            dict(event('2026-10-31T12:00:00Z', 'agents.credentials.issued', 'usr_o', None),
                 agent_id='agt_2', target_credential_id='cred_zz')]
        self.assertEqual(['cred_b'], [row['subject'] for row in tool.check(state)['unexplained']])

    def test_a_superusers_event_explains_a_grant_given_for_another_account_and_a_wrong_agent_does_not(self):
        state = self.state()
        state['credentials'] = {}
        state['audit'][2]['user_id'] = 'usr_root'          # agent_b's event, now a superuser's
        self.assertEqual([], tool.check(state)['unexplained'])
        state['audit'][2]['reason'] = 'agent agent_q'       # an event for another agent
        self.assertEqual(['agent_b'], [row['subject'] for row in tool.check(state)['unexplained']])

    def test_a_kept_change_audited_committed_unsaved_is_not_a_lead(self):
        state = self.state()
        state['credentials'] = {}
        state['coordinator_grants']['alpha'].pop('agent_b')
        state['agent_approval']['alpha'] = {'mode': 'by-person', 'set_by': 'usr_o', 'set_at': '2026-10-31T13:00:00Z'}
        state['audit'].append(dict(event('2026-10-31T13:00:00Z', 'projects.agent_approval', 'usr_o', 'alpha',
                                         outcome='committed-unsaved'), agent_approval='by-person'))
        self.assertEqual([], tool.check(state)['unexplained'])

    def test_an_unknown_schema_version_is_refused_and_nothing_is_checked(self):
        state = self.state()
        state['schema_version'] = 99
        with self.assertRaises(ValueError):
            tool.check(state)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'state.json'
            path.write_text(json.dumps(state), encoding='utf-8')
            self.assertEqual(2, tool.main([str(path)]))


if __name__ == '__main__':
    unittest.main()
