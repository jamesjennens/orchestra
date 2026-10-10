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
import fcntl
import json
import os
import record_json
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from admin import ConfigurationUnreadable,deployment_document,deployment_password,environment,project_dir,root_path,operators as configured_operators,verifiers as configured_verifiers,review_workflow_writes as configured_review_writes
import bd_refusals
import native
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
from http_authority import AuthorityConfig, NativeRunner, CAP_PROJECT_ADMIN, descriptor_actor_denial, http_actor_denial, journal_path, run_guarded, stamp_write

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

def _bd_read(root,path,actor,argv,resolver_absence=False):
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
    error object is a failed read, and no other guard read reads the object as absence
    (kittrial-5bb.138 items ``rc0`` and ``absence-pins``).
    """
    try:
        p=subprocess.run([str(root/'bin/bd'),'--directory',str(path),'--sandbox','--actor',actor,*argv],env=environment(root),capture_output=True,text=True,encoding='utf-8',timeout=60)
    except subprocess.TimeoutExpired:
        return None,'the read of the tasks timed out'
    said=(p.stderr or '').strip()
    if not (p.stdout or '').strip():
        if p.returncode and 'no issue found' in said:
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
    rows=[row for row in rows if isinstance(row,dict) and isinstance(row.get('id'),str)]
    if p.returncode and not rows and 'no issue found' not in said and not (resolver_absence and resolver_answer and p.returncode==1):
        return None,'bd could not read the tasks (%s)'%(said[-200:] or 'exit %d'%p.returncode)
    return rows,said

def _resolve_rows(root,path,actor,tokens):
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
    rows,said=_bd_read(root,path,actor,['show',*tokens,'--json'])
    if rows is None:return [],said
    if len({row['id'] for row in rows})==len(tokens) and 'no issue found' not in said:return rows,None
    found=[]
    for token in tokens:
        rows,said=_bd_read(root,path,actor,['show',token,'--json'])
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

def _guard_named_rows(root,path,name,args,attachments,actor):
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
    rows,problem=_resolve_rows(root,path,actor,request['targets'])
    if problem:
        raise ValueError('Refusing %s: %s, so the rows this would write are not known. Name each task by its id. Nothing was written.'%(command,problem))
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
    exits nonzero, an export that holds an unreadable row or a line that is not a row, or
    text that is not rows at all --, and a read that came back without the
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
    if any(not isinstance(row,dict) or row.get('malformed') for row in rows) \
            or (not rows and text.strip()):
        # One unreadable row refuses the whole read (not only its own names): the row may be
        # the one that holds the name, and an unreadable row's id never proves the slot
        # (kittrial-5bb.221). Text came back, but it is not rows at all (``null``, which the
        # row reader drops): the same failed read, not an empty tracker (kittrial-5bb.202).
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
SERVICE_ONLY_ACTIONS=('create-project','project-creations','creation-standing')

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
    # Two actions exist only for the web service and name no existing project
    # (kittrial-5bb.118 part 2); project_creation holds them, with what stops other callers.
    if request.get('action')=='create-project':
        import project_creation
        project_dir(root,request.get('project'))          # the name's shape, before anything else
        return project_creation.create_action(root,request,authority_config)
    if request.get('action')=='project-creations':
        import project_creation
        return project_creation.list_action(root,request,authority_config)
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
    if action in ('handoff','review','work'):
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
        runner=NativeRunner(run)
        # A malformed review_workflow_writes value reads as OFF and warns instead of
        # failing every work/review action (kittrial-5bb.110 item 2).
        switch_warnings=[]
        review_writes=configured_review_writes(root,warnings=switch_warnings)
        run_warnings.extend(switch_warnings)
        def work_effect():
            result=work_execute(path,actor,action,args,request.get('attachments',{}),runner,
                                operators=configured_operators(root),
                                verifiers=configured_verifiers(root),
                                review_writes=review_writes)
            # A handoff request and a decline are recorded in the kit's handoff journal and move
            # nothing in bd, so the runner saw no write; they are writes all the same (review
            # of kittrial-5bb.97). One that was already recorded (`reconciled`) wrote nothing.
            if action=='handoff' and len(args)==2 and isinstance(result,dict) and result.get('reconciled') is False:runner.wrote=True
            return {'returncode':0,'stdout':json.dumps(result,ensure_ascii=False,indent=2)+'\n','stderr':''.join(run_warnings)}
        with (path/'.coordination.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
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
        with (path/'.coordination.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            result=read_requirements(path,name,request.get('args',[]),run,configured_operators(root))
        return {'returncode':0,'stdout':json.dumps(result,ensure_ascii=False)+'\n','stderr':''.join(run_warnings)}
    if action=='owner-requirements':
        from requirement_http import web_action
        run_warnings=[]
        def run(argv):
            stdout,warnings=native.split(native.run(native.argv(root,path,actor,argv),environment(root)))
            if warnings:run_warnings.append(warnings)
            return stdout
        runner=NativeRunner(run)
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
    if action=='setup-status':
        # Read-only (kittrial-5bb.118): what the host knows about this project's setup,
        # for the web setup page. States, versions and times only; never guidance or
        # onboarding text. It takes no lock and writes nothing.
        if request.get('args',[]) not in ([],None):raise ValueError('Use setup-status without arguments')
        from admin import project_setup_status
        return {'returncode':0,'stdout':json.dumps(project_setup_status(root,name))+'\n','stderr':''}
    if action=='guidance':
        # The standing guidance channel (kittrial-5bb.99 slice 1, revised). The
        # endpoint is read-only except for the caller's own acknowledgement: the only
        # guidance writer is the operator host command `admin.py set-guidance`, and
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
                             'The only writer is the operator host command admin.py set-guidance.'%subcommand)
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
        runner=NativeRunner(run)
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
        runner=NativeRunner(run)
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
        # decide run for a signed-in member whose reviews.approve the guard has just
        # re-validated, and a submission's submitter is bound to the verified account.
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
        runner=NativeRunner(run)
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
        operators=configured_operators(root)
        verifiers=configured_verifiers(root)
        run_warnings=[]
        def coordinator_run(argv):
            stdout,warnings=native.split(native.run(native.argv(root,path,actor,argv),environment(root)))
            if warnings:run_warnings.append(warnings)
            return stdout
        runner=NativeRunner(coordinator_run)
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
                if target.is_file() and not target.is_symlink():
                    try:
                        previous=target.read_text(encoding='utf-8-sig')
                    except (UnicodeDecodeError,OSError):
                        # A damaged previous document must not stop the route from replacing it
                        # (review of 6f3007a, finding 1; kittrial-5bb.238 item 1): the host command
                        # replaces it, and a caller with no shell has no other way. The unreadable
                        # previous text is treated as changed, and the answer says so in a sentence
                        # rather than the bare UnicodeDecodeError that used to answer rc 2 with
                        # nothing written.
                        run_warnings.append('the previous ONBOARDING.md could not be read as UTF-8 text; '
                                            'it was treated as changed and replaced')
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
        runner=NativeRunner(run)
        def briefing_effect():
            return {'returncode':0,'stdout':briefing_execute(root,path,name,actor,action,args,request.get('attachments',{}),runner,
                                                             operators=configured_operators(root),
                                                             verifiers=configured_verifiers(root)),'stderr':''.join(run_warnings)}
        with (path/'.coordination.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            return guarded_write(root,request,journal_path(path),briefing_effect,
                               authority_config=authority_config,
                               require_authority=require_authority,runner=runner)
    if action in ('lifecycle','coordinate','requirement'):
        args=request.get('args',[])
        if not isinstance(args,list) or len(args)!=1 or not isinstance(args[0],str):raise ValueError('Expected one JSON payload')
        payload=record_json.loads(args[0])
        run_warnings=[]
        def run(argv):
            stdout,warnings=native.split(native.run(native.argv(root,path,actor,argv),environment(root)))
            if warnings:run_warnings.append(warnings)
            return stdout
        runner=NativeRunner(run)
        def lifecycle_effect():
            if action=='lifecycle':result=apply_native(payload,actor,runner,
                                                       operators=configured_operators(root),journal=path)
            elif action=='coordinate':
                from coordination import apply_native as coordinate
                result=coordinate(payload,actor,runner,path)
            else:
                from requirement_records import apply_native as requirement_apply
                result=requirement_apply(payload,actor,runner,path)
            return {'returncode':0,'stdout':json.dumps(result,ensure_ascii=False)+'\n','stderr':''.join(run_warnings)}
        with (path/'.coordination.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            return guarded_write(root,request,journal_path(path),lifecycle_effect,
                               authority_config=authority_config,
                               require_authority=require_authority,runner=runner)
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
            return {'returncode':0,'stdout':json.dumps(render(rows,path/'views',configured_operators(root),configured_verifiers(root)))+'\n','stderr':p.stderr+''.join(refresh_warnings)}
    if action!='bd':raise ValueError('Unknown action')
    args=request.get('args',[])
    if not isinstance(args,list) or not args or any(not isinstance(a,str) or '\0' in a for a in args):raise ValueError('Expected argument list')
    if args[0] not in ALLOWED:raise ValueError('Command is outside the contributor interface; use admin.py for setup/maintenance')
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
        with (path/'.coordination.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            # First the rows the write names (an existing id given to create, a write that names
            # none, the merge slot); then the guards that read what those rows carry.
            named=_guard_named_rows(root,path,name,args,request.get('attachments',{}),actor)
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
                return {'returncode':p.returncode,'stdout':p.stdout,'stderr':p.stderr}
            runner=NativeRunner(bd_dispatch)
            def bd_effect():return runner(final)
            return guarded_write(root,request,journal_path(path),bd_effect,
                               authority_config=authority_config,
                               require_authority=require_authority,runner=runner)

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
        authority_config=AuthorityConfig(a.authority_store,a.authority_lock,a.service_namespace)
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
        import actor_names
        if isinstance(e,actor_names.TrackerUnreadable):
            # The export could not be read (bd failed, or answered something that is not
            # rows): a host fault, not a refusal of the request. The service reads this mark
            # and answers 503 "nothing was changed" (kittrial-5bb.188 item 1); `fault` is how
            # it tells a read the service may retry from a rejection.
            answer['fault']='tracker'
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
