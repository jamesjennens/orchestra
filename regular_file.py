"""Dependency-free, nonblocking reads of host-owned regular files."""
import os
import stat


def _is_reparse_point(info):
    attribute = getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0)
    return bool(attribute and getattr(info, 'st_file_attributes', 0) & attribute)


def _not_regular(path):
    raise ValueError('%s is not a regular file' % path)


def open_regular(path, validate=None):
    """Open one regular file for binary reads without following links or blocking."""
    nofollow = getattr(os, 'O_NOFOLLOW', 0)
    before = os.lstat(path)
    if _is_reparse_point(before) or not stat.S_ISREG(before.st_mode):
        if not (nofollow and stat.S_ISLNK(before.st_mode) and not _is_reparse_point(before)):
            _not_regular(path)
    flags = os.O_RDONLY | getattr(os, 'O_NONBLOCK', 0) | nofollow | getattr(os, 'O_BINARY', 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or _is_reparse_point(opened):
            _not_regular(path)
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise ValueError('%s changed while opening' % path)
        if validate is not None:
            validate(opened)
        handle = os.fdopen(descriptor, 'rb')
        descriptor = None
        return handle
    finally:
        if descriptor is not None:
            os.close(descriptor)


def read_regular_text(path, limit=None, raw=False, validate=None, prefix=None):
    """Read strict UTF-8 text, or bytes in raw mode, from one regular file.

    ``limit`` is a total byte ceiling. ``prefix`` instead returns at most that many
    bytes without rejecting a larger file. The optional validator receives the opened
    file's stat result.
    """
    for name, value in (('limit', limit), ('prefix', prefix)):
        if value is not None and (type(value) is not int or value < 0):
            raise ValueError('Regular-file byte %s must be a non-negative integer' % name)
    if limit is not None and prefix is not None:
        raise ValueError('Regular-file byte limit and prefix are mutually exclusive')

    def check(opened):
        if limit is not None and opened.st_size > limit:
            raise ValueError('%s exceeds its byte bound' % path)
        if validate is not None:
            validate(opened)

    with open_regular(path, validate=check) as handle:
        if prefix is not None:
            data = handle.read(prefix)
        elif limit is not None:
            data = handle.read(limit + 1)
        else:
            data = handle.read()
    if limit is not None and len(data) > limit:
        raise ValueError('%s exceeds its byte bound' % path)
    return data if raw else data.decode('utf-8')
