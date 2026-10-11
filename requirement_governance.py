"""Project requirements governance, with immutable history and restore binding.

Callers serialize changes with the project coordination lock. Owner authorization
belongs to the service adapter; neither this file nor its absence grants rights.
"""
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from coordination import atomic, identifier
from requirements import content_hash, load_json

FILE = '.requirements-governance.json'
SOURCE_FILE = '.requirements-governance-source.json'
DEFAULT_GOVERNANCE_MODE = 'simple'
MODES = ('simple', 'governed')
FIELDS = {'schema_version', 'project', 'revisions'}
REVISION_FIELDS = {'revision', 'previous_sha256', 'mode', 'actor', 'via', 'at',
                   'operation_id', 'sha256'}
SOURCE_FIELDS = {'schema_version', 'project', 'source_project', 'restored_from',
                 'revision', 'sha256', 'origins'}
HASH = re.compile(r'[0-9a-f]{64}\Z')
PROJECT = re.compile(r'[a-z][a-z0-9]{1,23}\Z')


def project_name(value):
    if not isinstance(value, str) or not PROJECT.fullmatch(value):
        raise ValueError('Invalid requirements governance project')
    return value


def digest(value):
    if not isinstance(value, str) or not HASH.fullmatch(value):
        raise ValueError('Invalid requirements governance hash')
    return value


def validate(record):
    if (not isinstance(record, dict) or set(record) != FIELDS
            or type(record.get('schema_version')) is not int or record['schema_version'] != 1):
        raise ValueError('Invalid requirements governance record')
    project_name(record['project'])
    revisions = record['revisions']
    if not isinstance(revisions, list) or not revisions:
        raise ValueError('Requirements governance needs a revision chain')
    previous = None
    operations = set()
    for number, entry in enumerate(revisions, 1):
        if not isinstance(entry, dict) or set(entry) != REVISION_FIELDS:
            raise ValueError('Invalid requirements governance revision')
        if type(entry['revision']) is not int or entry['revision'] != number:
            raise ValueError('Requirements governance revisions must be consecutive')
        if entry['previous_sha256'] != previous:
            raise ValueError('Broken requirements governance chain')
        if entry['mode'] not in MODES or entry['via'] not in ('session', 'provisioning'):
            raise ValueError('Invalid requirements governance mode or authority')
        if entry['via'] == 'provisioning' and number != 1:
            raise ValueError('Provisioning can initialize requirements governance only')
        if not isinstance(entry['actor'], str) or not entry['actor'].strip():
            raise ValueError('Requirements governance needs an actor')
        if entry['via'] == 'session' and not re.fullmatch(r'usr_[0-9a-f]{16}', entry['actor']):
            raise ValueError('Requirements governance needs a human account')
        try:
            if not isinstance(entry['at'], str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z', entry['at']):
                raise ValueError()
            datetime.strptime(entry['at'], '%Y-%m-%dT%H:%M:%SZ')
        except ValueError:
            raise ValueError('Requirements governance needs a valid UTC timestamp') from None
        identifier(entry['operation_id'])
        if entry['operation_id'] in operations:
            raise ValueError('Requirements governance repeats an operation')
        operations.add(entry['operation_id'])
        digest(entry['sha256'])
        if content_hash(entry) != entry['sha256']:
            raise ValueError('Requirements governance revision hash mismatch')
        previous = entry['sha256']
    return record


def validate_source(binding, governance):
    validate(governance)
    if (not isinstance(binding, dict) or set(binding) not in (SOURCE_FIELDS, SOURCE_FIELDS | {'restored_projects'})
            or type(binding.get('schema_version')) is not int or binding['schema_version'] != 1):
        raise ValueError('Invalid requirements governance restore binding')
    for name in ('project', 'source_project', 'restored_from'):
        project_name(binding[name])
    number = binding['revision']
    if type(number) is not int or not 1 <= number <= len(governance['revisions']):
        raise ValueError('Invalid requirements governance restore revision')
    if binding['source_project'] != governance['project']:
        raise ValueError('Requirements governance restore source mismatch')
    prefix = dict(governance, revisions=governance['revisions'][:number])
    digest(binding['sha256'])
    if binding['sha256'] != content_hash(prefix):
        raise ValueError('Requirements governance restore hash mismatch')
    origins = binding['origins']
    if not isinstance(origins, list) or not origins:
        raise ValueError('Requirements governance restore needs its source ranges')
    previous = 0
    for origin in origins:
        if (not isinstance(origin, dict) or set(origin) != {'through_revision', 'project'}
                or type(origin['through_revision']) is not int
                or not previous < origin['through_revision'] <= number):
            raise ValueError('Invalid requirements governance source range')
        project_name(origin['project'])
        previous = origin['through_revision']
    if previous != number or origins[0]['project'] != governance['project']:
        raise ValueError('Requirements governance source ranges do not cover the restored history')
    projects = binding.get('restored_projects', [binding['restored_from'], binding['project']])
    if (not isinstance(projects, list) or not projects or any(not isinstance(p, str) for p in projects)
            or len(set(projects)) != len(projects)
            or binding['project'] not in projects or binding['restored_from'] not in projects):
        raise ValueError('Invalid requirements governance restored projects')
    for project in projects:
        project_name(project)
    return binding


def validate_files(files, project=None):
    record, binding = files.get(FILE), files.get(SOURCE_FILE)
    if FILE not in files:
        if SOURCE_FILE in files:
            raise ValueError('Requirements governance restore binding has no history')
        return
    validate(record)
    if SOURCE_FILE in files:
        validate_source(binding, record)
    identity = binding['project'] if binding is not None else record['project']
    if project is not None and identity != project:
        raise ValueError('Requirements governance belongs to a different project')


def safe_path(directory, name):
    path = Path(directory) / name
    if Path(directory).is_symlink() or path.is_symlink() or path.with_suffix('.tmp').is_symlink():
        raise ValueError('Requirements governance paths must not be symlinks')
    return path


def snapshot(directory, project):
    files = {}
    for name in (FILE, SOURCE_FILE):
        path = safe_path(directory, name)
        if path.exists():
            try:
                files[name] = load_json(path)
            except (ValueError, OSError):
                raise ValueError('Requirements governance cannot be read; ask the host operator to repair it') from None
    validate_files(files, project)
    return files


def current(directory, project):
    files = snapshot(directory, project)
    if FILE not in files:
        return {'revision': 0, 'sha256': None, 'mode': 'governed'}
    entry = files[FILE]['revisions'][-1]
    return {name: entry[name] for name in ('revision', 'sha256', 'mode')}


def read_state(directory, project, comparison_time=None):
    """Project governance with clock diagnostics, never an authority decision.

    Revisions and hashes order the history. A restored history can legitimately
    be ahead of this host's clock; report it without changing or refusing it.
    """
    from http_authority import JOURNAL_MAX_SKEW_SECONDS
    files = snapshot(directory, project)
    if FILE not in files:
        return {'revision': 0, 'sha256': None, 'mode': 'governed'}
    entries = files[FILE]['revisions']
    result = {name: entries[-1][name] for name in ('revision', 'sha256', 'mode')}
    comparison = time.time() if comparison_time is None else comparison_time
    warnings = []
    count = 0
    for entry in entries:
        timestamp = datetime.strptime(entry['at'], '%Y-%m-%dT%H:%M:%SZ').replace(
            tzinfo=timezone.utc).timestamp()
        if timestamp - comparison > JOURNAL_MAX_SKEW_SECONDS:
            count += 1
            if len(warnings) < 8:
                warnings.append({'revision': entry['revision'],
                                 'message': 'Governance revision %d is ahead of the host clock; '
                                            'ask the host operator to check the clock and history.'
                                            % entry['revision']})
    if warnings:
        result.update(warnings=warnings, warnings_truncated=count > len(warnings))
    return result


def validate_evidence(directory, project, evidence):
    """Historical authority binds an actual simple-mode entry, including restores.

    Restored history retains its source identity; newly appended decisions bind
    the destination. Current membership and current mode cannot erase history.
    """
    return validate_evidence_files(snapshot(directory, project), project, evidence)


def validate_evidence_files(files, project, evidence):
    """The same project/history binding for a captured offline snapshot."""
    validate_files(files, project)
    record = files.get(FILE)
    claimed = evidence['governance']
    number = claimed['revision']
    if record is None or number > len(record['revisions']):
        raise ValueError('Owner requirement evidence has no governance revision')
    entry = record['revisions'][number - 1]
    if claimed != {name: entry[name] for name in ('revision', 'sha256', 'mode')}:
        raise ValueError('Owner requirement evidence governance does not match its history')
    binding = files.get(SOURCE_FILE)
    identity = project
    if binding and number <= binding['revision']:
        identity = next(origin['project'] for origin in binding['origins']
                        if number <= origin['through_revision'])
    # Server-created decisions after a restore bind the destination even when
    # it still uses an unchanged source governance entry. Preserve that lineage
    # on further restores as well as the original entry's identity.
    identities = {identity, project}
    if binding:
        identities.update(binding.get('restored_projects', [binding['restored_from'], binding['project']]))
    if evidence['project'] not in identities:
        raise ValueError('Owner requirement evidence belongs to a different project')


def entry(number, previous, mode, actor, via, operation_id, at=None):
    record = {'revision': number, 'previous_sha256': previous, 'mode': mode,
              'actor': actor, 'via': via, 'at': at or time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
              'operation_id': operation_id}
    record['sha256'] = content_hash(record)
    return record


def initialize(directory, project, actor, operation_id):
    """Only genuinely new provisioning calls this, using its durable creation ID."""
    project_name(project)
    identifier(operation_id)
    files = snapshot(directory, project)
    if FILE in files:
        first = files[FILE]['revisions'][0]
        if (SOURCE_FILE in files or first['via'] != 'provisioning'
                or first['operation_id'] != operation_id or first['actor'] != actor):
            raise ValueError('Requirements governance was not initialized by this creation')
        return current(directory, project)
    record = {'schema_version': 1, 'project': project,
              'revisions': [entry(1, None, DEFAULT_GOVERNANCE_MODE, actor,
                                  'provisioning', operation_id)]}
    validate(record)
    atomic(safe_path(directory, FILE), record)
    return current(directory, project)


def set_mode(directory, project, mode, actor, operation_id, expected_revision, expected_sha256):
    """CAS under the caller's coordination/authority locks; preserve all history."""
    if mode not in MODES:
        raise ValueError('Requirements governance must be simple or governed')
    identifier(operation_id)
    if type(expected_revision) is not int or expected_revision < 0:
        raise ValueError('Invalid expected requirements governance revision')
    if expected_revision == 0:
        if expected_sha256 is not None:
            raise ValueError('Missing requirements governance has no hash')
    else:
        digest(expected_sha256)
    files = snapshot(directory, project)
    state = current(directory, project)
    record = files.get(FILE, {'schema_version': 1, 'project': project, 'revisions': []})
    for prior in record['revisions']:
        if prior['operation_id'] == operation_id:
            if (prior['mode'] != mode or prior['actor'] != actor or prior['via'] != 'session'
                    or prior['revision'] != expected_revision + 1
                    or prior['previous_sha256'] != expected_sha256):
                raise ValueError('Requirements governance operation was already used differently')
            return {name: prior[name] for name in ('revision', 'sha256', 'mode')}
    if state['revision'] != expected_revision or state['sha256'] != expected_sha256:
        raise ValueError('Requirements changed. Reload and try again.')
    if state['mode'] == mode:
        return state
    updated = dict(record, revisions=record['revisions'] + [entry(
        state['revision'] + 1, state['sha256'], mode, actor, 'session', operation_id)])
    validate(updated)
    atomic(safe_path(directory, FILE), updated)
    return current(directory, project)


def restored_files(files, source, destination):
    """Bind an unchanged source history to the destination before restore writes.

    Subsequent appends preserve the validated prefix. A second restore replaces
    the binding with the full prefix it actually restored, not a renamed history.
    """
    validate_files(files, source)
    result = dict(files)
    if FILE in files and source != destination:
        record = files[FILE]
        binding = files.get(SOURCE_FILE)
        origins = list(binding['origins']) if binding else []
        if not origins or origins[-1]['through_revision'] < len(record['revisions']):
            origins.append({'through_revision': len(record['revisions']), 'project': source})
        result[SOURCE_FILE] = {'schema_version': 1, 'project': destination,
                               'source_project': record['project'], 'restored_from': source,
                               'revision': len(record['revisions']), 'sha256': content_hash(record),
                               'origins': origins}
        result[SOURCE_FILE]['restored_projects'] = sorted(set(
            (binding.get('restored_projects', [binding['restored_from'], source]) if binding else [source])
            + [source, destination]))
    validate_files(result, destination)
    return result
