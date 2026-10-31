"""Offline proof of schema bytes at a configured local repository commit.

The repository comes from host configuration, never a contributor report. Read
raw Git objects with fixed commands and verify the commit/tree/blob hash chain.
No checkout, filters, hooks, reference traversal, URL access or fetch is requested.
Partial/promisor clones are refused; lazy fetching and all protocols are disabled.
"""
import hashlib
import os
import re
import subprocess
import threading
import time
from pathlib import Path, PurePosixPath

from interface_compatibility import BYTE_MAX

OBJECT_MAX = BYTE_MAX
PATH_MAX = 32
TOTAL_MAX = 10 * BYTE_MAX
TIMEOUT = 10


class Unproved(ValueError):
    pass


def _git(repository, args, limit, deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0: raise Unproved('source proof timed out')
    env = {k:v for k,v in os.environ.items() if not k.startswith('GIT_')}
    env.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull,
               GIT_ALLOW_PROTOCOL='', GIT_PROTOCOL_FROM_USER='0',
               GIT_NO_LAZY_FETCH='1', GIT_TERMINAL_PROMPT='0')
    command = ['git', '--no-replace-objects', '--no-optional-locks', '-C', str(repository), *args]
    try:
        process = subprocess.Popen(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except OSError:
        raise Unproved('source reader unavailable') from None
    output = []
    def read_output():
        try: output.append(process.stdout.read(limit + 1))
        except OSError: pass  # No bytes is an unproved result, not a thread traceback.
    reader = threading.Thread(target=read_output, daemon=True)
    started = False
    try:
        reader.start(); started = True; reader.join(remaining)
        if reader.is_alive() or not output or len(output[0]) > limit:
            process.kill()
            raise Unproved('source output or time budget')
        try: status = process.wait(timeout=max(0.01, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            process.kill(); raise Unproved('source proof timed out') from None
        return status, output[0]
    finally:
        if process.poll() is None: process.kill()
        process.wait()
        if started: reader.join()
        process.stdout.close()


def prove(pointer, repository):
    """Return exact bytes + bounded proof, or raise Unproved; never follows a URL."""
    if not isinstance(pointer, dict) or pointer.get('type') != 'repo-path': raise Unproved('local repo-path proof required')
    commit, path, expected = pointer.get('commit'), pointer.get('path'), pointer.get('sha256')
    if not isinstance(commit, str) or not re.fullmatch(r'(?:[0-9a-f]{40}|[0-9a-f]{64})', commit): raise Unproved('full source commit required')
    if not isinstance(expected, str) or not re.fullmatch('[0-9a-f]{64}', expected): raise Unproved('schema sha256 required')
    if pointer.get('anchor') is not None: raise Unproved('schema fragments unsupported')
    if not isinstance(path, str) or not path or len(path) > 500 or '\\' in path or '\x00' in path:
        raise Unproved('bounded relative schema path required')
    parts = PurePosixPath(path).parts
    if path.startswith('/') or '..' in parts or '.' in path.split('/') or len(parts) > PATH_MAX or '/'.join(parts) != path:
        raise Unproved('bounded relative schema path required')
    repository = Path(repository)
    if not repository.is_dir(): raise Unproved('configured repository unavailable')
    deadline = time.monotonic() + TIMEOUT
    code, partial = _git(repository, ['config', '--local', '--includes', '--get-regexp', r'^(extensions\.partialclone|remote\..*\.promisor)$'], 8192, deadline)
    if code not in (0, 1) or partial.strip(): raise Unproved('partial/promisor source unsupported')
    spent = 0
    def read(oid, kind):
        nonlocal spent
        code, size = _git(repository, ['cat-file', '-s', oid], 32, deadline)
        if code or not size.strip().isdigit(): raise Unproved('source object unavailable')
        length = int(size)
        if length > OBJECT_MAX or spent + length > TOTAL_MAX: raise Unproved('source object budget')
        spent += length
        code, body = _git(repository, ['cat-file', kind, oid], length, deadline)
        if code or len(body) != length: raise Unproved('source object changed or has wrong type')
        algorithm = hashlib.sha1 if len(oid) == 40 else hashlib.sha256
        digest = algorithm(kind.encode() + b' ' + str(length).encode() + b'\x00' + body).hexdigest()
        if digest != oid: raise Unproved('source object hash mismatch')
        return body
    body = read(commit, 'commit')
    first = body.split(b'\n', 1)[0]
    if not first.startswith(b'tree '): raise Unproved('source commit tree unavailable')
    tree = first[5:].decode('ascii'); width = len(commit) // 2
    if not re.fullmatch('[0-9a-f]{%d}' % len(commit), tree): raise Unproved('invalid source tree')
    chain = [commit]
    for index, part in enumerate(parts):
        chain.append(tree); data = read(tree, 'tree'); offset = 0; matches = []
        while offset < len(data):
            end = data.find(b'\x00', offset)
            if end < 0 or end + 1 + width > len(data): raise Unproved('malformed source tree')
            head = data[offset:end]; mode, sep, name = head.partition(b' ')
            if not sep: raise Unproved('malformed source tree entry')
            oid = data[end+1:end+1+width].hex(); offset = end+1+width
            if name == part.encode('utf-8'): matches.append((mode, oid))
        if len(matches) != 1: raise Unproved('schema path absent or ambiguous at source commit')
        mode, tree = matches[0]
        if index < len(parts) - 1:
            if mode != b'40000': raise Unproved('schema parent is not a tree')
        elif mode not in (b'100644', b'100755'): raise Unproved('schema is not a regular blob')
    content = read(tree, 'blob'); chain.append(tree)
    actual = hashlib.sha256(content).hexdigest()
    if actual != expected: raise Unproved('schema bytes differ from pinned sha256')
    return content, {'source_commit':commit, 'path':path, 'sha256':actual,
                     'objects':chain, 'bytes':len(content), 'proof':'git-object-hash-chain-v1'}
