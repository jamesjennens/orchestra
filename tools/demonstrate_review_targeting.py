"""Demonstrate the review-operation targeting error messages on a disposable fixture.

This is evidence instrumentation for kittrial-5bb.5, not a unit test: it prints
the refusal text for the reported caller mistake and for correct usage so the
before/after wording can be recorded in a checkpoint. It mutates only an
in-memory synthetic issue.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import review_workflow as w

issue = dict(id='task-1', assignee='worker', status='in_progress', comments=[])


def run(args):
    cid = str(len(issue['comments']) + 1)
    issue['comments'].append(dict(id=cid, text=args[3], author='worker',
                                  created_at='2026-09-16T00:00:00Z'))
    return json.dumps({'id': cid})


def send(label, payload, actor='worker'):
    try:
        result = w.execute([issue], 'task-1', actor, payload, run)
        print(f'{label}: ACCEPTED {json.dumps(result)}')
    except ValueError as error:
        print(f'{label}: REFUSED -> {error}')


def body(op, operation_id, previous, **extra):
    return dict(schema_version=1, operation=op, operation_id=operation_id,
                task='task-1', previous=previous, **extra)


contribution = dict(repository='ssh://git.example/project', commit='a' * 40,
                    base_commit='b' * 40,
                    delivery=dict(kind='remote', remote='ssh://git.example/project',
                                  branch='worker/task'),
                    summary='Implementation and test evidence')

print('--- no contribution recorded yet ---')
send('approve(unknown id, no contribution)', body(
    'approve', 'o1', None, contribution='unknown', summary='Reviewed'), 'reviewer')

print('--- first contribution ---')
send('contribute(revision 1)', body('contribute', 'o2', None, supersedes=None, **contribution))
state = w.project(issue)
first = state['contribution']['comment_id']
print(f'    contribution id = {first}, latest_comment_id = {state["latest_comment_id"]}')

print('--- reviewer requests a change so latest_comment_id diverges ---')
send('request-changes(on contribution)', body(
    'request-changes', 'o3', first, contribution=first,
    items=[dict(id='fix', text='Correct the edge case')]), 'reviewer')
state = w.project(issue)
latest = state['latest_comment_id']
print(f'    contribution id = {state["contribution"]["comment_id"]}, latest_comment_id = {latest}')

print('--- the reported caller mistake and its neighbours ---')
send('approve(latest_comment_id)  <-- reported mistake', body(
    'approve', 'o4', latest, contribution=latest, summary='Reviewed'), 'reviewer')
send('approve(other id)', body(
    'approve', 'o5', latest, contribution='not-a-real-id', summary='Reviewed'), 'reviewer')
send('approve(correct contribution id)', body(
    'approve', 'o6', latest, contribution=first, summary='Reviewed'), 'reviewer')
print('resulting review_state:', w.project(issue)['review_state'])
