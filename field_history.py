"""Which task fields changed since a checkpoint, from the tracker's own version history
(kittrial-5bb.354).

A checkpoint records no copy of the task's title, description or acceptance, and its
activity cursor hashes them together with the status, assignee and labels, so the kit
alone cannot tell a description edit from a claim. The tracker can: ``bd history TASK
--json`` lists one Dolt commit per write, newest first, each with the whole issue as it
stood (``CommitDate`` and ``Issue``; measured on bd 1.2.2). The value a field had at the
checkpoint is the one in the newest commit at or before the checkpoint's time.

What it does NOT record is WHO made a write: every commit's ``Committer`` is ``beads``,
whatever actor the write named. So a field change is reported with its time, its current
and previous values (clipped, terminal-safe) and ``author: null``, never with a guessed
author.

Only the 'changes since your checkpoint' section reads this, on brief and the web task
read, never on a wait or next poll, and only when the checkpoint's cursor is stale. The
read is bounded (``HISTORY_LIMIT`` commits); a history that does not reach back to the
checkpoint, a compacted one, or one that cannot be read says so instead of guessing.
"""
import record_json

#: The three fields this section and the marker watch. Never status, assignee,
#: priority, notes or labels: a claim is not a change the agent must read.
FIELDS = ('title', 'description', 'acceptance_criteria')
#: At most this many commits are read per task read.
HISTORY_LIMIT = 50
#: Each value is clipped to this many characters.
VALUE_CHARS = 300
#: The fixed native read. ``--readonly`` makes bd itself refuse any write.
def command(task, limit=HISTORY_LIMIT):
    return ['history', task, '--json', '--limit', str(limit), '--readonly']

UNKNOWN_NOTE = ('Which task fields changed could not be determined (%s); read the task itself '
                'with show for its current title, description and acceptance.')
WHO_NOTE = 'who made the change is not recorded'


def parse(text):
    """The commits of one ``bd history --json`` answer, newest first, or None.

    Each is ``(moment, {field: value})``. Anything not in that shape makes the whole
    answer unusable (None): a partial reading could name the wrong previous value.
    """
    from activity import parse_moment
    try:
        data = record_json.loads(text)
    except (ValueError, TypeError, RecursionError):
        return None
    if not isinstance(data, list):
        return None
    commits = []
    for item in data:
        if not isinstance(item, dict) or not isinstance(item.get('Issue'), dict):
            return None
        try:
            moment = parse_moment(item.get('CommitDate'), 'history commit date')
        except (ValueError, TypeError):
            return None
        issue = item['Issue']
        values = {}
        for name in FIELDS:
            value = issue.get(name)
            if value is not None and not isinstance(value, str):
                return None
            values[name] = value or ''
        commits.append((moment, values))
    commits.sort(key=lambda commit: commit[0], reverse=True)
    return commits


def changes(text, row, checkpoint_timestamp, limit=HISTORY_LIMIT):
    """``(fields, unknown_note)``: the changed fields since the checkpoint, or why not known.

    ``fields`` is a list of ``{field, author, changed_at, edits, current, previous,
    previous_recorded, note}``; ``unknown_note`` is None when the reading is exact.
    """
    from activity import parse_moment, utc_text
    from briefing import clip, plain_text
    if text is None:
        return [], UNKNOWN_NOTE % 'the tracker history could not be read'
    commits = parse(text)
    if commits is None:
        return [], UNKNOWN_NOTE % 'the tracker history was not in the expected form'
    try:
        stamp = parse_moment(checkpoint_timestamp, 'checkpoint timestamp')
    except (ValueError, TypeError):
        return [], UNKNOWN_NOTE % 'the checkpoint has no readable time'
    before = [values for moment, values in commits if moment <= stamp]
    if not before:
        reason = ('the tracker history read stops at %d commits, after the checkpoint' % limit
                  if len(commits) >= limit else 'the tracker history does not reach back to the checkpoint')
        return [], UNKNOWN_NOTE % reason
    at_checkpoint = before[0]
    after = [(moment, values) for moment, values in commits if moment > stamp]   # newest first
    found = []
    for name in FIELDS:
        current = str(row.get(name) or '')
        previous = at_checkpoint[name]
        if current == previous:
            continue
        # Each commit after the checkpoint whose value differs from the commit before it is
        # one edit; the newest such commit is when the field last changed.
        chain = [values[name] for _moment, values in reversed(after)]           # oldest first
        edits = 0
        last = None
        prior = previous
        for (moment, values) in reversed(after):
            if values[name] != prior:
                edits += 1
                last = moment
            prior = values[name]
        found.append({'field': name, 'author': None,
                      'changed_at': utc_text(last) if last is not None else None,
                      'edits': edits if chain else None,
                      'current': clip(plain_text(current), VALUE_CHARS),
                      'previous': clip(plain_text(previous), VALUE_CHARS),
                      'previous_recorded': True, 'note': WHO_NOTE})
    return found, None
