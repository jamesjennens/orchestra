"""Bounded task checkpoint/post summaries from one caller-visible snapshot."""

from activity import parse_moment, utc_text
from briefing import checkpoint_state, identity
from coordination import is_merge_slot
from reserved_comments import is_record_anchor, reserved_match

IDS_MAX = 100
NEXT_ACTION_MAX = 500
AUTHOR_MAX = 96
COMMENT_ID_MAX = 161


def task_ids(values):
    """Validate the complete closed request before a transport reads anything."""
    if not isinstance(values, list) or not 1 <= len(values) <= IDS_MAX:
        raise ValueError('Task summary requires 1..100 distinct task IDs')
    for value in values:
        identity(value)
    if len(set(values)) != len(values):
        raise ValueError('Task summary requires distinct task IDs')
    return values


def parse_args(args):
    from work import Parser
    parser = Parser(add_help=False)
    parser.add_argument('tasks', nargs='+')
    parser.add_argument('--json', action='store_true')
    try:
        parsed, extra = parser.parse_known_args([value for value in args if value != '--json'])
    except ValueError:
        raise ValueError('Task summary requires 1..100 distinct task IDs [--json]') from None
    if extra:
        raise ValueError('Unknown task summary arguments')
    return task_ids(parsed.tasks)


def clipped(value, limit):
    text = value if isinstance(value, str) else ''
    return text[:limit], max(0, len(text) - limit)


def missing(task):
    # A hidden task has exactly the same answer as an unknown one.
    return dict(task=task, found=False, status=None, checkpoint=None,
                last_post=None, coverage='unknown')


def comment_identity(comment):
    value = comment.get('id')
    if isinstance(value, bool) or not isinstance(value, (str, int)) or not 1 <= len(str(value)) <= COMMENT_ID_MAX:
        raise ValueError('Unusable comment identity')
    identity(str(value))
    return str(value)


def item(row):
    status = row.get('status')
    answer = dict(task=row['id'], found=True, status=status if isinstance(status, str) and len(status) <= 64 else None,
                  checkpoint=None, last_post=None, coverage='validated')
    comments = row.get('comments')
    if comments is None:
        comments = []
    if (row.get('malformed') or not isinstance(comments, list)
            or any(not isinstance(c, dict) or not isinstance(c.get('text', ''), str) for c in comments)):
        return dict(answer, status=None, coverage='unknown')
    if answer['status'] is None:
        answer['coverage'] = 'unknown'
    try:
        state = checkpoint_state(row, normalize=False)
        if state['invalid'] and answer['coverage'] == 'validated':
            answer['coverage'] = 'partial'
        if state['current'] is not None:
            payload, comment = state['current']
            cid = comment_identity(comment)
            from lane_state import newest
            declared = newest(row.get('comments'), row['id'])
            declared_state = (declared[1]['state'] if declared is not None
                              and str(declared[2].get('id')) == cid else 'unknown')
            action, omitted = clipped(payload['next_action'], NEXT_ACTION_MAX)
            answer['checkpoint'] = dict(comment_id=cid,
                timestamp=utc_text(parse_moment(comment.get('created_at'))),
                state=declared_state, next_action=action, next_action_omitted_chars=omitted,
                open_items_count=len(payload['open_items']))
    except (ValueError, TypeError, KeyError, StopIteration, OverflowError):
        answer.update(checkpoint=None, coverage='unknown')
    try:
        latest = None
        for comment in row.get('comments') or []:
            if not isinstance(comment, dict):
                raise ValueError('Unusable comment')
            body = comment.get('text')
            if not isinstance(body, str) or not body.strip() or reserved_match(body) is not None:
                continue
            cid = comment_identity(comment)
            candidate = (parse_moment(comment.get('created_at')), cid, comment)
            if latest is None or candidate[:2] > latest[:2]:
                latest = candidate
        if latest is not None:
            moment, cid, comment = latest
            author, omitted = clipped(comment.get('author'), AUTHOR_MAX)
            answer['last_post'] = dict(comment_id=cid, timestamp=utc_text(moment),
                                       author=author, author_omitted_chars=omitted)
    except (ValueError, TypeError, KeyError, OverflowError):
        answer.update(last_post=None, coverage='unknown')
    return answer


def read(rows, ids, project=None):
    """Filter before selection; never query again to distinguish missing IDs."""
    ids = task_ids(ids)
    wanted = set(ids)
    selected = {}
    duplicates = set()
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get('id'), str) or row['id'] not in wanted:
            continue
        if project is not None and row.get('project_id') not in (None, project):
            continue
        if is_record_anchor(row) or is_merge_slot(row):
            continue
        if row['id'] in selected:
            duplicates.add(row['id'])
        selected[row['id']] = row
    return dict(schema_version=1, items=[
        item(selected[task]) if task in selected and task not in duplicates else missing(task)
        for task in ids])
