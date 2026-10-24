# Checkpoint template (task-checkpoint-v1)

This page is `docs checkpoint-template`. The payload to send is the JSON block at the end;
copy it to `checkpoint.json`, replace `example-task`, `previous`, `activity_cursor` and the
four texts, fill `source_commit` and `branch` when they are known (otherwise leave them
empty), then send it with:

```sh
b checkpoint TASK --file checkpoint.json
```

The same payload is kept as `templates/CHECKPOINT.json`.
`tests/test_checkpoint_template_limits.py` fails when this page, that file and the code
disagree, so a changed limit that is not carried into this page is caught before a release.

## Field limits

The checkpoint validator and `checkpoint --help` read their numbers from
`briefing.CHECKPOINT_FIELD_LIMITS`; this table states the same numbers.

| Field | Limit |
| --- | --- |
| source_commit | 128 characters (may be empty) |
| branch | 200 characters (may be empty) |
| intent | 600 characters |
| acceptance | 1000 characters |
| summary | 1000 characters |
| next_action | 600 characters |
| open_items | 100 entries |
| open item text | 400 characters |
| open item source | 240 characters |
| resolved | 100 entries |
| resolved reason | 400 characters |
| resolved evidence | 240 characters |
| whole checkpoint | 80000 canonical bytes (80 KB) |
| lane_state.waiting_on | 96 characters |

A refusal names the field, the length it had and the limit, for example
`summary: 1001 characters, the limit is 1000`. An item refusal also names the path and
the id, for example `open_items[1].text (id blocked-on-keys): 401 characters, the limit
is 400`. A record with several problems is refused once, with every problem named.

## Carrying open items forward

`brief.checkpoint.open_items` is only the first ten items. `brief.carry_open_items` is
the complete list, in order, and `brief.checkpoint_template.body.open_items` already
holds every one of them (the template's `carried_open_items` is that count). Copy the
template's list (or `carry_open_items`), never the first ten. When `carry_open_items` is
null the items could not all be read: read the brief again before writing the checkpoint.

## Pointers in source and evidence

An open item's `source` and a resolved item's `evidence` may contain whitespace-separated
`KIND:VALUE` tokens among ordinary prose. The kinds are `task`, `comment`, `ref`, `cap`,
`item`, `file`, `room`, `commit`, `doc` and `archive`. Values contain 1–200 characters
from `A–Z a–z 0–9 . _ / # : -`; the whole field still has its existing 240-character limit.
For example: `task:example-task ref:host.build.rule (the original decision)`.
Punctuation is not stripped: `task:x.` names `x.`, and `task:x:` names `x:`.
Separate sentence punctuation with whitespace when you mean `task:x`.
Prose and malformed tokens remain legal; stored text is never rewritten.

The CLI's `unresolved.items` and HTTP's `checkpoint.open_items` keep the original
checkpoint item shape and can be copied verbatim when the list is complete. Respect
paging: `carry_open_items` and the checkpoint template body hold the complete list.
Resolution is separate, keyed by item id: CLI `unresolved.pointer_items` and
`resolved.pointer_items`; HTTP `checkpoint.pointer_items` and
`checkpoint.resolved_pointer_items`. Do not copy those metadata maps into a checkpoint.
Older kits can read and carry the same source/evidence strings unchanged.

There are five read states:

- `live`: the target exists in loaded authorized data; this does not prove the claim.
- `superseded`: an explicitly retired or replaced record, not an additive `follows` delivery.
- `unavailable`: absent, inaccessible, malformed, conflicted or ambiguous targets;
  task/comment requests for record anchors and hidden record comments are unavailable too.
  Answers have no target label or follow command and do not distinguish these cases.
- `unchecked`: uniform for every target of an unloaded kind, including file, room,
  item, commit, doc and archive. Resolution performs no new I/O.
- `unresolved`: beyond 10 resolved pointers per item or 50 per brief, counted in `pointers_omitted`.

Every pointer carries `trust: data`. Labels are escaped before clipping to 120 characters,
without cutting a Unicode escape. The text brief folds line breaks in pointer labels,
source, reason and evidence, and escapes direction marks. Pure prose does not build a
pointer index.

## lane_state (a sibling, not a checkpoint field)

`lane_state` travels in the same payload FILE beside the checkpoint object and is popped
out before the checkpoint is validated. Do not put it inside the checkpoint object, and do
not send `checkpoint`: the writer sets that to the comment id it publishes.

The required fields are `schema_version` (1), `task` and `state`. `state` is one of
`working`, `waiting-on-coordinator`, `waiting-on-review`, `waiting-on-user` or `idle`.
A waiting state also requires `waiting_on` (a role `coordinator`/`reviewer`/`user`, or an
actor id, at most 96 characters; refused without one). `expect_by` is an optional
ISO-8601 time with an offset; a past value is accepted and flagged, and only an
unparseable one is refused.

```json
{"schema_version": 1, "task": "example-task", "state": "working"}
```

## Payload

```json
{
  "schema_version": 1,
  "task": "example-task",
  "previous": null,
  "activity_cursor": "COPY_CURRENT_ACTIVITY_CURSOR_FROM_BRIEF",
  "source_commit": "",
  "branch": "",
  "intent": "Describe the intended outcome (at most 600 characters).",
  "acceptance": "Describe the acceptance criteria (at most 1000 characters).",
  "summary": "Describe the current verified position (at most 1000 characters).",
  "next_action": "Describe the next action and who should take it (at most 600 characters).",
  "open_items": [],
  "resolved": [],
  "lane_state": {
    "schema_version": 1,
    "task": "example-task",
    "state": "working"
  }
}
```
