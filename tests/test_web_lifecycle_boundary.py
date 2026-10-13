"""The new service action cannot borrow another route's capability or a bound key."""
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parent)]
import admin
import http_authority as authority
try:
    import endpoint
except ModuleNotFoundError as error:
    if error.name != 'fcntl':
        raise
    endpoint = None


class CapabilityTests(unittest.TestCase):
    def test_closed_operation_capabilities_share_the_table(self):
        for operation, capability in [('read', authority.CAP_READ),
                                       ('scope', authority.CAP_COORDINATE), ('fact', authority.CAP_COORDINATE)]:
            request = dict(action='web-lifecycle', project='p', args=[json.dumps(dict(operation=operation))])
            self.assertEqual(authority.required_capability(request), capability)
        for operation in ('release', 'enabled', 'approve', 'merge-acquire', None):
            self.assertIsNone(authority.required_capability(dict(action='web-lifecycle',
                args=[json.dumps(dict(operation=operation))])))

    def test_writer_has_its_own_false_default_and_refuses_damaged_values(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            document = root / 'deployment.private.json'
            for configuration, expected in [({}, False), ({'lifecycle_void_writes': True}, False),
                                              ({'web_lifecycle_mark_writes': True}, True)]:
                document.write_text(json.dumps(configuration), encoding='utf-8')
                self.assertIs(admin.web_lifecycle_mark_writes(root), expected)
            for raw in (1, 'true', [], None):
                document.write_text(json.dumps(dict(web_lifecycle_mark_writes=raw)), encoding='utf-8')
                with self.assertRaisesRegex(ValueError, 'operator repairs'):
                    admin.web_lifecycle_mark_writes(root)


@unittest.skipIf(endpoint is None, 'endpoint imports fcntl; POSIX only')
class EndpointBoundaryTests(unittest.TestCase):
    def test_capability_project_mismatch_stops_before_every_reader(self):
        config = SimpleNamespace(store=Path('must-not-read-lifecycle-authority'))
        for operation, wrong in [('read', authority.CAP_COORDINATE),
                                  ('scope', authority.CAP_TASKS), ('fact', authority.CAP_APPROVE)]:
            request = dict(project='p', actor='usr_' + '0' * 16, action='web-lifecycle',
                           args=[json.dumps(dict(operation=operation))], operation_id='boundary')
            needed = authority.required_capability(request)
            for block in (dict(project='p', capability=wrong), dict(project='foreign', capability=needed)):
                with self.subTest(operation=operation, block=block), \
                     patch.object(endpoint, 'project_dir', side_effect=AssertionError('project read')), \
                     patch.object(endpoint.native, 'run', side_effect=AssertionError('native read')), \
                     patch.object(authority, 'read_state', side_effect=AssertionError('authority read')):
                    result = endpoint.execute(Path('absent-lifecycle-root'), dict(request, authority=block),
                                              authority_config=config, require_authority=True)
                    self.assertEqual((result['returncode'], result['authority_status']), (126, 403))

    def test_bound_project_and_principal_keys_never_reach_service_action(self):
        request = dict(project='p', actor='op', action='web-lifecycle', args=['{}'])
        for confinement in (dict(key_projects=['p']), dict(key_principal='some-principal')):
            with self.subTest(confinement=confinement), \
                 patch.object(endpoint, 'project_dir', side_effect=AssertionError('project read')):
                with self.assertRaisesRegex(ValueError, 'only to the web service'):
                    endpoint.execute(Path('absent-lifecycle-root'), request, **confinement)


if __name__ == '__main__':
    unittest.main()
