"""kittrial-5bb.518: the AGENT.md the set-up page writes carries a version, and its text cannot change without a bump.

The first line of the file is ``AGENT.md version N (task)``. ``GET /v1/agents/me`` answers the N the server knows
(``agent_md.version``) so an agent with an older file can tell. The hash below pins the text of every variant the
page writes; changing any word of AGENT.md changes the hash, and this test then fails until the version is raised
(web/js/agentSetup.js AGENT_MD_VERSION_LINE and http_service.ApiHandler.AGENT_MD_VERSION) and the new pin is added.
"""
import hashlib
import re
import sys
import unittest
from pathlib import Path

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
sys.path.insert(0, str(KIT / 'tests'))
import http_service
import test_coordinator_agent_md as md

#: AGENT.md version -> sha256 of the four texts the page writes for the fixed payload below, in order
#: (worker, worker with certificate, coordinator, coordinator with certificate), each ended by a NUL.
#: Candidate 1112a: version 2 first ships with this candidate, so its pin is the text as integrated (with .521/.541,
#: .550 and .559), not the text on .518's own base (01c340cc...), which was never on main.
PINS = {2: '1eeed6d71b17d7e6fe6230e1aa52f8138da23118fe6964d1bb90a9af00c8acd1'}


class VersionTests(md.CoordinatorGuideTests):

    def texts(self):
        worker = self.guides('[]')
        coordinator = self.guides("[{ project: 'alpha', effective: true }]")
        return [worker['guide'], worker['cert'], coordinator['guide'], coordinator['cert']]

    def test_the_first_line_is_the_version_line_and_the_server_knows_the_same_version(self):
        for text in self.texts():
            first = text.split('\n', 1)[0]
            self.assertEqual('AGENT.md version 2 (kittrial-5bb.518)', first)
            self.assertTrue(text.split('\n')[1].startswith('# '))
            found = int(re.fullmatch(r'AGENT\.md version (\d+) \(kittrial-5bb\.\d+\)', first).group(1))
            self.assertEqual(http_service.ApiHandler.AGENT_MD_VERSION, found)

    def test_the_text_cannot_change_without_a_version_bump(self):
        texts = self.texts()
        version = int(re.match(r'AGENT\.md version (\d+) ', texts[0]).group(1))
        digest = hashlib.sha256(''.join(text + '\0' for text in texts).encode('utf-8')).hexdigest()
        self.assertIn(version, PINS, 'a new AGENT.md version needs its pin: %s' % digest)
        self.assertEqual(PINS[version], digest,
                         'AGENT.md text changed without a version bump (raise AGENT_MD_VERSION_LINE and '
                         'ApiHandler.AGENT_MD_VERSION, then pin %s under the new version)' % digest)

    def test_rule_nine_says_the_setting_and_that_the_server_is_authoritative(self):
        step = self.guides("[{ project: 'alpha', effective: true }]")['steps'][8]
        for text in ("depends on the project's agent_approval setting (GET /v1/projects/PROJECT answers it, and so "
                     "does approval_permission in the brief): by-agent lets you, by-person needs another party.",
                     "Your owner's server may be newer than this file. Where a read answers approval_permission, "
                     "it is authoritative."):
            self.assertIn(text, step)


# The base class's own tests are run by test_coordinator_agent_md; this module runs only its own.
for _name in [n for n in dir(md.CoordinatorGuideTests) if n.startswith('test_')]:
    if _name not in VersionTests.__dict__:
        setattr(VersionTests, _name, None)

if __name__ == '__main__':
    unittest.main()
