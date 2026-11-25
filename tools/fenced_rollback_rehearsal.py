#!/usr/bin/env python3
"""Rehearse a rollback below the fenced credentials (kittrial-5bb.607), in a scratch folder only.

    python3 tools/fenced_rollback_rehearsal.py --scratch /path/to/new/empty/folder [--older COMMIT]

It never touches a running office: everything lives under --scratch, which must not exist yet or be
empty, the web services it starts listen on 127.0.0.1 on a free port for the length of one request,
and HOME and TMPDIR point into the scratch folder. It needs git and this kit's repository (for the
older kit, by ``git archive``). It deletes nothing; remove the scratch folder yourself afterwards.

The steps, each run as its own Python process:

1. **issue** (this kit): a new state, one project, a rooms-only agent and an ordinary agent. The rooms
   credential gets 403 on the project's tasks, and the ordinary one 200.
2. **older** (the older kit, b1f2356c by default, on the same state file): the rooms token is 401 to the
   older kit's own credential lookup and to a GET of the tasks through its own web server. The older kit
   then writes the file (a sign-in) and keeps the two fenced keys it does not know.
3. **forward** (this kit again): the rooms credential is there but suspended, because another kit wrote
   the file. ``admin.py fenced-credentials confirm`` lifts the suspension, and the token then gets 403
   on the tasks again: rooms only.

Exit 0 when every step answered as above, 1 otherwise, with each answer printed as JSON lines.
"""
import sys
if sys.version_info < (3, 10):
    # Before every other import, and in syntax Python 3.6 reads: an older interpreter failed in
    # an import further down, with a traceback that hid the cause (kittrial-5bb.191).
    sys.stderr.write('fenced_rollback_rehearsal.py needs Python 3.10 or newer and was started with Python %d.%d.%d (%s). '
                     'Nothing was carried out. Run it with Python 3.10 or newer.\n'
                     % (sys.version_info[0], sys.version_info[1], sys.version_info[2], sys.executable))
    sys.exit(2)
import argparse
import json
import os
import secrets
import subprocess
import tarfile
from pathlib import Path

KIT = Path(__file__).resolve().parents[1]
OLDER = 'b1f2356cdc53a0283b083577ed3113b56e64d568'

SERVE = r'''
import http.client, threading
def ask(service, method, path, token=None, body=None):
    import http_service
    backend = http_service.InProcessBackend(service)
    httpd = http_service.create_server(service, backend, host='127.0.0.1', port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        client = http.client.HTTPConnection('127.0.0.1', httpd.server_address[1], timeout=30)
        headers = {'Content-Type': 'application/json'} if body is not None else {}
        if token:
            headers['Authorization'] = 'Bearer ' + token
        client.request(method, path, body=None if body is None else json.dumps(body), headers=headers)
        answer = client.getresponse()
        data = answer.read()
        client.close()
        return answer.status, (json.loads(data) if data else None)
    finally:
        httpd.shutdown()
        httpd.server_close()
'''

ISSUE = r'''
import json, sys
sys.path.insert(0, sys.argv[1])
from http_auth import Service, Store
''' + SERVE + r'''
state, password = sys.argv[2], sys.argv[3]
Service.bootstrap_superuser(Store(state), 'rehearsal-admin', password)
service = Service(Store(state), public_url='https://rehearsal.invalid')
_, session = ask(service, 'POST', '/v1/sessions', body={'username': 'rehearsal-admin', 'password': password})
admin = session['session']['token']
_, project = ask(service, 'POST', '/v1/projects', admin, {'name': 'rehearsal'})
pid = project['id']
made = {}
for name, scopes in (('Scribe', ['rooms']), ('Plain', None)):
    body = {'name': name, 'working_directory': '/scratch/' + name, 'projects': [pid]}
    if scopes:
        body['scopes'] = scopes
    status, answer = ask(service, 'POST', '/v1/agents', admin, body)
    made[name] = answer['credential']['secret']
service.store.save()
print(json.dumps({'step': 'issue', 'project': pid, 'rooms': made['Scribe'], 'plain': made['Plain'],
                  'rooms_tasks': ask(service, 'GET', '/v1/projects/%s/tasks' % pid, made['Scribe'])[0],
                  'plain_tasks': ask(service, 'GET', '/v1/projects/%s/tasks' % pid, made['Plain'])[0]}))
'''

OLDER_STEP = r'''
import json, sys
sys.path.insert(0, sys.argv[1])
from http_auth import Service, Store, HttpError
''' + SERVE + r'''
state, password, project, token = sys.argv[2:6]
service = Service(Store(state), public_url='https://rehearsal.invalid')
try:
    service.authenticate(token)
    looked = 200
except HttpError as error:
    looked = error.status
tasks = ask(service, 'GET', '/v1/projects/%s/tasks' % project, token)[0]
service.login('rehearsal-admin', password)          # the older kit writes the file
kept = sorted(key for key in json.load(open(state)) if key.startswith('fenced_'))
print(json.dumps({'step': 'older', 'lookup': looked, 'tasks': tasks, 'kept': kept}))
'''

FORWARD = r'''
import json, sys
sys.path.insert(0, sys.argv[1])
import admin
from http_auth import Service, Store, HttpError
''' + SERVE + r'''
state, project, token = sys.argv[2:5]
def status():
    try:
        Service(Store(state), public_url='https://rehearsal.invalid').authenticate(token)
        return 200
    except HttpError as error:
        return error.status
before = status()
suspended = bool(next(c for c in Store(state).state['credentials'].values()
                      if c.get('scopes') == ['rooms']).get('suspended'))
said = admin.fenced_credentials(state, 'confirm')
after = status()
tasks = ask(Service(Store(state), public_url='https://rehearsal.invalid'), 'GET',
            '/v1/projects/%s/tasks' % project, token)[0]
print(json.dumps({'step': 'forward', 'suspended': suspended, 'before_confirm': before, 'confirm': said,
                  'after_confirm': after, 'tasks': tasks}))
'''


def run(code, kit, scratch, *arguments):
    environment = dict(os.environ, HOME=str(scratch / 'home'), TMPDIR=str(scratch / 'tmp'))
    done = subprocess.run([sys.executable, '-I', '-c', code, str(kit)] + [str(a) for a in arguments],
                          capture_output=True, text=True, timeout=900, env=environment, cwd=str(scratch))
    if done.returncode != 0:
        raise SystemExit('A step failed:\n' + done.stderr[-4000:])
    return json.loads(done.stdout.strip().splitlines()[-1])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--scratch', required=True, help='a folder that does not exist yet, or is empty')
    parser.add_argument('--older', default=OLDER, help='the older kit commit (default b1f2356c)')
    options = parser.parse_args(argv)
    scratch = Path(options.scratch).resolve()
    if scratch.exists() and any(scratch.iterdir()):
        raise SystemExit('%s is not empty: give a new folder; nothing was changed.' % scratch)
    for name in ('home', 'tmp', 'state', 'older'):
        (scratch / name).mkdir(parents=True, exist_ok=True)
    archive = scratch / 'older.tar'
    with open(archive, 'wb') as handle:
        subprocess.run(['git', '-C', str(KIT), 'archive', '--format=tar', options.older], stdout=handle, check=True)
    with tarfile.open(archive) as bundle:
        bundle.extractall(scratch / 'older')
    archive.unlink()
    state = scratch / 'state' / 'http-state.json'
    password = 'rehearsal-' + secrets.token_hex(12)
    issued = run(ISSUE, KIT, scratch, state, password)
    older = run(OLDER_STEP, scratch / 'older', scratch, state, password, issued['project'], issued['rooms'])
    forward = run(FORWARD, KIT, scratch, state, issued['project'], issued['rooms'])
    for step in (dict(issued, rooms='(secret)', plain='(secret)'), older, forward):
        print(json.dumps(step))
    good = (issued['rooms_tasks'] == 403 and issued['plain_tasks'] == 200
            and older['lookup'] == 401 and older['tasks'] == 401
            and older['kept'] == ['fenced_credential_tokens', 'fenced_credentials']
            and forward['suspended'] and forward['before_confirm'] == 401
            and forward['after_confirm'] == 200 and forward['tasks'] == 403)
    print('REHEARSAL %s (scratch %s; remove it when done)' % ('PASSED' if good else 'FAILED', scratch))
    return 0 if good else 1


if __name__ == '__main__':
    sys.exit(main())
