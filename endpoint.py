#!/usr/bin/env python3
"""One request per process. JSON on stdin/stdout; no contributor shell interpolation.

The same entry point serves the SSH worker and the trusted HTTP service. The HTTP
service adds ``--authority-store``/``--authority-lock`` (and ``--require-authority``
for mutations) to the launch command; those are server-side configuration and are
never taken from the request body. An SSH-shaped request therefore cannot choose the
live-authority document or the lock path, and can only omit the check because it has
no HTTP principal at all.
"""
import argparse
import fcntl
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from admin import environment,project_dir,root_path,operators as configured_operators,verifiers as configured_verifiers
import native
from render import render
from lifecycle import apply_native
from version import report
from reserved_comments import (carries_record_label, check_raw_request, comment_target,
                               first_reserved_label, is_record_anchor, is_requirement_record_anchor,
                               label_guard_request,
                               operator_only_in_args, raw_file_flag_in_args,
                               reserved_label_in_args, refuse_http_actor, status_change_targets,
                               unresolved_bd_flags)
from http_authority import AuthorityConfig, NativeRunner, http_actor_denial, journal_path, run_guarded

ALLOWED={'list','show','ready','search','count','create','update','close','reopen','comments','dep','state','lint'}
# Legacy name kept for operators reading this file; enforcement is the
# spelling-aware reserved_comments.operator_only_in_args() below.
FORBIDDEN={'--directory','-C','--db','--repo','--global','--actor','--author','--profile','--graph','--config','--metadata'}
FILE_FLAGS={'--body-file','--design-file','--file','-f'}

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
    except ValueError:raise ValueError('Could not parse the current labels of %s before the label write; refusing.'%(task,))
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
    except ValueError:raise ValueError('Could not parse the comments of %s before the label write; refusing.'%(task,))
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
    except ValueError:raise ValueError('Could not parse the current rows of %s before the status write; refusing.'%(', '.join(tokens),))
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
    kittrial-5bb.92 item 4). A requirement/brd-section record anchor is covered by the
    same guard through `is_requirement_record_anchor` (kittrial-5bb.92 review item 1).
    Any flag that moves status or assignee counts, including the short `-s` spellings,
    `--claim`, `--defer` and `--assignee` (`status_change_targets`). Runs under the same
    coordination lock as the write it guards, reads every named target in ONE native
    read, and fails closed when the target cannot be resolved: bd would otherwise act on
    the last touched issue, which this guard cannot verify.
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
        if is_requirement_record_anchor(row):
            raise ValueError('Refusing to %s %s: it is a requirement/brd-section record anchor, whose status only '
                             'its record operations may change.'%(command,canonical))

def execute(root,request,authority_config=None,require_authority=False):
    name=request['project'];path=project_dir(root,name)
    if not (path/'.beads/metadata.json').is_file():raise ValueError('Unknown/uninitialized project')
    actor=request.get('actor','')
    refuse_http_actor(actor,authority_config is not None)
    # Launched by the HTTP service, an HTTP-shaped actor still needs the verified
    # descriptor on every action, with or without --require-authority (review 01a10262).
    denied=http_actor_denial(request,authority_config)
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
            result=session_execute(path,name,args,export,actor=actor)
        result['provenance'] = {'kit': report(Path(__file__).resolve().parent, 'kit')}
        return {'returncode':0,'stdout':json.dumps(result,ensure_ascii=False)+'\n','stderr':''.join(session_warnings)}
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
        def work_effect():
            return {'returncode':0,'stdout':json.dumps(work_execute(path,actor,action,args,request.get('attachments',{}),runner,
                                                                    operators=configured_operators(root),
                                                                    verifiers=configured_verifiers(root)),ensure_ascii=False,indent=2)+'\n','stderr':''.join(run_warnings)}
        with (path/'.coordination.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            return run_guarded(request,journal_path(path),work_effect,
                               authority_config=authority_config,
                               require_authority=require_authority,runner=runner)
    if action in ('onboard','docs'):
        from onboarding import execute as onboard
        return {'returncode':0,'stdout':onboard(Path(__file__).resolve().parent,path,name,actor,action,request.get('args',[]),endpoint=Path(__file__).resolve()),'stderr':''}
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
        if (not isinstance(ids,list) or len(ids)>ANCHOR_READ_IDS_MAX or len(set(ids))!=len(ids)
                or any(not isinstance(x,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,160}',x) for x in ids)):
            raise ValueError('anchors takes no arguments, or at most %d distinct task ids'%ANCHOR_READ_IDS_MAX)
        if ids:
            completed=native.run(native.argv(root,path,actor,['show',*ids,'--json','--include-comments']),environment(root))
            try:missing=completed.returncode==1 and json.loads(completed.stdout).get('error')=='no issues found matching the provided IDs'
            except (ValueError,AttributeError):missing=False
            if missing:
                # Every listed row was deleted after the list: none is an anchor.
                rows,warnings=[],completed.stderr or ''
            else:
                stdout,warnings=native.split(completed)
                rows=json.loads(stdout or '[]')
                rows=[r for r in (rows if isinstance(rows,list) else [rows]) if isinstance(r,dict) and r.get('id') in ids]
        else:
            stdout,warnings=native.split(native.run(native.argv(root,path,actor,['export','--all']),environment(root)))
            rows=[json.loads(line) for line in stdout.splitlines() if line.strip()]
        return {'returncode':0,'stdout':json.dumps({'schema_version':1,'anchors':record_anchor_ids(rows)})+'\n','stderr':warnings}
    if action=='ref':
        # The reference catalog (.41 slice 1, kittrial-5bb.66). Reads (get, list, help)
        # read their own key, or the catalog in two native reads (reference_records), take no
        # coordination lock and are not run_guarded (like the anchors read); propose
        # and revise are writes, under the lock and the operation journal.
        import reference_records
        args=request.get('args',[])
        if not isinstance(args,list) or any(not isinstance(x,str) or '\0' in x for x in args):raise ValueError('Expected argument list')
        run_warnings=[]
        def run(argv):
            stdout,warnings=native.split(native.run(native.argv(root,path,actor,argv),environment(root)))
            if warnings:run_warnings.append(warnings)
            return stdout
        if not args or args[0] not in reference_records.CONTRIBUTOR_OPERATIONS:
            result=reference_records.read(args,run,configured_operators(root))
            return {'returncode':0,'stdout':json.dumps(result,ensure_ascii=False)+'\n','stderr':''.join(run_warnings)}
        payload=reference_records.write_payload(args,request.get('attachments',{}))
        runner=NativeRunner(run)
        def ref_effect():
            result=reference_records.apply_native(payload,actor,runner,path,operators=configured_operators(root))
            return {'returncode':0,'stdout':json.dumps(result,ensure_ascii=False)+'\n','stderr':''.join(run_warnings)}
        with (path/'.coordination.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            return run_guarded(request,journal_path(path),ref_effect,
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
            return run_guarded(request,journal_path(path),capability_effect,
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
            return run_guarded(request,journal_path(path),proposal_effect,
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
            return run_guarded(request,journal_path(path),briefing_effect,
                               authority_config=authority_config,
                               require_authority=require_authority,runner=runner)
    if action in ('lifecycle','coordinate','requirement'):
        args=request.get('args',[])
        if not isinstance(args,list) or len(args)!=1 or not isinstance(args[0],str):raise ValueError('Expected one JSON payload')
        payload=json.loads(args[0])
        run_warnings=[]
        def run(argv):
            stdout,warnings=native.split(native.run(native.argv(root,path,actor,argv),environment(root)))
            if warnings:run_warnings.append(warnings)
            return stdout
        runner=NativeRunner(run)
        def lifecycle_effect():
            if action=='lifecycle':result=apply_native(payload,actor,runner)
            elif action=='coordinate':
                from coordination import apply_native as coordinate
                result=coordinate(payload,actor,runner,path)
            else:
                from requirement_records import apply_native as requirement_apply
                result=requirement_apply(payload,actor,runner,path)
            return {'returncode':0,'stdout':json.dumps(result,ensure_ascii=False)+'\n','stderr':''.join(run_warnings)}
        with (path/'.coordination.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            return run_guarded(request,journal_path(path),lifecycle_effect,
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
        return {'returncode':0,'stdout':json.dumps(result,ensure_ascii=False)+'\n','stderr':''}
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
            rows=[json.loads(line) for line in p.stdout.splitlines() if line.strip()]
            return {'returncode':0,'stdout':json.dumps(render(rows,path/'views',configured_operators(root),configured_verifiers(root)))+'\n','stderr':p.stderr}
    if action!='bd':raise ValueError('Unknown action')
    args=request.get('args',[])
    if not isinstance(args,list) or not args or any(not isinstance(a,str) or '\0' in a for a in args):raise ValueError('Expected argument list')
    if args[0] not in ALLOWED:raise ValueError('Command is outside the contributor interface; use admin.py for setup/maintenance')
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
            _guard_reserved_labels(root,path,args,actor)
            _guard_record_anchor_status(root,path,args,actor)
            def bd_dispatch(argv):
                p=subprocess.run([str(root/'bin/bd'),'--directory',str(path),'--sandbox','--actor',actor,*argv],env=environment(root),capture_output=True,text=True,encoding='utf-8',timeout=120)
                return {'returncode':p.returncode,'stdout':p.stdout,'stderr':p.stderr}
            runner=NativeRunner(bd_dispatch)
            def bd_effect():return runner(final)
            return run_guarded(request,journal_path(path),bd_effect,
                               authority_config=authority_config,
                               require_authority=require_authority,runner=runner)

def main():
    p=argparse.ArgumentParser();p.add_argument('--root',required=True)
    p.add_argument('--authority-store',help='server-side live-authority document (HTTP service only)')
    p.add_argument('--authority-lock',help='server-side authority lock (defaults to STORE.lock)')
    p.add_argument('--require-authority',action='store_true',
                   help='refuse a mutation that omits the live-authority descriptor')
    a=p.parse_args()
    authority_config=None
    if a.authority_store:
        authority_config=AuthorityConfig(a.authority_store,a.authority_lock)
    try:
        text=sys.stdin.read(2_000_001)
        if len(text)>2_000_000:raise ValueError('Request exceeds 2 MB')
        answer=execute(root_path(a.root),json.loads(text),authority_config=authority_config,
                       require_authority=a.require_authority)
    except subprocess.TimeoutExpired:
        answer={'returncode':124,'stdout':'','stderr':'Command timed out; mutation outcome may be uncertain. Inspect state before retrying.\n'}
    except Exception as e:
        answer={'returncode':2,'stdout':'','stderr':f'{type(e).__name__}: {e}\n'}
    print(json.dumps(answer,ensure_ascii=False))

if __name__=='__main__':main()
