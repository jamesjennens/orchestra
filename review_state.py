"""The single review-state projection shared by `review`, `brief` and `work`.

The raw append-only workflow chain and the scoped lifecycle integration evidence
are combined here exactly once, so those three read surfaces cannot disagree
about whether the current contribution is integrated.

Raw workflow state stays available and clearly separate as ``workflow_state``.
Integration evidence is additive in ``integration``. Two deliberately different
questions are answered by two different fields:

* ``lifecycle_matches_contribution`` (top level, in `brief` and `work`) keeps its
  ORIGINAL meaning: does the scope currently shown in ``lifecycle`` /
  ``lifecycle_scope`` -- the NEWEST recorded scope -- belong to the current
  contribution? A client that applies the newest-scope ``lifecycle`` values to
  the current contribution must check this field first; it is False as soon as a
  later scope for other work is recorded.
* ``integration.matches_contribution`` is the additive ANY-scope answer: does any
  recorded scope name the FULL contribution commit? Scope order does not matter.

The ``integrated`` decision itself is any-pass-wins by design: a contribution is
integrated when ANY scope whose ``source_commit`` equals its FULL commit records
``integrated=passed``. A later release/live-verified scope therefore cannot
return settled work to the awaiting-integration queue, and an older pass is not
undone by a newer failure recorded against the same commit under a different
scope. ``integration.newest_fact`` exposes the newest matching scope's value, so
that rule is auditable rather than accidental.

Each ``prior_contributions`` entry carries the SAME additive ``integration``
block, computed from that entry's own FULL commit and the same scopes, so a read
of a follow-on says whether the replaced revision is integrated instead of only
showing its record, commit and relation. Prior entries keep every existing key;
the any-pass-wins rule above is applied unchanged.

Two admission rules narrow the passing scopes before any-pass-wins is applied,
and neither switches the rule to newest-wins:

* an explicit, operator-audited REVERT record (`Kind: integration-revert-v1`)
  removes ONE recorded integration commit from the passing set, so the
  contribution reads as not integrated for that commit; a later
  ``integrated=passed`` recording a DIFFERENT ``integration_commit`` is not
  affected and re-integrates the work. ``integration.reverted`` reports this;
* the owner decision on kittrial-5bb.32 (comment 01a0eea4) is to KEEP
  any-pass-wins. The conflict it can still produce -- a newer matching scope
  whose value differs from the reported one -- is not silently resolved; it is
  surfaced by ``integration_disagreements`` and by a warning on every read.
"""


def integration(contribution, scopes, reverts=None):
    """Effective integration evidence for one contribution.

    ``scopes`` is the ``lifecycle.integration_evidence`` entry for the task: one
    entry per scope token, each carrying the order of its NEWEST recording, so a
    recurring token cannot resurrect an older position. The reported scope is the
    newest scope that records a trusted pass, otherwise the newest matching scope
    of any value; ties break on the scope token, so the reported scope is
    deterministic. ``fact`` is ``passed`` when any matching scope passes
    (any-pass-wins); ``newest_fact`` reports the newest matching scope's value.

    ``reverts`` is an optional list of validated revert records, each carrying
    ``contribution`` (the contribution comment id) and ``integration_commit``
    (``{"contribution", "integration_commit", "revert_commit", "reason",
    "evidence", "operator"}``). A matching scope whose ``integration_commit`` is
    named by a revert for this contribution no longer counts as a passing scope,
    so the any-pass-wins answer is "not integrated for that commit" while every
    other scope keeps its meaning. A pass recorded afterwards under a different
    integration commit is untouched and re-integrates the work. ``reverted``
    reports whether any named commit was removed; ``fact`` is ``reverted`` when
    nothing passes and the reported scope is a reverted one.
    """
    commit=str((contribution or {}).get('commit') or '')
    cid=str((contribution or {}).get('comment_id') or '')
    matching=[entry for entry in (scopes or [])
              if commit and str((entry.get('scope') or {}).get('source_commit') or '').lower()==commit.lower()]
    def recency(entry):
        order=entry.get('order')
        return (order if isinstance(order,int) else -1,str(entry.get('scope_token') or ''))
    reverted_commits={str(r.get('integration_commit') or '').lower()
                      for r in (reverts or [])
                      if str(r.get('contribution') or '')==cid and r.get('integration_commit')}
    def is_reverted(entry):
        integration_commit=str((entry.get('scope') or {}).get('integration_commit') or '').lower()
        return bool(integration_commit) and integration_commit in reverted_commits
    passing=[entry for entry in matching if (entry.get('integrated') or {}).get('value')=='passed']
    passed=[entry for entry in passing if not is_reverted(entry)]
    newest=max(matching,key=recency) if matching else None
    chosen=max(passed,key=recency) if passed else newest
    scope=(chosen or {}).get('scope') or {}
    reverted_fact=bool(passing) and not passed and newest is not None and is_reverted(newest)
    if reverted_fact:
        value='reverted'
    elif chosen:
        value=(chosen.get('integrated') or {}).get('value','unknown')
    else:
        value='unknown'
    return {'fact':value,
            'scope':chosen.get('scope') if chosen else None,
            'scope_token':chosen.get('scope_token') if chosen else None,
            'newest_scope':newest.get('scope') if newest else None,
            'source_commit':scope.get('source_commit') or None,
            'integration_commit':scope.get('integration_commit') or None,
            'matches_contribution':bool(matching),
            'newest_fact':(newest.get('integrated') or {}).get('value','unknown') if newest else 'unknown',
            'newest_scope_token':newest.get('scope_token') if newest else None,
            'reverted':reverted_fact}


def integration_disagreements(result):
    """Machine-readable any-pass-wins conflicts for one effective projection.

    An entry is reported whenever, for the CURRENT contribution or any
    ``prior_contributions`` entry, ``integration.newest_fact`` differs from
    ``integration.fact``: the newest matching scope disagrees with the scope the
    any-pass-wins rule reports. Both facts and both scopes are named, so a reader
    can see what conflicted without re-deriving the rule. The owner decision on
    kittrial-5bb.32 is to keep any-pass-wins, so this is deliberately a signal,
    not a state change. ``kind`` distinguishes the two causes:

    * ``newest-scope-disagrees`` -- a newer matching scope records a different
      value (typically ``integrated=failed``) while an older pass still governs;
    * ``reverted`` -- an operator reverted the reported integration commit, so
      the reported fact is ``reverted``. A later pass under a different
      integration commit clears it.

    Returns an empty list for a result that carries no integration block (a raw
    workflow projection) and for a prior entry without one.
    """
    found=[]
    def block(current, comment_id, relation):
        if not isinstance(current, dict):
            return
        evidence=current.get('integration')
        if not isinstance(evidence, dict):
            return
        fact, newest=evidence.get('fact'), evidence.get('newest_fact')
        if fact==newest or fact is None or newest is None:
            return
        found.append({'kind':'reverted' if evidence.get('reverted') else 'newest-scope-disagrees',
                      'contribution':comment_id,'relation':relation,
                      'fact':fact,'newest_fact':newest,
                      'scope':evidence.get('scope'),'newest_scope':evidence.get('newest_scope')})
    block(result, ((result.get('contribution') or {}).get('comment_id')
                   if isinstance(result.get('contribution'), dict) else None), None)
    for prior in result.get('prior_contributions') or []:
        if isinstance(prior, dict):
            block(prior, prior.get('comment_id'), prior.get('relation'))
    return found


def disagreement_warnings(disagreements):
    """Human-readable one-line warnings for ``integration_disagreements`` entries."""
    lines=[]
    for item in disagreements:
        where=('the current contribution' if item.get('contribution') is None else
               'contribution ' + str(item.get('contribution')) +
               (' (relation: ' + item['relation'] + ')' if item.get('relation') else ''))
        lines.append('Integration fact disagreement for ' + where + ': the any-pass-wins fact is ' +
                     str(item.get('fact')) + ' under scope ' + _scope_text(item.get('scope')) +
                     ' but the newest matching scope records ' + str(item.get('newest_fact')) +
                     ' under scope ' + _scope_text(item.get('newest_scope')) +
                     '; any-pass-wins is retained by owner decision, so both facts and scopes are named here')
    return lines


def _scope_text(scope):
    if not isinstance(scope, dict):
        return 'unknown'
    return '{source_commit=%s, integration_commit=%s, release_id=%s, environment=%s}' % (
        scope.get('source_commit') or '-', scope.get('integration_commit') or '-',
        scope.get('release_id') or '-', scope.get('environment') or '-')


def effective(result, scopes=None, workflow_state=None):
    """Apply the integration overlay to one raw workflow projection result.

    ``workflow_state`` overrides the raw state recorded alongside the effective
    one; `project` passes the pre-legacy-label state so the raw chain value stays
    visible even when the legacy ``review-ready`` label supplies the state.

    Every ``prior_contributions`` entry gets the same additive ``integration``
    block as the current contribution, computed from that entry's own commit over
    the same ``scopes``. Existing entry keys are untouched, so a follow-on read
    can answer "is the revision this one follows already integrated?" without
    re-implementing the rule. The answer is only added when the raw result
    carries the key, which keeps direct callers of this function working.

    The answer also consults ``result['reverts']`` (validated operator revert
    records, supplied by the workflow projection; empty when absent), and every
    ``newest_fact``/``fact`` disagreement it finds is reported additively as
    ``integration_disagreements`` plus one warning on the existing ``warnings``
    list, so the owner decision to keep any-pass-wins stays visible on every read
    instead of being silently resolved.
    """
    reverts=result.get('reverts') or []
    evidence=integration(result.get('contribution'),scopes,reverts)
    state=result['review_state']
    if state=='awaiting-integration' and evidence['matches_contribution'] and evidence['fact']=='passed':
        state='integrated'
    answer=dict(result,review_state=state,
                workflow_state=result['review_state'] if workflow_state is None else workflow_state,
                integration=evidence)
    priors=result.get('prior_contributions')
    if priors is not None:
        answer['prior_contributions']=[dict(c,integration=integration(c,scopes,reverts)) for c in priors]
    disagreements=integration_disagreements(answer)
    answer['integration_disagreements']=disagreements
    warnings=answer.get('warnings')
    if warnings is not None:
        warnings.extend(disagreement_warnings(disagreements))
    return answer


def scopes_for(rows, task):
    """The lifecycle integration-evidence scopes for one task (empty when absent).

    The one place that decides how raw native rows become the scoped evidence the
    projection consumes, so `review`, `brief`, `work` and the review receipt all
    read the same values.
    """
    from lifecycle import integration_evidence
    return next((r['scopes'] for r in integration_evidence(rows) if r['id']==task),[])


def reverts_for(rows, task, operators=None):
    """The validated operator revert records for one task (empty when absent).

    Thin, import-safe bridge to ``review_workflow.revert_records`` so callers that
    only hold raw native rows (``work``, the HTTP adapters) can hand the shared
    projection the same reverts ``review``/``brief`` see. An unconfigured operator
    allowlist authorizes nobody, so an unaudited record is ignored here exactly as
    it is there; a reader never fails because of one.
    """
    from review_workflow import revert_records
    issue=next((r for r in rows if r.get('id')==task and r.get('issue_type')!='event'),None)
    if issue is None:
        return []
    return revert_records(issue,operators)[0]


def project(issue, scopes=None, operators=None, reverts=None):
    """Raw workflow state plus the effective, integration-aware review state.

    ``operators`` is the void-record operator authority the caller already
    resolved (the endpoint's deployment allowlist, or admin.py's); it is threaded
    straight through to the void-aware review projection so an operator void is
    only applied when its author is on that same allowlist. It adds no new state
    and defaults to None, which keeps the pre-existing host fallback intact.

    ``reverts`` is the optional validated operator revert list; when omitted the
    raw workflow projection reads it from the same issue under the same operator
    authority, so every caller gets the audited revert projection without
    threading it manually.
    """
    from review_workflow import project as workflow
    result=workflow(issue, operators)
    if reverts is not None:
        result=dict(result,reverts=list(reverts))
    raw=result['review_state']
    if raw=='none' and 'review-ready' in (issue.get('labels') or []):
        result=dict(result,review_state='legacy-review-ready')
    return effective(result,scopes,raw)
