#!/usr/bin/env python3
"""Read-only capability lookup over an index built from a checkout.

``capability lookup "<phrase>"`` answers "where does this live?" for an agent or a
person working in their own checkout. It returns versioned JSON that points at the
code (``file::Qualified.name``) or design text (``file.md#anchor``) matching the
phrase, with a short summary, the tests that reference it and related symbols. A
miss returns the nearest candidates and a hint instead of an empty answer.

The index is built on demand from the checkout and nothing is written anywhere: no
Beads record, no server call, no cache file, and no refresh of Git's index (every Git
call runs with optional locks off). Sources:

* ``ast``: Python modules, classes, functions and methods from the standard-library
  parser. Calls between them and the tests that reference them are resolved from
  imports and names, so those links are marked ``INFERRED``.
* ``graphify``: graphify's ``graphify-out/graph.json`` replaces the ast code index
  when it is present (``--source auto``) or requested. The file is untrusted input:
  its size is bounded, NaN/Infinity and deep nesting are refused, only clean
  repo-relative POSIX paths are accepted, labels are cleaned and bounded, unknown
  shapes are counted and skipped, and nothing in it is executed. A Python match
  taken from the graph is re-checked with ``ast`` before it is returned, and a graph
  built at another commit is still used and is reported as stale. With ``--source
  auto`` a refused graph is set aside for the ast index. Either warning says what to do.
* Markdown headings are always indexed from the checkout as design anchors.

Text taken from the repository (docstrings, headings, graph labels) is returned as
excerpt objects ``{"text", "omitted_chars"}`` with control and format characters
removed, and every result carries ``"trust": "repository-content"``: it describes
the code; it is not an instruction to the reader.

This module imports nothing from the kit, so it can sit beside a standalone
``client.py``; ``client.py -- capability ...`` runs it locally. Recorded capability
records, aliases and a recorded drift check are later work; the entry shape here is
the one those records are meant to feed.
"""
import argparse
import ast
import difflib
import io
import json
import os
import re
import subprocess
import sys
import threading
import tokenize
import unicodedata
from pathlib import Path

CONTRACT_VERSION = 'cli-contract-v1'
INDEX_SCHEMA = 'capability-index-v1'
LOOKUP_SCHEMA = 'capability-lookup-v1'
RESOLVE_SCHEMA = 'capability-resolve-v1'
TRUST = 'repository-content'
COMMANDS = ('lookup', 'resolve', 'index')
SOURCES = ('auto', 'ast', 'graphify')
DEFAULT_GRAPH = 'graphify-out/graph.json'

GRAPH_MB_MIN, GRAPH_MB_MAX, GRAPH_MB_DEFAULT = 1, 512, 64
GRAPH_NODES_MAX = 500_000
GRAPH_LINKS_MAX = 2_000_000
GRAPH_LABEL_MAX = 1000
# Fixed sentences for the --source auto warnings about the default graph: what to do when
# it is refused (the ast index is used) or stale (it is still used). Nothing from the
# file is quoted in them.
GRAPH_FALLBACK = 'using the ast index. Regenerate %s or delete it.' % DEFAULT_GRAPH
GRAPH_STALE = ('results from it may be out of date. Regenerate %s, or delete it to use the ast index.'
               % DEFAULT_GRAPH)
FILES_MAX = 20_000
FILE_BYTES_MAX = 2_000_000
PARSE_STACK_BYTES = (512 * 1024 * 1024, 255 * 1024 * 1024)  # Windows refuses 256 MB and above
PARSE_STACK_BYTES_PER_LEVEL = 512
NESTING_MAX_BIG_STACK = 50_000
NESTING_MAX = 2_000 if os.name == 'nt' else 5_000
# Per-file budget on the nodes ast.parse may build. It counts operators, brackets,
# commas, NAME tokens and the statement separators at bracket depth zero, so a
# statement-dense file (900,000 one-name lines, 190,000 calls, a 190,000-name list)
# is refused even though it has few operators. It is generous enough to keep the
# kit and the largest generated tables (60,000 rows, ~240,000 counted nodes) parsing.
EXPRESSION_TOKENS_MAX = 300_000
TOKENIZE_BUDGET_BYTES = 16_000_000
LINE_TEXT_MAX = 2_000
HEADING_LINE_MAX = 1_000
SUMMARY_SCAN_LINES = 40
ENTRIES_MAX = 200_000
HEADINGS_PER_FILE_MAX = 2_000
DEFINITIONS_PER_FILE_MAX = 5_000
TOTAL_BYTES_MAX = 256_000_000
LIMIT_MIN, LIMIT_MAX, LIMIT_DEFAULT = 1, 20, 5
PHRASE_MAX = 200
POINTERS_MAX = 100
POINTER_MAX = 400
NAME_LIMIT = 120
SUMMARY_LIMIT = 200
ALIASES_SHOWN = 8
TESTS_SHOWN = 8
RELATED_SHOWN = 8
CANDIDATE_FLOOR = 0.2

SKIP_DIRS = frozenset({'.git', '.hg', '.svn', '__pycache__', 'node_modules', '.venv', 'venv',
                       '.tox', '.nox', '.mypy_cache', '.pytest_cache', '.ruff_cache',
                       'graphify-out', '.eggs', 'site-packages'})
CONFIDENCES = frozenset({'EXTRACTED', 'INFERRED', 'AMBIGUOUS'})
STRUCTURAL = frozenset({'contains', 'method', 'rationale_for', 'defines'})
KIND_ORDER = {'class': 0, 'function': 0, 'method': 0, 'module': 1, 'doc-section': 2}

RELATION = re.compile(r'[a-z][a-z_]{0,39}')
LOCATION = re.compile(r'L([1-9][0-9]{0,6})')
COMMIT = re.compile(r'[0-9a-f]{40}|[0-9a-f]{64}')
GRAPH_ID = re.compile(r'[A-Za-z0-9_.:-]{1,200}')
SYMBOL = re.compile(r'[^\s:#/\\]{1,200}')
HEADING = re.compile(r' {0,3}(#{1,6})[ \t]+(.+?)(?:[ \t]+#+)?[ \t]*$')
FENCE = re.compile(r' {0,3}(`{3,}|~{3,})')
LINK = re.compile(r'\[([^\]]*)\]\([^)]*\)')
WORD = re.compile(r'[^\W_]+')
_TRY = tuple(node for node in (getattr(ast, 'Try', None), getattr(ast, 'TryStar', None)) if node)


class _Parser(argparse.ArgumentParser):
    """argparse that raises instead of printing usage and exiting."""

    def error(self, message):
        raise ValueError(message)


# ---------------------------------------------------------------- text and paths

def clean(value):
    """Untrusted text as one line: control, format and separator characters become
    spaces and runs of whitespace collapse."""
    text = ''.join(' ' if unicodedata.category(ch)[0] in 'CZ' else ch for ch in str(value))
    return ' '.join(text.split())


def excerpt(value, limit):
    """An excerpt object (the CLI contract's bounded-text shape), or None."""
    if value is None:
        return None
    text = clean(value)
    if not text:
        return None
    return {'text': text[:limit], 'omitted_chars': max(0, len(text) - limit)}


def words(text):
    """Lowercase word tokens; camelCase and snake_case are split."""
    text = re.sub(r'([a-z0-9])([A-Z])', r'\1 \2', str(text))
    text = re.sub(r'([A-Z]+)([A-Z][a-z])', r'\1 \2', text)
    return WORD.findall(text.casefold())


def stem(word):
    for suffix in ('ing', 'ed', 'es', 's', 'e'):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            return word[:-len(suffix)]
    return word


def normalized(text):
    return ' '.join(words(text))


def stems(text):
    return {stem(word) for word in words(text)}


def safe_relpath(value):
    """A clean repo-relative POSIX path, or None: never absolute, never `..`, never a
    backslash, drive letter, empty segment, control or format character, or any
    separator other than the ASCII space (no line or paragraph separator, no NBSP)."""
    if not isinstance(value, str) or not value or len(value) > POINTER_MAX:
        return None
    if '\\' in value or re.match(r'[A-Za-z]:', value) or value.startswith('/'):
        return None
    if any(unicodedata.category(ch)[0] in 'CZ' and ch != ' ' for ch in value):
        return None
    if any(part in ('', '.', '..') for part in value.split('/')):
        return None
    return value


def contained_file(root, rel):
    """The regular file at rel inside root, following no link out of the root."""
    try:
        full = (root / rel).resolve()
        full.relative_to(root)
    except (OSError, RuntimeError, ValueError):
        return None
    return full if full.is_file() else None


def is_test(rel):
    parts = rel.split('/')
    name = parts[-1]
    return (name.startswith('test_') or name.endswith('_test.py')
            or any(part in ('tests', 'test') for part in parts[:-1]))


def module_name(rel):
    parts = rel[:-3].split('/') if rel.endswith('.py') else rel.split('/')
    if parts[-1] == '__init__':
        parts = parts[:-1]
    return '.'.join(parts) or None


def slug(title):
    """GitHub-style heading anchor."""
    text = LINK.sub(r'\1', title).strip().lower()
    text = ''.join(ch for ch in text
                   if ch in '- _' or unicodedata.category(ch)[0] in 'LNM')
    return text.replace(' ', '-')


# ---------------------------------------------------------------- repository

def _git(root, args):
    # `git status` refreshes and rewrites .git/index under an optional lock, which can
    # race the caller's own `git add`/`commit`; a read-only lookup never takes it.
    env = dict(os.environ, GIT_OPTIONAL_LOCKS='0')
    try:
        run = subprocess.run(['git', '--no-optional-locks', '-C', str(root), *args], capture_output=True,
                             timeout=30, env=env, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    return run.stdout if run.returncode == 0 else None


def open_repo(path):
    start = Path(path or '.')
    if not start.is_dir():
        raise ValueError('--repo: expected an existing directory')
    start = start.resolve()
    top = _git(start, ['rev-parse', '--show-toplevel'])
    root = Path(top.decode('utf-8', 'replace').strip()).resolve() if top else start
    head = _git(root, ['rev-parse', 'HEAD']) if top else None
    commit = head.decode('ascii', 'replace').strip() if head else None
    commit = commit if commit and COMMIT.fullmatch(commit) else None
    status = _git(root, ['status', '--porcelain', '--untracked-files=no']) if commit else None
    return {'root': root, 'git': top is not None, 'commit': commit,
            'dirty': None if status is None else bool(status.strip())}


def list_files(repo, suffixes):
    root = repo['root']
    names = None
    if repo['git']:
        listed = _git(root, ['ls-files', '-z', '--cached', '--others', '--exclude-standard'])
        if listed is not None:
            names = [name for name in listed.decode('utf-8', 'replace').split('\0') if name]
    if names is None:
        names = []
        for directory, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith('.'))
            for filename in filenames:
                names.append(Path(directory, filename).relative_to(root).as_posix())
    return [name for name in sorted(set(names))
            if name.endswith(suffixes) and not any(part in SKIP_DIRS for part in name.split('/')[:-1])]


class Index:
    """Entries keyed by pointer, plus counts of what was skipped and why."""

    def __init__(self, repo):
        self.repo = repo
        self.entries = []
        self.by_id = {}
        self.skipped = {}
        self.warnings = []
        self.code_source = 'ast'
        self.graph = None

    def skip(self, reason, count=1):
        self.skipped[reason] = self.skipped.get(reason, 0) + count

    def add(self, entry):
        if entry['id'] in self.by_id:
            self.skip('duplicate-pointer')
            return None
        if len(self.entries) >= ENTRIES_MAX:
            if 'entry-limit' not in self.skipped:
                self.warnings.append('indexed the first %d entries only' % ENTRIES_MAX)
            self.skip('entry-limit')
            return None
        self.by_id[entry['id']] = entry
        self.entries.append(entry)
        return entry

    def describe(self):
        repo = self.repo
        return {'schema': INDEX_SCHEMA, 'code_source': self.code_source, 'graph': self.graph,
                'repo': {'git': repo['git'], 'commit': repo['commit'], 'dirty': repo['dirty']},
                'entries': len(self.entries), 'skipped': dict(sorted(self.skipped.items()))}


def entry(pointer, kind, name, symbol, rel, line, end_line, summary, source, aliases,
          module=None, graph_id=None):
    item = {'id': pointer, 'kind': kind, 'name': name, 'symbol': symbol, 'file': rel,
            'line': line, 'end_line': end_line, 'summary': summary, 'source': source,
            'test': is_test(rel), 'module': module, 'aliases': [], 'tests': set(),
            'related': {}}
    for alias in aliases:
        if alias and alias not in item['aliases']:
            item['aliases'].append(alias)
    if graph_id is not None:
        item['graph_id'] = graph_id
    return item


def relate(source, relation, target, confidence):
    source['related'].setdefault((relation, 'out', target['id']), confidence)
    target['related'].setdefault((relation, 'in', source['id']), confidence)


def read_files(repo, names, index):
    """(rel, bytes) for each bounded, contained, readable file."""
    total = 0
    root = repo['root']
    for position, rel in enumerate(names):
        if position >= FILES_MAX:
            index.skip('file-limit', len(names) - position)
            index.warnings.append('indexed the first %d files only' % FILES_MAX)
            return
        if safe_relpath(rel) is None:
            index.skip('unsafe-path')
            continue
        full = contained_file(root, rel)
        if full is None:
            index.skip('unreadable')
            continue
        try:
            size = full.stat().st_size
            if size > FILE_BYTES_MAX:
                index.skip('file-too-large')
                continue
            if total + size > TOTAL_BYTES_MAX:
                index.skip('byte-budget', len(names) - position)
                index.warnings.append('stopped indexing at %d MB of source' % (TOTAL_BYTES_MAX // 1_000_000))
                return
            data = full.read_bytes()
        except OSError:
            index.skip('unreadable')
            continue
        total += len(data)
        yield rel, data


# ---------------------------------------------------------------- python (ast)

def _bodies(node):
    """Statement lists of a compound statement that can still define module- or
    class-level names (conditional or guarded definitions)."""
    if isinstance(node, ast.If):
        return [node.body, node.orelse]
    if isinstance(node, (ast.With, ast.AsyncWith)):
        return [node.body]
    if _TRY and isinstance(node, _TRY):
        return [node.body, *[handler.body for handler in node.handlers], node.orelse, node.finalbody]
    return []


def definitions(body, prefix='', in_class=False):
    """(qualname, kind, node) for every addressable class, function and method.
    Definitions local to a function body are not addressable and are not listed."""
    for node in body:
        if isinstance(node, ast.ClassDef):
            qualname = prefix + node.name
            yield qualname, 'class', node
            yield from definitions(node.body, qualname + '.', True)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield prefix + node.name, 'method' if in_class else 'function', node
        else:
            for statements in _bodies(node):
                yield from definitions(statements, prefix, in_class)


def first_line(docstring):
    if not docstring:
        return None
    for line in docstring.strip().split('\n'):
        if line.strip():
            return line.strip()[:LINE_TEXT_MAX]
    return None


def absolute_module(module, package, level, name):
    if level == 0:
        return name
    parts = (module.split('.') if module else []) if package else (module.split('.')[:-1] if module else [])
    if level - 1 > len(parts):
        return None
    base = parts[:len(parts) - (level - 1)]
    return '.'.join(base + ([name] if name else [])) or None


def walk(node):
    """ast.walk without its per-field generator overhead (order is irrelevant here)."""
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        for field in current._fields:
            value = getattr(current, field, None)
            if isinstance(value, list):
                stack.extend(child for child in value if isinstance(child, ast.AST))
            elif isinstance(value, ast.AST):
                stack.append(value)


def statements(body):
    """Every statement, at any depth, without visiting expressions: imports are
    statements, so this finds them all at a fraction of a full walk's cost."""
    stack = list(body)
    while stack:
        node = stack.pop()
        yield node
        for field in ('body', 'orelse', 'finalbody', 'handlers', 'cases'):
            value = getattr(node, field, None)
            if isinstance(value, list):
                stack.extend(child for child in value if isinstance(child, ast.AST))


def import_aliases(tree, module, package):
    aliases = {}
    for node in statements(tree.body):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    aliases[alias.asname] = ('module', alias.name)
                else:
                    top = alias.name.split('.')[0]
                    aliases[top] = ('module', top)
        elif isinstance(node, ast.ImportFrom):
            base = absolute_module(module, package, node.level, node.module)
            if base is None:
                continue
            for alias in node.names:
                if alias.name != '*':
                    aliases[alias.asname or alias.name] = ('symbol', base, alias.name)
    return aliases


def _reference(node):
    if isinstance(node, ast.Name):
        return ('name', node.id)
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        return ('attr', node.value.id, node.attr)
    return None


def references(node, everything):
    """(calls, names): references made by calls, and (when asked, for tests) every
    name or module.attribute reference in the definition."""
    calls, names = set(), set()
    for sub in walk(node):
        if isinstance(sub, ast.Call):
            called = _reference(sub.func)
            if called:
                calls.add(called)
        elif everything:
            found = _reference(sub)
            if found:
                names.add(found)
    return calls, names


class TooComplex(ValueError):
    """Source whose expressions could nest deeper than this run can parse safely."""


# Per-run parse state: the worker stack granted (0 for none), and how many
# bytes the fallback guard may still tokenize. run() resets it.
_parse = {'stack_bytes': 0, 'tokenize_left': TOKENIZE_BUDGET_BYTES, 'budget_spent': False}


def _reset_parse_state():
    _parse.update(stack_bytes=0, tokenize_left=TOKENIZE_BUDGET_BYTES, budget_spent=False)


# Characters and keywords that can each add one level to an expression tree. Brackets
# and displays cannot nest past the tokenizer's 200-level limit, and indentation stops
# at 100, so long chains of these within one logical line are the only way to build a
# very deep tree.
NESTING = re.compile(rb'[-+*/%@&|^~.<>(\[]|\b(?:not|if|lambda|await|yield)\b')
NESTING_OPS = frozenset({'+', '-', '*', '/', '//', '%', '@', '&', '|', '^', '~', '**', '<<', '>>',
                         '.', '(', '['})
# `for` and `in` are counted by the exact pass as well as the keyword operators: a
# comprehension nests a ListComp/SetComp/DictComp/GeneratorExp plus a comprehension
# node per `for`, so `[i for i in [...]]` repeated would otherwise undercount by one
# level per repetition.
NESTING_WORDS = frozenset({'not', 'if', 'lambda', 'await', 'yield', 'for', 'in'})

# Bytes counted as one expression token each when budgeting a file: the NESTING
# operators and brackets plus the comma that separates flat elements and the '=' of
# assignment and of `==`/`!=`/`<=`/`>=`. `and`, `or`, `in` and `is` build wide (flat)
# nodes too, and every NAME token is counted separately, so the keyword operators are
# covered by the generic NAME count. A '.' between two digits is a float literal,
# not an attribute access, and does not count.
_EXPRESSION_BYTES = frozenset(b'-+*/%@&|^~.<>([,=')
_DIGITS = frozenset(b'0123456789')
_NAME_START = frozenset(b'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_')
_NAME_CHARS = _NAME_START | _DIGITS



def _skip_string(data, start):
    """The index just past the string literal that starts at `start` (a quote byte).

    Triple quotes and backslash escapes are honoured. An unterminated single-line
    string conservatively swallows the rest of the file: that can only join logical
    lines, never split one."""
    quote = data[start]
    triple = data[start:start + 3] == bytes([quote]) * 3
    end = start + (3 if triple else 1)
    size = len(data)
    while end < size:
        char = data[end]
        if char == 0x5C:  # a backslash escapes the next byte
            end += 2
            continue
        if triple:
            if data[end:end + 3] == bytes([quote]) * 3:
                return end + 3
            end += 1
        elif char == quote:
            return end + 1
        elif char == 0x0A:  # an unterminated single-line string: stay conservative
            return size
        else:
            end += 1
    return size


def _expression_tokens(data):
    """A cheap per-file bound on how many AST nodes one file can build.

    Counts operators, brackets and commas outside strings and comments (a large data
    string is one node, so its text must not count). The nesting pre-count alone is
    blind to a flat but huge expression: `1<1<...`, `(1,1,...)`, `[1,1,...]` and
    `1 and 1 and ...` are not deep, so ast.parse builds millions of sibling nodes and
    hundreds of MB of objects on CPython 3.10/3.11.

    Every NAME token and every statement separator (a newline or `;` at bracket depth
    zero) counts too: a statement-dense file, such as 900,000 one-name lines, 190,000
    calls or a single 190,000-name list, has almost no operators but just as many
    nodes. A newline or `;` inside brackets is a continuation, not a statement, and a
    newline inside a triple-quoted string is string text, so neither counts."""
    size = len(data)
    position = total = depth = 0
    while position < size:
        char = data[position]
        if char == 0x23:  # '#': a comment is not code
            found = data.find(b'\n', position)
            position = size if found < 0 else found
            continue
        if char in (0x27, 0x22):  # a string literal is one node
            position = _skip_string(data, position)
            continue
        if char in (0x28, 0x5B, 0x7B):  # ( [ { open a bracket
            depth += 1
            if char in _EXPRESSION_BYTES:
                total += 1
            position += 1
            continue
        if char in (0x29, 0x5D, 0x7D):  # ) ] } close one
            if depth:
                depth -= 1
            position += 1
            continue
        if char in _EXPRESSION_BYTES:
            if not (char == 0x2E and 0 < position < size - 1
                    and data[position - 1] in _DIGITS and data[position + 1] in _DIGITS):
                total += 1
            position += 1
            continue
        if char == 0x0A or char == 0x3B:  # a newline or ';' ends a statement
            if depth == 0:
                total += 1
            position += 1
            continue
        if char in _NAME_START:
            end = position + 1
            while end < size and data[end] in _NAME_CHARS:
                end += 1
            total += 1  # every NAME is a node, keyword operators included
            position = end
            continue
        position += 1
    return total


def _logical_line_peak(data, limit):
    """A cheap upper bound on the nesting-capable tokens in any one logical line.

    One byte scan: comments and string literals are skipped for bracket tracking, a
    newline ends a logical line only at bracket depth zero with no backslash
    continuation, and `NESTING` counts the tokens in each such chunk. Counting the raw
    bytes (string and comment text included) can only make the bound larger, and a
    chunk never ends before the true logical line does, so a peak at or below `limit`
    proves that no logical line nests more than `limit`. Returns as soon as one chunk
    passes it."""
    size = len(data)
    position = start = depth = peak = 0
    while position < size:
        char = data[position]
        if char == 0x23:  # '#': a comment runs to the end of the line
            found = data.find(b'\n', position)
            position = size if found < 0 else found
            continue
        if char in (0x27, 0x22):  # a string literal
            position = _skip_string(data, position)
            continue
        if char in (0x28, 0x5B, 0x7B):  # ( [ {
            depth += 1
        elif char in (0x29, 0x5D, 0x7D):  # ) ] }
            if depth:
                depth -= 1
        elif char == 0x0A:  # a newline ends the logical line only at depth zero
            continued = position and (data[position - 1] == 0x5C or (
                data[position - 1] == 0x0D and position > 1 and data[position - 2] == 0x5C))
            if depth == 0 and not continued:
                count = len(NESTING.findall(data[start:position + 1]))
                if count > peak:
                    peak = count
                    if peak > limit:
                        return peak
                start = position + 1
        position += 1
    if start < size:
        count = len(NESTING.findall(data[start:size]))
        if count > peak:
            peak = count
    return peak


def _is_fstring(string):
    """True for a string token that is an f-string (before 3.12 the whole f-string is
    one STRING token, so its embedded operators must be counted by hand)."""
    return 'f' in string.split('"')[0].split("'")[0].lower()


def _atom_before(token):
    """True when `token` can end an atom, so a following `(` or `[` is a call or
    subscript: a postfix bracket that keeps the expression chain. Otherwise the bracket
    opens a display (or a grouping), whose comma-separated elements are siblings."""
    if token is None:
        return False
    if token.type in (tokenize.NAME, tokenize.NUMBER, tokenize.STRING):
        return True
    return token.type == tokenize.OP and token.string in (')', ']', '}')


def _deepest_logical_line(text, limit):
    """An upper bound on the expression tree depth ast.parse will build on one logical
    line, counting operators inside f-strings (a single STRING token before Python
    3.12). Returns as soon as one line passes `limit`, so a hostile file costs no more
    than that line.

    Comma-separated elements of a display are siblings, so the chain resets after each
    one at the display's depth: a 60,000-row table is shallow, not 60,000 levels deep.
    A call or subscript bracket after an atom keeps the chain, so `a[1,2][3,4]...` and
    `f(x,y)(z)()` still accumulate.

    The returned number is a lower bound on the real AST depth and stays within a
    small constant of it: the depth property tests require `computed >= real - 6` at
    every nesting depth of every shape they generate (the six levels are the fixed
    wrappers this pass does not model: the statement around the expression, and the
    JoinedStr/FormattedValue pair plus a format spec of an f-string). The lag must not
    grow with depth, so a display contributes one level above its deepest element and
    the prefix chain already counted before the opening bracket applies on top of
    that. It is therefore added rather than compared in a `max()`: `-[-1]` must count
    3, not 2, or a `[-`-repeated file would undercount by one level per nesting and
    reach ast.parse at twice the trusted depth.

    `text` is the decoded source, not raw bytes: tokenizing bytes would re-read the
    file's coding cookie and, for a `# coding: utf-7` file re-encoded as UTF-8, decode
    the UTF-8 bytes with the wrong codec."""
    deepest = chain = 0
    stack = []  # [is a display, chain before the bracket, deepest element so far]
    previous = None
    for token in tokenize.generate_tokens(io.StringIO(text).readline):
        kind, string = token.type, token.string
        if kind in (tokenize.NEWLINE, tokenize.ENDMARKER):
            deepest, chain, stack = max(deepest, chain), 0, []
        elif kind == tokenize.OP:
            if string in '([{':
                display = string == '{' or not _atom_before(previous)
                stack.append([display, chain, 0])
                if display:
                    chain = 0
                else:
                    chain += 1
            elif string in ')]}':
                if stack:
                    display, outer, inner = stack.pop()
                    if display:
                        chain = outer + 1 + max(inner, chain)
            elif string == ',' and stack and stack[-1][0]:
                if chain > stack[-1][2]:
                    stack[-1][2] = chain
                chain = 0
            elif string in NESTING_OPS:
                chain += 1
        elif kind == tokenize.NAME and string in NESTING_WORDS:
            chain += 1
        elif kind == tokenize.STRING and _is_fstring(string):
            chain += len(NESTING.findall(string.encode('utf-8', 'replace')))
        if chain > limit:
            return chain
        previous = token
    return max(deepest, chain)


def _decode_source(data):
    """The source text the parser will see, with its PEP 263 coding cookie honoured.

    The cheap gate must run on exactly this text: CPython 3.10 and 3.11 accept a
    `# coding: utf-7` cookie, and inside a UTF-7 `+...-` shift sequence ASCII operators
    are base64-encoded, so a byte scan of the raw file sees almost nothing. A cookie
    whose codec cannot decode the file is a parse error, never a fall-through to the
    raw bytes (which is what let a hostile UTF-7 file reach ast.parse)."""
    try:
        encoding, _ = tokenize.detect_encoding(io.BytesIO(data).readline)
        return data.decode(encoding)
    except (SyntaxError, UnicodeError, LookupError, ValueError):
        raise SyntaxError('cannot decode source') from None


def parse_python(data, rel):
    """ast.parse that cannot crash the interpreter.

    CPython 3.10 converts a deeply nested expression (a long `1+1+...`, `a.b.b...`,
    `f()()...`, `a[0][0]...` or `1<<1<<...` chain) to Python objects with unchecked C
    recursion and crashes the process instead of raising RecursionError. run() parses on
    a worker thread with a 512 MB stack (255 MB where the platform allows no more, as on
    Windows), and trusts it for at most NESTING_MAX_BIG_STACK levels: 50,000 levels need
    about 25 MB of C stack, so one hostile file cannot commit hundreds of MB.

    The source is decoded first with its declared encoding (`_decode_source`), and the
    cheap pre-count runs on `text.encode('utf-8')`: a UTF-7 coding cookie would
    otherwise hide an ASCII operator chain inside a base64 `+...-` shift sequence, and
    at most this text is handed to ast.parse.

    A cheap per-logical-line pre-count (`_logical_line_peak`: one byte scan, no
    tokenizer) decides whether the exact tokenize pass is needed. Ordinary files,
    dot-heavy ones included, are parsed directly even when a whole-file count would be
    large; only a file whose logical line may out-nest the limit is tokenized. A second
    cheap count (`_expression_tokens`) bounds a flat but huge file independently of its
    depth, so `1<1<...`, `(1,1,...)` or `1 and 1 and ...` is skipped before ast.parse
    can build millions of sibling nodes. If no worker thread could be made, the fallback
    guard on the calling thread uses NESTING_MAX (5,000, or 2,000 on Windows): the same
    pre-count, then an exact tokenize pass (early return, 16 MB per-run budget) against
    NESTING_MAX. Python 3.11 and later raise RecursionError instead of crashing, which
    is caught.
    """
    if _parse['stack_bytes']:
        limit = min(NESTING_MAX_BIG_STACK, _parse['stack_bytes'] // PARSE_STACK_BYTES_PER_LEVEL)
    else:
        limit = NESTING_MAX
    text = _decode_source(data)
    encoded = text.encode('utf-8')
    if _expression_tokens(encoded) > EXPRESSION_TOKENS_MAX:
        raise TooComplex('the file has more than %d expression tokens' % EXPRESSION_TOKENS_MAX)
    if _logical_line_peak(encoded, limit) > limit:
        if _parse['tokenize_left'] < len(encoded):
            _parse['budget_spent'] = True
            raise TooComplex('the per-run tokenize budget is spent')
        _parse['tokenize_left'] -= len(encoded)
        try:
            deepest = _deepest_logical_line(text, limit)
        except (tokenize.TokenError, SyntaxError, UnicodeDecodeError, ValueError):
            raise SyntaxError('cannot tokenize') from None
        if deepest > limit:
            raise TooComplex('a logical line nests more than %d levels' % limit)
    return ast.parse(text, filename=rel)


def parse_warnings(skipped):
    """Warnings for files and entries a run left out, so a cap is never silent."""
    found = []
    if skipped.get('too-complex'):
        found.append('skipped %d Python file(s) that could nest too deeply or hold too many '
                     'expressions to parse safely%s'
                     % (skipped['too-complex'],
                        ' (the per-run tokenize budget was spent)' if _parse['budget_spent'] else ''))
    if skipped.get('parse-error'):
        found.append('skipped %d Python file(s) that could not be parsed'
                     % skipped['parse-error'])
    if skipped.get('heading-limit'):
        found.append('skipped %d heading(s) beyond %d per Markdown file'
                     % (skipped['heading-limit'], HEADINGS_PER_FILE_MAX))
    if skipped.get('definition-limit'):
        found.append('skipped %d definition(s) beyond %d per Python file'
                     % (skipped['definition-limit'], DEFINITIONS_PER_FILE_MAX))
    return found


def _start_worker(target):
    worker = threading.Thread(target=target, name='capability-parse', daemon=True)
    worker.start()
    return worker


def with_parse_stack(function):
    """Run function() on a worker thread with a big stack (see parse_python).

    Every allowed stack size is tried in turn (512 MB, then 255 MB): a size the platform
    will not grant is skipped, and one it grants but cannot start a thread with is
    retried at the next size. Only when no worker can be started does the run fall back
    to the calling thread with the stricter guard (32-bit builds, strict overcommit).
    The default stack size is restored for every later thread.
    """
    box = {}

    def target():
        try:
            box['value'] = function()
        except BaseException as exc:  # re-raised on the calling thread
            box['error'] = exc

    for size in PARSE_STACK_BYTES:
        try:
            previous = threading.stack_size(size)
        except (ValueError, RuntimeError, OverflowError):
            continue
        _parse['stack_bytes'] = size
        try:
            worker = _start_worker(target)
        except (RuntimeError, MemoryError):
            worker = None
        finally:
            threading.stack_size(previous)
        if worker is None:
            continue
        worker.join()
        if 'error' in box:
            raise box['error']
        return box['value']
    _parse['stack_bytes'] = 0
    return function()


def index_python(rel, data, index, facts):
    try:
        tree = parse_python(data, rel)
        found = list(definitions(tree.body))
    except TooComplex:
        index.skip('too-complex')
        return
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        index.skip('parse-error')
        return
    module = module_name(rel)
    package = rel.endswith('__init__.py')
    stem_name = rel.rsplit('/', 1)[-1][:-3]
    end = data.count(b'\n') + (0 if data.endswith(b'\n') or not data else 1)
    summary = first_line(ast.get_docstring(tree))
    module_entry = index.add(entry(rel, 'module', module or stem_name, None, rel, 1, max(end, 1),
                                   summary, 'ast', [module, stem_name, rel], module=module))
    aliases = import_aliases(tree, module, package)
    facts['files'][rel] = {'module': module, 'aliases': aliases, 'entry': module_entry}
    test = is_test(rel)
    if len(found) > DEFINITIONS_PER_FILE_MAX:
        index.skip('definition-limit', len(found) - DEFINITIONS_PER_FILE_MAX)
        found = found[:DEFINITIONS_PER_FILE_MAX]
    for qualname, kind, node in found:
        name = qualname.rsplit('.', 1)[-1]
        dotted = '%s.%s' % (module, qualname) if module else qualname
        item = index.add(entry('%s::%s' % (rel, qualname), kind, name, qualname, rel, node.lineno,
                               getattr(node, 'end_lineno', None), first_line(ast.get_docstring(node)),
                               'ast', [name, qualname, dotted], module=module))
        if item is None or kind == 'class':
            continue
        owner = qualname.rsplit('.', 1)[0] if kind == 'method' else None
        test_function = test and name.startswith('test')
        calls, names = references(node, test_function)
        facts['defs'].append((item, rel, owner, calls, (names | calls) if test_function else None))


class _Modules:
    """Resolve dotted module names to indexed modules, allowing a unique suffix
    (so `pkg.mod` finds `src/pkg/mod.py`)."""

    def __init__(self, index):
        self.exact = {}
        self.suffix = {}
        self.symbols = {}
        for item in index.entries:
            if item['source'] != 'ast' or not item['module']:
                continue
            if item['kind'] == 'module':
                self.exact[item['module']] = item
                parts = item['module'].split('.')
                for start in range(1, len(parts)):
                    self.suffix.setdefault('.'.join(parts[start:]), []).append(item['module'])
            else:
                self.symbols[(item['module'], item['symbol'])] = item

    def module(self, dotted):
        if dotted in self.exact:
            return dotted
        matches = self.suffix.get(dotted, [])
        return matches[0] if len(matches) == 1 else None

    def symbol(self, dotted, qualname):
        module = self.module(dotted)
        return self.symbols.get((module, qualname)) if module else None


def _resolve(reference, context, modules):
    module, aliases, owner = context
    if reference[0] == 'name':
        name = reference[1]
        imported = aliases.get(name)
        if imported and imported[0] == 'symbol':
            found = modules.symbol(imported[1], imported[2])
            if found is None:
                submodule = modules.module('%s.%s' % (imported[1], imported[2]))
                found = modules.exact.get(submodule) if submodule else None
            return found
        if imported:
            resolved = modules.module(imported[1])
            return modules.exact.get(resolved) if resolved else None
        return modules.symbols.get((module, name))
    _, base, attr = reference
    if base in ('self', 'cls') and owner:
        return modules.symbols.get((module, '%s.%s' % (owner, attr)))
    imported = aliases.get(base)
    if imported and imported[0] == 'module':
        return modules.symbol(imported[1], attr)
    if imported:
        return modules.symbol(imported[1], '%s.%s' % (imported[2], attr))
    return modules.symbols.get((module, '%s.%s' % (base, attr)))


def link_python(index, facts):
    modules = _Modules(index)
    for item, rel, owner, calls, refs in facts['defs']:
        info = facts['files'][rel]
        context = (info['module'], info['aliases'], owner)
        for reference in sorted(calls):
            target = _resolve(reference, context, modules)
            # Tests are listed under `tests`; `related` stays about the code itself.
            if (target is not None and target is not item and target['kind'] != 'module'
                    and not item['test']):
                relate(item, 'calls', target, 'INFERRED')
        for reference in sorted(refs or ()):
            target = _resolve(reference, context, modules)
            if target is not None and not target['test']:
                target['tests'].add(item['id'])
    for rel, info in facts['files'].items():
        if not is_test(rel):
            continue
        for imported in info['aliases'].values():
            resolved = modules.module(imported[1])
            target = modules.exact.get(resolved) if resolved else None
            if target is not None and not target['test']:
                target['tests'].add(rel)


# ---------------------------------------------------------------- markdown

def markdown_headings(data):
    """(line, level, title, anchor, end_line, summary) for each heading outside code
    fences, with GitHub-style anchors (duplicates get -1, -2...).

    Linear in the file: section ends come from one pass with a stack of open headings,
    a heading line longer than HEADING_LINE_MAX is treated as text, and a summary is
    looked for in at most SUMMARY_SCAN_LINES lines after its heading.
    """
    lines = [line.rstrip('\r') for line in data.decode('utf-8-sig', 'replace').split('\n')]
    if len(lines) > 1 and lines[-1] == '':
        lines.pop()  # the final newline ends the last line; it does not start another
    fence = None
    found = []
    for number, line in enumerate(lines, 1):
        marker = FENCE.match(line)
        if marker:
            token = marker.group(1)
            if fence is None:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence):
                fence = None
            continue
        if fence is None and len(line) <= HEADING_LINE_MAX:
            heading = HEADING.match(line)
            if heading:
                found.append((number, len(heading.group(1)), heading.group(2)))
    ends = [len(lines)] * len(found)
    open_headings = []
    for position, (number, level, _) in enumerate(found):
        while open_headings and found[open_headings[-1]][1] >= level:
            ends[open_headings.pop()] = number - 1
        open_headings.append(position)
    seen = {}
    result = []
    for position, (number, level, raw) in enumerate(found):
        title = clean(LINK.sub(r'\1', raw))
        base = slug(title)
        if not base:
            continue
        count = seen.get(base, 0)
        seen[base] = count + 1
        anchor = base if count == 0 else '%s-%d' % (base, count)
        end = ends[position]
        summary = None
        inside = None
        for text in lines[number:min(end, number + SUMMARY_SCAN_LINES)]:
            marker = FENCE.match(text)
            if marker:
                token = marker.group(1)
                if inside is None:
                    inside = token
                elif token[0] == inside[0] and len(token) >= len(inside):
                    inside = None
                continue
            text = text.strip()
            if inside or not text or text.startswith(('|', '<!--')) or (
                    len(text) <= HEADING_LINE_MAX and HEADING.match(text)):
                continue
            summary = re.sub(r'^(?:[-*+]|\d+\.|>)\s+', '', text[:LINE_TEXT_MAX])
            break
        result.append((number, level, title, anchor, end, summary))
    return result


def index_markdown(rel, data, index):
    headings = markdown_headings(data)
    if len(headings) > HEADINGS_PER_FILE_MAX:
        index.skip('heading-limit', len(headings) - HEADINGS_PER_FILE_MAX)
        headings = headings[:HEADINGS_PER_FILE_MAX]
    for number, _, title, anchor, end, summary in headings:
        index.add(entry('%s#%s' % (rel, anchor), 'doc-section', title, anchor, rel, number, end,
                        summary, 'markdown', [title, anchor.replace('-', ' ')]))


# ---------------------------------------------------------------- graphify graph.json

def _refuse_constant(name):
    raise ValueError('non-finite number %s is not allowed' % name)


def read_graph(path, max_bytes):
    """Parse graph.json defensively: bounded bytes, UTF-8, no NaN/Infinity, bounded
    nesting. Errors name the problem without echoing file content."""
    try:
        size = path.stat().st_size
        if size > max_bytes:
            raise ValueError('graph.json is larger than the %d MB limit (--max-graph-mb)' % (max_bytes // 1_000_000))
        with path.open('rb') as handle:
            data = handle.read(max_bytes + 1)
    except OSError:
        raise ValueError('graph.json cannot be read') from None
    if len(data) > max_bytes:
        raise ValueError('graph.json is larger than the %d MB limit (--max-graph-mb)' % (max_bytes // 1_000_000))
    try:
        text = data.decode('utf-8-sig')
    except UnicodeDecodeError:
        raise ValueError('graph.json is not UTF-8') from None
    try:
        document = json.loads(text, parse_constant=_refuse_constant)
    except RecursionError:
        raise ValueError('graph.json nests too deeply') from None
    except ValueError as exc:
        raise ValueError('graph.json is not valid JSON (%s)' % clean(exc)[:120]) from None
    if not isinstance(document, dict):
        raise ValueError('graph.json must be a JSON object with "nodes" and "links"')
    nodes = document.get('nodes')
    links = document['links'] if 'links' in document else document.get('edges', [])
    if not isinstance(nodes, list) or not isinstance(links, list):
        raise ValueError('graph.json "nodes" and "links" (or "edges") must be lists')
    if len(nodes) > GRAPH_NODES_MAX or len(links) > GRAPH_LINKS_MAX:
        raise ValueError('graph.json has more than %d nodes or %d links' % (GRAPH_NODES_MAX, GRAPH_LINKS_MAX))
    commit = document.get('built_at_commit')
    return nodes, links, commit if isinstance(commit, str) and COMMIT.fullmatch(commit) else None


def index_graph(nodes, links, index):
    """Add graphify code nodes as entries; every malformed item is counted and skipped,
    and so is a node whose file is not in this checkout (a graph claim about code the
    reader cannot open is noise, or a stale or planted pointer)."""
    present = {}
    raw = {}
    for node in nodes:
        if not isinstance(node, dict):
            index.skip('graph-node-shape')
            continue
        graph_id = node.get('id')
        if not isinstance(graph_id, str) or not GRAPH_ID.fullmatch(graph_id):
            index.skip('graph-node-id')
            continue
        if graph_id in raw:
            # Both copies are dropped: neither can be trusted to be the real one.
            raw[graph_id] = None
            index.skip('graph-duplicate-id')
            continue
        raw[graph_id] = node
    parents, rationale, edges = {}, {}, []
    for link in links:
        if not isinstance(link, dict):
            index.skip('graph-link-shape')
            continue
        source, target, relation = link.get('source'), link.get('target'), link.get('relation')
        if not (isinstance(source, str) and isinstance(target, str) and isinstance(relation, str)
                and RELATION.fullmatch(relation)):
            index.skip('graph-link-shape')
            continue
        if raw.get(source) is None or raw.get(target) is None:
            index.skip('graph-link-dangling')
            continue
        confidence = link.get('confidence') if link.get('confidence') in CONFIDENCES else None
        if relation == 'method':
            parents.setdefault(target, source)
        elif relation == 'rationale_for':
            text = raw[source].get('label')
            if isinstance(text, str) and len(text) <= GRAPH_LABEL_MAX:
                rationale.setdefault(target, text)
        edges.append((source, target, relation, confidence))
    by_graph_id = {}
    for graph_id, node in raw.items():
        if node is None or node.get('file_type') != 'code':
            continue
        source_file = node.get('source_file')
        if source_file in (None, ''):
            continue
        rel = safe_relpath(source_file)
        if rel is None:
            index.skip('graph-unsafe-path')
            continue
        if rel not in present:
            present[rel] = contained_file(index.repo['root'], rel) is not None
        if not present[rel]:
            index.skip('graph-missing-file')
            continue
        label = node.get('label')
        if not isinstance(label, str) or len(label) > GRAPH_LABEL_MAX or not clean(label):
            index.skip('graph-label')
            continue
        label = clean(label)
        location = node.get('source_location')
        match = LOCATION.fullmatch(location) if isinstance(location, str) else None
        line = int(match.group(1)) if match else None
        basename = rel.rsplit('/', 1)[-1]
        if line == 1 and (label == rel or rel.endswith('/' + label) or label == basename):
            kind, name, symbol, pointer = 'module', label, None, rel
        else:
            if label.startswith('.') and label.endswith('()'):
                kind, name = 'method', label[1:-2]
                parent = raw.get(parents.get(graph_id)) or {}
                owner = clean(parent.get('label', '')) if isinstance(parent.get('label'), str) else ''
                symbol = '%s.%s' % (owner, name) if owner and SYMBOL.fullmatch(owner) and '.' not in owner else name
            elif label.endswith('()'):
                kind, name = 'function', label[:-2]
                symbol = name
            else:
                kind, name, symbol = 'class', label, label
            if not SYMBOL.fullmatch(name) or not SYMBOL.fullmatch(symbol):
                index.skip('graph-label')
                continue
            pointer = '%s::%s' % (rel, symbol)
        stem_path = rel.rsplit('.', 1)[0].replace('/', '.')
        item = index.add(entry(pointer, kind, name, symbol, rel, line, None, rationale.get(graph_id),
                               'graphify', [name, symbol, '%s.%s' % (stem_path, symbol) if symbol else stem_path],
                               graph_id=graph_id))
        if item is not None:
            by_graph_id[graph_id] = item
    for source, target, relation, confidence in edges:
        first, second = by_graph_id.get(source), by_graph_id.get(target)
        if first is None or second is None or first is second or relation in STRUCTURAL:
            continue
        if first['test'] or second['test']:
            if first['test'] and not second['test']:
                second['tests'].add(first['id'])
            continue
        relate(first, relation, second, confidence)


def load_graph_index(repo, index, graph_arg, source, max_mb):
    """Replace the ast code index with graph.json; True when the graph was used. With
    --source auto, a default graph that is refused is set aside with a warning that says
    why and what to do; with an explicit graph source the refusal is an error. A stale
    graph is used either way, with a warning."""
    root = repo['root']
    path = Path(graph_arg) if graph_arg else root / DEFAULT_GRAPH
    if graph_arg is None:
        if contained_file(root, DEFAULT_GRAPH) is None:
            if path.exists() or source == 'graphify':
                problem = 'graphify-out/graph.json is missing or resolves outside the checkout'
                if source == 'graphify':
                    raise ValueError(problem)
                index.warnings.append('%s; %s' % (problem, GRAPH_FALLBACK))
            return False
    elif not path.is_file():
        raise ValueError('--graph: expected an existing graph.json file')
    try:
        nodes, links, commit = read_graph(path, max_mb * 1_000_000)
    except ValueError as exc:
        if source == 'auto' and graph_arg is None:
            index.warnings.append('%s; %s' % (exc, GRAPH_FALLBACK))
            return False
        raise
    try:
        shown = path.resolve().relative_to(root).as_posix()
    except (OSError, ValueError):
        shown = '<outside the checkout>'
    stale = None
    if commit and repo['commit']:
        stale = commit != repo['commit']
    index.code_source = 'graphify'
    index.graph = {'path': shown, 'built_at_commit': commit, 'stale': stale,
                   'nodes': len(nodes), 'links': len(links)}
    if stale and source == 'auto' and graph_arg is None:
        index.warnings.append('graph.json is stale (it was built at %s but the checkout is at %s); %s'
                              % (commit[:12], repo['commit'][:12], GRAPH_STALE))
    elif stale:
        index.warnings.append('graph.json was built at %s but the checkout is at %s; pointers may have '
                              'moved. Rebuild the graph, or use --source ast.' % (commit[:12], repo['commit'][:12]))
    elif repo['dirty']:
        index.warnings.append('the checkout has uncommitted changes that graph.json does not include')
    index_graph(nodes, links, index)
    return True


# ---------------------------------------------------------------- building and views

def build_index(repo, source='auto', graph_arg=None, max_mb=GRAPH_MB_DEFAULT):
    if source not in SOURCES:
        raise ValueError('--source: expected one of %s' % ', '.join(SOURCES))
    if graph_arg is not None and source == 'ast':
        raise ValueError('--graph needs --source auto or graphify')
    if isinstance(max_mb, bool) or not isinstance(max_mb, int) or not GRAPH_MB_MIN <= max_mb <= GRAPH_MB_MAX:
        raise ValueError('--max-graph-mb: expected %d..%d' % (GRAPH_MB_MIN, GRAPH_MB_MAX))
    index = Index(repo)
    use_graph = source != 'ast' and load_graph_index(repo, index, graph_arg, source, max_mb)
    names = list_files(repo, ('.md',) if use_graph else ('.py', '.md'))
    facts = {'files': {}, 'defs': []}
    for rel, data in read_files(repo, names, index):
        if rel.endswith('.py'):
            index_python(rel, data, index, facts)
        else:
            index_markdown(rel, data, index)
    if not use_graph:
        link_python(index, facts)
    index.warnings.extend(parse_warnings(index.skipped))
    return index


def view(item, score=None):
    related = sorted(item['related'].items(), key=lambda pair: (pair[0][1] != 'out', pair[0][0], pair[0][2]))
    out = {'id': item['id'], 'kind': item['kind'], 'name': excerpt(item['name'], NAME_LIMIT),
           'file': item['file'], 'line': item['line'], 'end_line': item['end_line'],
           'summary': excerpt(item['summary'], SUMMARY_LIMIT), 'source': item['source'],
           'test': item['test'],
           'aliases': [clean(alias)[:NAME_LIMIT] for alias in item['aliases'][:ALIASES_SHOWN]],
           'tests': sorted(item['tests'])[:TESTS_SHOWN], 'tests_total': len(item['tests']),
           'related': [{'relation': relation, 'direction': direction, 'id': other,
                        'confidence': confidence}
                       for (relation, direction, other), confidence in related[:RELATED_SHOWN]],
           'related_total': len(related)}
    if 'graph_id' in item:
        out['graph_id'] = item['graph_id']
    if score is not None:
        out['score'] = round(score, 3)
    return out


def _order(item):
    return (item['test'], KIND_ORDER.get(item['kind'], 3), item['id'])


# ---------------------------------------------------------------- resolve

def split_pointer(pointer):
    """(file, '::' | '#' | '', target) for file::symbol, file#anchor or file."""
    if '::' in pointer:
        rel, target = pointer.split('::', 1)
        return rel, '::', target
    if '#' in pointer:
        rel, target = pointer.rsplit('#', 1)
        return rel, '#', target
    return pointer, '', ''


def resolve_pointer(repo, pointer, graph_index=None):
    """Whether one pointer still resolves in the checkout. resolved is None when this
    version cannot check that kind of pointer (the reason says why)."""
    shown = clean(pointer)[:POINTER_MAX]
    result = {'pointer': shown, 'resolved': False, 'kind': None, 'line': None,
              'end_line': None, 'basis': None, 'reason': None}
    rel, separator, target = split_pointer(pointer) if isinstance(pointer, str) else ('', '', '')
    if safe_relpath(rel) is None or (separator and not target) or len(pointer) > POINTER_MAX:
        result['reason'] = 'invalid-pointer'
        return result
    full = contained_file(repo['root'], rel)
    if full is None:
        result['reason'] = 'file-missing'
        return result
    if separator == '':
        result.update(resolved=True, kind='module' if rel.endswith('.py') else 'file', basis='file')
        return result
    if separator == '#':
        if not rel.endswith('.md'):
            result.update(resolved=None, reason='unsupported-file-type')
            return result
        try:
            headings = markdown_headings(full.read_bytes())
        except OSError:
            result.update(resolved=None, reason='unreadable')
            return result
        for number, _, _, anchor, end, _ in headings:
            if anchor == target:
                result.update(resolved=True, kind='doc-section', line=number, end_line=end, basis='markdown')
                return result
        result['reason'] = 'anchor-missing'
        result['basis'] = 'markdown'
        return result
    if rel.endswith('.py'):
        try:
            if full.stat().st_size > FILE_BYTES_MAX:
                result.update(resolved=None, reason='file-too-large')
                return result
            tree = parse_python(full.read_bytes(), rel)
            found = {qualname: (kind, node) for qualname, kind, node in definitions(tree.body)}
        except TooComplex:
            result.update(resolved=None, reason='too-complex')
            return result
        except (OSError, SyntaxError, ValueError, RecursionError, MemoryError):
            result.update(resolved=None, reason='parse-error')
            return result
        result['basis'] = 'ast'
        if target in found:
            kind, node = found[target]
            result.update(resolved=True, kind=kind, line=node.lineno,
                          end_line=getattr(node, 'end_lineno', None))
        else:
            result['reason'] = 'symbol-missing'
        return result
    if graph_index is not None and graph_index.code_source == 'graphify':
        item = graph_index.by_id.get(pointer)
        result['basis'] = 'graph'
        if item is not None:
            result.update(resolved=True, kind=item['kind'], line=item['line'])
        else:
            result['reason'] = 'symbol-missing'
        return result
    result.update(resolved=None, reason='unsupported-file-type')
    return result


# ---------------------------------------------------------------- lookup

def _keys(item):
    keys = {normalized(alias) for alias in item['aliases']}
    keys.add(normalized(item['id']))
    keys.discard('')
    return keys


def lookup(index, phrase, limit=LIMIT_DEFAULT):
    if not isinstance(phrase, str) or len(phrase) > PHRASE_MAX:
        raise ValueError('phrase: expected text up to %d characters' % PHRASE_MAX)
    text = clean(phrase)
    if not text:
        raise ValueError('lookup needs a nonempty phrase')
    key = normalized(text)
    keys = {item['id']: _keys(item) for item in index.entries}
    exact = [item for item in index.entries
             if text == item['id'] or text in item['aliases'] or (key and key in keys[item['id']])]
    exact.sort(key=_order)
    result = {'phrase': excerpt(text, PHRASE_MAX), 'normalized': key, 'corrections': {},
              'found': bool(exact), 'match_type': 'exact' if exact else None,
              'matches': [], 'total_matches': len(exact), 'candidates': [], 'hint': None}
    if exact:
        result['matches'] = [view(item) for item in exact[:limit]]
        return result
    vocabulary, names_of = set(), {}
    for item in index.entries:
        names = set()
        for alias in item['aliases'][:3]:
            tokens = words(alias)
            vocabulary.update(tokens)
            names.update(stem(token) for token in tokens)
        names_of[item['id']] = names
    # A phrase word that no name uses is read as the closest word that one does
    # ("refrsh" -> "refresh"), and the correction is reported.
    corrected = []
    for word in words(text):
        if word not in vocabulary and len(word) >= 4:
            close = difflib.get_close_matches(word, vocabulary, n=1, cutoff=0.8)
            if close:
                result['corrections'][word] = close[0]
                word = close[0]
        corrected.append(word)
    wanted = {stem(word) for word in corrected}
    scores = {}
    for item in index.entries if wanted else ():
        names = names_of[item['id']]
        context = stems(item['file']) | (stems(item['summary']) if item['summary'] else set())
        name_hits = len(wanted & names)
        context_hits = len(wanted & (names | context))
        if not context_hits:
            continue
        scores[item['id']] = (0.6 * name_hits / len(wanted)
                              + 0.25 * (name_hits / len(names) if names else 0)
                              + 0.15 * context_hits / len(wanted))
    fuzzy_key = ' '.join(corrected)
    if fuzzy_key:
        by_key = {}
        for item in index.entries:
            for candidate_key in keys[item['id']]:
                by_key.setdefault(candidate_key, []).append(item)
        for close in difflib.get_close_matches(fuzzy_key, list(by_key), n=limit * 4, cutoff=0.75):
            ratio = difflib.SequenceMatcher(None, fuzzy_key, close).ratio()
            for item in by_key[close]:
                scores[item['id']] = max(scores.get(item['id'], 0), 0.9 * ratio)
    ranked = []
    for item_id, score in scores.items():
        item = index.by_id[item_id]
        score = score * (0.85 if item['test'] else 1.0)
        if score >= CANDIDATE_FLOOR:
            ranked.append((-score, _order(item), item))
    ranked.sort(key=lambda row: (row[0], row[1]))
    result['candidates'] = [view(item, -negative) for negative, _, item in ranked[:limit]]
    result['hint'] = {
        'text': ('No name or alias matched this phrase. Look the best candidate up by its id, '
                 'or try an exact symbol or heading name. When you find the right place, '
                 'note the phrase you searched for and the pointer you found in your task '
                 'checkpoint so it can become an alias; recorded aliases are not supported yet.'),
        'retry': 'capability lookup "<candidate id>"'}
    return result


def verify_matches(index, views):
    """Graph-sourced Python matches are re-checked with ast before they are returned."""
    if index.code_source != 'graphify':
        return
    for item in views:
        if item['source'] != 'graphify':
            continue
        if not item['file'].endswith('.py'):
            item['verified'] = None
            continue
        check = resolve_pointer(index.repo, item['id'])
        item['verified'] = check['resolved']
        if check['resolved']:
            item['line'], item['end_line'] = check['line'], check['end_line']


# ---------------------------------------------------------------- command line

HELP_TOKENS = ('-h', '--help')
VALUE_OPTIONS = {'--repo', '--source', '--graph', '--max-graph-mb', '--limit'}


def help_requested(args):
    for position, token in enumerate(args):
        if token in HELP_TOKENS and not (position and args[position - 1] in VALUE_OPTIONS):
            return True
    return False


def help_payload(command):
    common = [
        {'flag': '--repo PATH', 'description': 'checkout to index (default: the current directory; '
                                              'the Git top level is used when there is one)'},
        {'flag': '--source auto|ast|graphify', 'description': 'code index source; auto uses '
                                                             'graphify-out/graph.json when present, else ast'},
        {'flag': '--graph FILE', 'description': 'read this graph.json (implies graphify unless --source is given)'},
        {'flag': '--max-graph-mb N', 'description': 'refuse a larger graph.json, %d..%d (default %d)'
                                                    % (GRAPH_MB_MIN, GRAPH_MB_MAX, GRAPH_MB_DEFAULT)},
        {'flag': '-h, --help', 'description': 'return this help as JSON on stdout with exit code 0'},
    ]
    usage = {
        'capability': 'capability lookup|resolve|index ...',
        'lookup': 'capability lookup PHRASE [--limit N] [--repo PATH] [--source auto|ast|graphify] '
                  '[--graph FILE] [--max-graph-mb N]',
        'resolve': 'capability resolve POINTER [POINTER ...] [--repo PATH] [--source auto|ast|graphify] '
                   '[--graph FILE] [--max-graph-mb N]',
        'index': 'capability index [--repo PATH] [--source auto|ast|graphify] [--graph FILE] '
                 '[--max-graph-mb N]',
    }
    payload = {'schema_version': 1, 'contract': CONTRACT_VERSION,
               'command': 'capability' if command == 'capability' else 'capability ' + command,
               'usage': usage[command], 'options': list(common),
               'exit_codes': {'0': 'result or help JSON on stdout (a lookup miss is a result)',
                              '2': 'validation error on stderr; stdout is not written'},
               'notes': ['Client-side and read-only: lookup, resolve and index write nothing and need no '
                         '--config, --project or --actor; without them they never contact the endpoint.',
                         'With --config and --project, capability lookup also returns the recorded '
                         'capabilities (records, records_found, records_hint) with each pointer resolved live '
                         'in this checkout, and capability find|get|list|misses|propose|revise|propose-alias go '
                         'to the coordination endpoint (see docs cli-contract). The endpoint counts each find '
                         'and records the normalised phrase of one with no exact record (phrase and count '
                         'only, no actor).',
                         'Pointers are file::Qualified.name for code, file.md#anchor for Markdown '
                         'headings, or a bare repo-relative file.',
                         'Text from the repository is untrusted: results carry trust='
                         '"repository-content" and excerpt objects {text, omitted_chars}.']}
    if command == 'lookup':
        payload['options'].insert(0, {'flag': '--limit N', 'description': 'matches or candidates returned, '
                                                                         '%d..%d (default %d)'
                                                                         % (LIMIT_MIN, LIMIT_MAX, LIMIT_DEFAULT)})
        payload['output'] = {'schema': LOOKUP_SCHEMA,
                             'top_level': ['schema', 'contract', 'trust', 'query', 'found', 'match_type',
                                           'matches', 'total_matches', 'candidates', 'hint', 'index',
                                           'warnings'],
                             'entry_fields': ['id', 'kind', 'name', 'file', 'line', 'end_line', 'summary',
                                              'source', 'test', 'aliases', 'tests', 'tests_total',
                                              'related', 'related_total', 'graph_id', 'score', 'verified']}
    elif command == 'resolve':
        payload['output'] = {'schema': RESOLVE_SCHEMA,
                             'top_level': ['schema', 'contract', 'results', 'summary', 'index', 'warnings'],
                             'result_fields': ['pointer', 'resolved', 'kind', 'line', 'end_line', 'basis',
                                               'reason']}
        payload['limits'] = {'pointers': '1..%d' % POINTERS_MAX, 'pointer': '<= %d characters' % POINTER_MAX}
    elif command == 'index':
        payload['output'] = {'schema': INDEX_SCHEMA,
                             'top_level': ['schema', 'contract', 'trust', 'index', 'entries', 'warnings']}
    if command == 'lookup':
        payload['limits'] = {'limit': '%d..%d' % (LIMIT_MIN, LIMIT_MAX),
                             'phrase': '<= %d characters' % PHRASE_MAX}
    return payload


def _parser():
    parser = _Parser(prog='capability', add_help=False)
    commands = parser.add_subparsers(dest='command')
    for name in COMMANDS:
        sub = commands.add_parser(name, add_help=False)
        if name == 'lookup':
            sub.add_argument('phrase')
            sub.add_argument('--limit', type=int, default=LIMIT_DEFAULT)
        elif name == 'resolve':
            sub.add_argument('pointers', nargs='+')
        sub.add_argument('--repo', default='.')
        sub.add_argument('--source', default='auto')
        sub.add_argument('--graph')
        sub.add_argument('--max-graph-mb', type=int, default=GRAPH_MB_DEFAULT)
        sub.add_argument('--json', action='store_true')
    return parser


def execute(args):
    """(result, warnings) for one command; ValueError for any refusal."""
    if not isinstance(args, list) or any(not isinstance(arg, str) or '\0' in arg for arg in args):
        raise ValueError('Expected argument list')
    if help_requested(args):
        command = args[0] if args and args[0] in COMMANDS else 'capability'
        return help_payload(command), []
    if not args or args[0] not in COMMANDS:
        raise ValueError('expected one of: %s (see capability --help)' % ', '.join(COMMANDS))
    options = _parser().parse_args(args)
    source = options.source
    if options.graph is not None and source == 'auto':
        source = 'graphify'
    if options.command == 'lookup' and not LIMIT_MIN <= options.limit <= LIMIT_MAX:
        raise ValueError('--limit: expected %d..%d' % (LIMIT_MIN, LIMIT_MAX))
    if options.command == 'resolve' and len(options.pointers) > POINTERS_MAX:
        raise ValueError('resolve: expected 1..%d pointers' % POINTERS_MAX)
    repo = open_repo(options.repo)
    if options.command == 'resolve':
        index = None
        needs_graph = any(split_pointer(pointer)[1] == '::' and not split_pointer(pointer)[0].endswith('.py')
                          for pointer in options.pointers)
        graph_available = source == 'graphify' or contained_file(repo['root'], DEFAULT_GRAPH) is not None
        if needs_graph and source != 'ast' and graph_available:
            index = build_index(repo, source, options.graph, options.max_graph_mb)
        results = [resolve_pointer(repo, pointer, index) for pointer in options.pointers]
        summary = {'resolved': sum(1 for row in results if row['resolved'] is True),
                   'missing': sum(1 for row in results if row['resolved'] is False),
                   'unknown': sum(1 for row in results if row['resolved'] is None)}
        warnings = list(index.warnings) if index else []
        if _parse['budget_spent'] and not index:
            warnings.append('the per-run tokenize budget was spent; some pointers read too-complex')
        payload = {'schema': RESOLVE_SCHEMA, 'contract': CONTRACT_VERSION, 'trust': TRUST,
                   'results': results, 'summary': summary,
                   'index': index.describe() if index else {
                       'schema': INDEX_SCHEMA, 'code_source': 'ast', 'graph': None,
                       'repo': {'git': repo['git'], 'commit': repo['commit'], 'dirty': repo['dirty']}},
                   'warnings': warnings}
        return payload, warnings
    index = build_index(repo, source, options.graph, options.max_graph_mb)
    if options.command == 'index':
        entries = sorted(index.entries, key=lambda item: (item['file'], item['line'] or 0, item['id']))
        payload = {'schema': INDEX_SCHEMA, 'contract': CONTRACT_VERSION, 'trust': TRUST,
                   'index': index.describe(), 'entries': [view(item) for item in entries],
                   'warnings': index.warnings}
        return payload, index.warnings
    found = lookup(index, options.phrase, options.limit)
    verify_matches(index, found['matches'])
    verify_matches(index, found['candidates'])
    payload = {'schema': LOOKUP_SCHEMA, 'contract': CONTRACT_VERSION, 'trust': TRUST,
               'query': {'phrase': found['phrase'], 'normalized': found['normalized'],
                         'corrections': found['corrections']},
               'found': found['found'], 'match_type': found['match_type'],
               'matches': found['matches'], 'total_matches': found['total_matches'],
               'candidates': found['candidates'], 'hint': found['hint'],
               'index': index.describe(), 'warnings': index.warnings}
    return payload, index.warnings


def run(args):
    """(returncode, stdout, stderr) in the CLI contract's shape: JSON on stdout only on
    success; a refusal is `ValueError: ...` on stderr with exit code 2."""
    _reset_parse_state()
    try:
        payload, warnings = with_parse_stack(lambda: execute(list(args)))
    except ValueError as exc:
        return 2, '', 'ValueError: %s\n' % exc
    finally:
        _reset_parse_state()
    stderr = ''.join('warning: %s\n' % warning for warning in warnings)
    # ASCII JSON: every non-ASCII character, including the line and paragraph separators
    # some readers treat as line breaks, travels as a \u escape.
    return 0, json.dumps(payload, ensure_ascii=True, indent=2) + '\n', stderr


def main(argv=None):
    code, stdout, stderr = run(sys.argv[1:] if argv is None else argv)
    sys.stdout.write(stdout)
    sys.stderr.write(stderr)
    return code


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
    sys.exit(main())
