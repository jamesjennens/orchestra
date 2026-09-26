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
"""


def integration(contribution, scopes):
    """Effective integration evidence for one contribution.

    ``scopes`` is the ``lifecycle.integration_evidence`` entry for the task: one
    entry per scope token, each carrying the order of its NEWEST recording, so a
    recurring token cannot resurrect an older position. The reported scope is the
    newest scope that records a trusted pass, otherwise the newest matching scope
    of any value; ties break on the scope token, so the reported scope is
    deterministic. ``fact`` is ``passed`` when any matching scope passes
    (any-pass-wins); ``newest_fact`` reports the newest matching scope's value.
    """
    commit=str((contribution or {}).get('commit') or '')
    matching=[entry for entry in (scopes or [])
              if commit and str((entry.get('scope') or {}).get('source_commit') or '').lower()==commit.lower()]
    def recency(entry):
        order=entry.get('order')
        return (order if isinstance(order,int) else -1,str(entry.get('scope_token') or ''))
    passed=[entry for entry in matching if (entry.get('integrated') or {}).get('value')=='passed']
    newest=max(matching,key=recency) if matching else None
    chosen=max(passed,key=recency) if passed else newest
    scope=(chosen or {}).get('scope') or {}
    return {'fact':(chosen.get('integrated') or {}).get('value','unknown') if chosen else 'unknown',
            'scope':chosen.get('scope') if chosen else None,
            'scope_token':chosen.get('scope_token') if chosen else None,
            'source_commit':scope.get('source_commit') or None,
            'integration_commit':scope.get('integration_commit') or None,
            'matches_contribution':bool(matching),
            'newest_fact':(newest.get('integrated') or {}).get('value','unknown') if newest else 'unknown',
            'newest_scope_token':newest.get('scope_token') if newest else None}


def effective(result, scopes=None, workflow_state=None):
    """Apply the integration overlay to one raw workflow projection result.

    ``workflow_state`` overrides the raw state recorded alongside the effective
    one; `project` passes the pre-legacy-label state so the raw chain value stays
    visible even when the legacy ``review-ready`` label supplies the state.
    """
    evidence=integration(result.get('contribution'),scopes)
    state=result['review_state']
    if state=='awaiting-integration' and evidence['matches_contribution'] and evidence['fact']=='passed':
        state='integrated'
    return dict(result,review_state=state,
                workflow_state=result['review_state'] if workflow_state is None else workflow_state,
                integration=evidence)


def scopes_for(rows, task):
    """The lifecycle integration-evidence scopes for one task (empty when absent).

    The one place that decides how raw native rows become the scoped evidence the
    projection consumes, so `review`, `brief`, `work` and the review receipt all
    read the same values.
    """
    from lifecycle import integration_evidence
    return next((r['scopes'] for r in integration_evidence(rows) if r['id']==task),[])


def project(issue, scopes=None):
    """Raw workflow state plus the effective, integration-aware review state."""
    from review_workflow import project as workflow
    result=workflow(issue)
    raw=result['review_state']
    if raw=='none' and 'review-ready' in (issue.get('labels') or []):
        result=dict(result,review_state='legacy-review-ready')
    return effective(result,scopes,raw)
