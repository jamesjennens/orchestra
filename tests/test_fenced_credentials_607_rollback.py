"""The rollback rehearsal of kittrial-5bb.607, automated against b1f2356c itself.

The new kit issues a rooms-only credential and saves. Then b1f2356c's own code, run in a subprocess on
the same state file, is asked for it: its one credential lookup finds nothing (401), and a GET of the
project's tasks through its own HTTP server is 401 too. b1f2356c then saves (a session sign-in), and keeps
the fenced collections it does not know. The new kit loads that file again: the credential is there, but
suspended, because another kit wrote the file (condition 2). After the operator confirms it, it works
again for rooms only.

b1f2356c is the last kit without the fence. The test is skipped where that commit is not in the
repository (a shallow clone); the CI that gates .607 fetches it.
"""
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
sys.path.insert(0, str(KIT / 'tests'))

import admin
import http_auth
from http_auth import Service, Store
import test_fenced_credentials_607 as fenced

OLDER = 'b1f2356cdc53a0283b083577ed3113b56e64d568'

PROBE = r'''
import json, sys, threading, http.client
sys.path.insert(0, sys.argv[1])
from http_auth import Store, Service, HttpError
import http_service
state, token, project, user, password = sys.argv[2:7]
store = Store(state)
service = Service(store, public_url='https://office.example.invalid')
answer = {}
try:
    service.authenticate(token)
    answer['authenticate'] = 200
except HttpError as error:
    answer['authenticate'] = error.status
backend = http_service.InProcessBackend(service)
httpd = http_service.create_server(service, backend, host='127.0.0.1', port=0)
thread = threading.Thread(target=httpd.serve_forever, daemon=True)
thread.start()
try:
    client = http.client.HTTPConnection('127.0.0.1', httpd.server_address[1], timeout=15)
    client.request('GET', '/v1/projects/%s/tasks' % project, headers={'Authorization': 'Bearer ' + token})
    answer['tasks'] = client.getresponse().status
    client.close()
finally:
    httpd.shutdown()
    httpd.server_close()
service.login(user, password)               # the older kit writes the file
answer['kept'] = sorted(key for key in json.load(open(state)) if key.startswith('fenced_'))
print(json.dumps(answer))
'''


def older_kit_present():
    try:
        return subprocess.run(['git', '-C', str(KIT), 'cat-file', '-e', OLDER + '^{commit}'],
                              capture_output=True, timeout=60).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


@unittest.skipUnless(older_kit_present(), 'b1f2356c is not in this repository (a shallow clone)')
class RollbackRehearsal(fenced.FencedCase):
    def setUp(self):
        super().setUp()
        self.older = Path(tempfile.mkdtemp(prefix='older-kit-'))
        self.addCleanup(shutil.rmtree, self.older, ignore_errors=True)
        archive = self.older / 'kit.tar'
        with open(archive, 'wb') as handle:
            subprocess.run(['git', '-C', str(KIT), 'archive', '--format=tar', OLDER], stdout=handle, check=True,
                           timeout=300)
        with tarfile.open(archive) as bundle:
            bundle.extractall(self.older / 'kit', filter='data') if hasattr(tarfile, 'data_filter') \
                else bundle.extractall(self.older / 'kit')
        archive.unlink()

    def run_older(self):
        self.service.store.save()
        environment = dict(os.environ, HOME=str(self.older), TMPDIR=str(self.older))
        done = subprocess.run([sys.executable, '-I', '-c', PROBE, str(self.older / 'kit'), str(self.service.store.path),
                               self.rooms, self.project, fenced.ADMIN_NAME, fenced.ADMIN_PASSWORD],
                              capture_output=True, text=True, timeout=600, env=environment, cwd=str(self.older))
        self.assertEqual(0, done.returncode, done.stderr[-3000:])
        return json.loads(done.stdout.strip().splitlines()[-1])

    def test_the_older_kit_answers_401_keeps_the_fence_and_the_new_kit_suspends_it_until_confirmed(self):
        answer = self.run_older()
        self.assertEqual(401, answer['authenticate'])
        self.assertEqual(401, answer['tasks'])
        self.assertEqual(['fenced_credential_tokens', 'fenced_credentials'], answer['kept'])
        store = Store(self.service.store.path)
        self.assertTrue(store.state['credentials'][self.rooms_id].get('suspended'))
        admin.fenced_credentials(self.service.store.path, 'confirm')
        store = Store(self.service.store.path)
        service = Service(store, public_url='https://office.example.invalid')
        principal = service.authenticate(self.rooms)
        self.assertEqual(['rooms'], list(principal.scopes))


if __name__ == '__main__':
    unittest.main()
