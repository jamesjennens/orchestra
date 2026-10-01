#!/usr/bin/env python3
"""Strict, in-process stand-in for ``endpoint.py`` used by the round-3 probe.

It speaks the same stdin/stdout JSON envelope as ``endpoint.py`` and imports the
*checkout under test* for every canonical rule, so the same script can be pointed at
two revisions and will apply each revision's own canonical validation:

* the ``endpoint.py`` transport rules (forbidden raw file flags, action routing,
  ``@attachment:`` transport) are reproduced verbatim;
* ``checkpoint``/``review``/``history`` run the checkout's real
  ``briefing.execute``/``work.execute``/``briefing.history_page`` against an
  in-process canonical row store, so field-set, transition and history-page limits
  are the real ones;
* when the checkout also ships ``http_authority`` (the revision under test), the
  live-authority re-validation and durable operation journal run exactly as the
  deployed endpoint runs them. A checkout without that module behaves like the
  endpoint it ships (no boundary), which is what makes the probe able to reproduce
  the pre-fix findings.

The canonical effect is emulated (a JSON row store), not a real Beads database; the
probe documents that boundary in its report.
"""
import argparse
import inspect
import json
import os
import sys
from pathlib import Path

CODE_DIR = Path(os.environ.get('STRICT_ENDPOINT_CODE_DIR') or
                Path(__file__).resolve().parents[1]).resolve()
if str(CODE_DIR) not in sys.path:
    sys.path.insert(0, str(CODE_DIR))

ALLOWED = {'list', 'show', 'ready', 'search', 'count', 'create', 'update', 'close',
           'reopen', 'comments', 'dep', 'state', 'lint'}
FORBIDDEN = {'--directory', '-C', '--db', '--repo', '--global', '--actor', '--author',
             '--profile', '--graph', '--config', '--metadata'}
FILE_FLAGS = {'--body-file', '--design-file', '--file', '-f'}

try:  # the revision under test may not ship the shared boundary yet
    import http_authority
except ImportError:  # pragma: no cover - only the pre-fix revision
    http_authority = None


def load(path):
    if path.exists():
        return json.loads(path.read_text(encoding='utf-8'))
    return {'rows': [], 'seq': 0, 'comments': 0, 'journal': {}}


def save(path, state):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(state), encoding='utf-8')
    temporary.replace(path)


class Canonical:
    """Emulated ``bd`` over one JSON file; enforces the real transport rules."""

    def __init__(self, root, project):
        self.root = Path(root)
        self.project = project
        self.state_path = self.root / 'canonical.json'
        self.path = self.root / project
        self.path.mkdir(parents=True, exist_ok=True)

    def rows(self):
        return load(self.state_path)['rows']

    def _mutate(self, change):
        state = load(self.state_path)
        result = change(state)
        save(self.state_path, state)
        return result

    def _row(self, state, task):
        for row in state['rows']:
            if row.get('id') == task:
                return row
        raise ValueError('task not found')

    def bd(self, argv, *, enforce=True):
        """Run one emulated bd command; returns (returncode, stdout, stderr).

        ``enforce`` mirrors ``endpoint.py``: the contributor interface rules apply to
        the caller-supplied ``bd`` action only; the internal ``run`` closure the
        canonical workflow modules use (``export``, ``comments add``, ``update``) is
        not re-filtered.
        """
        if not argv:
            raise ValueError('Expected argument list')
        command, rest = argv[0], argv[1:]
        if enforce:
            if command in FORBIDDEN or any(a.split('=', 1)[0] in FORBIDDEN for a in argv):
                raise ValueError('Connection/identity/file configuration flags are operator-only')
            if command not in ALLOWED:
                raise ValueError('Command is outside the contributor interface')
        if command == 'create':
            title = rest[0] if rest else ''
            description = ''
            if '--body-file' in rest:
                source = Path(rest[rest.index('--body-file') + 1])
                description = source.read_text(encoding='utf-8') if source.exists() else ''

            def change(state):
                state['seq'] += 1
                task_id = 'kittrial-5bb.%d' % state['seq']
                state['rows'].append({
                    'id': task_id, 'title': title, 'description': description, 'status': 'open',
                    'assignee': None, 'issue_type': 'task', 'comments': [],
                    'labels': [], 'dependencies': [], 'created_at': '2026-01-01T00:00:00Z',
                })
                return task_id
            task_id = self._mutate(change)
            row = next(r for r in self.rows() if r['id'] == task_id)
            return 0, json.dumps(row), ''
        if command == 'list':
            return 0, json.dumps(self.rows()), ''
        if command == 'show':
            row = next((r for r in self.rows() if r['id'] == (rest[0] if rest else '')), None)
            if row is None:
                return 2, '', 'task not found'
            return 0, json.dumps(row), ''
        if command == 'export':
            text = '\n'.join(json.dumps(r) for r in self.rows()) + ('\n' if self.rows() else '')
            return 0, text, ''
        if command == 'update':
            task = rest[0] if rest else ''

            def change(state):
                row = self._row(state, task)
                index = 1
                while index < len(rest):
                    flag = rest[index]
                    if flag == '--remove-label':
                        index += 2
                        continue
                    if flag in ('--status', '--assignee', '--title', '--description'):
                        if index + 1 >= len(rest):
                            raise ValueError('Missing value for %s' % flag)
                        row[flag[2:].replace('-', '_')] = rest[index + 1]
                        index += 2
                        continue
                    if flag == '--json':
                        index += 1
                        continue
                    index += 1
                if row.get('status') == 'in_progress':
                    row['status'] = 'in_progress'
                return row
            row = self._mutate(change)
            return 0, json.dumps(row), ''
        if command == 'comments':
            if rest[:1] != ['add'] and len(rest) >= 1:
                # `bd comments TASK --json`: the task's comments, as bd prints them.
                row = next((r for r in self.rows() if r['id'] == rest[0]), None)
                if row is None:
                    return 2, '', 'task not found'
                return 0, json.dumps(row.get('comments') or []), ''
            if rest[:1] != ['add'] or len(rest) < 3:
                return 2, '', 'unsupported comments form'
            task, text = rest[1], rest[2]

            def change(state):
                row = self._row(state, task)
                state['comments'] += 1
                comment = {'id': str(state['comments']), 'author': 'emulated',
                           'created_at': '2026-01-01T00:00:%02dZ' % (state['comments'] % 60),
                           'text': text}
                row.setdefault('comments', []).append(comment)
                return comment['id']
            comment_id = self._mutate(change)
            return 0, json.dumps({'id': comment_id}), ''
        raise ValueError('unsupported bd command %r' % command)

    def run(self, argv):
        code, stdout, stderr = self.bd(argv, enforce=False)
        if code:
            raise ValueError(stderr or stdout)
        return stdout


def materialize(args, attachments, tmp):
    """``endpoint.py``'s @attachment substitution, verbatim in behaviour."""
    final = []
    for index, value in enumerate(args):
        if not isinstance(value, str) or '\0' in value:
            raise ValueError('Expected argument list')
        if value.split('=', 1)[0] in FILE_FLAGS:
            raise ValueError('Use client attachment transport; raw server file paths '
                             'are not accepted')
        if value.startswith('@attachment:'):
            key = value.partition(':')[2]
            item = attachments.get(key)
            if not isinstance(item, dict) or item.get('flag') not in FILE_FLAGS or \
                    not isinstance(item.get('text'), str):
                raise ValueError('Invalid attachment')
            destination = Path(tmp) / ('%d.txt' % index)
            destination.write_text(item['text'], encoding='utf-8')
            final.extend([item['flag'], str(destination)])
        else:
            final.append(value)
    return final


def envelope(code, stdout='', stderr='', **extra):
    payload = {'returncode': code, 'stdout': stdout, 'stderr': stderr}
    payload.update(extra)
    return payload


def dispatch(canonical, request, tmp, run=None):
    action = request.get('action')
    args = request.get('args') or []
    attachments = request.get('attachments') or {}
    actor = request.get('actor', '')
    project = request['project']
    run = canonical.run if run is None else run
    if not isinstance(args, list):
        raise ValueError('Expected argument list')
    if action == 'bd':
        if not args or args[0] not in ALLOWED:
            raise ValueError('Command is outside the contributor interface')
        final = materialize(args, attachments, tmp)
        code, stdout, stderr = canonical.bd(final)
        return envelope(code, stdout, stderr)
    if action == 'checkpoint':
        from briefing import execute as briefing_execute
        if len(args) != 2 or not args[1].startswith('@attachment:'):
            raise ValueError('Use checkpoint TASK --file checkpoint.json')
        items = {k: dict(v, path=str(Path(tmp) / k)) for k, v in attachments.items()}
        output = briefing_execute(canonical.root, canonical.path, project, actor, 'checkpoint',
                                  args, items, run)
        return envelope(0, output)
    if action in ('brief', 'history'):
        from briefing import execute as briefing_execute
        items = {k: dict(v, path=str(Path(tmp) / k)) for k, v in attachments.items()}
        output = briefing_execute(canonical.root, canonical.path, project, actor, action,
                                  args, items, run)
        return envelope(0, output)
    if action == 'review':
        from work import execute as work_execute
        items = {k: dict(v, path=str(Path(tmp) / k)) for k, v in attachments.items()}
        result = work_execute(canonical.path, actor, 'review', args, items, run)
        return envelope(0, json.dumps(result, ensure_ascii=False) + '\n')
    if action in ('work', 'handoff'):
        from work import execute as work_execute
        items = {k: dict(v, path=str(Path(tmp) / k)) for k, v in attachments.items()}
        result = work_execute(canonical.path, actor, action, args, items, run)
        return envelope(0, json.dumps(result, ensure_ascii=False) + '\n')
    if action == 'anchors':
        # endpoint.py's read-only anchors action (kittrial-5bb.71): the anchors among
        # the given task ids (bd show --include-comments there), or the snapshot.
        try:
            from reserved_comments import ANCHOR_READ_IDS_MAX, record_anchor_ids
        except ImportError:  # a revision that predates the action
            raise ValueError('Unknown action')
        if len(args) > ANCHOR_READ_IDS_MAX or len(set(args)) != len(args):
            raise ValueError('anchors takes no arguments, or at most %d distinct task ids'
                             % ANCHOR_READ_IDS_MAX)
        rows = [json.loads(line) for line in run(['export', '--all']).splitlines() if line.strip()]
        if args:
            rows = [row for row in rows if row.get('id') in args]
        return envelope(0, json.dumps({'schema_version': 1, 'anchors': record_anchor_ids(rows)}) + '\n')
    if action == 'lifecycle':
        from lifecycle import apply_native
        payload = json.loads(args[0]) if args and isinstance(args[0], str) else None
        result = apply_native(payload, actor, run)
        return envelope(0, json.dumps(result, ensure_ascii=False) + '\n')
    if action == 'coordinate':
        from coordination import apply_native as coordinate
        payload = json.loads(args[0]) if args and isinstance(args[0], str) else None
        result = coordinate(payload, actor, run, canonical.path)
        return envelope(0, json.dumps(result, ensure_ascii=False) + '\n')
    raise ValueError('Unknown action')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', required=True)
    parser.add_argument('--authority-store')
    parser.add_argument('--authority-lock')
    parser.add_argument('--require-authority', action='store_true')
    arguments = parser.parse_args()
    try:
        request = json.loads(sys.stdin.read(2_000_001))
    except Exception as error:  # noqa: BLE001
        print(json.dumps(envelope(2, stderr='bad request: %s\n' % error)))
        return
    root = Path(arguments.root)
    canonical = Canonical(root, request.get('project', 'project'))
    journal_path = (http_authority.journal_path(canonical.path)
                    if http_authority is not None and hasattr(http_authority, 'journal_path')
                    else canonical.path / '.http-operations.json')
    tmp = canonical.path / '.attachments'
    tmp.mkdir(parents=True, exist_ok=True)
    config = None
    if http_authority is not None and hasattr(http_authority, 'AuthorityConfig') and \
            arguments.authority_store:
        config = http_authority.AuthorityConfig(arguments.authority_store,
                                                arguments.authority_lock)
    # The instrumented runner is the effect's only route to native state: a refusal
    # raised before its first write is proven pre-effect and keeps rc=2.
    run_callable = canonical.run
    runner = None
    if http_authority is not None and hasattr(http_authority, 'NativeRunner'):
        runner = http_authority.NativeRunner(canonical.run)
        run_callable = runner
    try:
        if http_authority is not None and hasattr(http_authority, 'run_guarded'):
            parameters = inspect.signature(http_authority.run_guarded).parameters
            kwargs = {}
            if 'authority_config' in parameters:
                kwargs = {'authority_config': config,
                          'require_authority': arguments.require_authority}
            if 'runner' in parameters:
                kwargs['runner'] = runner
            answer = http_authority.run_guarded(
                request, journal_path,
                lambda: dispatch(canonical, request, tmp, run_callable), **kwargs)
        else:
            answer = dispatch(canonical, request, tmp, run_callable)
    except Exception as error:  # noqa: BLE001 - report, never traceback
        answer = envelope(2, stderr='%s: %s\n' % (type(error).__name__, error))
    print(json.dumps(answer, ensure_ascii=False))


if __name__ == '__main__':
    main()
