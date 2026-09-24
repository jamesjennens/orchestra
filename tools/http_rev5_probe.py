#!/usr/bin/env python3
"""Reproduce the three round-5 findings against the checkout under test.

    python tools/http_rev5_probe.py --code-dir <checkout> --label rev4

The same script is pointed at the pre-fix and post-fix revisions. It imports each
checkout's own ``http_authority``/``endpoint.py`` and detects the boundary
signature, so it reports ``closed: false`` on rev4 (5ee7b42) and ``closed: true``
on rev5.

Findings:
  A  validation-errors-become-uncertain   (pre-write refusal flattened to 124)
  B  journal-capacity-lockout             (journal fills permanently)
  C  ssh-can-claim-principal              (SSH descriptor asserts a principal)
  D  real-endpoint-ssh                    (A through the real endpoint.py, POSIX)
  E  real-service-http                    (A/C through the real HTTP service + real
                                           endpoint.py backend, POSIX)

The D/E cases are skipped when ``os.name != 'posix'`` (``endpoint.py`` imports
``fcntl``). E drives the real ``http_service.create_server`` with
``EndpointBackend`` bound to the checkout's ``endpoint.py`` and a disposable
``bin/bd`` shim over an emulated JSON row store.
"""
import argparse
import http.client
import importlib.util
import inspect
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

BD_SHIM = '''#!/usr/bin/env python3
"""Disposable bd shim for the round-5 real-service probe."""
import os
import sys
from pathlib import Path

code_dir = os.environ['REV5_PROBE_CODE_DIR']
sys.path.insert(0, code_dir)
sys.path.insert(0, str(Path(code_dir) / 'tools'))
from strict_canonical_endpoint import Canonical  # noqa: E402

argv = sys.argv[1:]
directory = None
rest = []
index = 0
while index < len(argv):
    value = argv[index]
    if value == '--directory':
        directory = argv[index + 1]
        index += 2
        continue
    if value == '--actor':
        index += 2
        continue
    if value == '--sandbox':
        index += 1
        continue
    rest.append(value)
    index += 1
path = Path(directory)
canonical = Canonical(path.parents[1], path.name)
try:
    # ``endpoint.py`` already enforces the contributor interface for the caller-supplied
    # ``bd`` action; the internal ``run`` closure is not re-filtered there either.
    code, out, err = canonical.bd(rest, enforce=False)
except Exception as error:  # noqa: BLE001
    sys.stderr.write('%s: %s\\n' % (type(error).__name__, error))
    sys.exit(1)
if out:
    sys.stdout.write(out)
if err:
    sys.stderr.write(err)
sys.exit(code)
'''


def load_module(code_dir):
    if str(code_dir) not in sys.path:
        sys.path.insert(0, str(code_dir))
    import http_authority
    return http_authority


def guarded(module, request, journal, effect, config=None, require_authority=False,
            runner=None):
    """Call run_guarded across the rev4/rev5 signature difference."""
    parameters = inspect.signature(module.run_guarded).parameters
    kwargs = {}
    if 'authority_config' in parameters:
        kwargs['authority_config'] = config
        kwargs['require_authority'] = require_authority
    if 'runner' in parameters and runner is not None:
        kwargs['runner'] = runner
    return module.run_guarded(request, journal, effect, **kwargs)


def make_runner(module):
    calls = []

    def dispatch(argv):
        calls.append(list(argv))
        return 'ok'

    if hasattr(module, 'NativeRunner'):
        return module.NativeRunner(dispatch), calls
    return dispatch, calls


def authority_state(project='p'):
    now = time.time()
    return {
        'schema_version': 1,
        'users': {'usr_a': {'id': 'usr_a', 'disabled': False}},
        'sessions': {'sess_a': {'user_id': 'usr_a', 'revoked': False,
                                'absolute_expires': now + 3600,
                                'idle_expires': now + 3600}},
        'credentials': {},
        'projects': {project: {'id': project, 'archived': False}},
        'memberships': {project: {'usr_a': 'owner'}},
    }


def descriptor(user='usr_a', session='sess_a', project='p'):
    return {'via': 'session', 'user_id': user, 'session_hash': session,
            'project': project, 'capability': 'tasks.write', 'now': time.time() + 5}


def ok(stdout='ok'):
    return {'returncode': 0, 'stdout': stdout, 'stderr': ''}


def read_journal(path):
    try:
        data = json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


# ------------------------------------------------- A. pre-write refusal -> rc=2
def probe_validation(module, tmp):
    journal = tmp / 'j-validation.json'
    runner, calls = make_runner(module)
    effects = {'n': 0}

    def effect():
        effects['n'] += 1
        runner(['export', '--all'])
        if effects['n'] == 1:
            raise ValueError('Invalid attachment')
        return ok('after-release')

    request = {'project': 'p', 'actor': 'a', 'action': 'review',
               'args': ['task-1', '@attachment:0'], 'operation_id': 'op-rev5-validation'}
    try:
        first = guarded(module, request, journal, effect, runner=runner)
        raised = None
    except Exception as error:  # noqa: BLE001 - the fixed boundary re-raises
        first = {'returncode': None}
        raised = '%s: %s' % (type(error).__name__, error)
    released = 'op-rev5-validation' not in read_journal(journal)
    retry = guarded(module, request, journal, effect, runner=runner)
    observations = {'raised': raised, 'first': first.get('returncode'),
                    'identity_released': released, 'retry': retry.get('returncode'),
                    'effects': effects['n'], 'invocations': calls}
    closed = (raised is not None and 'Invalid attachment' in raised and released and
              effects['n'] == 2 and retry.get('returncode') == 0)
    return {'observations': observations, 'closed': closed}


# ------------------------------------------------- B. journal retention/lockout
def probe_retention(module, tmp):
    limit = getattr(module, 'JOURNAL_LIMIT', 2000)
    retention = getattr(module, 'JOURNAL_RETENTION_SECONDS', None)
    request = {'project': 'p', 'actor': 'x', 'action': 'bd', 'args': ['create', 'x']}
    effects = {'n': 0}

    def effect():
        effects['n'] += 1
        return ok('ran')

    def fill(path, age):
        moment = time.time() - age
        data = {}
        for index in range(limit):
            data['op-fill-%05d' % index] = {
                'state': 'committed', 'request_hash': 'seed', 'principal': 'actor:x',
                'at': moment - index,
                'envelope': {'returncode': 0, 'stdout': 'seeded', 'stderr': ''}}
        Path(path).write_text(json.dumps(data), encoding='utf-8')

    # Expired receipts: rev5 must reclaim them instead of locking the project out.
    # rev4 has no receipt window, so the same (1000 s old) entries are permanent.
    expired_path = tmp / 'j-expired.json'
    fill(expired_path, (retention + 60) if retention else 1000)
    before = effects['n']
    reopened = guarded(module, dict(request, operation_id='op-rev5-new'), expired_path,
                       effect)
    reopened_effects = effects['n'] - before

    # Live receipts must still fail closed for a genuinely new identity.
    live_path = tmp / 'j-live.json'
    fill(live_path, 10)
    before = effects['n']
    live = guarded(module, dict(request, operation_id='op-rev5-live-new'), live_path, effect)
    live_effects = effects['n'] - before

    # An in-window retry replays; an expired retry is refused as expired.
    replay_path = tmp / 'j-replay.json'
    replay_request = dict(request, operation_id='op-rev5-replay')
    first = guarded(module, replay_request, replay_path, effect)
    retry = guarded(module, replay_request, replay_path, effect)
    stale_path = tmp / 'j-stale.json'
    stale_request = dict(request, operation_id='op-rev5-stale')
    stale_path.write_text(json.dumps({'op-rev5-stale': {
        'state': 'committed', 'request_hash': module.operation_hash(stale_request),
        'principal': module.principal_key(stale_request),
        'at': time.time() - (retention + 60 if retention else 1000),
        'envelope': {'returncode': 0, 'stdout': 'stale', 'stderr': ''}}}),
        encoding='utf-8')
    before = effects['n']
    stale = guarded(module, stale_request, stale_path, effect)
    stale_effects = effects['n'] - before

    observations = {'limit': limit, 'retention': retention,
                    'reopened_status': reopened.get('returncode'),
                    'reopened_effects': reopened_effects,
                    'live_status': live.get('returncode'), 'live_effects': live_effects,
                    'replay_status': first.get('returncode'), 'retry_stdout': retry.get('stdout'),
                    'stale_status': stale.get('returncode'),
                    'stale_effects': stale_effects,
                    'stale_message': (stale.get('stderr') or '').strip()[:80]}
    closed = (reopened.get('returncode') == 0 and reopened_effects == 1 and
              live.get('returncode') == 124 and live_effects == 0 and
              retry.get('stdout') == 'ran' and
              stale.get('returncode') == 2 and stale_effects == 0)
    return {'observations': observations, 'closed': closed}


# ------------------------------------------------- C. SSH cannot claim a principal
def probe_principal(module, tmp):
    store = tmp / 'principal-state.json'
    store.write_text(json.dumps(authority_state()), encoding='utf-8')
    config = module.AuthorityConfig(str(store)) if hasattr(module, 'AuthorityConfig') else None
    journal = tmp / 'j-principal.json'
    effects = []

    def effect(label):
        def run():
            effects.append(label)
            return ok(label)
        return run

    request = {'project': 'p', 'actor': 'attacker', 'action': 'bd', 'args': ['create', 'x'],
               'operation_id': 'op-rev5-forged', 'authority': descriptor()}
    ssh_key = module.principal_key(request)
    first = guarded(module, request, journal, effect('ssh'))
    second = guarded(module, request, journal, effect('http'), config=config,
                     require_authority=True)
    observations = {'ssh_principal': ssh_key, 'first': first.get('returncode'),
                    'second': second.get('returncode'), 'effects': effects}
    closed = (ssh_key == 'actor:attacker' and first.get('returncode') == 0 and
              second.get('returncode') == 2 and effects == ['ssh'])
    return {'observations': observations, 'closed': closed}


# ------------------------------------------------- D. real endpoint.py (SSH)
def _disposable_root(tmp, code_dir):
    root = tmp / 'endpoint-root'
    (root / 'bin').mkdir(parents=True)
    (root / 'projects' / 'probe' / '.beads').mkdir(parents=True)
    (root / 'projects' / 'probe' / '.beads' / 'metadata.json').write_text('{}',
                                                                         encoding='utf-8')
    (root / 'deployment.private.json').write_text(json.dumps({'password': 'probe'}),
                                                  encoding='utf-8')
    shim = root / 'bin' / 'bd'
    shim.write_text(BD_SHIM, encoding='utf-8')
    shim.chmod(0o755)
    return root


def _invoke_endpoint(root, code_dir, request):
    completed = subprocess.run([sys.executable, str(code_dir / 'endpoint.py'),
                                '--root', str(root)],
                               input=json.dumps(request), text=True, encoding='utf-8',
                               capture_output=True, timeout=120,
                               env=dict(os.environ, REV5_PROBE_CODE_DIR=str(code_dir)))
    return json.loads(completed.stdout)


def probe_real_endpoint(module, tmp, code_dir):
    if os.name != 'posix':
        return {'skipped': 'endpoint.py imports fcntl; run on POSIX for this case'}
    root = _disposable_root(tmp, code_dir)
    review = {'project': 'probe', 'actor': 'attacker', 'action': 'review',
              'args': ['kittrial-1', '@attachment:0'],
              'attachments': {'0': {'flag': '--file', 'text': '{}'}},
              'operation_id': 'op-rev5-real-review'}
    work = {'project': 'probe', 'actor': 'attacker', 'action': 'work',
            'args': ['--nope'], 'operation_id': 'op-rev5-real-work'}
    first = _invoke_endpoint(root, code_dir, review)
    second = _invoke_endpoint(root, code_dir, review)
    work_first = _invoke_endpoint(root, code_dir, work)
    journal = read_journal(root / 'projects' / 'probe' / '.http-operations.json')
    observations = {'review_first': first.get('returncode'),
                    'review_first_stderr': (first.get('stderr') or '').strip()[:120],
                    'review_retry': second.get('returncode'),
                    'work_status': work_first.get('returncode'),
                    'work_stderr': (work_first.get('stderr') or '').strip()[:120],
                    'identity_released': 'op-rev5-real-review' not in journal}
    closed = (first.get('returncode') == 2 and 'Payload task mismatch' in
              (first.get('stderr') or '') and second.get('returncode') == 2 and
              work_first.get('returncode') == 2 and 'unrecognized arguments' in
              (work_first.get('stderr') or '') and
              'op-rev5-real-review' not in journal)
    return {'observations': observations, 'closed': closed}


# ------------------------------------------------- E. real HTTP service
def _http(port, method, path, body=None, token=None):
    client = http.client.HTTPConnection('127.0.0.1', port, timeout=60)
    headers = {}
    if body is not None:
        headers['Content-Type'] = 'application/json'
    if token:
        headers['Authorization'] = 'Bearer ' + token
    try:
        client.request(method, path, body=json.dumps(body) if body is not None else None,
                       headers=headers)
        response = client.getresponse()
        status = response.status
        raw = response.read()
    finally:
        client.close()
    try:
        data = json.loads(raw) if raw else None
    except ValueError:
        data = None
    return status, data


def probe_real_http_service(module, tmp, code_dir):
    if os.name != 'posix':
        return {'skipped': 'endpoint.py imports fcntl; real HTTP service needs POSIX'}
    sys.path.insert(0, str(code_dir))
    from http_auth import Service, Store
    from http_service import EndpointBackend, create_server, MAX_BODY_BYTES

    root = _disposable_root(tmp, code_dir)
    store = Store(tmp / 'service-state.json')
    Service.bootstrap_superuser(store, 'root-admin', 'correct-horse-battery-staple')
    service = Service(store)
    backend = EndpointBackend(sys.executable, str(code_dir / 'endpoint.py'), str(root),
                              service=service)
    httpd = create_server(service, backend, host='127.0.0.1', port=0, trusted_proxies=(),
                          max_body=MAX_BODY_BYTES)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        status, admin = _http(port, 'POST', '/v1/sessions',
                              {'username': 'root-admin',
                               'password': 'correct-horse-battery-staple'})
        admin_token = admin['session']['token']
        _, account = _http(port, 'POST', '/v1/accounts', {'username': 'alex'},
                           token=admin_token)
        _http(port, 'POST', '/v1/accounts/%s/password' % account['id'],
              {'new_password': 'alex-password-1'}, token=admin_token)
        _, session = _http(port, 'POST', '/v1/sessions',
                           {'username': 'alex', 'password': 'alex-password-1'})
        token = session['session']['token']
        _, project = _http(port, 'POST', '/v1/projects',
                           {'name': 'Probe', 'project_id': 'probe'}, token=token)
        _, task = _http(port, 'POST', '/v1/projects/probe/tasks', {'title': 'probe task'},
                        token=token)
        _http(port, 'POST', '/v1/projects/probe/tasks/%s/claim' % task['id'], {},
              token=token)
        approve_status, approve = _http(
            port, 'POST', '/v1/projects/probe/tasks/%s/reviews' % task['id'],
            {'operation': 'approve', 'schema_version': 1, 'previous': None,
             'operation_id': 'op-rev5-http-approve', 'contribution': 'c0ffee1234',
             'summary': 'accepted'}, token=token)
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
    observations = {'project': project.get('id') if project else None,
                    'task': task.get('id') if task else None,
                    'approve_status': approve_status,
                    'approve_code': (approve or {}).get('error', {}).get('code')}
    closed = approve_status == 422 and observations['approve_code'] == 'invalid_payload'
    return {'observations': observations, 'closed': closed}


PROBES = (
    ('validation-errors-become-uncertain', 'A', probe_validation),
    ('journal-capacity-lockout', 'B', probe_retention),
    ('ssh-can-claim-principal', 'C', probe_principal),
    ('real-endpoint-ssh', 'D', probe_real_endpoint),
    ('real-service-http', 'E', probe_real_http_service),
)


def main():
    parser = argparse.ArgumentParser(description='Round-5 validation/retention/identity probe')
    parser.add_argument('--code-dir', required=True)
    parser.add_argument('--label', default='checkout')
    parser.add_argument('--out')
    arguments = parser.parse_args()
    code_dir = Path(arguments.code_dir).resolve()
    module = load_module(code_dir)
    root = Path(os.environ.get('ORCHESTRA_TEST_TMP', str(Path.cwd() / '.runtime' / 'probe')))
    report = {'label': arguments.label, 'code_dir': str(code_dir),
              'runner_boundary': 'runner' in inspect.signature(module.run_guarded).parameters,
              'retention': getattr(module, 'JOURNAL_RETENTION_SECONDS', None),
              'findings': {}}
    for name, letter, probe in PROBES:
        tmp = root / ('rev5-%s-%s-%s' % (arguments.label, letter, os.getpid()))
        tmp.mkdir(parents=True, exist_ok=True)
        try:
            if name == 'real-endpoint-ssh' or name == 'real-service-http':
                report['findings'][name] = probe(module, tmp, code_dir)
            else:
                report['findings'][name] = probe(module, tmp)
        except Exception as error:  # noqa: BLE001 - a probe failure is itself evidence
            report['findings'][name] = {'error': '%s: %s' % (type(error).__name__, error),
                                        'closed': False}
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    text = json.dumps(report, indent=2, default=str)
    if arguments.out:
        Path(arguments.out).write_text(text + '\n', encoding='utf-8')
    print(text)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
