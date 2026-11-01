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
            'coordinator_grants': {'alpha': {
                'agent_a': {'granted_by': 'usr_o', 'granted_at': '2026-10-31T10:00:00Z'},
                'agent_b': {'granted_by': 'usr_o', 'granted_at': '2026-10-31T11:00:00Z'},
                'agent_c': {'granted_by': 'usr_o', 'granted_at': '2026-10-01T00:00:00Z'}}},
            'release_grants': {},
            'agent_approval': {'alpha': {'mode': 'by-agent', 'set_by': 'usr_o', 'set_at': '2026-10-31T10:05:00Z'}},
            'credentials': {'cred_1': {'user_id': 'usr_o', 'project_id': 'alpha', 'created_at': '2026-10-31T10:10:00Z'}},
            'audit': [event('2026-10-30T00:00:00Z', 'login', 'usr_o', None),
                      event('2026-10-31T10:00:01Z', 'agents.coordinator.grant', 'usr_o', 'alpha'),
                      # agent_b's grant: only a failed attempt of another account was audited
                      event('2026-10-31T11:00:00Z', 'agents.coordinator.grant', 'usr_x', 'alpha'),
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


if __name__ == '__main__':
    unittest.main()
