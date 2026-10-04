"""Creating a project on the host for the web service (kittrial-5bb.118 part 2).

An operator creates a project with ``admin.py add-project``. A named web account that a
superuser granted "may create projects" can now cause the same work from the web
interface. This module is the host half of that: it runs the same initialization as
``add-project`` and keeps one small record per creation, so that

* a refusal leaves nothing behind (no directory, no database, no record);
* a creation that stops half way is never guessed at: the record says ``incomplete``,
  the name stays taken, an operator finishes it (``admin.py finish-project``) or
  removes it (``admin.py retire-project --force``), and nothing is dropped by the kit;
* the names an account holds are countable on the host, including the half-made ones.

The records live in ``<root>/project-creations/NAME.json`` (0600). A record's states:

``started``     the intent, written before the first write. A process that was killed
                leaves this state; with a non-empty project directory it reads as
                ``incomplete``.
``incomplete``  the work stopped after something was made.
``created``     the project is initialized and its first backup passed.
``removed``     an operator retired the half-made directory.

Only the endpoint calls :func:`create`, and only for a request the web service made
(see ``endpoint.py``). Nothing here decides who may create: the caller has already
checked the grant against the live authority store, under its lock.
"""
import contextlib
import io
import json
import os
import time
from pathlib import Path

RECORDS_DIR = 'project-creations'
LOCK_NAME = '.lock'
STATES = ('started', 'incomplete', 'created', 'removed')
#: The stages of the work, in order; a record names the one that was running.
STAGES = ('init', 'configure', 'backup-target', 'merge-slot', 'first-backup')
ERROR_LIMIT = 300
#: One sentence for a name that cannot be used, whatever the reason (taken by a project,
#: held by another creation, or retired), so the answer does not say which.
NOT_AVAILABLE = 'Project name %s is not available: choose another name'


class NothingMade(ValueError):
    """The creation was refused, or failed, with nothing left behind."""


def _stamp():
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())


def records_dir(root):
    path = Path(root) / RECORDS_DIR
    if path.is_symlink():
        raise ValueError('The %s directory must not be a symlink' % RECORDS_DIR)
    return path


def record_path(root, name):
    import admin
    admin.validate_name(name)
    return records_dir(root) / (name + '.json')


def read_record(root, name):
    """The creation record for a name, or None. A damaged record is an error, not absence."""
    path = record_path(root, name)
    if path.is_symlink():
        raise ValueError('A project creation record must not be a symlink')
    if not path.is_file():
        return None
    record = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(record, dict) or record.get('project') != name or record.get('state') not in STATES \
            or not isinstance(record.get('by'), str):
        raise ValueError('The project creation record for %s is damaged; an operator must look at %s'
                         % (name, path))
    return record


def write_record(root, name, record):
    import admin
    directory = records_dir(root)
    directory.mkdir(mode=0o700, exist_ok=True)
    admin.atomic_private_write(record_path(root, name), json.dumps(record, sort_keys=True, indent=1) + '\n')


def delete_record(root, name):
    path = record_path(root, name)
    if path.is_file() and not path.is_symlink():
        path.unlink()


def made(root, name):
    """What exists for a name on the host: ``nothing``, ``partial`` or ``initialized``."""
    import admin
    path = admin.project_dir(root, name)
    if not path.exists() or (path.is_dir() and not any(path.iterdir())):
        return 'nothing'
    return 'initialized' if (path / '.beads' / 'metadata.json').is_file() else 'partial'


def effective_state(root, record):
    """A record's state as an operator should read it.

    ``started`` with something on disk is ``incomplete`` (the process was stopped);
    ``started`` with nothing on disk is ``started`` (a retry resumes it).
    """
    if record['state'] == 'started' and made(root, record['project']) != 'nothing':
        return 'incomplete'
    return record['state']


def records(root):
    """Every creation record, sorted by name, each with its ``effective`` state."""
    directory = records_dir(root)
    if not directory.is_dir():
        return []
    found = []
    for path in sorted(directory.glob('*.json')):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            record = read_record(root, path.stem)
        except ValueError:
            found.append({'project': path.stem, 'state': 'damaged', 'effective': 'damaged', 'by': None})
            continue
        if record is not None:
            found.append(dict(record, effective=effective_state(root, record)))
    return found


def holds(root, account, registered=()):
    """The names ``account`` holds on the host that the web service has not registered.

    These count toward the account's limit beside its registered projects: a creation
    that is under way, one that stopped half way, and one that finished on the host
    whose registration has not been written yet. A removed one holds nothing.
    """
    registered = set(registered)
    return sorted(record['project'] for record in records(root)
                  if record.get('by') == account and record['effective'] in ('started', 'incomplete', 'created')
                  and record['project'] not in registered)


@contextlib.contextmanager
def creation_lock(root):
    """One creation at a time on a host: the count and the name check are then exact."""
    from http_authority import file_lock
    directory = records_dir(root)
    directory.mkdir(mode=0o700, exist_ok=True)
    with file_lock(directory / LOCK_NAME, timeout=600.0):
        yield


def _bounded(error):
    text = '%s: %s' % (type(error).__name__, error)
    return text if len(text) <= ERROR_LIMIT else text[:ERROR_LIMIT] + '...'


def create(root, name, account, operation_id, limit=None, registered=(), initialize=None, known=()):
    """Create ``name`` for ``account``; returns the result the web service acts on.

    ``{'status': 'created', ...}`` or ``{'status': 'incomplete', ...}``. Raises
    :class:`NothingMade` when the request is refused or fails with nothing left behind;
    the same request may then simply be sent again.

    ``limit`` is the account's limit (None for a superuser) and ``registered`` the
    names the web service already counts for it; a name the account holds on the host
    only is counted here, under the creation lock. ``known`` is every project id the
    web service has a record for, counted or not (an archived project is known and no
    longer counted, so its host record must not count it again). ``initialize`` is the
    work (``admin.initialize_project`` by default; tests pass their own).
    """
    import admin
    root = Path(root)
    admin.validate_name(name)
    initialize = initialize or admin.initialize_project
    with creation_lock(root):
        try:
            admin.refuse_retired_name(root, name)
        except ValueError:
            raise NothingMade(NOT_AVAILABLE % name) from None
        record = read_record(root, name)
        state = made(root, name)
        if record is not None and record['state'] != 'removed':
            if record['by'] != account:
                raise NothingMade(NOT_AVAILABLE % name)
            effective = effective_state(root, record)
            if effective == 'created' and state == 'initialized':
                # Finished on the host (by an earlier request, or by an operator after a
                # stop): the web service only has to write its own record.
                return {'status': 'created', 'project': name, 'adopted': True,
                        'backup': _coverage(root, name)}
            if effective == 'incomplete':
                return {'status': 'incomplete', 'project': name, 'stage': record.get('stage'),
                        'message': incomplete_message(name)}
            # ``started`` with nothing on disk: resume, as add-project resumes an empty directory.
        elif state != 'nothing':
            # A project an operator made, or a directory nobody recorded.
            raise NothingMade(NOT_AVAILABLE % name)
        if limit is not None:
            held = set(holds(root, account, set(registered) | set(known))) - {name}
            used = len(set(registered)) + len(held)
            if used >= limit:
                raise NothingMade('The limit of %d project(s) for this account is reached (%d in use, counting '
                                  'creations that are not finished)' % (limit, used))
        record = {'project': name, 'by': account, 'operation_id': operation_id, 'state': 'started',
                  'stage': None, 'started_at': _stamp()}
        write_record(root, name, record)

        def stage(label):
            record['stage'] = label
            write_record(root, name, record)
        output = io.StringIO()
        try:
            # The work prints what add-project prints; none of it may reach the endpoint's
            # own output, which is one JSON envelope.
            with contextlib.redirect_stdout(output):
                initialize(root, name, stage)
        except BaseException as error:
            if made(root, name) == 'nothing':
                path = admin.project_dir(root, name)
                if path.is_dir():
                    with contextlib.suppress(OSError):
                        path.rmdir()
                delete_record(root, name)
                if isinstance(error, Exception):
                    raise NothingMade('The project could not be created and nothing was made (%s). '
                                      'Try again.' % _bounded(error)) from None
                raise
            record.update(state='incomplete', stopped_at=_stamp(), error=_bounded(error))
            write_record(root, name, record)
            if not isinstance(error, Exception):
                raise
            return {'status': 'incomplete', 'project': name, 'stage': record.get('stage'),
                    'message': incomplete_message(name)}
        record.update(state='created', stage=None, completed_at=_stamp())
        write_record(root, name, record)
        return {'status': 'created', 'project': name, 'adopted': False, 'backup': _coverage(root, name)}


def incomplete_message(name):
    return ('Project %s was started on the server and did not finish. Nothing is registered in the web '
            'interface, and the name is held. An operator must finish it (admin.py finish-project %s) or '
            'remove it (admin.py retire-project %s --actor OPERATOR --reason REASON --force).' % (name, name, name))


def _coverage(root, name):
    import admin
    try:
        covers = admin.scheduled_backup_covers(root, name)
    except OSError:
        covers = None
    return 'covered' if covers else 'unknown' if covers is None else 'not-covered'


def finish(root, name, finish_steps=None):
    """Operator: complete a creation that stopped half way. Returns the record.

    Possible only when the project was initialized (its database and metadata exist);
    the remaining steps are each safe to repeat. A directory that was not initialized
    cannot be finished: remove it instead.
    """
    import admin
    root = Path(root)
    with creation_lock(root):
        record = read_record(root, name)
        if record is None:
            raise ValueError('There is no project creation record for %s' % name)
        effective = effective_state(root, record)
        if effective == 'created':
            return dict(record, effective='created')
        if effective == 'removed':
            raise ValueError('The creation of %s was removed; its name is retired' % name)
        state = made(root, name)
        if state == 'nothing':
            raise ValueError('Nothing was made for %s: the same web request creates it, or run admin.py '
                             'add-project %s' % (name, name))
        if state != 'initialized':
            raise ValueError('projects/%s was not initialized (it has no .beads/metadata.json), so it cannot be '
                             'finished. Remove it: admin.py retire-project %s --actor OPERATOR --reason REASON '
                             '--force' % (name, name))
        (finish_steps or admin.finish_project_steps)(root, name)
        record.update(state='created', stage=None, completed_at=_stamp(), finished_by='operator')
        record.pop('error', None)
        write_record(root, name, record)
        return dict(record, effective='created')


def mark_removed(root, name, actor):
    """Called by retire-project: a half-made creation that was retired holds nothing."""
    record = read_record(root, name)
    if record is None or record['state'] in ('created', 'removed'):
        return None
    record.update(state='removed', removed_at=_stamp(), removed_by=actor)
    write_record(root, name, record)
    return record


def attention(root):
    """The creations an operator has to look at: incomplete or damaged ones."""
    return [{'project': record['project'], 'state': record['effective'], 'by': record.get('by'),
             'stage': record.get('stage'), 'started_at': record.get('started_at'),
             'stopped_at': record.get('stopped_at')}
            for record in records(root) if record['effective'] in ('incomplete', 'damaged')]


# ---------------------------------------------------------------- the endpoint actions
#: Actions that exist only for the web service. Each is refused unless the endpoint was
#: started with the service's authority arguments AND the request carries a
#: live-authority descriptor that passes for exactly the capability named here.
SERVICE_ACTIONS = {'create-project': 'project.host-create', 'project-creations': 'accounts.admin',
                   'set-onboarding': 'project.admin'}
CREATION_JOURNAL = 'operations.sqlite3'


def service_descriptor(request, authority_config, action):
    """The verified shape of a service-only request's descriptor, or raise.

    What stops an SSH caller depends on the installation. Where contributor keys are
    confined to the forced command, the authority arguments never reach the endpoint
    (``ssh_forced_command`` drops them), so the action is refused here. Where a key is
    not confined, its holder has a shell as the service account and can already run
    ``admin.py add-project``; this action gives such a caller nothing new.
    """
    from http_authority import http_actor_id
    if authority_config is None:
        raise ValueError('%s is available only to the web service' % action)
    authority = request.get('authority')
    if not isinstance(authority, dict):
        raise ValueError('%s needs the live-authority descriptor of a signed-in account' % action)
    descriptor = {key: value for key, value in authority.items() if key not in ('store', 'lock')}
    if descriptor.get('capability') != SERVICE_ACTIONS[action] or descriptor.get('via') != 'session':
        raise ValueError('%s needs a session descriptor for %s' % (action, SERVICE_ACTIONS[action]))
    actor = request.get('actor')
    if not isinstance(actor, str) or http_actor_id(actor) != actor or actor != descriptor.get('user_id'):
        raise ValueError('%s acts only under the signed-in account itself' % action)
    return descriptor


def create_action(root, request, authority_config, initialize=None):
    """The ``create-project`` endpoint action: the response envelope.

    The grant and the limit are checked here, against the live authority store and under
    its lock (``run_guarded`` re-runs ``decide`` for the descriptor), not only in the web
    service. The limit is then checked once more under the host's creation lock, where
    the names the account holds on the host only are counted too. The operation identity
    is reserved before the first write and the result is recorded with it.
    """
    from http_authority import created_projects, project_grant, read_state, run_guarded
    descriptor = service_descriptor(request, authority_config, 'create-project')
    name = request.get('project')
    if request.get('args', []) not in ([], None):
        raise ValueError('Use create-project without arguments')
    if not isinstance(request.get('operation_id'), str) or not request['operation_id']:
        raise ValueError('create-project needs an operation id')
    account = descriptor['user_id']

    def effect():
        state = read_state(authority_config.store)
        user = (state.get('users') or {}).get(account) or {}
        grant = project_grant(user)
        limit = None if user.get('superuser') else (grant or {}).get('limit', 0)
        registered = created_projects(state, account)
        try:
            known = [pid for pid in (state.get('projects') or {}) if isinstance(pid, str)]
            result = create(root, name, account, request['operation_id'], limit=limit, registered=registered,
                            initialize=initialize, known=known)
        except NothingMade as refusal:
            # rc 2 releases the operation identity: nothing was made, so the request may be sent again.
            return {'returncode': 2, 'stdout': '', 'stderr': 'ValueError: %s\n' % refusal}
        result['grant'] = {'limit': limit, 'granted_by': (grant or {}).get('granted_by'),
                           'used': len(set(registered) | set(holds(root, account, known)))}
        return {'returncode': 0, 'stdout': json.dumps(result, ensure_ascii=False) + '\n', 'stderr': ''}
    directory = records_dir(root)
    directory.mkdir(mode=0o700, exist_ok=True)
    return run_guarded(request, directory / CREATION_JOURNAL, effect, authority_config=authority_config,
                       require_authority=True)


def list_action(root, request, authority_config):
    """The read-only ``project-creations`` action, for superusers."""
    from http_authority import AuthorityDenied, decide, read_state
    descriptor = service_descriptor(request, authority_config, 'project-creations')
    try:
        decide(read_state(authority_config.store), descriptor)
    except AuthorityDenied as denied:
        return {'returncode': 126, 'stdout': '', 'stderr': '%s\n' % denied.message,
                'authority_status': denied.status}
    return {'returncode': 0, 'stdout': json.dumps({'schema_version': 1, 'items': attention(root)}) + '\n',
            'stderr': ''}
