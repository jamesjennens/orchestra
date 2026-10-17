#!/usr/bin/env python3
"""One request per process. JSON on stdin/stdout; no contributor shell interpolation.

The same entry point serves the SSH worker and the trusted HTTP service. The HTTP
service adds ``--authority-store``/``--authority-lock`` (and ``--require-authority``
for mutations) to the launch command; those are server-side configuration and are
never taken from the request body. An SSH-shaped request therefore cannot choose the
live-authority document or the lock path, and can only omit the check because it has
no HTTP principal at all. ``--service-namespace`` is the web service's own actor
namespace (``http`` unless it was started with another): at use the endpoint refuses a
credential named under it, and only its launcher knows it (kittrial-5bb.188 item 3).
"""
import sys
if sys.version_info < (3, 10):
    # Before every other import, and in syntax Python 3.6 reads: an older interpreter failed in
    # an import further down, with a traceback that hid the cause (kittrial-5bb.191).
    sys.stderr.write('endpoint.py needs Python 3.10 or newer and was started with Python %d.%d.%d (%s). '
                     'Nothing was carried out. Set "python" in the client configuration to an interpreter of 3.10 or newer on the server; on an office installation that is the bundled one, INSTALL_ROOT/current/python-runtime/..., as add-project prints it.\n'
                     % (sys.version_info[0], sys.version_info[1], sys.version_info[2], sys.executable))
    sys.exit(2)
import argparse
import contextlib
import difflib
import fcntl
import json
import os
import record_json
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from admin import ConfigurationUnreadable,deployment_document,deployment_password,environment,project_dir,root_path,operators as configured_operators,verifiers as configured_verifiers,review_workflow_writes as configured_review_writes,coordinator_rules as configured_coordinator_rules
import bd_refusals
import native
import raw_lifecycle
from coordination import is_merge_slot, merge_slot_sentence
from render import render
from lifecycle import apply_native
from version import report
from reserved_comments import (carries_record_label, check_raw_request, comment_target,
                               first_reserved_label, is_record_anchor, label_guard_request,
                               operator_only_in_args, raw_file_flag_in_args,
                               is_merge_slot_id, shown_token, write_targets, MERGE_SLOT_LABEL, MERGE_SLOT_SUFFIX,
                               reserved_label_in_args, refuse_http_actor, status_change_targets, title_change_targets,
                               unresolved_bd_flags)
from http_authority import AuthorityConfig, NativeRunner, CAP_APPROVE, CAP_PROJECT_ADMIN, verified_web_authority, block_mismatch_denial, required_capability, descriptor_actor_denial, http_actor_denial, is_mutating_invocation, journal_path, run_guarded, stamp_write

#: One warning per line on stderr (kittrial-5bb.197 review request 01a12894, item 6):
#: the configuration warnings and the native warnings are collected in one list, and
#: three joined warnings with no separator read as one run-on line.
def warning_text(warnings):
    """The stderr text of a warning list, one line per warning."""
    return ''.join(line if line.endswith('\n') else line+'\n' for line in warnings)


def owner_loader(path,rule):
    """A zero-argument reader of the project's actor -> principal map.

    The map (``sessions.owners``) is read only when a rule needs it. Advisory
    readers catch failures and retain name comparison. Independent-approval
    writers fail closed, including listed operators. The other naming rules can
    decide an exact assignee or directly listed name without a registry read.
    A failure names the setting and recovery once, without registry contents.
    """
    def load():
        import sessions
        try:
            return sessions.owners(path)
        except (ValueError,OSError):
            from review_workflow import CoordinatorRuleRefusal
            raise CoordinatorRuleRefusal('The installation setting %s is on, and the session registry cannot be read. '
                             'An operator turns the rule off with `admin.py coordinator-rules off --rule %s '
                             '--actor OPERATOR`, or repairs the session registry; nothing was changed.'
                             %(rule,rule)) from None
    return load


def party_owner_loader(path):
    """A lazy actor -> principal reader for the attest/hold/release-hold party rule.

    That rule is NOT one of the switchable coordinator rules (kittrial-5bb.10 review
    item 2), so a review action carries this map whatever ``coordinator_writes`` and
    ``independent_approval`` say. Unlike :func:`owner_loader` a damaged registry does
    not raise here: the review write catches it, compares names, and says in its answer
    that parties could not be compared.
    """
    def load():
        import sessions
        return sessions.owners(path)
    return load

ALLOWED={'list','show','ready','search','count','create','update','close','reopen','comments','dep','state','lint'}
# Legacy name kept for operators reading this file; enforcement is the
# spelling-aware reserved_comments.operator_only_in_args() below.
FORBIDDEN={'--directory','-C','--db','--repo','--global','--actor','--author','--profile','--graph','--config','--metadata'}
FILE_FLAGS={'--body-file','--design-file','--file','-f'}

def _guidance_ack_version(args):
    """The version named by `guidance ack --version VERSION` (kittrial-5bb.99).

    The caller must name the version it actually read with the explicit
    ``--version`` flag; the endpoint never assumes the current version and never
    accepts a bare positional version, so the documented form is the only form
    (kittrial-5bb.99 review `small` 3).
    """
    if not args:
        raise ValueError('Name the guidance version you read: guidance ack --version VERSION')
    if args[0]=='--version':
        if len(args)!=2:raise ValueError('Use guidance ack --version VERSION')
        return args[1]
    if args[0].startswith('--version='):
        if len(args)!=1:raise ValueError('Use guidance ack --version VERSION')
        value=args[0].split('=',1)[1]
        if not value:raise ValueError('Use guidance ack --version VERSION')
        return value
    raise ValueError('Use guidance ack --version VERSION')

def _native_labels(root,path,actor,task):
    """Canonical id and labels of one native issue, read through pinned bd.

    Used only by _guard_reserved_labels(), which runs under the same
    coordination lock as the write it guards, so the read cannot race a
    kit-mediated mutation. bd resolves an issue id by unambiguous suffix, so
    the argv token (`3q2`) is not always the canonical row id (`pp-3q2`);
    the returned rows are matched against the token and an unresolved or
    ambiguous read raises rather than reporting "no labels".
    """
    p=subprocess.run([str(root/'bin/bd'),'--directory',str(path),'--sandbox','--actor',actor,'show',task,'--json'],env=environment(root),capture_output=True,text=True,encoding='utf-8',timeout=60)
    if p.returncode:raise ValueError('Could not read the current labels of %s before the label write, so the reserved-label guard cannot verify it: %s'%(task,(p.stderr or p.stdout).strip()))
    try:rows=json.loads(p.stdout)
    except (ValueError, RecursionError):raise ValueError('Could not parse the current labels of %s before the label write; refusing.'%(task,))
    if isinstance(rows,dict):rows=[rows]
    if not isinstance(rows,list):raise ValueError('Unexpected native read for %s; refusing the label write.'%(task,))
    matched={}
    for row in rows:
        if not isinstance(row,dict):continue
        rid=row.get('id')
        if not isinstance(rid,str):continue
        if rid==task or rid.endswith('.'+task) or rid.endswith('-'+task):
            matched.setdefault(rid,set(row.get('labels') or []))
    if len(matched)!=1:
        raise ValueError('Could not resolve %s to exactly one native issue before the label write (matched: %s), so the reserved-label guard cannot verify it; refusing. Pass the canonical issue id.'%(task,', '.join(sorted(matched)) or 'none'))
    rid,labels=next(iter(matched.items()))
    return rid,labels

def _native_comments(root,path,actor,task):
    """The comments of one canonical issue, for the record-anchor check."""
    p=subprocess.run([str(root/'bin/bd'),'--directory',str(path),'--sandbox','--actor',actor,'comments',task,'--json'],env=environment(root),capture_output=True,text=True,encoding='utf-8',timeout=60)
    if p.returncode:raise ValueError('Could not read the comments of %s before the label write, so the record-anchor guard cannot verify it; refusing.'%(task,))
    try:rows=json.loads(p.stdout)
    except (ValueError, RecursionError):raise ValueError('Could not parse the comments of %s before the label write; refusing.'%(task,))
    if not isinstance(rows,list):raise ValueError('Unexpected comment read for %s; refusing the label write.'%(task,))
    return rows

def _guard_reserved_labels(root,path,args,actor):
    """Read-before-write guard for the reserved label namespace.

    Refusing a reserved label *value* is not enough: bd copies parent labels
    onto `create --parent X` children, and `--set-labels`/`--remove-label`
    replace labels on an existing issue. Both are read first, under the lock
    the mutation will hold, so a reserved label (a coordination `request:`/
    `request-content:` label, or a controlled requirement type/state label)
    can neither reach a contributor-created issue nor be removed from an
    operator-created holder.
    """
    request=label_guard_request(args)
    if request is None:return
    if request['ambiguous']:
        raise ValueError('Refusing label-affecting request: the flags could not be resolved unambiguously, so the reserved request/request-content/requirement label namespace cannot be verified; no native write was attempted. Pass one explicit target (and one --parent) with no unknown flags and a valid --no-inherit-labels value.')
    if request['kind']=='inherit':
        canonical,labels=_native_labels(root,path,actor,request['target'])
        label=first_reserved_label(list(labels))
        if label is not None:
            raise ValueError('Refusing create --parent %s: the parent currently holds the reserved label %s, and bd copies parent labels onto a new child unless --no-inherit-labels is given, which would make a second holder of the coordination/requirement namespace (for example a child that inherits requirement:accepted without F3 acceptance evidence). Re-run with --no-inherit-labels, or use the coordination create-child workflow (coordination.py).'%(canonical,label))
        return
    for target in request['targets']:
        canonical,labels=_native_labels(root,path,actor,target)
        label=first_reserved_label(list(labels))
        if label is not None:
            raise ValueError('Refusing to replace labels on %s: it currently holds the reserved label %s, which only coordination.py and requirement_records.py may write. Replacing or removing it would silently drop the coordination namespace or an operator acceptance; use the coordination workflow (coordination.py) or the requirement command (requirement_records.py draft|revise, admin.py requirement-apply); --add-label remains available for ordinary labels.'%(canonical,label))
        # A record type label (reference/proposal/contribution-settings/capability) is
        # an ordinary label on an ordinary task, but on a real record anchor - one that
        # also holds a v1 record of the same family - its labels belong to the record
        # operations (kittrial-5bb.64): replacing or removing them is refused.
        row={'labels':sorted(labels)}
        if carries_record_label(row):
            row['comments']=_native_comments(root,path,actor,canonical)
            if is_record_anchor(row):
                raise ValueError('Refusing to replace labels on %s: it is a reference/proposal/settings/capability record anchor, whose labels only its record operations may change. --add-label remains available for ordinary labels.'%(canonical,))

def _native_anchor_rows(root,path,actor,tokens):
    """Resolve every named token to its canonical id and row, in ONE native read.

    `bd show ID... --json --include-comments` returns each row's labels and comments
    together, so the whole status guard costs one read per call however many ids the
    invocation names (kittrial-5bb.92 review item 1, one read per call). bd resolves an
    issue id by unambiguous suffix, so each requested token is matched against the
    returned rows; a token that resolves to zero or several rows fails closed, exactly
    as `_native_labels` does for one token.
    """
    p=subprocess.run([str(root/'bin/bd'),'--directory',str(path),'--sandbox','--actor',actor,
                      'show',*tokens,'--json','--include-comments'],env=environment(root),
                     capture_output=True,text=True,encoding='utf-8',timeout=60)
    if p.returncode:raise ValueError('Could not read %s before the status write, so the record-anchor guard cannot verify it: %s'%(', '.join(tokens),(p.stderr or p.stdout).strip()))
    try:rows=json.loads(p.stdout)
    except (ValueError, RecursionError):raise ValueError('Could not parse the current rows of %s before the status write; refusing.'%(', '.join(tokens),))
    if isinstance(rows,dict):rows=[rows]
    if not isinstance(rows,list):raise ValueError('Unexpected native read for %s; refusing the status change.'%(', '.join(tokens),))
    resolved={}
    for token in tokens:
        matched={}
        for row in rows:
            if not isinstance(row,dict):continue
            rid=row.get('id')
            if not isinstance(rid,str):continue
            if rid==token or rid.endswith('.'+token) or rid.endswith('-'+token):
                matched.setdefault(rid,row)
        if len(matched)!=1:
            raise ValueError('Could not resolve %s to exactly one native issue before the status write (matched: %s), so the record-anchor guard cannot verify it; refusing. Pass the canonical issue id.'%(token,', '.join(sorted(matched)) or 'none'))
        rid,row=next(iter(matched.items()))
        resolved[token]=(rid,row)
    return resolved

def _claims_the_row(args):
    """True when the bd invocation claims the row or makes it in_progress (kittrial-5bb.293).

    Flags are matched by PARSED argument, not substring: a ``--notes`` text that
    contains ``in_progress`` never triggers this (rev3).
    """
    args=list(args or [])
    if '--claim' in args or any(a.startswith('--claim=') for a in args):
        return True
    for i,a in enumerate(args):
        if a in ('--status','-s') and i+1<len(args) and args[i+1]=='in_progress':
            return True
        if a.startswith('--status=') and a.split('=',1)[1]=='in_progress':
            return True
    return False


def _guard_open_dependency_on_claim(root,path,actor,row):
    """Refuse a claim on a task blocked by an open dependency (kittrial-5bb.293 item 3a)."""
    from work import BLOCKING_DEPENDENCY_TYPES, dependency_index, dependency_ref, open_dependency_ids, open_dependency_sentence
    deps=[]
    ids=[]
    for d in row.get('dependencies') or []:
        other_id,kind,_=dependency_ref(d)
        if kind in BLOCKING_DEPENDENCY_TYPES and other_id:
            deps.append(d);ids.append(other_id)
    if not deps:return
    # Fail CLOSED: read the other ends in ONE native read; a missing one is open.
    completed=subprocess.run([str(root/'bin/bd'),'--directory',str(path),'--sandbox','--actor',actor,
                             'show',*ids,'--json'],env=environment(root),
                            capture_output=True,text=True,encoding='utf-8',timeout=60)
    try:
        others=json.loads(completed.stdout or '[]')
    except ValueError:
        others=[]
    if isinstance(others,dict):others=[others]
    by_id=dependency_index([r for r in others if isinstance(r,dict)])
    missing=[i for i in ids if i not in by_id]
    if missing:
        raise ValueError('Refusing to claim %s: it is blocked by an open dependency: `%s`. '
                         'Its status cannot be read, so the claim is refused.'
                         %(row.get('id'),'`, `'.join(missing[:5])))
    open_deps=open_dependency_ids(row,[r for r in others if isinstance(r,dict)],index=by_id)
    if open_deps:
        raise ValueError('Refusing to claim %s: %s'
                         %(row.get('id'),open_dependency_sentence(open_deps) or 'it is blocked by an open dependency'))


def _guard_record_anchor_status(root,path,args,actor):
    """Read-before-write guard: a status change never moves a record anchor.

    A reference/proposal/settings/capability anchor is created closed on purpose and is
    hidden from work by `is_record_anchor`; the raw bd path let a contributor `reopen`
    it (or `close` a forged open one), which the record operations own (kittrial-5bb.64,
    kittrial-5bb.92 item 4). Requirement and brd-section records are deliberately NOT
    covered: they stay visible as work items, so live projects claim, close, reassign
    and defer them through this endpoint exactly as on main. Keeping requirement records
    that are not work items out of the claimable pool is kittrial-5bb.97 (a
    not-a-work-item label), designed with the jjbp coordinator (kittrial-5bb.92 review
    item 1). Any flag that moves status or assignee counts, including the short `-s`
    spellings, `--claim`, `--defer` and `--assignee` (`status_change_targets`). Runs
    under the same coordination lock as the write it guards, reads every named target in
    ONE native read, and fails closed when the target cannot be resolved: bd would
    otherwise act on the last touched issue, which this guard cannot verify.
    """
    request=status_change_targets(args)
    if request is None:return
    command,targets=request
    if targets is None:
        raise ValueError('Refusing %s: name exactly the issue(s) to change; bd would otherwise act on the last '
                         'touched issue, whose record-anchor status cannot be verified.'%command)
    rows=_native_anchor_rows(root,path,actor,targets)
    for token in targets:
        canonical,row=rows[token]
        if is_record_anchor(row):
            raise ValueError('Refusing to %s %s: it is a reference/proposal/settings/capability record anchor, '
                             'whose status only its record operations may change.'%(command,canonical))
        # The merge slot is held and released only through `coordinate` (kittrial-5bb.113).
        if is_merge_slot(row):
            raise ValueError('Refusing to %s %s: %s. Its holder changes only through `coordinate`.'
                             %(command,canonical,merge_slot_sentence(canonical)))
        # A claim (or an assignee/status change that makes the row in_progress) on a
        # task blocked by an open dependency is refused, the same rule the HTTP claim
        # route and work.claimable() apply (kittrial-5bb.293 item 3a). Fails CLOSED
        # when the other ends cannot be read (item 3c).
        if command in ('claim', 'update') and _claims_the_row(args):
            _guard_open_dependency_on_claim(root,path,actor,row)

def _guard_record_anchor_title(root,path,args,actor,rows=None):
    """Read-before-write guard: the title of a record anchor is not changed through bd (kittrial-5bb.97).

    The kit finds a reference, proposal, settings or capability record by its anchor's
    title, so a renamed anchor is a record nobody finds again. The same rows as
    `_guard_record_anchor_status` protects. Requirement and brd-section records are worked
    as tasks and are renamed like tasks.

    ``rows`` are the rows `_guard_named_rows` has just read for this write (without their
    comments). An anchor carries a record type label, so only a row with such a label can
    be one, and only those are read again, with their comments, in one native read: a
    title change on ordinary rows costs no read beyond the one every write makes
    (kittrial-5bb.113). Without ``rows`` every named row is read.
    """
    targets=title_change_targets(args)
    if targets is None:return
    if targets=='unnamed':
        raise ValueError('Refusing update --title: name exactly the issue(s) to change; bd would otherwise act on the '
                         'last touched issue, which cannot be checked for a record anchor.')
    if rows is not None:
        targets=[row['id'] for row in rows if carries_record_label(row)]
        if not targets:return
    read=_native_anchor_rows(root,path,actor,targets)
    for token in targets:
        canonical,row=read[token]
        if is_record_anchor(row):
            raise ValueError('Refusing to update the title of %s: it is a reference/proposal/settings/capability record '
                             'anchor, and its title is how the kit finds it.'%canonical)

#: bd 1.2.2's structured answer when `show ID` resolved to no single row. bd prints it both
#: for an id that does not exist and for an id that is an ambiguous prefix of several; only
#: the stderr differs ("no issue found" against "ambiguous ID ... Use more characters to
#: disambiguate"). An exact id wins over bd's substring resolver (measured), so this answer
#: means no row has exactly the id asked for (kittrial-5bb.138).
BD_NO_MATCH_ERROR='no issues found matching the provided IDs'

def _bd_read(root,path,actor,argv,resolver_absence=False,strict=False):
    """One native read for a guard: ``(rows, stderr)``, or ``(None, why)`` when the read itself failed.

    A read that times out, exits non-zero for any reason but "no issue found", or prints
    something that is not JSON is a failure, never "nothing there": the write it guards
    is then refused. An empty answer (exit 0 and no output) is a failure too: bd prints
    ``[]`` for an empty JSON list, so an empty stdout means the read did not answer, and
    reading it as "no such id" let ``create --id NEW`` replace a row the read had not
    seen (kittrial-5bb.135, after review 01a10c0b).

    ``resolver_absence`` is for the ``create --id`` existence check: it additionally reads
    bd's own structured no-match answer as an absence, which is what makes the check exact.
    With ``p-abc`` and ``p-abd`` present, ``show p-ab`` answers rc 1 with that object and
    "ambiguous ID" on stderr, and no row has exactly ``p-ab``, so the create proceeds as it
    did before kittrial-5bb.135; any other non-zero exit still fails closed. The object means
    absent only at exactly rc 1 and only when ``resolver_absence`` asks for it: rc 0 with an
    error object is a failed read. A strict guard read also reads the object at rc 1 as an
    absence (kittrial-5bb.260 F1), only so that its refusal can name the token: every strict
    caller turns an absence into a refusal, so no write follows from it (kittrial-5bb.138
    items ``rc0`` and ``absence-pins``).
    """
    try:
        p=subprocess.run([str(root/'bin/bd'),'--directory',str(path),'--sandbox','--actor',actor,*argv],env=environment(root),capture_output=True,text=True,encoding='utf-8',timeout=60)
    except subprocess.TimeoutExpired:
        return None,'the read of the tasks timed out'
    said=(p.stderr or '').strip()
    if not (p.stdout or '').strip():
        if p.returncode and 'no issue found' in said:
            # kittrial-5bb.260 (mutants): this branch and the no-match-object branch
            # below are REDUNDANT layers by design - one answer shape comes on stderr
            # with no stdout, the other as the resolver's rc-1 object on stdout - and
            # nothing pins them separately because they fail closed together in the
            # native tests; a change to one must keep its sibling in step.
            return [],said                      # bd's own "that id does not exist" answer
        if p.returncode:
            return None,'bd could not read the tasks (%s)'%(said[-200:] or 'exit %d'%p.returncode)
        # Exit 0 with no output: bd prints `[]` for an empty JSON list, so this read did
        # not answer. Reading it as "no such id" accepted `create --id NEW` (kittrial-5bb.135).
        return None,'bd gave no answer (%s)'%(said[-200:] or 'exit %d'%p.returncode)
    try:answer=record_json.loads(p.stdout)
    except ValueError:return None,'bd gave an unreadable answer'
    resolver_answer=isinstance(answer,dict) and answer.get('error')==BD_NO_MATCH_ERROR
    # A structured error object is an answer only at the exit code bd uses for it (1 for the
    # resolver's no-match object). rc 0 with an error object, or the no-match object with any
    # other exit code, is a failed read: the rule is exactly "rc 1 and the exact no-match
    # object means absent, anything else refuses" (kittrial-5bb.138 item rc0).
    if isinstance(answer,dict) and 'error' in answer and p.returncode!=1:
        return None,'bd could not read the tasks (%s)'%(said[-200:] or 'exit %d'%p.returncode)
    rows=[answer] if isinstance(answer,dict) else answer
    if not isinstance(rows,list):return None,'bd gave an unreadable answer'
    if strict and any(not isinstance(row,dict) or not isinstance(row.get('id'),str) for row in rows):
        if resolver_answer and p.returncode==1:
            # kittrial-5bb.260 F1: bd's exact no-match object at rc 1 is an ABSENCE, not
            # a failed read - reading it as absence here (exactly as the create --id
            # existence check below does) lets _resolve_rows name the token in its
            # refusal instead of answering 'an incomplete answer'. The guarded write
            # still refuses; only the sentence changes.
            return [],said
        return None,'bd gave an incomplete answer'
    rows=[row for row in rows if isinstance(row,dict) and isinstance(row.get('id'),str)]
    if p.returncode and not rows and 'no issue found' not in said and not (resolver_absence and resolver_answer and p.returncode==1):
        return None,'bd could not read the tasks (%s)'%(said[-200:] or 'exit %d'%p.returncode)
    return rows,said

def _resolve_rows(root,path,actor,tokens,strict=False):
    """The rows bd resolves ``tokens`` to: ``(rows, problem)``.

    One native read for all of them. bd answers with the rows it found and names the
    others on stderr; when the count does not account for every token, each is read by
    itself so the refusal can name the one at fault. ``problem`` is set when a token
    does not resolve to exactly one row, or when a read failed.
    """
    # A token that starts with a dash would be read by bd as a flag of `show`; the lone dash is an id to bd.
    for token in tokens:
        if not isinstance(token,str) or not token.strip() or (token.startswith('-') and token!='-'):
            return [],'%s is not a task id'%shown_token(token)
    rows,said=_bd_read(root,path,actor,['show',*tokens,'--json'],strict=strict)
    if rows is None:return [],said
    if len({row['id'] for row in rows})==len(tokens) and 'no issue found' not in said:return rows,None
    found=[]
    for token in tokens:
        rows,said=_bd_read(root,path,actor,['show',token,'--json'],strict=strict)
        if rows is None:return [],said
        if len(rows)!=1:return [],'%s does not name exactly one task'%shown_token(token)
        found.append(rows[0])
    return found,None

def _guard_new_id(root,path,name,new_id,actor):
    """``create --id``: the id must be this project's, in bd's own lower-case shape, and must not exist.

    bd 1.2.2 answers ``create TITLE --id EXISTING`` with rc 0 and REPLACES that row: the
    title is the new one, description, acceptance criteria, notes and assignee are
    emptied, status goes back to open and priority to the default (measured; review
    01a10c0b). On the merge slot that also freed a held slot. An explicit id that does
    not exist stays allowed.

    The existence read is ``show``, because ``list --all --id`` does not see every row
    class: an ephemeral row (``create --ephemeral``) and a gate row are both absent from
    it, and ``create --id`` replaced the ephemeral row in the review (kittrial-5bb.135).
    ``show`` answers for both (measured on bd 1.2.2), so the check asks it and refuses on
    an EXACT id match only: bd resolves an id from any substring, and a row merely near
    the name is not the row ``create --id`` would replace. bd answers an id that is an
    ambiguous prefix of several rows with rc 1 and its structured no-match object, and an
    exact id wins over that resolver (measured), so ``resolver_absence`` reads the
    ambiguous answer as "no row has exactly this id" and lets the create proceed; with
    ``p-abc`` and ``p-abd`` present, ``create --id p-ab`` made the row before
    kittrial-5bb.135 and does again (kittrial-5bb.138). Ids are case-sensitive to bd, so
    the shape rule is what keeps a look-alike that differs only in case from being made.
    """
    shown=shown_token(new_id)
    if is_merge_slot_id(new_id) or any(is_merge_slot_id(new_id[:cut]) for cut,ch in enumerate(new_id) if ch=='.'):
        raise ValueError('Refusing create --id %s: that id belongs to a merge slot, an internal record. Nothing was written.'%shown)
    # bd files an id with a trailing dot or hyphen as a child of the row it looks like
    # (`VICTIM.` made a row of that exact id filed under VICTIM; kittrial-5bb.135), so the
    # last character must be a letter or digit.
    if not re.fullmatch(re.escape(name)+r'-[a-z0-9](?:[a-z0-9.-]{0,94}[a-z0-9])?',new_id):
        raise ValueError('Refusing create --id %s: an explicit id is %s-NAME in lower-case letters, digits, dots and hyphens, ending in a letter or digit. Nothing was written.'%(shown,name))
    rows,said=_bd_read(root,path,actor,['show',new_id,'--json'],resolver_absence=True)
    if rows is None:
        raise ValueError('Refusing create --id %s: could not check whether a task with that id exists (%s). Nothing was written.'%(shown,said))
    if any(row['id']==new_id for row in rows):
        raise ValueError('Refusing create --id %s: a task with that id exists, and bd would replace its title, description, status and assignee. Choose another id, or use update. Nothing was written.'%shown)

def _guard_named_rows(root,path,name,args,attachments,actor,strict=False):
    """Every contributor write must name the rows it writes, and none may be the merge slot.

    ``write_targets`` (reserved_comments) says which tokens of the command name rows and
    refuses the forms whose rows are not named: no id (bd would use the last touched
    row), ``close --claim-next`` and ``--continue`` (bd chooses the row), creation from a
    file. Every named token is then resolved through bd, because bd resolves an id from
    any substring of it; a token bd cannot resolve to one row, or a read that fails,
    refuses the write. Runs under the project's coordination lock, which every write
    through this endpoint holds, so no row named here is made or replaced by another
    such write between the read and the write. A host `bd` run outside the kit takes no
    such lock.
    """
    request=write_targets(args,attachments)
    if request is None:return
    command=request['command']
    if request['new_id'] is not None:_guard_new_id(root,path,name,request['new_id'],actor)
    if request['refusal']:
        raise ValueError('Refusing %s: %s. Nothing was written.'%(command,request['refusal']))
    if not request['targets']:return
    rows,problem=_resolve_rows(root,path,actor,request['targets'],strict=strict)
    if problem:
        # kittrial-5bb.260 F1: a RESOLUTION problem (a mistyped or ambiguous id) gets no
        # lifecycle advice - naming the token is the whole remedy for a typo; the advice
        # stays on every other strict problem, where the dedicated lifecycle action is.
        resolution=('does not name exactly one task' in problem or 'is not a task id' in problem)
        advice=(' '+raw_lifecycle.ADVICE) if strict and not resolution else ''
        refusal=raw_lifecycle.Refusal if strict else ValueError
        raise refusal('Refusing %s: %s, so the rows this would write are not known. Name each task by its id. Nothing was written.%s'%(command,problem,advice))
    slot=name+MERGE_SLOT_SUFFIX
    for row in rows:
        if row['id']==slot or is_merge_slot(row):
            if status_change_targets(args) is not None:
                # A claim, a close, a reopen, an assignment: the sentence these have had since the first delivery.
                raise ValueError('Refusing to %s %s: %s. Its holder changes only through `coordinate`.'
                                 %(command,row['id'],merge_slot_sentence(row['id'])))
            raise ValueError('Refusing %s on %s: %s. Nothing but `coordinate` writes it; its merge-create operation repairs a damaged slot.'
                             %(command,row['id'],merge_slot_sentence(row['id'])))
    return rows

def guarded_write(root,request,journal,effect,**options):
    """``run_guarded``, after the server's configuration has been read (kittrial-5bb.156).

    Every write that reserves an operation identity needs ``deployment.private.json``: bd
    takes its password from it. Read for the first time inside the guarded write, a file cut
    short or closed to this user left the operation "outcome unknown" with nothing written,
    and its idempotency key unusable until that expired (seen on real bd). Read here, it
    refuses the write with nothing done and nothing reserved. The password is asked for as
    well as the file: a file that parses and has none failed in the same place (seen on real
    bd too). Only these writes read it ahead of time: an action that needs nothing from the
    file is not stopped by its damage, and one that reads it on the way fails there, as it
    always did.
    """
    # Whatever is at the name, or nothing: a file that is missing, a directory, a dangling
    # link or a FIFO failed in the same place as a damaged one, after the reservation
    # (review of revision 2). No write of this kind works without the file.
    deployment_document(root/'deployment.private.json')
    deployment_password(root)
    return run_guarded(request,journal,effect,**options)

def configuration_fault(root,error):
    """Whether ``error`` is a failure to read the server's own configuration file.

    The kit's class for a file that is not JSON, not text or not an object; and an
    ``OSError`` that names that file (it cannot be opened: closed to this user, a directory,
    gone while the service runs).
    """
    if isinstance(error,ConfigurationUnreadable):return True
    if not isinstance(error,OSError) or isinstance(error,TimeoutError):return False
    try:return Path(os.fsdecode(error.filename))==Path(root)/'deployment.private.json'
    except (TypeError,ValueError):return False       # it names no file, or a descriptor

def tracker_actors(root,path,before=None,own=()):
    """The names this project's tracker already holds as an author or assignee, older than
    ``before`` (all of them when it is None); kittrial-5bb.188 item 1.

    One ``bd export --all`` for the project, through the same read ``admin.py
    credential-actors`` makes: a bd process that opens the project's database. That is the
    cost of judging a plain name by the rows it has, and it is paid where the rule is
    applied, never by every write (docs/HTTP_DEPLOYMENT.md says so). What depends on it:
    ``reserved_actors`` feeds the tracker names to the worker-credential name rule on every
    HTTP-path write (``descriptor_actor_denial``, kittrial-5bb.184/188) and to the
    ``actor-standing`` rows read the web service makes when it issues a credential, so a
    read that fails blocks every write under a plain name and every issue, for the whole
    project.

    Rows are parsed with the row bound of kittrial-5bb.141 (``record_json.loads_rows``,
    ``ROW_NESTING_MAX`` 750), not the 64-level record-comment guard that used to refuse
    this read on one row nested 65 levels (kittrial-5bb.221): a row nested up to 750
    levels parses normally and its own author, assignee and comment names count. ANY row
    that cannot be parsed -- unparseable text, deeper than 750, a line cut short -- still
    refuses the whole read exactly as before: an unreadable row may be the row that holds
    the name, and a name the tracker might hold must not become issuable or writable
    through a worker credential (revision-2 review item 1: a comment holding U+0085, or
    751-level metadata, hid a row's author and a credential under that name was issued and
    wrote). A marked row never counts as the project's merge slot: its id can be recovered
    from text nobody has read, and the whole-read proof must rest on a row that was read.

    A tracker that cannot be read raises ``actor_names.TrackerUnreadable`` -- a bd that
    exits nonzero, or text that is not rows at all --, a read that came back holding a row
    that cannot be parsed raises ``actor_names.TrackerRowsUnreadable`` (its own sentence
    naming the row ids and the operator repair, never "try again shortly"; kittrial-5bb.243
    item N7), and a read that came back without the
    project's merge slot raises ``actor_names.TrackerMergeSlotMissing``: every project this
    kit makes holds that slot, and a missing slot is what the merge-create operation
    repairs, so it must not be answered as the transient fault "try again shortly"
    (kittrial-5bb.188 review of item 1, revision-3 item 3; kittrial-5bb.202 item 1). A read
    that answered no rows at all, the plain ``bd init`` shape, is one of the missing-slot
    cases, not a failed read: bd exited 0 and said nothing, so the tracker was read and the
    absent thing is the slot row (kittrial-5bb.202 review `documents-say-the-old-answer`;
    the empty-project decision is recorded in the lane plan and docs/HTTP_DEPLOYMENT.md).
    A project whose metadata records no Dolt server coordinates is not read at all: bd would
    fall back to an embedded database there, so that answer is the same host fault, never the
    empty-project decision (kittrial-5bb.202 rev-3 item 2, review F2).
    ``own`` are the lifetimes of earlier credentials of the same name whose rows are not
    held against this one (item 3)."""
    import actor_names
    from admin import run_bd
    # The SAME guard ``admin.project_merge_slot_state`` uses, for the same reason: without the
    # Dolt server coordinates recorded in ``.beads/metadata.json`` bd falls back to an EMBEDDED
    # database, creates ``.beads/embeddeddolt`` and exits 0 having printed nothing. That answer
    # is not this project's tracker at all, so it is the host fault and never the empty-project
    # missing-slot answer: reading it as "no rows at all" answered 503 merge_slot_missing and
    # advised a merge-create that then fails (kittrial-5bb.202 review of revision 2, F2).
    try:
        metadata=json.loads((path/'.beads'/'metadata.json').read_text(encoding='utf-8'))
    except (OSError,ValueError,UnicodeError):
        metadata=None
    coordinates=('dolt_server_host','dolt_server_port','dolt_server_user','dolt_database')
    if not isinstance(metadata,dict) or not all(metadata.get(key) for key in coordinates):
        raise actor_names.TrackerUnreadable()
    try:
        text=run_bd(root,path.name,['export','--all'])
        rows=record_json.loads_rows(text)
    except (subprocess.SubprocessError,OSError,ValueError,RecursionError):
        # bd could not answer, or answered something that is not rows: a host fault, never an
        # empty tracker and never a rejection of the caller's request.
        raise actor_names.TrackerUnreadable()
    offending=[row for row in rows if not isinstance(row,dict) or row.get('malformed')]
    read_any_row=any(isinstance(row,dict) and not row.get('malformed') for row in rows)
    if offending and read_any_row:
        # One unreadable row refuses the whole read (not only its own names): the row may be
        # the one that holds the name, and an unreadable row's id never proves the slot
        # (kittrial-5bb.221). TrackerRowsUnreadable, not the bare fault: the row will not
        # read again until an operator repairs it, so the answer names the row ids and the
        # repair instead of "try again shortly" (kittrial-5bb.243 item N7).
        raise actor_names.TrackerRowsUnreadable(
            row.get('id') if isinstance(row, dict) else None for row in offending)
    if offending or (not rows and text.strip()):
        # Nothing rows-like came back at all: text that is not rows, the same failed read,
        # not an empty tracker and not a row an operator can repair (kittrial-5bb.202 rev-3
        # item 4: the transient fault, never the missing-slot answer).
        raise actor_names.TrackerUnreadable()
    if not any(is_merge_slot(row) for row in rows):
        # Zero rows (a plain `bd init`: the read succeeded and the tracker holds nothing,
        # so what is absent is the merge-slot row) or rows without the slot row: only an
        # operator's merge-create puts it back (kittrial-5bb.202 item 1).
        raise actor_names.TrackerMergeSlotMissing()
    return actor_names.tracker_names(actor_names.tracker_marks(rows),before,own)

def reserved_actors(root,path,rows=False,own=()):
    """The names a worker credential's namespace may not be, as this host has them: the
    project's registered session actors, the installation's operator and verifier lists, and
    (only when ``rows`` asks for it) the project's tracker rows.

    ``rows`` is False (no tracker read, the cheap rule), True (every row, for a name being
    issued) or the instant the credential was issued, so that a credential's own rows are
    not held against it. ``own`` are the earlier same-name credentials' lifetimes whose rows
    are not held either (kittrial-5bb.188 item 3)."""
    from sessions import registered_actors
    names={'sessions':registered_actors(path),'operators':sorted(configured_operators(root)),
            'verifiers':sorted(configured_verifiers(root))}
    if rows:
        names['authors']=sorted(tracker_actors(root,path,None if rows is True else rows,own))
    return names

#: The actions that exist only for the web service: they name no existing project.
#: ``host-coordination`` and ``host-projects`` are reads for the web interface (kittrial-5bb.198,
#: host_reads.py): the operator list, the project's session names and the runtime's projects,
#: which a bound key is not told.
SERVICE_ONLY_ACTIONS=('create-project','project-creations','creation-standing','web-merge-slot','web-lifecycle','web-guidance',
                      'host-coordination','host-projects','assignment-roster','task-assign','task-release')

def key_project_refusal(request,key_projects):
    """Refuse, for a key bound to projects, a request that is not for one of them.

    Rule 1 of docs/COORDINATORS_PER_PROJECT_DESIGN.md (kittrial-5bb.193). ``key_projects``
    comes from the endpoint's own launch flags (``--key-project``, which only the forced
    command of an authorized_keys line passes) and is None for every other caller, who is
    not looked at. It is asked before anything else in ``execute``: nothing of another
    project is read, and the answer for another project is the answer for a project that
    does not exist, so a bound key cannot tell the two apart.
    """
    if key_projects is None:return
    action=request.get('action') if isinstance(request,dict) else None
    if action in SERVICE_ONLY_ACTIONS:
        raise ValueError('%s is available only to the web service'%action)
    project=request.get('project') if isinstance(request,dict) else None
    if not isinstance(project,str) or project not in key_projects:
        raise ValueError('Unknown/uninitialized project')

def lanes_scope_for(actor,operators,key_principal,owners=None):
    """The lanes read's route isolation (kittrial-5bb.263): ``'all'`` or ``'own'``.

    The host route (no bound key: ``key_principal`` is None) and a key bound to an operator
    may see every lane; any other bound key is a contributor route and sees only its own
    lane's row. What counts as an operator is the kit's ONE definition,
    ``review_workflow.listed_operator_actor``: an actor on the installation's operator
    allowlist, or an actor bound to a listed operator's principal (kittrial-5bb.263
    review ``operator-definition-and-web-scope``). ``owners`` is that actor -> principal
    map, or a callable returning it; a plain listed name is decided without reading it, and
    ``owners=None`` is judged on the allowlist alone. A read must not fail on a damaged
    session registry, so a loader failure falls back to the plain name. A member of another
    project is refused earlier, by ``key_project_refusal``, before any read at all. One
    function, so the scope cannot be computed one way for one caller and another way for
    the next.
    """
    if key_principal is None:
        return 'all'
    from review_workflow import listed_operator_actor
    try:
        allowed=listed_operator_actor(actor,operators,owners)
    except (ValueError,OSError):
        allowed=actor in set(operators or ())
    return 'all' if allowed else 'own'

def key_principal_refusal(request,path,key_principal):
    """Refuse, for a key bound to a principal, a request whose actor that principal does not own.

    Rule 2 of docs/COORDINATORS_PER_PROJECT_DESIGN.md (kittrial-5bb.194). ``key_principal``
    comes from the endpoint's own launch flags (``--key-principal``, which only the forced
    command of an authorized_keys line passes) and is None for every other caller, who is
    not looked at. A session registration is the one exception: it makes the new actor the
    key's principal's, so it is answered before that actor exists (sessions.execute writes
    the entry). Every other action must name an actor the project's registry gives to this
    principal; an actor with no entry has no principal, so a bound key cannot act as it.
    """
    if key_principal is None:return
    action=request.get('action') if isinstance(request,dict) else None
    args=request.get('args') if isinstance(request,dict) else None
    if action=='session' and isinstance(args,list) and args[:1]==['register']:return
    actor=request.get('actor') if isinstance(request,dict) else None
    from sessions import owners
    owned=owners(path)
    if not isinstance(actor,str) or owned.get(actor)!=key_principal:
        raise ValueError('This key is bound to principal %s and may act only as actors that principal '
                         'registered in this project; %s is not one of them'
                         % (key_principal,actor if isinstance(actor,str) and actor else repr(actor)))

#: The acceptance commands a confined coordinator may run through the endpoint (slice 3 of
#: docs/COORDINATORS_PER_PROJECT_DESIGN.md, kittrial-5bb.195). They are the host commands of
#: admin.py that rest on the operator allowlist, reachable here only because a bound key makes
#: the actor the server's fact instead of the caller's own word (rules 1 and 2, kittrial-5bb.193
#: and .194). Voiding, reverting, reconciling, backups, retiring and the rollout switches are
#: deliberately absent (James's answer to question 5, 2026-10-07).
COORDINATOR_COMMANDS=('guidance-set','guidance-clear','guidance-status','reference-apply',
                      'capability-apply','capability-verify','proposal-review','proposal-decide',
                      'handoff','set-onboarding')
#: The coordinator commands that only read (no journal, no server time; the read takes the
#: project's coordination lock, exactly as the host command does).
COORDINATOR_READS=('guidance-status',)
#: The coordinator commands whose attachment is plain text rather than a JSON payload.
COORDINATOR_TEXTS=('guidance-set','set-onboarding')
#: The payload ``operation`` values ``reference-apply`` and ``capability-apply`` may carry
#: through this route (review of 958e883, item 1; the owner decision of 2026-10-09 on
#: kittrial-5bb.238). ``accept`` is the ONLY one: a record reaches this route only after
#: somebody proposed it as a draft, so every acceptance has a recorded proposal before it (two
#: attributed steps; that proposer and accepter differ is NOT checked here). ``draft`` - the
#: library's direct accepted revision 1, which
#: creates a key that did not exist already accepted, on one party's own word - is refused
#: here and stays with the installation operator on the host (``admin.py reference-apply`` /
#: ``admin.py capability-apply``). The host command's operator route also allows ``retire`` on
#: a capability, the only route that withdraws an accepted entry, and ``propose``/``revise``
#: are the contributor route; both stay off this surface. ``incorporated`` is not an
#: entry-apply operation at all: it is a proposal disposition state reached through
#: ``proposal-review``/``proposal-decide``, so it is neither allowed nor needed here.
COORDINATOR_APPLY_OPERATIONS=('accept',)

def onboarding_directory_refusal(target):
    """The one sentence both ``ONBOARDING.md`` directory checks answer with.

    The pre-check and the refusal inside ``coordinator_effect`` have to say the same thing, and
    say it the same way, because either can answer the same request depending on nothing the
    caller can see: whether a directory was put there before or after the project lock was
    waited for (kittrial-5bb.259 item 1).
    """
    return ValueError('Project onboarding cannot be written: %s is a directory, not a file. '
                      'Remove the directory, or write the document by hand on the host. '
                      'Nothing was written.'%target.name)

def coordinator_refusal(request,root,key_principal):
    """Refuse the coordinator acceptance commands unless the caller is a key bound to a principal
    whose actor is on this installation's operator list (or, for ``capability-verify``, verifiers).

    Slice 3 of docs/COORDINATORS_PER_PROJECT_DESIGN.md. ``key_principal`` is passed only by the
    forced command of a bound authorized_keys line (kittrial-5bb.194); the actor has already
    been checked to be one the project's registry gives to that principal, so the operator
    allowlist is asked of a name the server chose, not of a name the caller typed. An
    installation that configures nothing sends no ``--key-principal``: this surface is then
    unreachable and every other action behaves exactly as it did.
    """
    if key_principal is None:
        raise ValueError('These are the acceptance commands of a confined coordinator: they need an SSH key '
                         'bound to a principal (--principal on its authorized_keys line), so that the actor is '
                         'the server\'s fact and not the caller\'s word. This request names no principal, so '
                         'nothing was changed; run the matching admin.py command on the host instead.')
    args=request.get('args') if isinstance(request,dict) else None
    command=args[0] if isinstance(args,list) and args and isinstance(args[0],str) else None
    actor=request.get('actor') if isinstance(request,dict) else None
    # The verifiers list may also record a capability check, as the host command allows.
    if command=='capability-verify' and actor in configured_verifiers(root):return
    from keyed_records import require_configured_operator
    require_configured_operator(actor,configured_operators(root),
                                'run %s through the endpoint'
                                %(command if command else 'a coordinator command'))

def coordinator_operation_refusal(command,where,operation,in_batch=False):
    """One sentence for a payload operation this route does not carry (kittrial-5bb.238).

    ``where`` says where the operation was found: ``carries`` for the single payload, or
    ``items[2] carries`` for one item of a batch. ``draft`` gets its own sentence, because it
    is the one operation a caller may expect from the host command: through this route a
    record is accepted only after somebody proposed it, so the direct accepted revision 1
    (a key created already accepted, on one party's own word) stays on the host. A batch item
    carries no operation of its own whatever the value - the batch itself is the acceptance -
    and that has its own sentence too.
    """
    if operation=='draft':
        return ('coordinator %s %s operation %r: a new record is proposed first (capability propose / '
                'ref propose) and then accepted, or created directly by the installation operator on the '
                'host (admin.py %s with operation draft). Nothing was written.'
                %(command,where,operation,command))
    if in_batch:
        return ('coordinator %s %s operation %r; a batch item carries no operation of its own - the batch '
                'itself is the acceptance, and %s is the only operation this route carries. Nothing was '
                'written.'%(command,where,operation,', '.join(COORDINATOR_APPLY_OPERATIONS)))
    return ('coordinator %s %s operation %r; this route carries only %s. Nothing was written.'
            %(command,where,operation,', '.join(COORDINATOR_APPLY_OPERATIONS)))

def coordinator_apply_refusal(payload,command):
    """Refuse a payload operation ``reference-apply``/``capability-apply`` is not meant to carry.

    The check is on the PAYLOAD, before the library is called, for the single form and the
    ``items`` batch alike, so a payload can never carry an operation the route does not name
    (review of 958e883, item 1: ``operation: retire`` reached ``apply_native(operator=True)``
    and superseded an accepted capability). ``accept`` is the only operation carried here (the
    owner decision of 2026-10-09 on kittrial-5bb.238): ``draft``, the direct accepted
    revision 1, is refused with its own sentence. The host commands are unchanged.
    """
    operation=payload.get('operation','accept')
    if operation not in COORDINATOR_APPLY_OPERATIONS:
        raise ValueError(coordinator_operation_refusal(command,'carries',operation))
    items=payload.get('items')
    if isinstance(items,list):
        for index,item in enumerate(items):
            if isinstance(item,dict) and 'operation' in item:
                raise ValueError(coordinator_operation_refusal(command,'items[%d] carries'%index,
                                                               item.get('operation'),in_batch=True))

def coordinator_payload(args,request):
    """The text of the one attachment a coordinator command carries, or None.

    The client transports a local file as ONE token ``@attachment:N`` with the flag kept on
    the attachment (``client.py`` ``_attachments``), exactly as it does for every other write,
    so the server never reads a path out of the request (review of 958e883, item 2: this used
    to demand the two tokens ``--file @attachment:N`` the client never sends). The two-token
    form is still accepted for a hand-built request, and a token that is neither is refused
    without ever being read as a path. ``args`` is everything after the subcommand.
    """
    if not args:return None
    if len(args)==1:
        flag=None;token=args[0]
    elif len(args)==2 and args[0] in ('--file','-f'):
        flag=args[0];token=args[1]
    else:
        raise ValueError('Use --file with a local JSON or text file; the server reads no file path')
    attachments=request.get('attachments') if isinstance(request,dict) else None
    item=None
    if isinstance(attachments,dict) and isinstance(token,str) and token.startswith('@attachment:'):
        item=attachments.get(token.partition(':')[2])
    if (not isinstance(item,dict) or not isinstance(item.get('text'),str)
            or item.get('flag') not in ('--file','-f')):
        raise ValueError('Invalid attachment: a coordinator command needs --file with one local JSON or '
                         'text file, carried as a single @attachment token; the server reads no file path')
    if flag is not None and item.get('flag')!=flag:
        raise ValueError('Invalid attachment: the flag on the attachment does not match the request')
    return item['text']

#: The endpoint's ONE table of read-only actions (kittrial-5bb.281 review item 2). An
#: action named here holds the project's ``.coordination.lock`` SHARED; every action
#: NOT named here holds it EXCLUSIVE, and a failure to classify is exclusive too. The
#: value is a predicate ``(args, operation) -> bool`` because three actions decide from
#: their arguments or their inner payload operation, not from the action name alone.
#: ``history`` is read-only even though it may CREATE ``.history-snapshots/<digest>.json``:
#: that cache is content-addressed and written through ``coordination.atomic``, whose
#: temporary is now unique per writer, so a lost rename race to identical bytes is
#: success and two first readers no longer collide (item 1). Every OTHER action must be
#: proved read-only before it joins this table; the endpoint lock test walks every action
#: and fails for a write that is not classified exclusive.
def _read_only_always(args,operation):
    return True

def _web_guidance_read_only(args,operation):
    from web_guidance import READ_OPERATIONS
    return operation in READ_OPERATIONS

READ_ONLY_ACTIONS={
    'brief':_read_only_always,
    'history':_read_only_always,
    'requirements':_read_only_always,
    'work':_read_only_always,
    # kittrial-5bb.285: the blocking wait's per-poll read. It is one work read (one export)
    # and nothing is written, so it takes the project lock SHARED like the work read itself.
    'wait':_read_only_always,
    'bd':lambda args,operation:not is_mutating_invocation(args),
    'web-lifecycle':lambda args,operation:operation=='read',
    'web-merge-slot':lambda args,operation:operation=='check',
    'web-guidance':_web_guidance_read_only,
    # kittrial-5bb.291 item 1: the roster read takes the project lock shared through
    # the one table, not a hand-coded LOCK_SH at its own call site. task-assign is
    # deliberately absent: it is a write and stays exclusive.
    'assignment-roster':_read_only_always,
}

def coordination_lock_shared(request,action=None,args=None,operation=None):
    """Whether THIS request may hold the project's ``.coordination.lock`` SHARED.

    The one decision point for every lock site in ``execute`` (kittrial-5bb.281 review
    item 2). An action outside :data:`READ_ONLY_ACTIONS` is exclusive, as is any request
    that carries an ``operation_id``: an operation identity is a reservation in the
    shared operation journal, and on main the exclusive project lock serialized it, so
    keeping it exclusive makes concurrent reads that share one id behave as they did
    there instead of answering "Operation identity reserved; outcome unknown".
    """
    action=action if action is not None else request.get('action','bd')
    args=request.get('args',[]) if args is None else args
    if request.get('operation_id'):return False
    rule=READ_ONLY_ACTIONS.get(action)
    return bool(rule(args,operation)) if rule is not None else False

@contextlib.contextmanager
def held_coordination_lock(path,shared=False):
    """Hold the project's ``.coordination.lock``, SHARED for a read and EXCLUSIVE for a write.

    The endpoint used to take the lock EXCLUSIVELY for reads too, so every web page
    request queued behind every other request in the project (kittrial-5bb.281). A
    shared lock still excludes a writer and is excluded by one, so a read can never
    observe a write half done - the invariant every read here was given the lock for;
    it only lets two reads run at the same time. ``shared`` comes from
    :func:`coordination_lock_shared` alone, so one action cannot be classified one way
    at one call site and another way at the next.
    """
    with (path/'.coordination.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
        yield


def execute(root,request,authority_config=None,require_authority=False,key_projects=None,key_principal=None):
    key_project_refusal(request,key_projects)
    # Rule 2: a key bound to a principal is refused the web-only actions here, before their
    # name is looked at. The gate proper needs the project's registry and is asked below,
    # after the project is known; this structural refusal means no action at all is answered
    # before the gate, so an action added above it later cannot slip past (kittrial-5bb.194
    # review, mutant N1; slice 1 does the same for a key bound to projects).
    if key_principal is not None:
        answered=request.get('action') if isinstance(request,dict) else None
        if answered in SERVICE_ONLY_ACTIONS:
            raise ValueError('%s is available only to the web service'%answered)
    # Slot requests bind the block before project, tracker, or authority reads.
    # The shared table is the sole endpoint action/operation capability mapping.
    if request.get('action') in ('coordinate','web-merge-slot','web-lifecycle','web-guidance','task-assign','assignment-roster','task-release'):
        denied=block_mismatch_denial(request,authority_config)
        if denied is not None:return denied
    # Two actions exist only for the web service and name no existing project
    # (kittrial-5bb.118 part 2); project_creation holds them, with what stops other callers.
    if request.get('action')=='create-project':
        import project_creation
        project_dir(root,request.get('project'))          # the name's shape, before anything else
        return project_creation.create_action(root,request,authority_config)
    if request.get('action')=='project-creations':
        import project_creation
        return project_creation.list_action(root,request,authority_config)
    if request.get('action')=='host-projects':
        import host_reads
        return host_reads.projects(request,root)
    if request.get('action')=='creation-standing':
        import project_creation
        return project_creation.standing_action(root,request,authority_config)
    name=request['project'];path=project_dir(root,name)
    if not (path/'.beads/metadata.json').is_file():raise ValueError('Unknown/uninitialized project')
    # A creation the web interface started and that has not finished is not a project yet
    # (kittrial-5bb.118 part 2, review 01a109cc): it has no backup target, merge slot or first
    # backup, so nothing is served from it and the web service cannot register it.
    import project_creation
    unfinished=project_creation.unfinished(root,name)
    if unfinished:raise ValueError('Unknown/uninitialized project: '+unfinished)
    actor=request.get('actor','')
    refuse_http_actor(actor,authority_config is not None)
    # Rule 2 (kittrial-5bb.194): a key bound to a principal acts only as that principal's
    # actors in this project. Asked before any action runs; the registry is the only source.
    key_principal_refusal(request,path,key_principal)
    # Launched by the HTTP service, an HTTP-shaped actor still needs the verified
    # descriptor on every action, with or without --require-authority (review 01a10262).
    denied=http_actor_denial(request,authority_config)
    if denied is not None:return denied
    # And a name WITHOUT that shape, sent by the web service with a descriptor, is written only
    # by a worker credential inside a namespace that is nobody else's (kittrial-5bb.184).
    denied=descriptor_actor_denial(request,authority_config,lambda rows=False,own=():reserved_actors(root,path,rows,own))
    if denied is not None:return denied
    # The block must be FOR this request (kittrial-5bb.261): the project it names is this
    # project, and the capability it names is the one this action needs. Before any action.
    denied=block_mismatch_denial(request,authority_config)
    if denied is not None:return denied
    if request.get('action')=='session':
        from sessions import execute as session_execute
        args=request.get('args',[])
        if not isinstance(args,list) or any(not isinstance(a,str) or '\0' in a for a in args):raise ValueError('Expected argument list')
        if args[:1]!=['register'] and not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.@/-]{0,95}',actor):raise ValueError('Supply a session actor')
        session_warnings=[]
        def export():
            stdout,warnings=native.split(native.run(native.argv(root,path,actor,['export','--all'],scoped=False),environment(root)))
            if warnings:session_warnings.append(warnings)
            return stdout
        with (path/'.coordination.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            result=session_execute(path,name,args,export,actor=actor,principal=key_principal)
        result['provenance'] = {'kit': report(Path(__file__).resolve().parent, 'kit')}
        answer={'returncode':0,'stdout':json.dumps(result,ensure_ascii=False)+'\n','stderr':''.join(session_warnings)}
        # The session writes are not guarded writes; they carry the server's time all the same
        # (kittrial-5bb.97). show and run status only read.
        # A request that is already recorded writes nothing (`reconciled`) and carries none.
        writes=args[:1] in (['register'],['resume']) or (args[:1]==['run'] and args[1:2] in (['start'],['heartbeat'],['end']))
        if writes and result.get('reconciled') is not True:stamp_write(answer)
        return answer
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.@/-]{0,95}',actor):raise ValueError('Supply a short contributor/session actor')
    action=request.get('action','bd')
    if action in ('handoff','review','work','wait'):
        from work import execute as work_execute
        args=request.get('args',[])
        if not isinstance(args,list) or any(not isinstance(x,str) or '\0' in x for x in args):raise ValueError('Expected argument list')
        run_warnings=[]
        def run(argv):
            stdout,warnings=native.split(native.run(native.argv(root,path,actor,argv),environment(root)))
            if warnings:run_warnings.append(warnings)
            return stdout
        # The instrumented runner is the work effect's only route to native state, so
        # a validation refusal raised before any write is provably pre-effect.
        runner=NativeRunner(run, project=path)
        # A malformed review_workflow_writes value reads as OFF and warns instead of
        # failing every work/review action (kittrial-5bb.110 item 2).
        switch_warnings=[]
        review_writes=configured_review_writes(root,warnings=switch_warnings)
        # kittrial-5bb.284: the direction readers must not report an outstanding direction
        # that no reader could ever disposition. The installation switch is read here, once,
        # exactly as review_workflow_writes is.
        from briefing import checkpoint_writes_enabled
        provenance_writes=checkpoint_writes_enabled(root)
        rules=configured_coordinator_rules(root,warnings=switch_warnings)
        run_warnings.extend(switch_warnings)
        # The principal rule of slice 7 and the party comparison of rule 3 both need the
        # project's actor -> principal map, and only when a rule that consults it is on:
        # an installation that configures nothing reads no registry here. Advisory
        # reads retain name comparison on failure; independent-approval writers
        # must load ownership strictly, even for a directly listed operator.
        registry_rule='coordinator_writes' if rules['coordinator_writes'] else 'independent_approval'
        if rules['coordinator_writes'] or rules['independent_approval']:
            owners=owner_loader(path,registry_rule)
        elif action=='review':
            # The party rule of attest/hold/release-hold is not switchable
            # (kittrial-5bb.10 review item 2): a review action always carries the lazy
            # map, and a damaged registry is reported by the write ("parties could not
            # be compared") instead of refusing. Nothing else reads it while the rules
            # are off.
            owners=party_owner_loader(path)
        else:
            owners=None
        # The lanes read's route isolation (kittrial-5bb.263): the host route and a key
        # bound to an operator may see every lane; any other bound key sees only its own
        # row. `configured_operators` is read once here and passed on unchanged.
        # "Operator" uses the kit's ONE definition (`review_workflow.listed_operator_actor`):
        # a listed name, or an actor bound to a listed principal. `owners` is the map the
        # rules already loaded; on a bound-key route that does not need a rule, it is read
        # advisory here (key_principal_refusal already reads the same registry for a bound
        # key), and a failure falls back to the plain name inside `lanes_scope_for`.
        #
        # Merge note (16e68c7): the web-approval fact is no longer computed inline here as
        # `web_approval`; that branch verified it with `verified_web_authority(...)` and
        # passes that boolean, so this side keeps only its own lanes isolation.
        operators=configured_operators(root)
        lanes_owners=owners
        if lanes_owners is None and key_principal is not None:
            def load_lanes_owners():
                import sessions
                return sessions.owners(path)
            lanes_owners=load_lanes_owners
        lanes_scope=lanes_scope_for(actor,operators,key_principal,lanes_owners)
        def work_effect():
            result=work_execute(path,actor,action,args,request.get('attachments',{}),runner,
                                operators=operators,
                                verifiers=configured_verifiers(root),
                                review_writes=review_writes,
                                provenance_writes=provenance_writes,
                                independent_approval=rules['independent_approval'],
                                coordinator_writes=rules['coordinator_writes'],
                                owners=owners,
                                verified_web_authority=verified_web_authority(request,authority_config,CAP_APPROVE,name),
                                lanes_scope=lanes_scope)
            # A handoff request and a decline are recorded in the kit's handoff journal and move
            # nothing in bd, so the runner saw no write; they are writes all the same (review
            # of kittrial-5bb.97). One that was already recorded (`reconciled`) wrote nothing.
            if action=='handoff' and len(args)==2 and isinstance(result,dict) and result.get('reconciled') is False:runner.wrote=True
            return {'returncode':0,'stdout':json.dumps(result,ensure_ascii=False,indent=2)+'\n','stderr':warning_text(run_warnings)}
        with held_coordination_lock(path,shared=coordination_lock_shared(request,action,args)):
            return guarded_write(root,request,journal_path(path),work_effect,
                               authority_config=authority_config,
                               require_authority=require_authority,runner=runner)
    if action=='requirements':
        from requirement_http import read as read_requirements
        run_warnings=[]
        def run(argv):
            stdout,warnings=native.split(native.run(native.argv(root,path,actor,argv),environment(root)))
            if warnings:run_warnings.append(warnings)
            return stdout
        with held_coordination_lock(path,shared=coordination_lock_shared(request,'requirements',request.get('args',[]))):
            result=read_requirements(path,name,request.get('args',[]),run,configured_operators(root))
        return {'returncode':0,'stdout':json.dumps(result,ensure_ascii=False)+'\n','stderr':''.join(run_warnings)}
    if action=='owner-requirements':
        from requirement_http import web_action
        run_warnings=[]
        def run(argv):
            stdout,warnings=native.split(native.run(native.argv(root,path,actor,argv),environment(root)))
            if warnings:run_warnings.append(warnings)
            return stdout
        runner=NativeRunner(run, project=path)
        with (path/'.coordination.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            answer=web_action(root,path,name,request,authority_config,runner,guarded_write)
        answer['stderr']=answer.get('stderr','')+''.join(run_warnings)
        return answer
    if action=='set-onboarding':
        # An owner sets the project's onboarding text from the web interface
        # (kittrial-5bb.118 part 2). Service-only; see onboarding.web_action.
        from onboarding import web_action
        with (path/'.coordination.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            return web_action(path,name,request,authority_config)
    if action in ('onboard','docs'):
        from onboarding import execute as onboard
        return {'returncode':0,'stdout':onboard(Path(__file__).resolve().parent,path,name,actor,action,request.get('args',[]),endpoint=Path(__file__).resolve()),'stderr':''}
    if action=='actor-standing':
        # Read-only (kittrial-5bb.184): for each name asked about, why a worker credential may
        # not write under it, or null. The web service asks before it issues one and when it
        # lists them. The answer says which rule, never the host's names. No lock, no write.
        # With ``tracker`` set it also reads the project's rows, which is one bd export: the
        # service asks for that only at issue (kittrial-5bb.188 item 1), and only the service
        # may ask (item 6): the flag is a launch-argument service, so a caller over SSH (no
        # authority store) cannot make the endpoint read a tracker by sending it.
        import actor_names
        names=request.get('args',[])
        if not isinstance(names,list) or not 1<=len(names)<=200 or any(not isinstance(n,str) or not 0<len(n)<=96 or '\0' in n for n in names):
            raise ValueError('Use actor-standing with 1 to 200 names')
        tracker=request.get('tracker')
        if tracker and authority_config is None:
            raise ValueError('actor-standing with rows is for the web service only; nothing was changed')
        own=request.get('own') if tracker else None
        own=own if isinstance(own,list) else ()
        rows=tracker if isinstance(tracker,str) else bool(tracker)
        reserved=reserved_actors(root,path,rows,own)
        return {'returncode':0,'stdout':json.dumps({'schema_version':1,'names':{n:actor_names.collision(n,**reserved) for n in names}})+'\n','stderr':''}
    if action=='host-coordination':
        # Read-only (kittrial-5bb.198): the operator list and the project's sessions with their
        # names and principals, for the web interface. No lock, no write; a web-service-only action.
        import host_reads
        return host_reads.coordination(request,path,configured_operators(root))
    if action=='setup-status':
        # Read-only (kittrial-5bb.118): what the host knows about this project's setup,
        # for the web setup page. States, versions and times only; never guidance or
        # onboarding text. It takes no lock and writes nothing.
        if request.get('args',[]) not in ([],None):raise ValueError('Use setup-status without arguments')
        from admin import project_setup_status
        return {'returncode':0,'stdout':json.dumps(project_setup_status(root,name))+'\n','stderr':''}
    if action=='web-guidance':
        from web_guidance import request_operation, web_action
        args=request.get('args',[])
        operation=request_operation(request)
        with held_coordination_lock(path,shared=coordination_lock_shared(request,action,args,operation)):
            return web_action(path,name,request,authority_config,configured_operators(root))
    if action=='guidance':
        # The standing guidance channel (kittrial-5bb.99 slice 1, revised). The
        # host/SSH guidance action is read-only except for the caller's acknowledgement.
        # Authenticated web writes use the distinct service-only action above, and
        # this allowlist refuses every other subcommand, so adding a write route here
        # cannot pass unnoticed (review `tests`: the test enumerates the accepted
        # subcommands and fails on any not in the read+ack set). `ack` names the
        # version the caller read with the explicit --version flag; it is
        # unauthenticated, so any actor the endpoint accepts may ack for itself
        # (review `registration-gate-locks-out-unregistered-lanes`).
        import guidance
        args=request.get('args',[])
        if not isinstance(args,list) or any(not isinstance(a,str) or '\0' in a for a in args):
            raise ValueError('Expected argument list')
        subcommand=args[0] if args else 'get'
        if subcommand not in ('get','version','ack','status'):
            raise ValueError('Unknown guidance action %r: the endpoint can only read guidance (get, version), '
                             'acknowledge the version the caller read (ack), or, for an operator, read status. '
                             'Host writes use admin.py set-guidance; authenticated web writes use the separate web service action.'%subcommand)
        if subcommand=='ack':
            version=_guidance_ack_version(args[1:])
            with (path/'.coordination.lock').open('a') as lock:
                fcntl.flock(lock,fcntl.LOCK_EX)
                result=guidance.acknowledge(path,actor,version)
        elif subcommand=='status':
            if len(args)!=1:raise ValueError('Use guidance status without arguments')
            # Endpoint status shows versions and actor names, never guidance text
            # (review `small` 1); the host guidance-status read adds the text.
            result=guidance.status(path,actor,configured_operators(root))
        elif subcommand=='version':
            if len(args)!=1:raise ValueError('Use guidance version without arguments')
            result=guidance.state(path,actor)
        else:
            result=guidance.read(path,args,actor)
        answer={'returncode':0,'stdout':json.dumps(result,ensure_ascii=False,indent=2)+'\n','stderr':''}
        return stamp_write(answer) if subcommand=='ack' and result.get('reconciled') is not True else answer
    if action=='anchors':
        # Read-only (kittrial-5bb.71): which rows are record anchors, by the predicate
        # every surface uses (reserved_comments.is_record_anchor), in ONE native read:
        # with task ids (at most ANCHOR_READ_IDS_MAX) only those rows, labels and
        # comments, through `bd show --include-comments`; with none, the whole
        # snapshot through `bd export --all`. Like refresh's export it takes no
        # coordination lock and writes no operation-journal row (it is not
        # run_guarded), so it never waits on a writer or makes one wait. One bd
        # command is one read; a row a concurrent writer has labelled but not yet
        # recorded reads as an ordinary row, exactly like one whose writer crashed.
        from reserved_comments import ANCHOR_READ_IDS_MAX, record_anchor_ids
        ids=request.get('args') or []
        raw_rows=[]
        if (not isinstance(ids,list) or len(ids)>ANCHOR_READ_IDS_MAX or len(set(ids))!=len(ids)
                or any(not isinstance(x,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,160}',x) for x in ids)):
            raise ValueError('anchors takes no arguments, or at most %d distinct task ids'%ANCHOR_READ_IDS_MAX)
        if ids:
            completed=native.run(native.argv(root,path,actor,['show',*ids,'--json','--include-comments']),environment(root))
            try:missing=completed.returncode==1 and json.loads(completed.stdout).get('error')=='no issues found matching the provided IDs'
            except (ValueError,AttributeError):missing=False
            if missing:
                # Every listed row was deleted after the list: none is an anchor.
                warnings=completed.stderr or ''
            else:
                stdout,warnings=native.split(completed)
                raw_rows=record_json.loads_array_rows(stdout or '[]')
                unreadable=[r for r in raw_rows if isinstance(r,dict) and r.get('malformed')]
                if unreadable:
                    unreadable_report='Unreadable issue row(s): %s' % ', '.join(
                        '%s (%s)' % (r.get('id') or 'unknown', r.get('error') or 'malformed') for r in unreadable)
                    warnings=(warnings + '\n' + unreadable_report).strip() if warnings else unreadable_report
        else:
            stdout,warnings=native.split(native.run(native.argv(root,path,actor,['export','--all']),environment(root)))
            raw_rows=record_json.loads_rows(stdout)
            unreadable=[r for r in raw_rows if isinstance(r,dict) and r.get('malformed')]
            if unreadable:
                unreadable_report='Unreadable issue row(s): %s' % ', '.join(
                    '%s (%s)' % (r.get('id') or 'unknown', r.get('error') or 'malformed') for r in unreadable)
                warnings=(warnings + '\n' + unreadable_report).strip() if warnings else unreadable_report
        from reserved_comments import RECORD_ANCHOR_LABELS
        def anchor_run(argv):
            result,warning=native.split(native.run(native.argv(root,path,actor,argv),environment(root)))
            if warning:anchor_warnings.append(warning)
            return result
        anchor_warnings=[]
        classified=record_json.classify(raw_rows,anchor_run,sorted(RECORD_ANCHOR_LABELS)) if raw_rows else []
        if anchor_warnings:warnings=(warnings+'\n'+'\n'.join(anchor_warnings)).strip()
        return {'returncode':0,'stdout':json.dumps({'schema_version':1,'anchors':record_anchor_ids(classified)})+'\n','stderr':warnings}
    if action=='ref':
        # The reference catalog (.41 slice 1, kittrial-5bb.66). Reads (get, list, find, help)
        # read their own key, or the catalog in two native reads (reference_records), take no
        # coordination lock and are not run_guarded (like the anchors read); propose
        # and revise are writes, under the lock and the operation journal. misses is a read
        # of the reference lookup-miss log, which find and get feed (kittrial-5bb.98).
        import reference_records
        args=request.get('args',[])
        if not isinstance(args,list) or any(not isinstance(x,str) or '\0' in x for x in args):raise ValueError('Expected argument list')
        run_warnings=[]
        def run(argv):
            stdout,warnings=native.split(native.run(native.argv(root,path,actor,argv),environment(root)))
            if warnings:run_warnings.append(warnings)
            return stdout
        if not args or args[0] not in reference_records.CONTRIBUTOR_OPERATIONS:
            import capability_misses
            log=capability_misses.REFERENCE
            def counted(phrase,found):
                # Telemetry, exactly as for capability find: its own non-blocking lock file,
                # never .coordination.lock; no bd call; it cannot fail or delay the read.
                try:capability_misses.record_find(path,phrase,found,which=log)
                except Exception:pass
            if args[:1]==['misses'] and not any(token in ('--help','-h') for token in args):
                # Read-only, no lock, not run_guarded. It reads the telemetry file, and the
                # catalog once (only when the log holds a phrase) to mark the misses an entry
                # would now answer. ASCII-escaped: the phrases are untrusted text.
                limit=capability_misses.options(args[1:],log)['limit']
                result=capability_misses.report(path,lambda:reference_records.NowAnswered(reference_records.read_rows(run),configured_operators(root)),limit,log)
                return {'returncode':0,'stdout':json.dumps(result,ensure_ascii=True)+'\n','stderr':''.join(run_warnings)}
            looked_up=reference_records.miss_phrase(args)
            try:result=reference_records.read(args,run,configured_operators(root))
            except ValueError as error:
                # `ref get` of a key nobody has recorded is a lookup that missed.
                if looked_up is not None and str(error).startswith('Unknown reference key'):counted(looked_up,False)
                raise
            if args[:1]==['find'] and isinstance(result,dict) and isinstance(result.get('found'),bool):
                counted(result.get('normalized'),result['found'])
            elif looked_up is not None:counted(looked_up,True)
            return {'returncode':0,'stdout':json.dumps(result,ensure_ascii=False)+'\n','stderr':''.join(run_warnings)}
        payload=reference_records.write_payload(args,request.get('attachments',{}))
        runner=NativeRunner(run, project=path)
        def ref_effect():
            result=reference_records.apply_native(payload,actor,runner,path,operators=configured_operators(root))
            return {'returncode':0,'stdout':json.dumps(result,ensure_ascii=False)+'\n','stderr':''.join(run_warnings)}
        with (path/'.coordination.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            return guarded_write(root,request,journal_path(path),ref_effect,
                               authority_config=authority_config,
                               require_authority=require_authority,runner=runner)
    if action=='capability':
        # The capability index (.60 slices 1a and 1b, kittrial-5bb.67/.69). get, list, find
        # and help are reads (one label-filtered bd list plus bd show; no lock, not
        # run_guarded); misses is a read of the lookup-miss log (capability_misses, kittrial-
        # 5bb.77), which find feeds; propose, revise, propose-alias and verify are writes,
        # under the lock and the journal. lookup, resolve, index and check run in the client.
        # A verification written here is always `unverified`; the deployment verifiers list
        # only decides how host-written (`admin.py capability-verify`) records are read.
        import capability_records
        args=request.get('args',[])
        if not isinstance(args,list) or any(not isinstance(x,str) or '\0' in x for x in args):raise ValueError('Expected argument list')
        run_warnings=[]
        def run(argv):
            stdout,warnings=native.split(native.run(native.argv(root,path,actor,argv),environment(root)))
            if warnings:run_warnings.append(warnings)
            return stdout
        if not args or args[0] not in capability_records.WRITE_COMMANDS:
            if args[:1]==['misses'] and not any(token in ('--help','-h') for token in args):
                # The lookup-miss log (kittrial-5bb.77): read-only, no lock, not run_guarded.
                # It reads the telemetry file, and the records once (only when the log holds
                # a phrase) to mark the misses that would now resolve. ASCII-escaped: the
                # phrases are untrusted text.
                import capability_misses
                limit=capability_misses.options(args[1:])['limit']
                result=capability_misses.report(path,lambda:capability_records.exact_index(capability_records.read_catalog(run)[0],configured_operators(root)),limit)
                return {'returncode':0,'stdout':json.dumps(result,ensure_ascii=True)+'\n','stderr':''.join(run_warnings)}
            result=capability_records.read(args,run,configured_operators(root),
                                           verifiers=configured_verifiers(root),journal=path)
            if args[:1]==['find'] and isinstance(result,dict) and isinstance(result.get('found'),bool):
                # Telemetry (kittrial-5bb.77): count the find and, with no exact match, its
                # normalised phrase. Its own non-blocking lock file, never .coordination.lock;
                # no bd call, no subprocess, no journal row; it cannot fail or delay the read.
                try:
                    import capability_misses
                    capability_misses.record_find(path,result.get('normalized'),result['found'])
                except Exception:pass
            return {'returncode':0,'stdout':json.dumps(result,ensure_ascii=False)+'\n','stderr':''.join(run_warnings)}
        operators=configured_operators(root)
        runner=NativeRunner(run, project=path)
        def capability_effect():
            result=capability_records.write(args,request.get('attachments',{}),actor,runner,path,operators,
                                            verifiers=configured_verifiers(root))
            return {'returncode':0,'stdout':json.dumps(result,ensure_ascii=False)+'\n','stderr':''.join(run_warnings)}
        with (path/'.coordination.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            return guarded_write(root,request,journal_path(path),capability_effect,
                               authority_config=authority_config,
                               require_authority=require_authority,runner=runner)
    if action=='proposal':
        # Contributed requirement proposals (.58 slices 1a and 1b, kittrial-5bb.68/.70).
        # get, list, mine and help are reads (no lock, not run_guarded); submit and
        # revise are writes, under the lock and the operation journal. Over SSH review,
        # decide and settings are NOT reachable: the actor is self-declared there, so
        # everything that rests on the operator allowlist is a host command (admin.py
        # proposal-review, proposal-decide, proposal-settings). The HTTP service is the
        # other authority: launched by it, with live authority required, review and
        # decide run for a signed-in member whose capability the guard has just re-validated
        # (reviews.approve for a review, project.admin for a decide: kittrial-5bb.261), and a submission's submitter is bound to the verified account.
        import proposal_records
        args=request.get('args',[])
        if not isinstance(args,list) or any(not isinstance(x,str) or '\0' in x for x in args):raise ValueError('Expected argument list')
        run_warnings=[]
        def run(argv):
            stdout,warnings=native.split(native.run(native.argv(root,path,actor,argv),environment(root)))
            if warnings:run_warnings.append(warnings)
            return stdout
        operators=configured_operators(root)
        by_service=authority_config is not None and require_authority
        writes=proposal_records.WRITE_COMMANDS+(proposal_records.HTTP_WRITE_COMMANDS if by_service else ())
        if not args or args[0] not in writes:
            # Launched by the HTTP service, a read returns the unfiltered view: the
            # service withholds coordinator text per caller from its server-bound identity.
            result=proposal_records.read(args,run,actor,operators,project=path,full=authority_config is not None)
            return {'returncode':0,'stdout':json.dumps(result,ensure_ascii=False)+'\n','stderr':''.join(run_warnings)}
        runner=NativeRunner(run, project=path)
        def proposal_effect():
            http=proposal_records.http_context(request,authority_config,require_authority)
            if authority_config is not None and http is None:
                raise ValueError('A proposal write through the HTTP service needs live authority')
            result=proposal_records.write(args,request.get('attachments',{}),actor,runner,path,operators,http=http)
            return {'returncode':0,'stdout':json.dumps(result,ensure_ascii=False)+'\n','stderr':''.join(run_warnings)}
        with (path/'.coordination.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            return guarded_write(root,request,journal_path(path),proposal_effect,
                               authority_config=authority_config,
                               require_authority=require_authority,runner=runner)
    if action=='coordinator':
        # Slice 3 of docs/COORDINATORS_PER_PROJECT_DESIGN.md (kittrial-5bb.195): the acceptance
        # commands a confined coordinator runs in its own project. These are the host commands
        # of admin.py; they are reachable here only because the caller is a bound key
        # (--key-principal, kittrial-5bb.194), so the actor is the one the project's registry
        # gives to the key's principal and not the caller's own word. An installation that
        # configures nothing sends no principal and reaches none of them (coordinator_refusal).
        args=request.get('args',[])
        if not isinstance(args,list) or not args or any(not isinstance(a,str) or '\0' in a for a in args):
            raise ValueError('Use coordinator with one of: %s'%', '.join(COORDINATOR_COMMANDS))
        command=args[0]
        if command not in COORDINATOR_COMMANDS:
            raise ValueError('Unknown coordinator command %r: a confined coordinator may run only %s. Voiding, '
                             'reverting, reconciling, backups, retiring and the rollout switches stay with the '
                             'installation operator.'%(command,', '.join(COORDINATOR_COMMANDS)))
        coordinator_refusal(request,root,key_principal)
        text=coordinator_payload(args[1:],request)
        payload=None
        if command in COORDINATOR_READS or command=='guidance-clear':
            if args[1:]:raise ValueError('Use coordinator %s without a payload'%command)
        elif command in COORDINATOR_TEXTS:
            if text is None:raise ValueError('coordinator %s needs --file with the text to set'%command)
        else:
            if text is None:raise ValueError('coordinator %s needs --file with its JSON payload'%command)
            try:
                payload=record_json.loads(text)
            except ValueError as error:
                # A bare JSONDecodeError is not a sentence a confined coordinator can act on
                # (review of 958e883, item 3c).
                raise ValueError('coordinator %s payload is not valid JSON (%s); supply one JSON object '
                                 'in the --file attachment. Nothing was changed.'%(command,error)) from None
            if not isinstance(payload,dict):
                raise ValueError('coordinator %s payload must be a JSON object'%command)
            if command in ('reference-apply','capability-apply'):
                coordinator_apply_refusal(payload,command)
        if command=='set-onboarding':
            # The document's name is checked BEFORE the guarded write, so no operation identity is
            # reserved and released for a refusal and no .http-operations.sqlite3 is created for one
            # (kittrial-5bb.259 item G1: this check used to sit inside the effect). The SYMLINK is
            # tested FIRST (item G2): a symlink to a directory matched is_dir() and was answered with
            # the directory sentence, where the base said the document must not be a symlink at all.
            #
            # Known and accepted order change (kittrial-5bb.259 item 2): this check answers before
            # the operation journal is consulted, so a COMMITTED operation_id retried after the host
            # turned the path into a directory or a symlink is answered with this refusal instead of
            # its recorded answer, and an identity reused with different text is answered with this
            # path sentence instead of 'Operation identity reused with a different request'. Both
            # need the host to change the path between two calls. The DIRECTORY refusal is made a
            # second time inside the effect as a backstop (coordinator_effect below), because this
            # check runs before the project lock and the effect can be reached long afterwards.
            target=path/'ONBOARDING.md'
            if target.is_symlink():
                raise ValueError('Project onboarding must not be a symlink')
            if target.is_dir():
                raise onboarding_directory_refusal(target)
        operators=configured_operators(root)
        verifiers=configured_verifiers(root)
        run_warnings=[]
        def coordinator_run(argv):
            stdout,warnings=native.split(native.run(native.argv(root,path,actor,argv),environment(root)))
            if warnings:run_warnings.append(warnings)
            return stdout
        runner=NativeRunner(coordinator_run, project=path)
        import contextlib
        @contextlib.contextmanager
        def held():
            # One hold of the project's coordination lock; closing the file releases it. A
            # batch command takes it once per item through this callable and releases it
            # between items, so the caller holds nothing around the batch (kittrial-5bb.67
            # review 01a0fc55), exactly as admin.py does for the same commands.
            with (path/'.coordination.lock').open('a') as lock:
                fcntl.flock(lock,fcntl.LOCK_EX)
                yield
        per_item_lock=(command=='capability-verify'
                       or (command in ('reference-apply','capability-apply')
                           and isinstance(payload,dict) and 'items' in payload))
        if command=='guidance-status':
            # The host read (`admin.py guidance-status`): a bound, listed actor may see the
            # text, which the endpoint's self-declared `guidance status` withholds. The host
            # command takes the project's coordination lock around the read, so this does too
            # (review of 958e883, item 4d): the answer is one snapshot, not a read racing a set.
            import guidance
            with held():
                result=guidance.status(path,actor,operators,host=True)
            return {'returncode':0,'stdout':json.dumps(result,ensure_ascii=False)+'\n','stderr':''}
        def coordinator_effect():
            if command=='guidance-set':
                import guidance
                result=guidance.write_guidance(path,text,actor)
            elif command=='guidance-clear':
                import guidance
                result=guidance.clear(path,actor)
            elif command=='set-onboarding':
                from onboarding import write_project
                from http_authority import server_time as now_stamp
                target=path/'ONBOARDING.md'
                previous=None
                # The second of the two directory checks, and why there are two: the pre-check
                # above answers the ordinary case and, for kittrial-5bb.259 item G1, answers before
                # any identity is reserved. It runs before the project lock, though, so this effect
                # can be reached long after it, with a directory put there while the call waited.
                # os.replace over a directory raises IsADirectoryError, which is NOT in
                # REFUSAL_EXCEPTIONS: the identity would stay reserved and the answer would be
                # rc 124 "outcome unknown" for a call that wrote nothing - the case kittrial-5bb.242
                # item 5 fixed. The sentence is the pre-check's, so the caller cannot tell the two
                # apart. The symlink is deliberately not repeated here: os.replace does not follow
                # a symlink at the destination, it replaces the link itself, so a link swapped in
                # after the pre-check cannot raise this way.
                if target.is_dir():
                    raise onboarding_directory_refusal(target)
                if target.is_file() and not target.is_symlink():
                    try:
                        previous=target.read_text(encoding='utf-8-sig')
                    except (UnicodeDecodeError,OSError):
                        # A damaged previous document must not stop the route from replacing it
                        # (review of 6f3007a, finding 1; kittrial-5bb.238 item 1): the host command
                        # replaces it, and a caller with no shell has no other way. The unreadable
                        # previous text is treated as changed, and the answer says so in a sentence
                        # rather than the bare UnicodeDecodeError that used to answer rc 2 with
                        # nothing written. The sentence ends in a newline like every other warning
                        # the route joins: without one the client printed "...and replacedserver_time:
                        # ..." on one line (kittrial-5bb.242 item 4).
                        run_warnings.append('the previous ONBOARDING.md could not be read as UTF-8 text; '
                                            'it was treated as changed and replaced\n')
                write_project(target,text)
                # This route does NOT probe the text for endpoint paths (review of 958e883, item 3b):
                # probe_endpoints resolves and reads every absolute *.py path the text names and
                # reveals one bit about each plus the resolved symlink target, and the server must
                # never read a path out of the request. The host `admin.py set-onboarding` keeps its
                # warning for the operator with a shell. `set_by` ECHOES the actor the server chose
                # for this request (kittrial-5bb.238 item 4): the route records no attribution of its
                # own, a request field called set_by is ignored, and the installation's own log is
                # the only trace of who set the text.
                result={'state':'set','source':'operator','bytes':len(text.encode('utf-8')),
                        'changed':previous!=text,'set_by':actor,'set_at':now_stamp()}
            elif command=='handoff':
                from handoff import execute as handoff_execute
                result=handoff_execute(path,actor,payload,runner,operator=True)
            elif command=='reference-apply':
                import reference_records
                if isinstance(payload,dict) and 'items' in payload:
                    result=reference_records.apply_batch(payload,actor,runner,path,operators=operators,lock=held)
                else:
                    payload.setdefault('operation','accept')
                    result=reference_records.apply_native(payload,actor,runner,path,operator=True,
                                                          operators=operators)
            elif command=='capability-apply':
                import capability_records
                if isinstance(payload,dict) and 'items' in payload:
                    result=capability_records.apply_batch(payload,actor,runner,path,operators=operators,lock=held)
                else:
                    result=capability_records.apply_native(payload,actor,runner,path,operator=True,
                                                           operators=operators)
            elif command=='capability-verify':
                import capability_verification
                result=capability_verification.verify_batch(payload,actor,runner,operators=operators,
                                                            verifiers=verifiers,journal=path,lock=held)
            else:
                import proposal_records
                result=proposal_records.dispose(payload,actor,runner,path,operators=operators,
                                                route='decide' if command=='proposal-decide' else 'review')
            # The answer carries the server's time when this call wrote: a file set (guidance,
            # onboarding), a record accepted, a verification, a disposition. A no-op - an
            # idempotent retry the record module answered `reconciled: true` or `changed: false`,
            # an items batch whose every item was already-accepted, or a verify whose every item
            # was already-recorded - wrote nothing and carries no server_time (review of 958e883,
            # finding 11: the comment used to claim this while guidance-set, set-onboarding and a
            # batch still carried server_time; review of 6f3007a, finding 4: an all-
            # already-recorded verify still did).
            no_op=(isinstance(result,dict)
                   and (result.get('reconciled') is True or result.get('changed') is False
                        or (command in ('reference-apply','capability-apply') and isinstance(payload,dict)
                            and isinstance(payload.get('items'),list) and bool(result.get('items'))
                            and all(isinstance(item,dict) and item.get('result')=='already-accepted'
                                    for item in result['items']))
                        or (command=='capability-verify' and isinstance(result.get('items'),list)
                            and bool(result['items'])
                            and all(isinstance(item,dict) and item.get('result')=='already-recorded'
                                    for item in result['items']))))
            if not (isinstance(result,dict) and result.get('reconciled') is True):runner.wrote=True
            if no_op:runner.wrote=False
            return {'returncode':0,'stdout':json.dumps(result,ensure_ascii=False)+'\n',
                    'stderr':''.join(run_warnings)}
        if per_item_lock:
            return guarded_write(root,request,journal_path(path),coordinator_effect,
                                 authority_config=authority_config,
                                 require_authority=require_authority,runner=runner)
        with held():
            return guarded_write(root,request,journal_path(path),coordinator_effect,
                                 authority_config=authority_config,
                                 require_authority=require_authority,runner=runner)
    if action in ('brief','history','checkpoint'):
        from briefing import execute as briefing_execute
        args=request.get('args',[])
        if not isinstance(args,list) or any(not isinstance(x,str) or '\0' in x for x in args):raise ValueError('Expected argument list')
        run_warnings=[]
        def run(argv):
            stdout,warnings=native.split(native.run(native.argv(root,path,actor,argv),environment(root)))
            if warnings:run_warnings.append(warnings)
            return stdout
        runner=NativeRunner(run, project=path)
        def briefing_effect():
            rules=configured_coordinator_rules(root,warnings=run_warnings)
            return {'returncode':0,'stdout':briefing_execute(root,path,name,actor,action,args,request.get('attachments',{}),runner,
                                                             operators=configured_operators(root),
                                                             verifiers=configured_verifiers(root),
                                                             independent_approval=rules['independent_approval'],
                                                             owners=owner_loader(path,'independent_approval')
                                                             if rules['independent_approval'] else None),'stderr':''.join(run_warnings)}
        with held_coordination_lock(path,shared=coordination_lock_shared(request,action,args)):
            return guarded_write(root,request,journal_path(path),briefing_effect,
                               authority_config=authority_config,
                               require_authority=require_authority,runner=runner)
    if action in ('assignment-roster','task-assign','task-release'):
        from http_authority import CAP_COORDINATE, CAP_TASKS, read_state, server_time
        from task_assignment import AssignmentError, eligible_roster, standing_allowed, validate_request, apply as assignment_apply
        from sessions import read_registry
        args=request.get('args', [])
        try:
            payload=None
            if action=='assignment-roster':
                if not isinstance(args,list) or len(args)>1:
                    raise AssignmentError(422,'Assignment roster takes one optional page request.')
                page=record_json.loads(args[0]) if args else {'offset':0,'limit':200}
                if (not isinstance(page,dict) or set(page)!={'offset','limit'}
                        or type(page['offset']) is not int or page['offset']<0
                        or type(page['limit']) is not int or not 1<=page['limit']<=200):
                    raise AssignmentError(422,'Assignment roster requires a nonnegative offset and limit 1-200.')
            elif action=='task-release':
                # The holder releases its OWN claim (kittrial-5bb.301). The body carries
                # the task, the reason, and (when held under a <agent>/<label> sub-label,
                # kittrial-5bb.300) the row's current assignee as expected_assignee, verified
                # by claim_limit.actor_holds against the authenticated caller so no caller
                # can clear another actor's assignee. It is the same canonical assignment the
                # owner's bulk release uses, so there is one way to clear an assignee. The
                # canonical change check below refuses a task the caller does not hold, ANY
                # delivered contribution (awaiting review, changes requested, approved or
                # integrated), and the merge slot.
                if not isinstance(args,list) or len(args)!=1 or not isinstance(args[0],str):
                    raise AssignmentError(422,'A claim release takes one closed JSON request.')
                fields=record_json.loads(args[0])
                if (not isinstance(fields,dict)
                        or not set(fields).issubset({'task_id','reason','expected_assignee'})
                        or not {'task_id','reason'}.issubset(fields)):
                    raise AssignmentError(422,'A claim release takes one task id and one reason.')
                from claim_limit import actor_holds
                expected_assignee=fields.get('expected_assignee') if 'expected_assignee' in fields else actor
                if not actor_holds(expected_assignee,actor):
                    raise AssignmentError(409,'That task is not claimed by you; only its holder can release it')
                payload={'schema_version':1,'operation_id':request.get('operation_id'),
                         'task':fields['task_id'],'expected_assignee':expected_assignee,'assignee':None,
                         'reason':fields['reason'],'allow_pending_review':False,
                         'allow_held_slot':False}
                validate_request(payload)
                if not request.get('operation_id') or payload['operation_id']!=request['operation_id']:
                    raise AssignmentError(422,'A claim release requires the same stable operation identity in its envelope and body.')
            else:
                if not isinstance(args,list) or len(args)!=1 or not isinstance(args[0],str):
                    raise AssignmentError(422,'Assignment takes one closed JSON request.')
                payload=record_json.loads(args[0])
                validate_request(payload)
                if not request.get('operation_id') or payload['operation_id']!=request['operation_id']:
                    raise AssignmentError(422,'Assignment requires the same stable operation identity in its envelope and body.')
            def assignment_authority():
                # A claim release needs only the ordinary task authority: it can only
                # clear the caller's own assignee (kittrial-5bb.301). Every other
                # assignee-only change still needs coordinate authority, so nobody gains
                # the power to take another actor's task.
                if action=='task-release':
                    if not verified_web_authority(request,authority_config,CAP_TASKS,name):
                        raise AssignmentError(403,'Verified task authority is required in this project.')
                elif not verified_web_authority(request,authority_config,CAP_COORDINATE,name):
                    raise AssignmentError(403,'Verified coordinate authority is required in this project.')
                for denied in (http_actor_denial(request,authority_config),
                               descriptor_actor_denial(request,authority_config,
                                   lambda rows=False,own=():reserved_actors(root,path,rows,own))):
                    if denied is not None:
                        raise AssignmentError(503 if denied.get('fault') else denied.get('authority_status',403),
                                              'Current assignment caller standing cannot be verified.')
            def roster():
                state=read_state(authority_config.store)
                try:registry=read_registry(path)
                except ValueError:raise AssignmentError(503,'The project session roster cannot be read.') from None
                def worker_allowed(target,credential_id,credential):
                    target_request={'project':name,'actor':target,'authority':{
                        'via':'credential','user_id':credential.get('user_id'),
                        'credential_id':credential_id,'project':name,'capability':'tasks.write'}}
                    return standing_allowed(descriptor_actor_denial(target_request,authority_config,
                                           lambda rows=False,own=():reserved_actors(root,path,rows,own)))
                return eligible_roster(state,name,registry,now=(request.get('authority') or {}).get('now'),
                                       worker_actor_allowed=worker_allowed)
            warnings=[]
            def assignment_run(argv):
                stdout,said=native.split(native.run(native.argv(root,path,actor,argv),environment(root)))
                if said:warnings.append(said)
                return stdout
            runner=NativeRunner(assignment_run,project=path)
            def effect():
                actors={item['actor'] for item in roster()}
                result=assignment_apply(payload,actor,runner,path,eligible=lambda target:target in actors,
                                        operators=configured_operators(root),at=server_time(),
                                        refuse_delivered=(action=='task-release'))
                return {'returncode':0,'stdout':json.dumps(result,ensure_ascii=False)+'\n','stderr':warning_text(warnings)}
            with held_coordination_lock(path,shared=coordination_lock_shared(request,action,args)):
                if action=='assignment-roster':
                    if authority_config is None:raise AssignmentError(403,'Verified coordinate authority is required in this project.')
                    # SHARED and bounded: two roster reads can hold it at once and a
                    # writer is still excluded, with the same 60 s limit as the
                    # exclusive wait (kittrial-5bb.291 item 5). The old flock(LOCK_SH)
                    # was shared but waited forever.
                    from http_authority import file_lock
                    with file_lock(authority_config.lock, shared=True):
                        assignment_authority();actors=roster();offset=page['offset'];limit=page['limit']
                        result={'schema_version':1,'items':actors[offset:offset+limit],'total':len(actors),
                                'more':offset+limit<len(actors),'offset':offset,
                                'note':'Existing project identities only. Assignment grants no access; target eligibility is checked again at the effect.'}
                        return {'returncode':0,'stdout':json.dumps(result,ensure_ascii=False)+'\n','stderr':''}
                status=[]
                def checked_authority():
                    try:assignment_authority()
                    except AssignmentError as error:status.append(error.status);raise
                answer=guarded_write(root,request,journal_path(path),effect,authority_config=authority_config,
                                     require_authority=True,runner=runner,identity_check=checked_authority)
                if status and answer.get('returncode')==2:answer['assignment_status']=status[-1]
                return answer
        except AssignmentError as error:
            return {'returncode':2,'stdout':'','stderr':str(error)+'\n','assignment_status':error.status}
        except ValueError as error:
            return {'returncode':2,'stdout':'','stderr':str(error)+'\n','assignment_status':422}
    if action=='web-lifecycle':
        from http_authority import (CAP_READ, CAP_COORDINATE, AuthorityDenied,
                                    actor_parties, decide, file_lock, read_state)
        from admin import web_lifecycle_mark_writes
        import web_lifecycle
        args=request.get('args',[])
        run_warnings=[]
        def lifecycle_run(argv):
            stdout,warnings=native.split(native.run(native.argv(root,path,actor,argv),environment(root)))
            if warnings:run_warnings.append(warnings)
            return stdout
        # The web lifecycle write compares a fact against the task's LIVE bounded
        # rows rather than a full export (kittrial-5bb.289 item 2).
        runner=NativeRunner(lifecycle_run,project=path,bounded_task_reads=True)
        denied_status=[]
        try:
            if not isinstance(args,list) or len(args)!=1 or not isinstance(args[0],str):
                raise web_lifecycle.LifecycleError(422,'Expected one lifecycle JSON payload')
            body=record_json.loads(args[0])
            operation,payload=web_lifecycle.validate(body,actor,request.get('operation_id'))
            capability=required_capability(request)
            if capability is None:
                raise web_lifecycle.LifecycleError(403,'This lifecycle operation has no endpoint capability binding')
            def lifecycle_authority():
                if not verified_web_authority(request,authority_config,capability,name):
                    denied_status.append(403)
                    raise web_lifecycle.LifecycleError(403,'Verified web authority required in this project')
                return True
            def lifecycle_identity():
                lifecycle_authority()
                if payload and payload['dimension'] in ('reviewed','integrated'):
                    from lifecycle import read_event_rows, require_unheld_fact
                    rows=read_event_rows(runner)
                    issue=next((row for row in rows if row.get('id')==body['task']),None)
                    # The same operators and project journal the canonical readers use
                    # (kittrial-5bb.272 review item 1): a voided release-hold reads held
                    # here too, never None where the caller has them.
                    try:require_unheld_fact(issue,payload['dimension'],body['task'],
                                            operators=configured_operators(root),journal=path)
                    except ValueError as error:
                        denied_status.append(409)
                        raise web_lifecycle.LifecycleError(409,str(error)) from None
            def lifecycle_parties(label):
                # Same full party policy as HTTP approval, from the configured
                # store under its held lock. No descriptor supplies this state.
                return actor_parties(read_state(authority_config.store),label,name)
            def lifecycle_effect():
                strict=operation=='read' or (payload and payload['dimension'] in ('reviewed','integrated'))
                operators=configured_operators(root) if strict else None
                owners=owner_loader(path,'web lifecycle approval') if strict else None
                try:
                    enabled=web_lifecycle_mark_writes(root) if (operation=='read' or
                              (payload['dimension']=='integrated' and payload['value']=='passed')) else False
                except ValueError as error:
                    raise web_lifecycle.LifecycleError(503,str(error)) from None
                if operation=='read':
                    descriptor=dict(request['authority'],capability=CAP_COORDINATE)
                    try:
                        decide(read_state(authority_config.store),descriptor)
                        coordinate=True
                    except AuthorityDenied:
                        coordinate=False
                    result=web_lifecycle.read(body['task'],actor,runner,path,coordinate=coordinate,
                                              mark_writes=enabled,operators=operators,owners=owners,
                                              parties=lifecycle_parties)
                else:
                    result=web_lifecycle.apply(body,actor,runner,path,name,request['operation_id'],
                                               authority=lifecycle_authority,parties=lifecycle_parties,
                                               mark_writes=enabled,operators=operators,owners=owners)
                return {'returncode':0,'stdout':json.dumps(result,ensure_ascii=False)+'\n',
                        'stderr':warning_text(run_warnings)}
            with held_coordination_lock(path,shared=coordination_lock_shared(request,'web-lifecycle',args,operation)):
                if operation=='read':
                    if authority_config is None:
                        raise web_lifecycle.LifecycleError(403,'Configured web authority required for lifecycle reads')
                    with file_lock(authority_config.lock):
                        lifecycle_authority()
                        return lifecycle_effect()
                answer=guarded_write(root,request,journal_path(path),lifecycle_effect,
                                     authority_config=authority_config,require_authority=True,
                                     runner=runner,identity_check=lifecycle_identity)
                if denied_status and answer.get('returncode')==2:
                    answer['lifecycle_status']=denied_status[-1]
                return answer
        except web_lifecycle.LifecycleError as error:
            return {'returncode':2,'stdout':'','stderr':str(error)+'\n','lifecycle_status':error.status}
    if action=='web-merge-slot':
        from http_authority import CAP_READ, CAP_COORDINATE, file_lock
        from web_merge_slot import SlotError, validate as slot_validate, apply as slot_apply
        from review_workflow import CoordinatorRuleRefusal
        args=request.get('args',[])
        run_warnings=[]
        def slot_run(argv):
            stdout,warnings=native.split(native.run(native.argv(root,path,actor,argv),environment(root)))
            if warnings:run_warnings.append(warnings)
            return stdout
        runner=NativeRunner(slot_run, project=path)
        try:
            if not isinstance(args,list) or len(args)!=1 or not isinstance(args[0],str):
                raise SlotError(422,'Expected one merge-slot JSON payload')
            payload=record_json.loads(args[0]);operation=slot_validate(payload)
            capability=required_capability(request)
            if capability is None:raise SlotError(403,'This merge-slot operation has no endpoint capability binding')
            denied_status=[]
            def slot_authority():
                descriptor=request.get('authority') or {}
                if (not verified_web_authority(request,authority_config,capability,name)
                        or (operation in ('create','force-release') and descriptor.get('via')!='session')):
                    denied_status.append(403)
                    raise SlotError(403,'Verified web authority required for this merge-slot operation in this project')
            def hold_slot_operators(root, warnings):
                # kittrial-5bb.297 review item (c): the web acquire reads the operator
                # allowlist ALWAYS now, and a malformed value keeps the old tolerance
                # (nobody, with a warning) instead of refusing an acquire that
                # answered 200 before the fix.
                try:
                    return configured_operators(root)
                except ConfigurationUnreadable as error:
                    warnings.append('deployment operators unreadable, so operator voids are not '
                                    'applied: '+str(error)[:160])
                    return None
            def slot_identity():
                slot_authority()
                if operation=='acquire':
                    from lifecycle import read_event_rows
                    from coordination import require_unheld_merge_task
                    rows=read_event_rows(runner)
                    issue=next((row for row in rows if row.get('id')==payload['task']),None)
                    try:
                        # kittrial-5bb.297 review item 'operators-on-every-surface':
                        # the configured operators ALWAYS, not only when
                        # independent_approval is on - an operator void that repairs
                        # the chain must be seen here exactly as the canonical
                        # readers see it, or the acquire stays refused while the
                        # coordinate route and the release accept.
                        require_unheld_merge_task(issue,payload['task'],
                                                  hold_slot_operators(root,run_warnings),path)
                    except CoordinatorRuleRefusal:
                        denied_status.append(409)
                        raise
            rules=configured_coordinator_rules(root,warnings=run_warnings)
            def slot_effect():
                result=slot_apply(payload,actor,runner,path,request.get('operation_id'),
                                  operators=hold_slot_operators(root,run_warnings),
                                  coordinator_writes=rules['coordinator_writes'],
                                  independent_approval=rules['independent_approval'],
                                  owners=owner_loader(path,'independent_approval'),
                                  review_writes=configured_review_writes(root,warnings=run_warnings))
                return {'returncode':0,'stdout':json.dumps(result,ensure_ascii=False)+'\n','stderr':warning_text(run_warnings)}
            with held_coordination_lock(path,shared=coordination_lock_shared(request,'web-merge-slot',args,operation)):
                if operation=='check':
                    if authority_config is None:raise SlotError(403,'Verified web authority required for the merge-slot read')
                    with file_lock(authority_config.lock):
                        slot_authority()
                        return slot_effect()
                if not request.get('operation_id'):
                    raise SlotError(422,'A stable operation identity is required for a merge-slot write')
                answer=guarded_write(root,request,journal_path(path),slot_effect,
                                     authority_config=authority_config,require_authority=True,
                                     runner=runner,identity_check=slot_identity)
                if denied_status and answer.get('returncode')==2:answer['merge_slot_status']=denied_status[-1]
                return answer
        except CoordinatorRuleRefusal as error:
            # Preserve the slot's conflict status and identify the native rule refusal.
            return {'returncode':2,'stdout':'','stderr':str(error)+'\n',
                    'merge_slot_status':409,'refused':'coordinator-rule'}
        except SlotError as error:
            return {'returncode':2,'stdout':'','stderr':str(error)+'\n','merge_slot_status':error.status}
    if action in ('lifecycle','coordinate','requirement'):
        args=request.get('args',[])
        if not isinstance(args,list) or len(args)!=1 or not isinstance(args[0],str):raise ValueError('Expected one JSON payload')
        payload=record_json.loads(args[0])
        run_warnings=[]
        def run(argv):
            stdout,warnings=native.split(native.run(native.argv(root,path,actor,argv),environment(root)))
            if warnings:run_warnings.append(warnings)
            return stdout
        # The canonical lifecycle write compares a fact against the task's LIVE
        # bounded rows rather than a full export (kittrial-5bb.289 item 2).
        runner=NativeRunner(run, project=path, bounded_task_reads=True)
        # Slices 6 and 7 (kittrial-5bb.197): the three rules are read once, from the
        # installation's own file, and an installation that configures nothing reads
        # them all as OFF. A malformed value reads as OFF with a warning, as
        # review_workflow_writes does, rather than failing every lifecycle write.
        switch_warnings=[]
        rules=configured_coordinator_rules(root,warnings=switch_warnings)
        run_warnings.extend(switch_warnings)
        # The registry is read only when a rule that needs it is on, and for the
        # lifecycle write only when `lifecycle_authors` is on: with the rules OFF a
        # malformed `operators` value must not stop `coordinate`, and a damaged
        # registry must not stop the reads or a listed operator's operation
        # (kittrial-5bb.197 review request 01a12894, item 4).
        # kittrial-5bb.297 review item 'operators-everywhere-really' (a): the operator
        # allowlist is passed ALWAYS for the coordinate write's hold check (one
        # operator void that repairs a chain is seen here exactly as the canonical
        # readers see it), not only when a rule is on; the rules still gate what they
        # gated. (c) A malformed value keeps the old tolerance: it reads as nobody
        # WITH a warning, exactly as a malformed rules value does, so a broken
        # setting never refuses a coordinate write that answered 200 before.
        coordinate_operators=None
        try:coordinate_operators=configured_operators(root)
        except ConfigurationUnreadable as error:
            run_warnings.append('deployment operators unreadable, so operator voids are not applied: '
                               +str(error)[:160])
        def lifecycle_effect():
            if action=='lifecycle':result=apply_native(payload,actor,runner,
                                                       operators=configured_operators(root),journal=path,
                                                       lifecycle_authors=rules['lifecycle_authors'],
                                                       independent_approval=rules['independent_approval'],
                                                       owners=owner_loader(path,'lifecycle_authors'))
            elif action=='coordinate':
                from coordination import apply_native as coordinate
                result=coordinate(payload,actor,runner,path,operators=coordinate_operators,
                                  coordinator_writes=rules['coordinator_writes'],
                                  independent_approval=rules['independent_approval'],
                                  owners=owner_loader(path,'coordinator_writes'),
                                  review_writes=configured_review_writes(root,warnings=switch_warnings))
            else:
                from requirement_records import apply_native as requirement_apply
                result=requirement_apply(payload,actor,runner,path)
            return {'returncode':0,'stdout':json.dumps(result,ensure_ascii=False)+'\n','stderr':warning_text(run_warnings)}
        # kittrial-5bb.297 review item 'rc2-scope-and-test-order': ONLY the coordinate
        # merge-acquire refusal answers rc 2 (the answer shape the web merge-slot
        # route returns). Every other CoordinatorRuleRefusal - a lifecycle write's
        # rule refusal included - keeps raising exactly as before: no contract
        # change beyond the one the item asked for.
        from review_workflow import MergeHoldRefusal
        try:
            with (path/'.coordination.lock').open('a') as lock:
                fcntl.flock(lock,fcntl.LOCK_EX)
                return guarded_write(root,request,journal_path(path),lifecycle_effect,
                                   authority_config=authority_config,
                                   require_authority=require_authority,runner=runner)
        except MergeHoldRefusal as error:
            return {'returncode':2,'stdout':'','stderr':str(error)+'\n','refused':'coordinator-rule'}
    if action == 'feedback':
        from feedback import execute as feedback_execute
        args=request.get('args',[])
        if not isinstance(args,list) or any(not isinstance(x,str) or '\0' in x for x in args):
            raise ValueError('Expected argument list')
        with (path/'.coordination.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            result=feedback_execute(path/'.feedback.jsonl',actor,args,request.get('attachments',{}))
        answer={'returncode':0,'stdout':json.dumps(result,ensure_ascii=False)+'\n','stderr':''}
        return answer if args[:1]==['list'] or result.get('reconciled') is True else stamp_write(answer)
    if action=='view':
        target=request.get('path','CURRENT.md')
        viewroot=(path/'views').resolve();view=(viewroot/target).resolve()
        if not view.is_relative_to(viewroot) or view.suffix not in ('.md','.jsonl'):raise ValueError('Invalid view path')
        return {'returncode':0,'stdout':view.read_text(encoding='utf-8'),'stderr':''}
    if action=='refresh':
        with (path/'.refresh.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            p=subprocess.run([str(root/'bin/bd'),'--directory',str(path),'--sandbox','export','--all'],env=environment(root),capture_output=True,text=True,encoding='utf-8',timeout=120)
            if p.returncode:return {'returncode':p.returncode,'stdout':p.stdout,'stderr':p.stderr}
            rows=record_json.loads_rows(p.stdout)
            from reserved_comments import RECORD_ANCHOR_LABELS
            def refresh_run(argv):
                stdout,warning=native.split(native.run(native.argv(root,path,actor,argv),environment(root)))
                if warning:refresh_warnings.append(warning)
                return stdout
            refresh_warnings=[]
            rows=record_json.classify(rows,refresh_run,sorted(RECORD_ANCHOR_LABELS)+['gt:slot'], ['event','gate'])
            from lifecycle import host_snapshot
            rows=host_snapshot(rows, run=refresh_run, project=path)
            return {'returncode':0,'stdout':json.dumps(render(rows,path/'views',configured_operators(root),configured_verifiers(root)))+'\n','stderr':p.stderr+''.join(refresh_warnings)}
    if action!='bd':raise ValueError('Unknown action')
    args=request.get('args',[])
    if not isinstance(args,list) or not args or any(not isinstance(a,str) or '\0' in a for a in args):raise ValueError('Expected argument list')
    if args[0] not in ALLOWED:
        # Refused as always - only the ANSWER changed (kittrial-5bb.283): the old one pointed a
        # worker at the installation operator's own host tool and gave it no commands at all.
        outside_contributor_interface(args[0])
    if args[:2]==['comments','list']:
        # The native tool answers this with its whole usage text (kittrial-5bb.97).
        raise ValueError('There is no `comments list`. Use `comments TASK` to list the comments of a task, and '
                         '`comments add TASK ...` to add one.')
    # Identity/connection/file flags in every pflag spelling (short, joined,
    # =value, boolean cluster) are operator-only. Shorthand knowledge is
    # per-command, so -a is --assignee on list/ready/search/count/create/update
    # but --author on `comments add`, -f is --force on close but --file on
    # comments/create, and an unknown shorthand fails closed so `-rC`/`-uC`
    # cannot switch project. `--` ends flag parsing, as in bd itself, so a body
    # operand after it is not a flag.
    if operator_only_in_args(args) is not None:raise ValueError('Connection/identity/file configuration flags are operator-only')
    # The reserved coordination/requirement label namespaces are written only
    # by coordination.py and requirement_records.py through their internal run
    # paths; the raw contributor bd path must not plant request:/
    # request-content: labels or the controlled requirement type/state labels,
    # nor reach them by inheriting them from a labelled parent or by replacing
    # the labels of an existing holder (checked under the lock below, before
    # the native write).
    label=reserved_label_in_args(args)
    if label==MERGE_SLOT_LABEL:raise ValueError('The label %s marks the merge slot of a project and is reserved: it is not added, removed or replaced on any task. Only `coordinate` writes it (merge-create restores it on a damaged slot).'%label)
    if label is not None:raise ValueError('Reserved coordination/requirement labels are operator-only; use the coordination request workflow (coordination.py) or the requirement command (requirement_records.py draft|revise)')
    # bd 1.2.2 accepts the undocumented `create --label` alias of --labels, so a
    # table built only from --help missed a whole label-writing spelling. Every
    # accepted alias is now in the table above; an unresolvable create/update
    # flag could still be another hidden/deprecated alias (or a value-taking
    # flag the scan would misread), so it fails closed here rather than being
    # assumed harmless.
    unrecognized=unresolved_bd_flags(args)
    if unrecognized:raise ValueError('Refusing create/update with unrecognized flag(s) %s: the reserved coordination/requirement label namespace cannot be verified, so no native write was attempted. Pass only documented bd 1.2.2 flags.'%(', '.join(str(token) for token in unrecognized),))
    # Positional dep/comment IDs are fine; file inputs must be transported explicitly.
    # Command-aware: `-f` is --file on comments/create but --force on close, so a
    # legitimate force-close is no longer refused as a raw server path.
    if raw_file_flag_in_args(args) is not None:raise ValueError('Use client attachment transport; raw server file paths are not accepted')
    # Raw comments add bodies (positional and transported file inputs) must not
    # carry forged machine-record prefixes: those records require their
    # dedicated structured operations with chain/ownership validation.
    # Checked before any temp file or native mutation; rejected writes leave
    # no native record. Actor/task context binds supported records to the
    # actual request target; unresolvable targets fail closed.
    check_raw_request(args, request.get('attachments', {}),
                      actor=actor, task=comment_target(args))
    with tempfile.TemporaryDirectory(prefix='request-',dir=root) as tmp:
        attachments=request.get('attachments',{})
        final=[]
        for i,a in enumerate(args):
            if a.startswith('@attachment:'):
                key=a.partition(':')[2]
                item=attachments.get(key)
                if not isinstance(item,dict) or item.get('flag') not in FILE_FLAGS or not isinstance(item.get('text'),str):raise ValueError('Invalid attachment')
                # bd refuses an empty body file; refuse it here, before any native call, so the
                # caller gets a clear refusal and not an uncertain outcome (review 01a10352).
                if item['flag']=='--body-file' and not item['text'].strip():raise ValueError('An attached description is empty; send an empty value inline to clear it')
                dest=Path(tmp)/f'{i}.txt';dest.write_text(item['text'],encoding='utf-8')
                final.extend([item['flag'],str(dest)])
            else: final.append(a)
        with held_coordination_lock(path,shared=coordination_lock_shared(request,'bd',args)):
            # First the rows the write names (an existing id given to create, a write that names
            # none, the merge slot); then the guards that read what those rows carry.
            from review_workflow import listed_operator_actor, operator_owners
            lifecycle_operators=configured_operators(root)
            lifecycle_guard=args[0] in ('create','update','dep','close','reopen') and not listed_operator_actor(
                actor, lifecycle_operators)
            def bound_raw_exemption():
                try:
                    return listed_operator_actor(actor,lifecycle_operators,operator_owners(path))
                except (ValueError,OSError):
                    return False
            try:
                named=_guard_named_rows(root,path,name,args,request.get('attachments',{}),actor,strict=lifecycle_guard)
            except raw_lifecycle.Refusal:
                if not bound_raw_exemption():raise
                lifecycle_guard=False
                named=_guard_named_rows(root,path,name,args,request.get('attachments',{}),actor,strict=False)
            if lifecycle_guard:
                resolved_request=write_targets(args,request.get('attachments',{}))
                resolved_tokens=resolved_request['targets'] if resolved_request is not None else None
                def lifecycle_rows(tokens):
                    # This exact vector was resolved natively above, under this
                    # lock. Reuse it without guessing which row any subset or
                    # differently spelled token means.
                    if tokens==resolved_tokens and isinstance(named,list):return named
                    # Ask bd for the actual tokens this particular guard reads.
                    # A suffix of another named row is not proof of what bd's
                    # substring resolver selected for a positional operand.
                    rows,problem=_resolve_rows(root,path,actor,tokens,strict=True)
                    if problem:raw_lifecycle.refuse('the affected tasks could not be read completely')
                    return rows
                try:
                    raw_lifecycle.guard(args,request.get('attachments',{}),lifecycle_rows)
                except raw_lifecycle.Refusal:
                    # Ordinary raw work does not depend on a registry. Only an
                    # otherwise refused lifecycle write needs principal evidence.
                    # An unreadable registry proves no exemption; preserve the
                    # lifecycle refusal rather than blocking unrelated writes.
                    if not bound_raw_exemption():raise
            _guard_record_anchor_status(root,path,args,actor)
            _guard_record_anchor_title(root,path,args,actor,named)
            _guard_reserved_labels(root,path,args,actor)
            def bd_dispatch(argv):
                p=subprocess.run([str(root/'bin/bd'),'--directory',str(path),'--sandbox','--actor',actor,*argv],env=environment(root),capture_output=True,text=True,encoding='utf-8',timeout=120)
                # When bd itself says no, before it writes, that is a refusal and not an outcome
                # nobody knows (kittrial-5bb.185): the identity is released and the caller is told
                # bd's sentence. Only what bd_refusals recognises; anything else is handed on as it came.
                refused=bd_refusals.refusal(p.returncode,p.stdout,p.stderr)
                if refused is not None:return bd_refusals.envelope(*refused)
                stdout=p.stdout
                # `ready` is the listing a client uses to pick work: keep the merge slot and
                # other internal records out of it, the same rule `work --claimable` already
                # applies (kittrial-5bb.290). Only the JSON form is filtered; a text ready
                # listing is left as bd wrote it.
                if argv and argv[0]=='ready' and p.returncode==0 and stdout.lstrip()[:1] in ('[','{'):
                    # record_json.loads, not a bare json.loads: bd's answer is not trusted
                    # input (a deeply nested one must not raise RecursionError), and
                    # tests/test_record_json.py counts every bare parse per file.
                    try:
                        rows=record_json.loads(stdout)
                    except ValueError:
                        rows=None
                    if isinstance(rows,list):
                        from work import drop_internal_from_ready
                        stdout=json.dumps(drop_internal_from_ready(rows))+'\n'
                    elif isinstance(rows,dict) and isinstance(rows.get('items'),list):
                        from work import drop_internal_from_ready
                        rows=dict(rows);rows['items']=drop_internal_from_ready(rows['items'])
                        stdout=json.dumps(rows)+'\n'
                return {'returncode':p.returncode,'stdout':stdout,'stderr':p.stderr}
            runner=NativeRunner(bd_dispatch, project=path)
            def bd_effect():return runner(final)
            return guarded_write(root,request,journal_path(path),bd_effect,
                               authority_config=authority_config,
                               require_authority=require_authority,runner=runner)

# --- the commands a contributor may run (kittrial-5bb.283) --------------------------------
# This is a COPY of the table in client.py, kept byte-for-byte equal by tests/test_endpoint_help.py:
# a worker is handed client.py on its own, so that file cannot import this module, and this module
# runs on the office host and must not import the client. `help` does not normally reach here - the
# client answers it without the server - so this copy is what an older client and a direct caller
# get. The refusal below used to point a worker at the installation operator's own host tool.

#: The commands a contributor may run, one line each (kittrial-5bb.283): ``client.py help`` prints
#: this, an unknown command is answered with it, and this module's refusal repeats it. The header
#: above says why the table is copied here rather than imported.
CONTRIBUTOR_COMMANDS = (
    ('brief TASK', 'the compact read of one task: its state, its newer activity, its checkpoint'),
    ('history TASK', "the task's comments and review records, newest first"),
    ('checkpoint TASK --file FILE', 'record where you are on a task, for the next run'),
    ('work', 'the tasks whose move is yours, and the ones you may claim'),
    ('wait', 'block until something is your move, then print it and its fingerprint'),
    ('ready', 'the tasks nobody holds that you may claim'),
    ('session register|resume|end', 'the actor session the endpoint keeps for you'),
    ('review TASK --file FILE', 'contribute a delivery, answer review items, or record a review'),
    ('handoff ...', 'hand a task to another actor, and read what the endpoint reconciled'),
    ('guidance get|ack', 'the worker guidance in force, and the acknowledgement of a version'),
    ('onboard', 'what a new worker is told about this project'),
    ('docs NAME', 'a kit document; read `docs start` and `docs worker-guide` first'),
    ('feedback add|list|correct', 'Orchestra feedback - about Orchestra itself, not about the project'),
    ('requirement ...', 'requirement records (the project owns these)'),
    ('requirements ...', 'requirement reads and owner answers'),
    ('ref ...', 'reference records'),
    ('capability ...', 'capability records: find, get, list, propose, revise, verify'),
    ('proposal ...', 'proposal records and their review'),
    ('coordinator ...', 'the coordinator reads for this project'),
    ('refresh', 'refresh the cached export the reads are answered from'),
    ('view [NAME]', 'render one of the project views'),
    ('list', 'the tasks of the project'),
    ('show TASK', 'one task as the tracker holds it'),
    ('search TEXT', 'the tasks matching a search'),
    ('count', 'how many tasks match'),
    ('create ...', 'create a task'),
    ('update TASK ...', 'change a task'),
    ('close TASK', 'close a task'),
    ('reopen TASK', 'reopen a closed task'),
    ('comments TASK', 'the comments of a task'),
    ('comments add TASK --file FILE', 'add a comment'),
    ('dep ...', 'the dependencies of a task'),
    ('state ...', 'the state events of a task'),
    ('lint', 'check the tracker for problems'),
)

#: The two documents a new worker should read, named in every answer this module gives (the task
#: says both by name: kittrial-5bb.283).
DOCS_TO_READ_FIRST = ('docs start', 'docs worker-guide')

#: The sentence every answer about a command outside the contributor interface carries: the refusal
#: used to point a worker at the installation operator's own host tool (kittrial-5bb.283).
ADMIN_TOOL_NOTE = 'admin.py is for the installation operator on the server, not for workers'

#: What someone typing a name that is nearly a command probably meant. The two the task names, and
#: the spellings seen in the field; anything else falls back to difflib.
NEAREST_COMMANDS = {'briefing': 'brief TASK', 'workerguide': 'docs worker-guide',
                    'worker-guide': 'docs worker-guide', 'workersguide': 'docs worker-guide'}

#: The actions the client dispatches itself; every other name in the table above is passed to the
#: endpoint. The dispatch in main() is written in terms of this tuple, and a test holds every one of
#: its names to a line of the table, so the help cannot fall behind the commands (kittrial-5bb.283).
CLIENT_ACTIONS = ('brief','history','checkpoint','onboard','docs','session','handoff','review','work',
                  'wait','feedback','requirement','requirements','ref','capability','proposal','guidance',
                  'coordinator')


def contributor_command_names():
    """The first word of every command in the table: what a contributor may type."""
    return tuple(dict.fromkeys(usage.split()[0] for usage, _text in CONTRIBUTOR_COMMANDS))


def nearest_contributor_command(name):
    """The command ``name`` probably meant, or ``None`` (kittrial-5bb.283).

    The names people actually mistype are answered from the table above; anything else is matched
    against the command names, so a near miss still gets an answer rather than silence.
    """
    if name in NEAREST_COMMANDS:
        return NEAREST_COMMANDS[name]
    close = difflib.get_close_matches(name, sorted(contributor_command_names()), n=1, cutoff=0.6)
    return close[0] if close else None


def contributor_help_text(unknown=None):
    """The answer a contributor gets: the commands it may run, and the two documents to read.

    ``unknown`` is the command someone typed that is not one of them; the answer then names the
    nearest command when there is one, and always says where admin.py belongs (kittrial-5bb.283).
    """
    lines = []
    if unknown is None:
        lines.append('The commands a contributor may run, one line each:')
    else:
        lines.append('%r is outside the contributor interface. The commands a contributor may run, '
                     'one line each:' % unknown)
    lines += ['  %s - %s' % (usage, text) for usage, text in CONTRIBUTOR_COMMANDS]
    lines.append('')
    lines.append('Read %s and %s first.' % DOCS_TO_READ_FIRST)
    if unknown is not None:
        nearest = nearest_contributor_command(unknown)
        if nearest is not None:
            lines.append('Did you mean `%s`?' % nearest)
    lines.append(ADMIN_TOOL_NOTE + '.')
    return '\n'.join(lines)


def client_out_of_date_sentence(name):
    """One plain sentence for a command the CLIENT runs, sent to the endpoint by mistake.

    An older ``client.py`` (before CLIENT_ACTIONS) passes ``guidance`` or ``feedback``
    through as an action; the endpoint refused it with the contributor command list,
    which lists those same commands -- a contradiction that sent one lane looking for
    the cause for hours (kittrial-5bb.299). The answer now says the command is the
    client's own, that this client is older than the installation's kit, and how to
    update it. The endpoint still refuses; it never runs a client-side action.
    """
    from version import KIT_VERSION
    return ('`%s` is run by the client itself, not by the server. This client is older than '
            'the installation\'s kit (version %s). Update it by copying client.py from the kit; '
            'it carries its own wait logic, so wait needs no other kit file beside it (the '
            'capability commands still need capabilities.py).'
            % (name, KIT_VERSION))


def outside_contributor_interface(name):
    """Refuse ``name`` with the commands a contributor may run (kittrial-5bb.283).

    One place so the answer cannot drift from the table above, and so the refusal is what a test
    can call without a runtime: the caller is still refused - the endpoint refuses everything
    outside the contributor interface - it is only told what it may run instead. A name the
    CURRENT client dispatches locally (CLIENT_ACTIONS) is answered with one plain out-of-date
    sentence instead of the refusal-plus-list contradiction (kittrial-5bb.299).
    """
    if name in CLIENT_ACTIONS:
        raise ValueError(client_out_of_date_sentence(name))
    raise ValueError(contributor_help_text(None if name in ('help', '-h', '--help') else name))


def main():
    # No prefix matching: the wrapper passes these flags exactly, and an abbreviation must
    # not silently select a different option (kittrial-5bb.223, finding 7).
    p=argparse.ArgumentParser(allow_abbrev=False);p.add_argument('--root',required=True)
    p.add_argument('--authority-store',help='server-side live-authority document (HTTP service only)')
    p.add_argument('--authority-lock',help='server-side authority lock (defaults to STORE.lock)')
    p.add_argument('--require-authority',action='store_true',
                   help='refuse a mutation that omits the live-authority descriptor')
    p.add_argument('--service-namespace',
                   help="the web service's own actor namespace (default http): a name the "
                        'service was started under is refused at use too (kittrial-5bb.188 item 3)')
    p.add_argument('--terminal-endpoint-timeout', type=float, default=150,
                   help='trusted service launch timeout for terminal write abandonment fencing')
    p.add_argument('--key-project',action='append',metavar='NAME',
                   help='a project the calling SSH key is bound to (repeatable; passed only by '
                        'ssh_forced_command.py from the authorized_keys line): every request for '
                        'another project is refused (kittrial-5bb.193)')
    p.add_argument('--key-principal',action='append',default=None,metavar='NAME',
                   help='the principal the calling SSH key belongs to (passed only by '
                        'ssh_forced_command.py from the authorized_keys line): every request whose '
                        'actor that principal does not own in the project is refused, except a '
                        'session registration, which makes the new actor its own (kittrial-5bb.194; '
                        'given twice, refused)')
    a=p.parse_args()
    authority_config=None
    if a.authority_store:
        authority_config=AuthorityConfig(a.authority_store,a.authority_lock,a.service_namespace,
                                         a.terminal_endpoint_timeout)
    try:
        if a.key_project is not None and a.authority_store:
            # A key line is not the web service, and the service passes no such flag.
            raise ValueError('--key-project is for an SSH key line and cannot be combined with --authority-store')
        if a.key_principal is not None and a.authority_store:
            raise ValueError('--key-principal is for an SSH key line and cannot be combined with --authority-store')
        from admin import validate_name
        key_projects=None if a.key_project is None else frozenset(validate_name(name) for name in a.key_project)
        if a.key_principal is not None and len(a.key_principal)>1:
            # The flag may be named at most once: argparse would otherwise keep the last and a
            # line that names two principals would be served as one of them (finding 7).
            raise ValueError('--key-principal names %s twice; a key may name at most one principal'
                             %', '.join(str(value) for value in a.key_principal))
        from sessions import valid_principal
        key_principal=None if a.key_principal is None else valid_principal(a.key_principal[0],'--key-principal')
        text=sys.stdin.read(2_000_001)
        if len(text)>2_000_000:raise ValueError('Request exceeds 2 MB')
        answer=execute(root_path(a.root),record_json.loads(text),authority_config=authority_config,
                       require_authority=a.require_authority,key_projects=key_projects,key_principal=key_principal)
    except subprocess.TimeoutExpired:
        answer={'returncode':124,'stdout':'','stderr':'Command timed out; mutation outcome may be uncertain. Inspect state before retrying.\n'}
    except TimeoutError as waited:
        # A wait for a lock ran out before anything was done (file_lock raises it while acquiring):
        # the server is busy, and the request may be sent again. Not a rejection of the request.
        answer={'returncode':75,'stdout':'','stderr':'Busy: %s. Nothing was done; try again shortly.\n'%waited}
    except Exception as e:
        answer={'returncode':2,'stdout':'','stderr':f'{type(e).__name__}: {e}\n'}
        import raw_lifecycle
        if isinstance(e,raw_lifecycle.Refusal):
            answer['stderr']=f'ValueError: {e}\n'
        from review_workflow import CoordinatorRuleRefusal
        if isinstance(e,CoordinatorRuleRefusal):
            # Preserve the host's ValueError sentence while identifying a kit rule
            # refusal explicitly. HTTP never decides this from words in stderr.
            answer['stderr']=f'ValueError: {e}\n'
            answer['refused']='coordinator-rule'
        import actor_names
        if isinstance(e,actor_names.TrackerUnreadable):
            # The export could not be read (bd failed, or answered something that is not
            # rows): a host fault, not a refusal of the request. The service reads this mark
            # and answers 503 "nothing was changed" (kittrial-5bb.188 item 1); `fault` is how
            # it tells a read the service may retry from a rejection.
            answer['fault']='tracker'
        if isinstance(e,actor_names.TrackerRowsUnreadable):
            # The export was read but holds a row that cannot be parsed: not transient, so
            # its own mark and its own sentence naming the row ids and the operator repair
            # (kittrial-5bb.243 item N7), beside the bare fault above for older readers.
            # The ids ride as a field, bounded and checked by the exception itself, so the
            # web service builds its answer from them and never from a stderr tail a row
            # could shape (r2 review item 1).
            answer['fault']='unreadable-rows'
            answer['unreadable_rows']=list(e.ids or ())
            answer['unreadable_total']=e.total
        if isinstance(e,actor_names.TrackerMergeSlotMissing):
            # The read came back without the merge slot (rows, or no rows at all): not
            # transient, so its own mark and its own sentence naming the merge-create repair
            # (kittrial-5bb.202 item 1). The service answers its own 503 code and sentence.
            answer['fault']='merge-slot'
        if configuration_fault(a.root,e):
            # Not a fault of the request: the server's own configuration file cannot be read.
            # The line names the file, as it does for the operator; `fault` lets the web service
            # say it in its own words and keep the path to its log (kittrial-5bb.156).
            if isinstance(e,ConfigurationUnreadable):answer['stderr']=f'ValueError: {e}\n'
            answer['fault']='configuration'
    print(json.dumps(answer,ensure_ascii=False))

if __name__=='__main__':main()
