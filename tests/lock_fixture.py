"""Separate existing handle-based project/config lock assertions from the new deployment mutex.

The deployment mutex's real exclusion, ordering and busy refusal have dedicated tests
in test_interface_lock; these old fixtures still assert their own original locks.
"""

def ordinary_calls(mock):
    # http_authority.file_lock uses integer descriptors, including its unlock;
    # original admin project/config locks pass the open handle itself.
    return [call for call in mock.call_args_list
            if not isinstance(call.args[0], int)
            and not str(getattr(call.args[0], 'name', '')).endswith('.interface-mutations.lock')]
