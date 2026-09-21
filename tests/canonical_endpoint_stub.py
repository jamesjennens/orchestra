#!/usr/bin/env python3
"""Disposable canonical endpoint used by the HTTP binding tests.

It speaks the same stdin/stdout JSON envelope as ``endpoint.py`` and persists its
canonical records in ``<root>/canonical.json``. That lets the HTTP service be
exercised across a process boundary against durable canonical state instead of an
in-process dict. It is a stand-in for the deployed ``bd`` commands (which need a
POSIX host and a real runtime), not a replacement for them.
"""
import argparse
import json
import sys
from pathlib import Path


def load(path):
    if path.exists():
        return json.loads(path.read_text(encoding='utf-8'))
    return {'tasks': {}, 'events': {}, 'seq': 0}


def save(path, state):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(state), encoding='utf-8')
    temporary.replace(path)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', required=True)
    args = parser.parse_args()
    root = Path(args.root)
    try:
        request = json.loads(sys.stdin.read(2_000_001))
    except Exception as error:  # noqa: BLE001 - report, never traceback
        print(json.dumps({'returncode': 2, 'stdout': '',
                          'stderr': 'bad request: %s' % error}))
        return
    action = request.get('action')
    argv = request.get('args') or []
    attachments = request.get('attachments') or {}
    state_path = root / 'canonical.json'
    state = load(state_path)
    events = state.setdefault('events', {})

    def emit(payload, code=0, stderr=''):
        text = payload if isinstance(payload, str) else json.dumps(payload)
        print(json.dumps({'returncode': code, 'stdout': text, 'stderr': stderr}))

    def attachment_text(key):
        item = attachments.get(key) or {}
        return item.get('text') or ''

    try:
        if action == 'bd':
            command = argv[0] if argv else ''
            if command == 'create':
                title = argv[1] if len(argv) > 1 else ''
                description = attachment_text('0') if '--body-file' in argv else ''
                state['seq'] += 1
                task_id = 'kittrial-5bb.%d' % state['seq']
                task = {'id': task_id, 'title': title, 'description': description,
                        'status': 'open', 'assignee': None}
                state['tasks'][task_id] = task
                events.setdefault(task_id, []).append({'action': 'task-created'})
                save(state_path, state)
                emit(task)
                return
            if command == 'list':
                emit(list(state['tasks'].values()))
                return
            if command == 'show':
                task = state['tasks'].get(argv[1]) if len(argv) > 1 else None
                if task is None:
                    emit('', 2, 'task not found')
                    return
                emit(task)
                return
            if command == 'update':
                task = state['tasks'].get(argv[1]) if len(argv) > 1 else None
                if task is None:
                    emit('', 2, 'task not found')
                    return
                for flag, field in (('--status', 'status'), ('--assignee', 'assignee'),
                                    ('--title', 'title')):
                    if flag in argv:
                        task[field] = argv[argv.index(flag) + 1]
                events.setdefault(task['id'], []).append({'action': 'task-updated'})
                save(state_path, state)
                emit(task)
                return
            emit('', 2, 'unknown bd command %r' % command)
            return
        if action == 'checkpoint':
            task_id = argv[0]
            record = json.loads(attachment_text('0'))
            events.setdefault(task_id, []).append({'action': 'checkpoint-added',
                                                   'summary': record.get('summary')})
            save(state_path, state)
            emit({'checkpoint': record})
            return
        if action == 'review':
            task_id = argv[0]
            record = json.loads(attachment_text('0'))
            events.setdefault(task_id, []).append({'action': record.get('operation')})
            save(state_path, state)
            emit({'review': record})
            return
        if action == 'history':
            emit(events.get(argv[0], []) if argv else [])
            return
        emit('', 2, 'unknown action %r' % action)
    except Exception as error:  # noqa: BLE001 - report, never traceback
        emit('', 1, '%s: %s' % (type(error).__name__, error))


if __name__ == '__main__':
    main()
