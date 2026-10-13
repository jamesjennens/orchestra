"""Keep sidecar-only unit fixtures separate from native VOID generation checks.

These fixtures supply coordination files but no initialized tracker. The actual
native boundary, including omitted families, is covered by NativeVoidTests.
"""
import functools
from unittest import mock
import admin


def without_void_tracker(function):
    @functools.wraps(function)
    def run(*args, **kwargs):
        with mock.patch.object(admin, 'restore_lifecycle_void_family', return_value=False):
            return function(*args, **kwargs)
    return run
