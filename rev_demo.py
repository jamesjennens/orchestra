"""Demonstrate the three rev1 release-deploy bugs on whichever kit is exported.

Run inside a kit checkout: python3 /tmp/rev_demo.py
Uses only APIs that exist in the rev1 kit; a missing explicit rollback operation is
reported as such.
"""
import sys

sys.path.insert(0, '.')
sys.path.insert(0, 'tests')
import test_lifecycle_release as T
import lifecycle


def run():
    # Item 1: a plain deploy of R2 after R1 must select only what is new.
    store = T.NativeStore(tasks=('trial-a',)).seed('trial-a')
    store.record(T.release_payload([T.target('trial-a')], operation='r1', release='r-1'))
    selection = lifecycle.release_selection(
        store.rows,
        {'source_commit': '', 'integration_commit': T.RELEASE_COMMIT,
         'release_id': 'r-2', 'environment': 'production'},
        lambda commit, release: True)
    print('item1 R2 targets after R1 deploy:', [item['task'] for item in selection['targets']])

    # Item 3: verifying R1 while R2 is live must not move the current scope.
    store2 = T.NativeStore(tasks=('trial-a',)).seed('trial-a')
    store2.record(T.release_payload([T.target('trial-a')], operation='r1', release='r-1'))
    store2.record(T.release_payload([T.target('trial-a')], operation='r2', release='r-2'))
    try:
        store2.record(T.release_payload([T.target('trial-a')], operation='verify-r1',
                                        release='r-1', live_verified=True))
        state = store2.facts('trial-a')
        print('item3 scope after late verify of R1:', state['scope']['release_id'],
              'deployed:', state['facts']['deployed']['value'])
    except Exception as exc:  # noqa: BLE001 - the demo reports the refusal
        print('item3 late verify refused:', type(exc).__name__, exc)

    # Item 2: an explicit rollback operation.
    print('item2 explicit rollback available:',
          hasattr(lifecycle, 'rollback_selection') or hasattr(lifecycle, 'LIVE'))


run()
