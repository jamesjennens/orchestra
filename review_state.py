"""The single review-state projection shared by `review`, `brief` and `work`.

The raw append-only workflow chain and the scoped lifecycle integration evidence
are combined here exactly once, so those three read surfaces cannot disagree
about whether the current contribution is integrated.

Raw workflow state stays available and clearly separate as ``workflow_state``.
Integration evidence is additive in ``integration``: a contribution counts as
integrated when ANY lifecycle scope whose ``source_commit`` equals its FULL
commit records ``integrated=passed``; scope order never changes the answer.
"""


def integration(contribution, scopes):
    """Effective integration evidence for one contribution.

    ``scopes`` is the ``lifecycle.integration_evidence`` entry for the task
    (newest recorded scope first). The chosen scope is reported for humans; the
    ``integrated`` decision is order-independent because every matching scope is
    inspected.
    """
    commit=str((contribution or {}).get('commit') or '')
    matching=[entry for entry in (scopes or [])
              if commit and str((entry.get('scope') or {}).get('source_commit') or '').lower()==commit.lower()]
    passed=[entry for entry in matching if (entry.get('integrated') or {}).get('value')=='passed']
    chosen=(passed or matching or [None])[0]
    scope=(chosen or {}).get('scope') or {}
    return {'fact':(chosen.get('integrated') or {}).get('value','unknown') if chosen else 'unknown',
            'scope':chosen.get('scope') if chosen else None,
            'scope_token':chosen.get('scope_token') if chosen else None,
            'source_commit':scope.get('source_commit') or None,
            'integration_commit':scope.get('integration_commit') or None,
            'matches_contribution':bool(matching)}


def project(issue, scopes=None):
    """Raw workflow state plus the scope-order-independent integration block."""
    from review_workflow import project as workflow
    result=workflow(issue)
    raw=result['review_state']
    if raw=='none' and 'review-ready' in (issue.get('labels') or []):
        result=dict(result,review_state='legacy-review-ready')
    evidence=integration(result.get('contribution'),scopes)
    if (result['review_state']=='awaiting-integration'
            and evidence['matches_contribution'] and evidence['fact']=='passed'):
        result=dict(result,review_state='integrated')
    return dict(result,workflow_state=raw,integration=evidence)
