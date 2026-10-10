#!/usr/bin/env python3
"""Reproduce the four round-4 findings at the shared authority/identity boundary.

    python tools/http_rev4_probe.py --code-dir <checkout> --label rev3

It imports the checkout's ``http_authority`` and exercises ``run_guarded`` directly,
detecting the boundary signature so the *same* script reports ``closed: false`` on
the pre-fix revision and ``closed: true`` on the fixed one. When the checkout's
``endpoint.py`` can run on this platform (POSIX only, because it imports ``fcntl``)
the authority finding is also reproduced through the real SSH entry point.

Findings:
  A  endpoint-trusts-request-authority   (request-chosen store/lock, opt-in check)
  B  operation-identity-not-principal-bound
  C  exception-path-duplicates
  D  journal-scope-and-bounds
"""
import sys
if sys.version_info < (3, 10):
    # Before every other import, and in syntax Python 3.6 reads: an older interpreter failed in
    # an import further down, with a traceback that hid the cause (kittrial-5bb.191).
    sys.stderr.write('http_rev4_probe.py needs Python 3.10 or newer and was started with Python %d.%d.%d (%s). '
                     'Nothing was carried out. Run it with Python 3.10 or newer.\n'
                     % (sys.version_info[0], sys.version_info[1], sys.version_info[2], sys.executable))
    sys.exit(2)
import argparse
import importlib.util
import inspect
import json
import os
import shutil
import time
from pathlib import Path


def load_module(code_dir):
    sys.path.insert(0, str(code_dir))
    import http_authority
    return http_authority


def is_new_api(module):
    return hasattr(module, 'AuthorityConfig') and \
        'authority_config' in inspect.signature(module.run_guarded).parameters


def config_for(module, store):
    if not is_new_api(module):
        return None
    return module.AuthorityConfig(str(store), str(store) + '.lock')


def call_guard(module, request, journal, effect, config=None, require_authority=False):
    if is_new_api(module):
        return module.run_guarded(request, journal, effect, authority_config=config,
                                  require_authority=require_authority)
    return module.run_guarded(request, journal, effect)


def authority_state():
    now = time.time()
    return {
        'schema_version': 1,
        'users': {'usr_a': {'id': 'usr_a', 'disabled': False},
                  'usr_b': {'id': 'usr_b', 'disabled': False}},
        'sessions': {
            'sess_a': {'user_id': 'usr_a', 'revoked': False,
                       'absolute_expires': now + 3600, 'idle_expires': now + 3600},
            'sess_b': {'user_id': 'usr_b', 'revoked': False,
                       'absolute_expires': now + 3600, 'idle_expires': now + 3600}},
        'credentials': {},
        'projects': {'p': {'id': 'p', 'archived': False}},
        'memberships': {'p': {'usr_a': 'owner', 'usr_b': 'owner'}},
    }


def descriptor(user, session, store=None, lock=None):
    item = {'via': 'session', 'user_id': user, 'session_hash': session, 'project': 'p',
            'capability': 'tasks.write', 'now': time.time() + 5}
    if store is not None:
        item['store'] = store
    if lock is not None:
        item['lock'] = lock
    return item


def ok(stdout='ok'):
    return {'returncode': 0, 'stdout': stdout, 'stderr': ''}


# ------------------------------------------------- A. request-chosen authority
def endpoint_case(module, tmp, code_dir):
    """The real SSH entry point: a hostile authority block must change nothing."""
    if os.name != 'posix':
        return {'skipped': 'endpoint.py imports fcntl; run on POSIX for this case'}
    sys.path.insert(0, str(code_dir))
    spec = importlib.util.spec_from_file_location('probe_endpoint', str(code_dir / 'endpoint.py'))
    endpoint = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(endpoint)
    root = tmp / 'endpoint-root'
    (root / 'bin').mkdir(parents=True)
    (root / 'projects' / 'probe' / '.beads').mkdir(parents=True)
    (root / 'projects' / 'probe' / '.beads' / 'metadata.json').write_text('{}', encoding='utf-8')
    (root / 'deployment.private.json').write_text(json.dumps({'password': 'probe'}),
                                                   encoding='utf-8')
    bd = root / 'bin' / 'bd'
    bd.write_text('#!/bin/sh\necho \'{"id":"kittrial-5bb.1","title":"x"}\'\nexit 0\n',
                  encoding='utf-8')
    bd.chmod(0o755)
    canary_lock = tmp / 'endpoint-caller-chosen' / 'nested' / 'x.lock'
    canary_store = tmp / 'endpoint-canary.json'
    canary_store.write_text('CANARY-NOT-JSON', encoding='utf-8')
    request = {'project': 'probe', 'actor': 'attacker', 'action': 'bd', 'args': ['create', 'x'],
               'operation_id': 'op-endpoint-a',
               'authority': descriptor('usr_a', 'sess_a', str(canary_store), str(canary_lock))}
    request['authority']['project'] = 'probe'
    result = endpoint.execute(root, request)
    return {'observations': {'returncode': result.get('returncode'),
                             'stderr': (result.get('stderr') or '').strip()[:120],
                             'caller_lock_created': canary_lock.exists()},
            'closed': (not canary_lock.exists()) and result.get('returncode') == 0}


def probe_authority(module, tmp, code_dir):
    server_store = tmp / 'server-state.json'
    server_store.write_text(json.dumps(authority_state()), encoding='utf-8')
    config = config_for(module, server_store)
    effects = {'n': 0}

    def effect():
        effects['n'] += 1
        return ok()

    # No trusted configuration: the SSH path must ignore the whole authority block,
    # including its caller-chosen store and lock paths.
    ssh_lock = tmp / 'ssh-caller' / 'nested' / 'x.lock'
    ssh_store = tmp / 'ssh-canary.json'
    ssh_store.write_text('CANARY-NOT-JSON', encoding='utf-8')
    ssh_request = {'project': 'p', 'actor': 'attacker', 'action': 'bd', 'args': ['create', 'x'],
                   'operation_id': 'op-a-ssh',
                   'authority': descriptor('usr_a', 'sess_a', str(ssh_store), str(ssh_lock))}
    first = call_guard(module, ssh_request, tmp / 'j-a-ssh.json', effect)
    ssh_ignored = (not ssh_lock.exists()) and first.get('returncode') == 0 and effects['n'] == 1

    # Trusted configuration: the server paths win and the hostile request paths are
    # never touched.
    cfg_lock = tmp / 'cfg-caller' / 'nested' / 'x.lock'
    cfg_store = tmp / 'cfg-canary.json'
    cfg_store.write_text('CANARY-NOT-JSON', encoding='utf-8')
    cfg_request = {'project': 'p', 'actor': 'attacker', 'action': 'bd', 'args': ['create', 'x'],
                   'operation_id': 'op-a-cfg',
                   'authority': descriptor('usr_a', 'sess_a', str(cfg_store), str(cfg_lock))}
    second = call_guard(module, cfg_request, tmp / 'j-a-cfg.json', effect, config=config)
    config_wins = (not cfg_lock.exists()) and second.get('returncode') == 0

    # A trusted mutation launch must refuse a descriptor-less request instead of
    # silently skipping the live check.
    bare = {'project': 'p', 'actor': 'attacker', 'action': 'bd', 'args': ['create', 'x'],
            'operation_id': 'op-a-bare'}
    third = call_guard(module, bare, tmp / 'j-a-bare.json', effect, config=config,
                       require_authority=True)
    optin_closed = third.get('returncode') == 126

    endpoint = endpoint_case(module, tmp, code_dir)
    return {'observations': {'ssh_ignored_request_authority': ssh_ignored,
                             'trusted_config_ignores_request_paths': config_wins,
                             'descriptor_required_when_configured': optin_closed,
                             'effects': effects['n'],
                             'endpoint': endpoint.get('observations', endpoint)},
            'closed': ssh_ignored and config_wins and optin_closed and
                      endpoint.get('closed', True)}


# ------------------------------------------- B. principal-bound operation identity
def probe_identity(module, tmp):
    server_store = tmp / 'ident-state.json'
    server_store.write_text(json.dumps(authority_state()), encoding='utf-8')
    config = config_for(module, server_store)
    effects = []
    journal = tmp / 'j-identity.json'

    def effect(label):
        def run():
            effects.append(label)
            return ok(label)
        return run

    def request(actor, user, session, operation_id):
        item = descriptor(user, session)
        if not is_new_api(module):
            # The pre-fix boundary reads the store/lock from the request; the fixed
            # one ignores those fields and uses the launch configuration.
            item['store'] = str(server_store)
            item['lock'] = str(server_store) + '.lock'
        return {'project': 'p', 'actor': actor, 'action': 'bd', 'args': ['create', 'x'],
                'operation_id': operation_id, 'authority': item}

    first = call_guard(module, request('a', 'usr_a', 'sess_a', 'op-shared'), journal,
                       effect('A'), config=config)
    second = call_guard(module, request('b', 'usr_b', 'sess_b', 'op-shared'), journal,
                        effect('B'), config=config)
    replay = call_guard(module, request('a', 'usr_a', 'sess_a', 'op-shared'), journal,
                        effect('A2'), config=config)
    conflict = second.get('returncode') == 2
    one_effect = len(effects) == 1 and effects[0] == 'A'
    exact_replay = replay.get('returncode') == 0 and replay.get('stdout') == 'A'
    return {'observations': {'first': first.get('returncode'),
                             'other_principal': second.get('returncode'),
                             'other_principal_stdout': second.get('stdout'),
                             'exact_replay': replay.get('stdout'),
                             'effects': effects},
            'closed': conflict and one_effect and exact_replay}


# --------------------------------------------------- C. exception after the effect
def probe_exception(module, tmp):
    journal = tmp / 'j-exception.json'
    effects = {'n': 0}
    request = {'project': 'p', 'actor': 'a', 'action': 'bd', 'args': ['create', 'x'],
               'operation_id': 'op-exception'}

    def effect():
        effects['n'] += 1
        if effects['n'] == 1:
            raise RuntimeError('native commit then transport failure')
        return ok('second')

    try:
        first = call_guard(module, request, journal, effect)
        first_raised = None
    except Exception as error:  # noqa: BLE001 - the pre-fix boundary re-raises
        first = {'returncode': None}
        first_raised = type(error).__name__
    retry = call_guard(module, request, journal, effect)
    closed = effects['n'] == 1 and retry.get('returncode') == 124
    observations = {'first_raised': first_raised, 'first': first,
                    'retry': retry.get('returncode'), 'effects': effects['n']}
    if hasattr(module, 'PreEffectFailure'):
        released = {'n': 0}

        def pre_effect():
            released['n'] += 1
            if released['n'] == 1:
                raise module.PreEffectFailure('validated before any write')
            return ok('after-release')

        request2 = dict(request, operation_id='op-pre-effect')
        try:
            call_guard(module, request2, journal, pre_effect)
        except module.PreEffectFailure:
            pass
        after = call_guard(module, request2, journal, pre_effect)
        observations['proven_pre_effect_releases'] = (
            released['n'] == 2 and after.get('returncode') == 0)
        closed = closed and observations['proven_pre_effect_releases']
    return {'observations': observations, 'closed': closed}


# --------------------------------------------------------- D. journal scope/bounds
def probe_journal(module, tmp):
    new_api = is_new_api(module)
    limit = getattr(module, 'JOURNAL_LIMIT', 2000) if new_api else 5000
    request = {'project': 'p', 'actor': 'x', 'action': 'bd', 'args': ['create', 'x']}
    request_hash = module.operation_hash(request)
    principal = module.principal_key(request) if hasattr(module, 'principal_key') else 'actor:x'
    journal = tmp / 'j-bounds.json'
    base = time.time() - 1000
    data = {}
    for index in range(limit):
        data['op-fill-%05d' % index] = {
            'state': 'committed', 'request_hash': request_hash, 'principal': principal,
            'at': base + index, 'envelope': {'returncode': 0, 'stdout': 'seeded', 'stderr': ''}}
    journal.write_text(json.dumps(data), encoding='utf-8')

    effects = {'n': 0}

    def effect():
        effects['n'] += 1
        return ok('ran')

    capacity = call_guard(module, dict(request, operation_id='op-capacity'), journal, effect)
    capacity_effects = effects['n']
    oldest = call_guard(module, dict(request, operation_id='op-fill-00000'), journal, effect)
    oldest_effects = effects['n'] - capacity_effects
    no_eviction = (oldest.get('returncode') == 0 and oldest.get('stdout') == 'seeded' and
                   oldest_effects == 0)

    # Oversized envelope: recorded by digest, never replayed as a (truncated) result.
    big_journal = tmp / 'j-big.json'
    big_request = dict(request, operation_id='op-big')
    if new_api:
        store = module.OperationJournal(str(big_journal), max_envelope=64)
        store.reserve('op-big', module.operation_hash(big_request), principal)
        store.complete('op-big', {'returncode': 0, 'stdout': 'Z' * 4096, 'stderr': ''},
                       module.operation_hash(big_request), principal)
    else:
        store = module.OperationJournal(str(big_journal), limit=5000)
        store.reserve('op-big', module.operation_hash(big_request))
        store.complete('op-big', {'returncode': 0, 'stdout': 'Z' * 4096, 'stderr': ''},
                       module.operation_hash(big_request))
    before_big = effects['n']
    big_retry = call_guard(module, big_request, big_journal, effect)
    big_effects = effects['n'] - before_big

    return {'observations': {'limit': limit, 'capacity_status': capacity.get('returncode'),
                             'capacity_effects': capacity_effects,
                             'oldest_retry': oldest.get('returncode'),
                             'oldest_effects': oldest_effects,
                             'oversized_retry': big_retry.get('returncode'),
                             'oversized_effects': big_effects},
            'closed': (capacity.get('returncode') == 124 and capacity_effects == 0 and
                       no_eviction and big_retry.get('returncode') == 124 and
                       big_effects == 0)}


PROBES = (
    ('authority-from-request', 'A', probe_authority),
    ('operation-identity', 'B', probe_identity),
    ('exception-after-effect', 'C', probe_exception),
    ('journal-bounds', 'D', probe_journal),
)


def main():
    parser = argparse.ArgumentParser(description='Round-4 authority/identity probe')
    parser.add_argument('--code-dir', required=True)
    parser.add_argument('--label', default='checkout')
    parser.add_argument('--out')
    arguments = parser.parse_args()
    code_dir = Path(arguments.code_dir).resolve()
    module = load_module(code_dir)
    root = Path(os.environ.get('ORCHESTRA_TEST_TMP', str(Path.cwd() / '.runtime' / 'probe')))
    report = {'label': arguments.label, 'code_dir': str(code_dir),
              'new_boundary': is_new_api(module), 'findings': {}}
    for name, letter, probe in PROBES:
        tmp = root / ('rev4-%s-%s-%s' % (arguments.label, letter, os.getpid()))
        tmp.mkdir(parents=True, exist_ok=True)
        try:
            if name == 'authority-from-request':
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
