"""Creating a project on the host for the web service (kittrial-5bb.118 part 2).

An operator creates a project with ``admin.py add-project``. A named web account that a
superuser granted "may create projects" can now cause the same work from the web
interface. This module is the host half of that: it runs the same initialization as
``add-project`` and keeps one small record per creation, so that

* a refusal leaves nothing behind (no directory, no database, no record);
* a creation that stops half way is never guessed at: the record says ``incomplete``,
  the name stays taken, an operator finishes it (``admin.py finish-project``) or
  removes it (``admin.py remove-creation``), and nothing is dropped by the kit;
* the names an account holds are countable on the host, including the half-made ones;
* the server holds at most a set number of project databases (``server_limit``), because
  every database on the server makes every bd write on it slower.

**The three steps of a creation** (review 01a109cc). The web service's authority lock is
held for the first and the last only, so nothing else waits for the initialization:

1. *Reserve*, under the authority lock: the grant and the account's limit are checked
   against the live store, the server limit and the name are checked on the host, and
   the record is written (``started``). From here the name is held and counts toward
   both limits.
2. *Work*, without the authority lock: the same initialization as ``add-project``.
3. *Confirm*, under the authority lock again (``run_guarded``): the authority and the
   server limit are checked once more and the result is recorded with the operation
   identity. If the grant was revoked or the account disabled meanwhile, the project
   stays on the host, ``created`` and not registered, and the request is refused.

One creation runs at a time on a host (the creation lock); a second one is told so at
once. While one runs, every other request is served as usual.

The records live in ``<root>/project-creations/NAME.json`` (0600). A record's states:

``started``     the intent, written before the first write. It READS as ``running``
                while the process that wrote it holds the creation lock. A process
                that was killed leaves this state: with a non-empty project directory
                it reads as ``incomplete``, with nothing made as ``stalled`` (the same
                request resumes it; ``admin.py remove-creation`` clears it).
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
#: What a record reads as (``effective``): the stored states plus the three a ``started`` one can be.
RUNNING, STALLED = 'running', 'stalled'
RUN_NAME = '.running'
#: At most this many project databases on one server unless an operator sets another number.
#: Counted over everything that has, or is about to have, a database: every directory under
#: projects/, every retired one, every creation that holds a name. See docs/OPERATIONS.md,
#: "The cost of many projects on one server".
SERVER_LIMIT_DEFAULT = 20
SERVER_LIMIT_KEY = 'project_database_limit'
SERVER_LIMIT_MAX = 500
#: Names the database server uses itself: a project of that name cannot be initialized. (Its other
#: names, such as information_schema, hold an underscore and are not project names anyway.)
RESERVED_NAMES = frozenset({'mysql', 'sys', 'dolt', 'doltcfg'})
#: The stages of the work, in order; a record names the one that was running.
STAGES = ('init', 'configure', 'requirements-governance', 'backup-target', 'merge-slot', 'first-backup')
ERROR_LIMIT = 300
#: One sentence for a name that cannot be used, whatever the reason (taken by a project,
#: held by another creation, or retired), so the answer does not say which.
NOT_AVAILABLE = 'Project name %s is not available: choose another name'
#: The ``by`` of a creation an operator made with ``admin.py add-project``. No web account
#: made it, so it is never counted toward an account's limit (kittrial-5bb.176).
HOST = 'host'


class NothingMade(ValueError):
    """The creation was refused, or failed, with nothing left behind."""


class Busy(Exception):
    """Another creation is running on this host; the request may be sent again shortly."""


BUSY = 'Another project is being created on this server. Try again in a minute.'
AT_SERVER_LIMIT = ('This server is at its limit of projects, so no new one can be created. Ask an operator of the '
                   'server to raise the limit or to make room.')


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
    if not os.path.lexists(path):
        return None
    if not path.is_file():
        # A directory, a device, anything that is not a file (kittrial-5bb.149): damaged, like a
        # file that cannot be parsed. It was read as "no record", and nothing listed it.
        raise ValueError('The project creation record for %s is not a file; an operator must look at %s'
                         % (name, path))
    try:
        text = path.read_text(encoding='utf-8')
    except OSError as error:
        # A file the service account cannot open (owned by another user after a restore, say) is
        # a damaged record too, not a failure of whatever was being done (kittrial-5bb.149).
        raise ValueError('The project creation record for %s cannot be opened (%s); an operator must look '
                         'at %s' % (name, type(error).__name__, path)) from None
    record = json.loads(text)
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


def running_name(root):
    """The project a creation is making right now, or None.

    Only while a process holds the creation lock; the name it wrote beside the lock
    says which record is its own. A stale name with no holder means nothing runs.
    """
    from http_authority import file_lock
    directory = records_dir(root)
    marker = directory / RUN_NAME
    if not marker.is_file() or marker.is_symlink():
        return None
    try:
        with file_lock(directory / LOCK_NAME, timeout=0):
            return None
    except TimeoutError:
        pass
    try:
        name = marker.read_text(encoding='utf-8').strip()
    except OSError:
        return None
    return name or None


def effective_state(root, record, running=None):
    """A record's state as an operator should read it.

    A ``started`` record is ``running`` while its own process holds the creation lock;
    otherwise ``incomplete`` when something was made, ``stalled`` when nothing was.
    """
    if record['state'] != 'started':
        return record['state']
    if running is not None and running == record['project']:
        return RUNNING
    return 'incomplete' if made(root, record['project']) != 'nothing' else STALLED


def records(root):
    """Every creation record, sorted by name, each with its ``effective`` state."""
    directory = records_dir(root)
    if not directory.is_dir():
        return []
    found = []
    running = running_name(root)
    import admin
    for path in sorted(directory.glob('*.json')):
        try:
            admin.validate_name(path.stem)
        except ValueError:
            # No project can have this name, so this is no project's creation record: it holds
            # no name and no place, and is listed only so that somebody moves it away.
            found.append({'project': path.stem, 'state': NOT_A_RECORD, 'effective': NOT_A_RECORD, 'by': None,
                          'file': path.name})
            continue
        try:
            record = read_record(root, path.stem)
        except ValueError:
            # Not JSON, the wrong shape, a symlink, a directory, a file that cannot be opened.
            found.append({'project': path.stem, 'state': 'damaged', 'effective': 'damaged', 'by': None})
            continue
        if record is not None:
            found.append(dict(record, effective=effective_state(root, record, running)))
    return found


def holds(root, account, registered=()):
    """The names ``account`` holds on the host that the web service has not registered.

    These count toward the account's limit beside its registered projects: a creation
    that is under way, one that stopped half way, and one that finished on the host
    whose registration has not been written yet. A removed one holds nothing.
    """
    registered = set(registered)
    return sorted(record['project'] for record in records(root)
                  if record.get('by') == account and record['effective'] in HOLDING
                  and record['project'] not in registered)


#: A file in the records directory whose name no project can have.
NOT_A_RECORD = 'not-a-record'
#: The readings of a record that hold its name and a place.
HOLDING = (RUNNING, STALLED, 'incomplete', 'created')


def server_limit(root):
    """The most project databases this server may hold (a deployment setting; default 20)."""
    import admin
    root = Path(root)
    marker = root / 'deployment.private.json'
    value = admin.deployment_document(marker).get(SERVER_LIMIT_KEY) if marker.is_file() else None
    if value is None:
        return SERVER_LIMIT_DEFAULT
    if type(value) is not int or not 1 <= value <= SERVER_LIMIT_MAX:
        raise ValueError('deployment %s must be a whole number from 1 to %d' % (SERVER_LIMIT_KEY, SERVER_LIMIT_MAX))
    return value


def set_server_limit(root, value, actor):
    """Operator: set the limit. The setting and its audit record are one private generation."""
    import admin
    from keyed_records import require_configured_operator
    root = Path(root)
    require_configured_operator(actor, admin.operators(root, strict=True), 'set the limit of project databases')
    if type(value) is not int or not 1 <= value <= SERVER_LIMIT_MAX:
        raise ValueError('The limit must be a whole number from 1 to %d' % SERVER_LIMIT_MAX)
    marker = Path(root) / 'deployment.private.json'
    if not marker.is_file():
        raise ValueError('Deployment is not installed; run install first')
    # Under the one lock every writer of this file takes (kittrial-5bb.136), and read again
    # under it, so a switch flip or an allowlist change made at the same instant is not lost,
    # and neither is this one. A holder that keeps the lock past its bound refuses this change.
    with admin.deployment_config_lock(root):
        cfg = admin.config(root)
        audit = cfg.get(SERVER_LIMIT_KEY + '_audit', [])
        if not isinstance(audit, list) or any(not isinstance(item, dict) for item in audit):
            raise ValueError('Invalid audit of the project database limit; reconcile before changing it')
        previous = server_limit(root)
        cfg[SERVER_LIMIT_KEY] = value
        cfg[SERVER_LIMIT_KEY + '_audit'] = (audit + [{'actor': actor, 'at': _stamp(), 'from': previous,
                                                      'to': value}])[-50:]
        admin.atomic_private_write(marker, json.dumps(cfg))
    return server_usage(root)


def server_names(root):
    """Every name that has, or is about to have, a database on this server.

    The directories under projects/ whatever their state, the retired ones (their
    databases are never dropped), and the creations that hold a name before anything
    is made. An archived project is one of the first: archiving is the web service's
    record and changes nothing on the host.
    """
    import admin
    root = Path(root)
    names = set()
    projects = root / 'projects'
    if projects.is_dir():
        names |= {path.name for path in projects.iterdir() if path.is_dir() and not path.is_symlink()}
    names |= {name for name, _ in admin.retired_entries(root)}
    # A record that cannot be read may stand for a database: it holds a place until an operator
    # has set it aside (kittrial-5bb.143).
    names |= {record['project'] for record in records(root) if record['effective'] in HOLDING + ('damaged',)}
    return names


def server_usage(root):
    """``{'used': n, 'limit': m}`` for the operator and the superuser panel."""
    return {'used': len(server_names(root)), 'limit': server_limit(root)}




@contextlib.contextmanager
def creation_lock(root, wait=600.0, name=None):
    """One creation at a time on a host: the count and the name check are then exact.

    ``wait=0`` is for a creation: it does not queue behind another one, it raises
    :class:`Busy`. An operator command waits. ``name`` is written beside the lock for the
    time it is held, so a reader can tell which record is the one being made.
    """
    from http_authority import file_lock
    directory = records_dir(root)
    directory.mkdir(mode=0o700, exist_ok=True)
    try:
        with file_lock(directory / LOCK_NAME, timeout=wait):
            marker = directory / RUN_NAME
            try:
                if name is not None:
                    import admin
                    admin.atomic_private_write(marker, name + '\n')
                yield
            finally:
                if name is not None:
                    with contextlib.suppress(OSError):
                        marker.unlink()
    except TimeoutError:
        if wait:
            raise ValueError('Timed out waiting for the project creation lock; a creation or an operator command '
                             'is still running') from None
        raise Busy(BUSY) from None


def _bounded(error):
    text = '%s: %s' % (type(error).__name__, error)
    return text if len(text) <= ERROR_LIMIT else text[:ERROR_LIMIT] + '...'


def reserve(root, name, account, operation_id, limit=None, registered=(), known=()):
    """Step 1, under the creation lock: decide and, if the work is to run, write the record.

    Returns a result (``created`` adopted, or ``incomplete``) when there is no work to
    do, else None after writing the ``started`` record. Raises :class:`NothingMade` for a
    refusal. From the moment the record is written the name is held and counts toward
    the account's limit and the server's.
    """
    import admin
    admin.validate_name(name)
    if name in RESERVED_NAMES:
        raise NothingMade('Project name %s is used by the database server itself: choose another name' % name)
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
            return {'status': 'created', 'project': name, 'adopted': True, 'backup': _coverage(root, name)}
        if effective == 'incomplete':
            return {'status': 'incomplete', 'project': name, 'stage': record.get('stage'),
                    'message': incomplete_message(name)}
        # ``stalled`` (started, nothing on disk): resume, as add-project resumes an empty directory.
    elif state != 'nothing':
        # A project an operator made, or a directory nobody recorded.
        raise NothingMade(NOT_AVAILABLE % name)
    if limit is not None:
        held = set(holds(root, account, set(registered) | set(known))) - {name}
        used = len(set(registered)) + len(held)
        if used >= limit:
            raise NothingMade('The limit of %d project(s) for this account is reached (%d in use, counting '
                              'creations that are not finished)' % (limit, used))
    if len(server_names(root) - {name}) >= server_limit(root):
        # The answer does not say how many projects other people have. A creation that is resumed
        # holds its own place already, so only the others are counted for it too; it is refused
        # only when they alone fill the server (an operator lowered the limit or added projects).
        raise NothingMade(AT_SERVER_LIMIT)
    # A retry preserves whether the original creation opted into the default.
    # Old, already-started creations must not acquire a new mode at release time.
    updated = {'project': name, 'by': account, 'operation_id': operation_id, 'state': 'started',
               'stage': None, 'started_at': record['started_at'] if record else _stamp()}
    if record is None or record.get('state') == 'removed':
        updated['requirements_governance'] = 'simple'
    elif 'requirements_governance' in record:
        updated['requirements_governance'] = record['requirements_governance']
    write_record(root, name, updated)
    return None


def work(root, name, initialize=None):
    """Step 2, under the creation lock and nothing else: the initialization. Returns the result.

    Raises :class:`NothingMade` when it failed with nothing left behind (the record is
    deleted and the request may be sent again). A stop after something was made leaves
    the record ``incomplete``.
    """
    import admin
    initialize = initialize or admin.initialize_project
    record = read_record(root, name)

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
        if not _record_is_ours(root, name, record):
            # Not this run's record any more: a late or interrupted run must not overwrite or
            # delete what another run wrote (kittrial-5bb.176 item 2).
            raise
        if made(root, name) == 'nothing':
            path = admin.project_dir(root, name)
            if path.is_dir():
                with contextlib.suppress(OSError):
                    path.rmdir()
            delete_record(root, name)
            if isinstance(error, Exception):
                note_failure(root, name, record.get('by'), error)
                try:
                    missing = admin.missing_bd_init_tools(root)
                except Exception:                                    # noqa: BLE001
                    missing = []
                if missing:
                    # The program's name is not host-private detail, and naming it is what lets the
                    # person who asked tell an operator what to look at, instead of the reason
                    # living only in project-creations/last-failure.txt (kittrial-5bb.176 item 3).
                    raise NothingMade(missing_tools_message(missing)) from None
                raise NothingMade(COULD_NOT) from None
            raise
        # The detail (which may name host paths and commands) stays in the record, for the operator.
        record.update(state='incomplete', stopped_at=_stamp(), error=_bounded(error))
        write_record(root, name, record)
        if not isinstance(error, Exception):
            raise
        return {'status': 'incomplete', 'project': name, 'stage': record.get('stage'),
                'message': incomplete_message(name)}
    record.update(state='created', stage=None, completed_at=_stamp())
    write_record(root, name, record)
    return {'status': 'created', 'project': name, 'adopted': False, 'backup': _coverage(root, name)}


def create(root, name, account, operation_id, limit=None, registered=(), initialize=None, known=(), wait=0):
    """Reserve and work in one call, for a caller that holds no other lock (and for tests).

    ``{'status': 'created', ...}`` or ``{'status': 'incomplete', ...}``. Raises
    :class:`NothingMade` when the request is refused or fails with nothing left behind,
    and :class:`Busy` when another creation is running. ``limit`` is the account's limit
    (None for a superuser) and ``registered`` the names the web service already counts
    for it; ``known`` is every project id the web service has a record for, counted or
    not (an archived project is known and no longer counted, so its host record must not
    count it again). ``initialize`` is the work (``admin.initialize_project`` by default).
    """
    root = Path(root)
    with creation_lock(root, wait=wait, name=name):
        result = reserve(root, name, account, operation_id, limit=limit, registered=registered, known=known)
        if result is not None:
            return result
        return work(root, name, initialize)


def incomplete_message(name):
    return ('Project %s was started on the server and did not finish. Nothing is registered in the web '
            'interface, and the name is held. An operator must finish it (admin.py finish-project %s) or '
            'remove it (admin.py remove-creation %s --actor OPERATOR --reason REASON).' % (name, name, name))


def _record_is_ours(root, name, record):
    """Whether the record on disk is still the one this run last wrote.

    A run that failed or was interrupted must not overwrite or delete a record another run
    wrote in the meantime (kittrial-5bb.176 item 2). The creation lock makes that all but
    impossible now; this check makes it impossible.
    """
    try:
        current = read_record(root, name)
    except (ValueError, OSError):
        return False
    return current == record


def host_busy_message(root, name):
    """What ``add-project`` is told when another creation holds the creation lock.

    The same name: the sentence ``admin.add_project`` has always used for a name it cannot
    use, so it appends :func:`unfinished_sentence` and the operator is told the creation is
    RUNNING. Another name: name the project that is being created (kittrial-5bb.176 item 1).
    """
    running = running_name(root)
    if running == name:
        return 'Project already exists; use it rather than initializing again'
    if running:
        return ('Another project is being created on this server right now (%s); wait for it to finish, then run '
                'this command again.' % running)
    return BUSY


def host_create(root, name, initialize, guards, requirements_default=True):
    """``admin.py add-project``'s path: the operator's route keeps the web route's record.

    ``guards`` refuses a name that cannot be used before anything is written, so a refusal
    still leaves no directory, no database and no record. A record already there in state
    ``started`` - an earlier ``add-project`` that stopped, or a web request that stalled -
    is reused rather than overwritten, so the run that finishes is the record that was
    begun, with the account that began it.

    It holds the SAME per-host creation lock the web route holds (``create``) for its whole
    run, and writes the same ``.running`` marker, so one creation runs at a time on the host
    whichever route started it (kittrial-5bb.176 items 1 and 2). A second creation is
    refused and told the name is RUNNING; ``remove-creation`` and ``retire-project`` refuse
    to touch the directory of a creation in flight; and the record this run writes is the
    one it owns, so a failure never downgrades or deletes a record another run wrote.

    A failure that made nothing deletes the record this call wrote, so a refusal still
    leaves nothing behind. A failure after something was made leaves the record
    ``incomplete``, which ``finish-project`` and ``remove-creation`` act on
    (kittrial-5bb.176). The directory is never removed here: ``admin.initialize_project``
    explains why (kittrial-5bb.162: a failure must not delete another creation's work).

    Returns the finished record. Raises what ``guards`` or the work raised, with the record
    left to match what is on the host.
    """
    root = Path(root)
    try:
        with creation_lock(root, wait=0, name=name):
            return _host_create_locked(root, name, initialize, guards, requirements_default)
    except Busy:
        raise ValueError(host_busy_message(root, name)) from None


def _host_create_locked(root, name, initialize, guards, requirements_default=True):
    guards(root, name)
    record = read_record(root, name)
    if record is None or record['state'] != 'started':
        record = {'project': name, 'by': HOST, 'state': 'started', 'stage': None, 'started_at': _stamp()}
        if requirements_default:
            record['requirements_governance'] = 'simple'

    def stage(label):
        record['stage'] = label
        write_record(root, name, record)

    record['stage'] = None
    write_record(root, name, record)
    try:
        initialize(root, name, stage)
    except BaseException as error:
        if not _record_is_ours(root, name, record):
            # The record on disk is not the one this run wrote: another run owns it now, and
            # a late or interrupted run must never overwrite or delete it.
            raise
        if made(root, name) == 'nothing':
            delete_record(root, name)
        else:
            record.update(state='incomplete', stopped_at=_stamp(), error=_bounded(error))
            write_record(root, name, record)
        raise
    record.update(state='created', stage=None, completed_at=_stamp())
    write_record(root, name, record)
    return record


def unfinished_sentence(root, name):
    """What ``add-project`` adds when the name is held by a creation that did not finish.

    ``''`` when there is no readable unfinished record, or when the creation finished or
    was removed. ``add-project`` appends this to "Project already exists" so a re-run names
    the commands that act on the leftover instead of being a dead end (kittrial-5bb.176).
    Never raises: a record it cannot read is not its business.
    """
    try:
        record = read_record(root, name)
        running = running_name(root)
        if record is None:
            # The marker written beside the lock names the creation in flight, and it is
            # written before the record: a creation that has just taken the lock holds its
            # name (kittrial-5bb.176 item 1).
            effective = RUNNING if running == name else None
        else:
            effective = effective_state(root, record, running)
    except (ValueError, OSError):
        return ''
    if effective in (None, 'created', 'removed', 'damaged', NOT_A_RECORD):
        return ''
    if effective == RUNNING:
        return ('A project creation for %s is running on this server right now; wait for it to finish and run '
                'this command again.' % name)
    return incomplete_message(name)


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
            return dict(record, effective='created', already=True)
        if effective == 'removed':
            raise ValueError('The creation of %s was removed' % name)
        state = made(root, name)
        if state == 'nothing':
            if record.get('by') == HOST:
                raise ValueError('Nothing was made for %s: run the command again (admin.py add-project %s), or '
                                 'remove the record: admin.py remove-creation %s --actor OPERATOR --reason REASON'
                                 % (name, name, name))
            raise ValueError('Nothing was made for %s: the same web request creates it, or remove the record: '
                             'admin.py remove-creation %s --actor OPERATOR --reason REASON' % (name, name))
        if state != 'initialized':
            raise ValueError('projects/%s was not initialized (it has no .beads/metadata.json), so it cannot be '
                             'finished, and its name cannot be used again: a database of that name may already '
                             'be on the server. Remove it: admin.py remove-creation %s --actor OPERATOR --reason '
                             'REASON' % (name, name))
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


def remove(root, name, actor, reason, retire=None):
    """Operator: remove a creation that did not finish. Returns what was done.

    Only for a creation whose record reads ``incomplete`` or ``stalled``. Refused for a
    name with no creation record, for a creation that finished (``created``: that is a
    project; ``retire-project`` is the command for a project) and for one that is
    running. Holds the creation lock, so no creation can start under it.

    * ``stalled`` (nothing was made): the record is deleted. The name is free again,
      because no directory and no database exist for it.
    * ``incomplete`` (something was made): the directory is retired, exactly as
      ``retire-project --force`` does it, and the record is marked removed. The name
      stays retired, because a database of that name may be on the server.
    """
    import admin
    from keyed_records import require_configured_operator
    root = Path(root)
    require_configured_operator(actor, admin.operators(root, strict=True), 'remove a project creation')
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError('A reason is required')
    try:
        context = creation_lock(root, wait=0)
        context.__enter__()
    except Busy:
        raise ValueError('Refusing to remove the creation of %s: a creation is running on this server (%s). '
                         'Wait for it to finish. Nothing was changed.' % (name, running_name(root) or 'unknown')) from None
    try:
        try:
            record = read_record(root, name)
        except ValueError:
            # A record that cannot be read (kittrial-5bb.143): it is set aside, never deleted, and
            # nothing else is touched, because what it stood for is not known.
            kept = keep_damaged_record(root, name)
            return {'project': name, 'removed': 'damaged-record', 'kept_as': kept.name, 'by': None,
                    'name': 'free' if made(root, name) == 'nothing' else 'a project with no creation record'}
        if record is None:
            raise ValueError('There is no project creation record for %s, so there is nothing to remove here. '
                             'For a project, see admin.py retire-project. Nothing was changed.' % name)
        effective = effective_state(root, record)
        if effective == 'created':
            raise ValueError('The creation of %s finished: it is a project, not an unfinished creation. '
                             'Nothing was changed.' % name)
        if effective == 'removed':
            raise ValueError('The creation of %s was already removed. Nothing was changed.' % name)
        if effective == STALLED:
            path = admin.project_dir(root, name)
            if path.is_dir():
                with contextlib.suppress(OSError):
                    path.rmdir()
            delete_record(root, name)
            return {'project': name, 'removed': 'record', 'name': 'free', 'by': record.get('by')}
        (retire or admin.retire_project)(root, name, actor, reason, force=True, creation_locked=True)
        record = read_record(root, name) or record
        if record['state'] != 'removed':
            record.update(state='removed', removed_at=_stamp(), removed_by=actor)
            write_record(root, name, record)
        return {'project': name, 'removed': 'directory', 'name': 'retired', 'by': record.get('by')}
    finally:
        context.__exit__(None, None, None)


def keep_damaged_record(root, name):
    """Rename a creation record that cannot be read to ``NAME.json.damaged-<UTC stamp>`` beside it.

    The kit's way with a damaged file (as for the review-writes audit): the bytes are kept
    for whoever has to find out what happened, under a name no reader takes for a record.
    """
    path = record_path(root, name)
    stamp = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())
    aside, number = path.with_name('%s.damaged-%s' % (path.name, stamp)), 2
    while aside.exists() or aside.is_symlink():
        aside = path.with_name('%s.damaged-%s.%d' % (path.name, stamp, number))
        number += 1
    os.rename(path, aside)
    return aside


#: What the two lists say an operator can do about each reading.
COMMANDS = {
    'incomplete': 'finish it (admin.py finish-project NAME) or remove it (admin.py remove-creation NAME --actor '
                  'OPERATOR --reason REASON)',
    STALLED: 'the same web request resumes it; or remove the record (admin.py remove-creation NAME --actor OPERATOR '
             '--reason REASON), which frees the name',
    RUNNING: 'nothing: it is being created now',
    'damaged': 'the record cannot be read, so the name is held: no web request can create or register a project '
               'called NAME. Look at the file; then '
               'set it aside (admin.py remove-creation NAME --actor OPERATOR --reason REASON): it is kept beside the '
               'records as NAME.json.damaged-STAMP and nothing else is touched. What is under projects/NAME, if '
               'anything, is then a project with no creation record: register it, or retire it (admin.py '
               'retire-project NAME)',
    NOT_A_RECORD: 'this file is not a creation record, because no project can have that name. It holds no name and '
                  'is not counted. Move project-creations/FILE out of that directory',
}
#: For a creation the operator's own ``add-project`` began (``by`` is :data:`HOST`) the same
#: command resumes it, not a web request (kittrial-5bb.176 item 4).
HOST_COMMANDS = {
    STALLED: 'run this command again (admin.py add-project NAME), which resumes it; or remove the record '
             '(admin.py remove-creation NAME --actor OPERATOR --reason REASON), which frees the name',
}
#: Put before the command of a damaged record whose project is initialized, and so is served.
SERVED_DAMAGED = ('projects/NAME is initialized and is SERVED WITH A DAMAGED CREATION RECORD: the kit cannot tell '
                  'whether its creation finished. The project itself is used as before; only its name is held. ')


def attention(root):
    """The creations an operator may have to look at: running, incomplete, stalled or damaged ones.

    A running one is listed as running, never as incomplete (review 01a109cc): the
    remove command run on one in flight pulled its directory away.
    """
    found = []
    for record in records(root):
        if record['effective'] not in COMMANDS:
            continue
        command = COMMANDS[record['effective']]
        if record.get('by') == HOST:
            command = HOST_COMMANDS.get(record['effective'], command)
        served = record['effective'] == 'damaged' and _initialized(root, record['project'])
        if served:
            command = SERVED_DAMAGED + command
        found.append({'project': record['project'], 'state': record['effective'], 'by': record.get('by'),
                      'stage': record.get('stage'), 'started_at': record.get('started_at'),
                      'stopped_at': record.get('stopped_at'), 'served': served,
                      'command': command.replace('NAME', record['project']).replace('FILE', record.get('file') or '')})
    return found


def _initialized(root, name):
    try:
        return made(root, name) == 'initialized'
    except (OSError, ValueError):
        return False


def unregistered(root):
    """The creations that finished on the host: the web service says which are not registered."""
    return [{'project': record['project'], 'state': 'created', 'by': record.get('by'),
             'completed_at': record.get('completed_at')}
            for record in records(root) if record['effective'] == 'created']


def unfinished(root, name):
    """Why the endpoint serves nothing from ``name``, or None: its creation is running, stopped or stalled.

    A registered project does not depend on its creation record (kittrial-5bb.149): a
    record that is absent, finished or DAMAGED does not stop the endpoint, exactly as a
    project an operator made, which has no record, is served. The caller has already
    required the project to be initialized, so a creation that died inside ``bd init``
    is refused whatever became of its record. The cost: an unfinished, unregistered
    creation whose record is then damaged is reachable without the web service, as an
    operator-made project is. A damaged record still holds the name, counts toward the
    server limit and blocks registration (:func:`registrable`).
    """
    import admin
    try:
        admin.validate_name(name)
        record = read_record(root, name)
    except ValueError:
        return None
    if record is None or record['state'] == 'removed':
        return None
    effective = effective_state(root, record, running_name(root))
    if effective == 'created':
        return None
    return ('Project %s is a creation that has not finished (%s). It cannot be registered until an operator '
            'finishes it (admin.py finish-project %s).' % (name, effective, name))


def registrable(root, name):
    """Whether the plain register route may take ``name``: no creation record, or one that finished.

    A name whose creation is running, stopped half way or stalled is not a project yet:
    registering it would put a project that has no backup target, merge slot or first
    backup in front of people (review 01a109cc). Returns the sentence of the refusal, or
    None.
    """
    import admin
    try:
        admin.validate_name(name)
    except ValueError:
        return None                    # not a name a creation can have, so there is no record to ask
    try:
        record = read_record(root, name)
    except ValueError:
        return 'The creation record of project %s is damaged; an operator must look at it first' % name
    if record is None or record['state'] == 'removed':
        return None
    effective = effective_state(root, record, running_name(root))
    if effective == 'created':
        return None
    return ('Project %s is a creation that has not finished (%s). It cannot be registered until an operator '
            'finishes it (admin.py finish-project %s).' % (name, effective, name))


# ---------------------------------------------------------------- the endpoint actions
#: Actions that exist only for the web service. Each is refused unless the endpoint was
#: started with the service's authority arguments AND the request carries a
#: live-authority descriptor that passes for exactly the capability named here.
SERVICE_ACTIONS = {'create-project': 'project.host-create', 'project-creations': 'accounts.admin',
                   'set-onboarding': 'project.admin', 'owner-requirements': 'project.admin',
                   'creation-standing': 'project.host-create'}
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


BUSY_RETURNCODE = 75          # EX_TEMPFAIL: nothing was done; send the request again shortly


def account_limit(state, account):
    """``(grant, limit)`` of an account in the live state; ``limit`` is None for a superuser only.

    An account with no grant has a limit of nothing, never no limit: the authority check
    refuses it before this is asked, and this does not rely on that.
    """
    from http_authority import project_grant
    user = (state.get('users') or {}).get(account) or {}
    grant = project_grant(user)
    if user.get('superuser') is True:
        return grant, None
    return grant, grant['limit'] if grant else 0


def _refused(sentence):
    # rc 2 releases the operation identity: nothing is registered, so the request may be sent again.
    return {'returncode': 2, 'stdout': '', 'stderr': 'ValueError: %s\n' % sentence}


FAILURE_FILE = 'last-failure.txt'
FAILURES_KEPT = 5


def note_failure(root, name, account, error, step=None):
    """Keep why a creation failed, for the operator: the newest, and the few before it (0600).

    The detail may name host paths and exceptions, which is why it stays here and is not
    in the answer to the person who asked. Never raises: a host that cannot write this
    file must still answer the request.
    """
    import admin
    with contextlib.suppress(Exception):
        directory = records_dir(root)
        directory.mkdir(mode=0o700, exist_ok=True)
        path = directory / FAILURE_FILE
        earlier = []
        with contextlib.suppress(Exception):
            before = json.loads(path.read_text(encoding='utf-8'))
            if isinstance(before, dict):
                kept = before.pop('earlier', [])
                earlier = [before] + [item for item in (kept if isinstance(kept, list) else []) if isinstance(item, dict)]
        entry = {'project': name if isinstance(name, str) else None, 'by': account, 'at': _stamp(),
                 'error': _bounded(error), 'earlier': earlier[:FAILURES_KEPT - 1]}
        if step:
            entry['step'] = step
        admin.atomic_private_write(path, json.dumps(entry, sort_keys=True) + '\n')


COULD_NOT = ('The project could not be created and nothing was made. Try again; if it fails again, ask an '
             'operator of the server.')
#: One sentence for a program ``bd init`` needs and this host has not got. The program's name is not
#: host-private detail, and the person who asked can pass it to an operator (kittrial-5bb.176 item 3).
MISSING_TOOLS = ('The server has no %s, which bd init needs to create a project; nothing was made. Ask an operator '
                 'of the server to install it and try again.')


def missing_tools_message(missing):
    """One sentence naming the programs a creation needs and this host has not got."""
    return MISSING_TOOLS % ' and '.join(missing)

MADE_NOT_REGISTERED = ('Project %s was made on the server, but it could not be registered in the web interface. Ask '
                       'an operator of the server to look at it. When that is repaired, create it again with the '
                       'same name: nothing is made twice, it is only registered. Do not create it under another name.')
MADE_WAITING = ('Project %s was made on the server; registering it had to wait for a lock. Send the same request '
                'again in a moment: nothing is made twice.')
NAME_RULE = 'Project: 2-24 lowercase letters/digits, beginning with a letter'
_NAME = r'[a-z][a-z0-9]{1,23}'


def _sentence_patterns():
    import re
    escaped = lambda text: re.escape(text).replace('%s', _NAME).replace('%d', r'[0-9]{1,6}')   # noqa: E731
    incomplete = re.escape(incomplete_message('NAME')).replace('NAME', _NAME)
    texts = [escaped(NOT_AVAILABLE), escaped('Project name %s is used by the database server itself: choose another name'),
             escaped('The limit of %d project(s) for this account is reached (%d in use, counting creations that '
                     'are not finished)'),
             escaped(AT_SERVER_LIMIT), escaped(AT_SERVER_LIMIT + ' Project %s was made on the server and is not registered.'),
             escaped(COULD_NOT), escaped(MADE_NOT_REGISTERED), escaped(NAME_RULE), incomplete]
    patterns = [re.compile(text) for text in texts]
    # The missing-tools sentence (item 3) names one or more programs joined by ' and '. Each is a
    # plain program name, never a path, so the recognised sentence stays free of host detail.
    tool = r'[a-z][a-z0-9+-]{0,23}'
    patterns.append(re.compile(re.escape(MISSING_TOOLS).replace(
        '%s', '(?:%s)(?: and (?:%s))*' % (tool, tool))))
    return patterns


def creation_sentence(line):
    """``line`` if it is, whole, one of the sentences a creation answers a person with; else None.

    The web service puts the endpoint's line into its answer only when this recognises
    it (kittrial-5bb.143). Anything else (an exception's text, a path on the host) is
    for the operator, whichever kit the endpoint is.
    """
    if not isinstance(line, str):
        return None
    for prefix in ('ValueError: ', 'RuntimeError: '):
        if line.startswith(prefix):
            line = line[len(prefix):]
    return line if any(pattern.fullmatch(line) for pattern in _sentence_patterns()) else None


def busy_sentence(line):
    """``line`` if it is, whole, one of the two sentences a creation answers busy with; else None.

    Anything else on return code 75 is the endpoint's own line for a lock wait that ran
    out, which names the lock file (kittrial-5bb.149).
    """
    import re
    if not isinstance(line, str):
        return None
    made = re.escape(MADE_WAITING).replace('%s', _NAME)
    return line if line == BUSY or re.fullmatch(made, line) else None


def _host_failure(root, name, account, error, progress):
    """The sentence for a creation that failed in a way the kit did not foresee.

    Chosen by what is on the host now, never from the error's text: a damaged record, a
    directory that cannot be written and a journal that is not a database all name host
    paths and exceptions (kittrial-5bb.143). The detail goes to :func:`note_failure`.
    """
    note_failure(root, name, account, error, step=progress.get('step'))
    result = progress.get('result')
    if isinstance(result, dict) and result.get('status') == 'created':
        return MADE_NOT_REGISTERED % name
    if progress.get('step') == 'reserve':
        # Nothing was made by this request. A record of this name that cannot be read holds
        # the name until an operator has looked at it: the same sentence as any taken name.
        try:
            read_record(root, name)
        except Exception:                                        # noqa: BLE001
            return NOT_AVAILABLE % name
        return COULD_NOT
    try:
        state = made(root, name)
    except Exception:                                            # noqa: BLE001
        state = 'unknown'
    return COULD_NOT if state == 'nothing' else incomplete_message(name)


def create_action(root, request, authority_config, initialize=None):
    """The ``create-project`` endpoint action: the response envelope.

    The three steps of the module text. The authority lock is held while the request is
    reserved and again while it is confirmed, never while the project is initialized, so
    no other request waits for a creation (review 01a109cc). The creation lock is held
    for all three, without waiting: a second creation is answered busy at once.

    Nothing but the kit's own creation sentences leaves this action as a refusal. A
    failure it did not foresee is answered by :func:`_host_failure` and noted for the
    operator; a wait for a lock that runs out still reads as busy.
    """
    import admin
    descriptor = service_descriptor(request, authority_config, 'create-project')
    name = request.get('project')
    if request.get('args', []) not in ([], None):
        raise ValueError('Use create-project without arguments')
    if not isinstance(request.get('operation_id'), str) or not request['operation_id']:
        raise ValueError('create-project needs an operation id')
    if not isinstance(name, str):
        raise ValueError(NAME_RULE)
    admin.validate_name(name)
    account = descriptor['user_id']
    root = Path(root)
    # A creation starts bd and reads the server's limit: it needs the server's configuration.
    # A file that cannot be read is the server's fault and is answered as that on every route
    # (the endpoint marks it), not as "could not be created; try again" (kittrial-5bb.156).
    admin.deployment_document(root / 'deployment.private.json')
    admin.deployment_password(root)
    progress = {'step': 'reserve', 'result': None}
    try:
        return _create_steps(root, request, authority_config, initialize, descriptor, name, account, progress)
    except TimeoutError:
        made_already = progress.get('result')
        if progress.get('step') == 'confirm' and isinstance(made_already, dict) and made_already.get('status') == 'created':
            # The wait ran out in the last step: the project IS made, so the endpoint's own
            # busy sentence ("Nothing was done") would not be true here.
            return {'returncode': BUSY_RETURNCODE, 'stdout': '', 'stderr': '%s\n' % (MADE_WAITING % name)}
        raise                                  # nothing was made: the endpoint answers busy
    except Exception as error:                 # noqa: BLE001
        return _refused(_host_failure(root, name, account, error, progress))


def _create_steps(root, request, authority_config, initialize, descriptor, name, account, progress):
    from http_authority import (AuthorityDenied, created_projects, decide, file_lock, read_state, run_guarded)

    def standing():
        state = read_state(authority_config.store)
        grant, limit = account_limit(state, account)
        known = [pid for pid in (state.get('projects') or {}) if isinstance(pid, str)]
        return state, grant, limit, created_projects(state, account), known
    directory = records_dir(root)
    directory.mkdir(mode=0o700, exist_ok=True)
    held = creation_lock(root, wait=0, name=name)
    try:
        held.__enter__()
    except Busy as busy:
        return {'returncode': BUSY_RETURNCODE, 'stdout': '', 'stderr': '%s\n' % busy}
    try:
        # 1. Reserve, under the authority lock: a revocation that committed first is seen here.
        with file_lock(authority_config.lock):
            state, grant, limit, registered, known = standing()
            try:
                decide(state, descriptor)
            except AuthorityDenied as denied:
                return {'returncode': 126, 'stdout': '', 'stderr': '%s\n' % denied.message,
                        'authority_status': denied.status}
            try:
                result = reserve(root, name, account, request['operation_id'], limit=limit, registered=registered,
                                 known=known)
            except NothingMade as refusal:
                return _refused(refusal)
        # 2. Work, with the authority lock released: every other request is served meanwhile.
        progress.update(step='work', result=result)
        if result is None:
            try:
                result = work(root, name, initialize)
            except NothingMade as refusal:
                return _refused(refusal)
        progress.update(step='confirm', result=result)

        # 3. Confirm, under the authority lock again (run_guarded re-runs the authority check).
        def effect():
            state, grant, limit, registered, known = standing()
            if result['status'] == 'created' and len(server_names(root)) > server_limit(root):
                # More databases than the limit now (an operator lowered it or added projects while
                # this one was made). It is made and stays on the host, not registered.
                return _refused(AT_SERVER_LIMIT + ' Project %s was made on the server and is not registered.' % name)
            result['grant'] = {'limit': limit, 'granted_by': (grant or {}).get('granted_by'),
                               'used': len(set(registered) | set(holds(root, account, known)))}
            return {'returncode': 0, 'stdout': json.dumps(result, ensure_ascii=False) + '\n', 'stderr': ''}
        return run_guarded(request, directory / CREATION_JOURNAL, effect, authority_config=authority_config,
                           require_authority=True)
    finally:
        held.__exit__(None, None, None)


def standing_action(root, request, authority_config):
    """The read-only ``creation-standing`` action: what the signed-in account holds on the host.

    ``held``: the names it holds that the web service has not registered (a creation that
    is running, stopped or stalled, or finished and not registered yet). ``server_full``:
    whether the server is at its limit, as a yes or no only. The session read uses it so
    that what the page says an account may do is what the endpoint will answer.
    """
    from http_authority import AuthorityDenied, decide, read_state
    descriptor = service_descriptor(request, authority_config, 'creation-standing')
    try:
        state = read_state(authority_config.store)
        decide(state, descriptor)
    except AuthorityDenied as denied:
        return {'returncode': 126, 'stdout': '', 'stderr': '%s\n' % denied.message,
                'authority_status': denied.status}
    known = [pid for pid in (state.get('projects') or {}) if isinstance(pid, str)]
    usage = server_usage(root)
    return {'returncode': 0, 'stdout': json.dumps({'schema_version': 1, 'held': holds(root, descriptor['user_id'], known),
                                                   'server_full': usage['used'] >= usage['limit']}) + '\n', 'stderr': ''}


def list_action(root, request, authority_config):
    """The read-only ``project-creations`` action, for superusers.

    ``items``: the creations that need an operator, or are running. ``created``: the
    ones that finished on the host; the web service says which of them it has not
    registered. ``server``: how many project databases the server holds and its limit.
    """
    from http_authority import AuthorityDenied, decide, read_state
    descriptor = service_descriptor(request, authority_config, 'project-creations')
    try:
        decide(read_state(authority_config.store), descriptor)
    except AuthorityDenied as denied:
        return {'returncode': 126, 'stdout': '', 'stderr': '%s\n' % denied.message,
                'authority_status': denied.status}
    # The limit comes from the deployment's configuration file. When that cannot be read the
    # list still answers, without the numbers: its error names the file (kittrial-5bb.149).
    try:
        server = server_usage(root)
    except (ValueError, OSError) as error:
        note_failure(root, None, descriptor.get('user_id'), error, step='list')
        server = None
    return {'returncode': 0, 'stdout': json.dumps({'schema_version': 1, 'items': attention(root),
                                                   'created': unregistered(root), 'server': server,
                                                   'server_readable': server is not None}) + '\n',
            'stderr': ''}
