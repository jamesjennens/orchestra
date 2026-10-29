"""Real-file checks for JS and TS capability pointers."""
import tempfile
import time
import tracemalloc
import unittest
from pathlib import Path
from unittest import mock

import capabilities


class JavaScriptSymbols(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = {'root': self.root}

    def resolve(self, text, target, extension='js'):
        path = self.root / ('file.' + extension)
        path.write_text(text, encoding='utf-8')
        return capabilities.resolve_pointer(self.repo, path.name + '::' + target)

    def test_function(self):
        self.assertTrue(self.resolve('function run() {}', 'run')['resolved'])

    def test_class(self):
        self.assertTrue(self.resolve('class Engine {}', 'Engine', 'mjs')['resolved'])

    def test_method(self):
        self.assertTrue(self.resolve('class Engine { start() {} }', 'Engine.start', 'cjs')['resolved'])

    def test_field_call_is_not_method(self):
        self.assertIs(self.resolve('class Engine { field = call(); start() {} }', 'Engine.call')['resolved'], False)
        self.assertTrue(self.resolve('class Engine { field = call(); start() {} }', 'Engine.start')['resolved'])

    def test_exported_const(self):
        self.assertTrue(self.resolve('export const value = 1, other = 2;', 'value')['resolved'])
        self.assertTrue(self.resolve('export let value = 1, other = 2;', 'other')['resolved'])

    def test_default_export(self):
        self.assertTrue(self.resolve('export default function () {}', 'default')['resolved'])

    def test_renamed_export(self):
        self.assertTrue(self.resolve('const local = 1; export { local as renamed };', 'renamed')['resolved'])

    def test_missing(self):
        self.assertIs(self.resolve('function present() {}', 'absent')['resolved'], False)

    def test_comment_and_string(self):
        self.assertIs(self.resolve('// function fake() {}\nconst x = "class fake {}";', 'fake')['resolved'], False)

    def test_string_only_name_is_missing(self):
        self.assertIs(self.resolve('const x = "function fake() {}";', 'fake')['resolved'], False)

    def test_expression_name_is_not_a_declaration(self):
        self.assertIs(self.resolve('const x = function fake() {};', 'fake')['resolved'], False)

    def test_template_braces(self):
        self.assertTrue(self.resolve('const text = `hello { world } ${`nested ${ {x: 1}.x }`}`; function real() {}', 'real')['resolved'])

    def test_typescript_generic_and_type_export(self):
        text = 'export type { Shape as Figure }; export function identity<T>(x: T): T { return x; }'
        self.assertTrue(self.resolve(text, 'identity', 'ts')['resolved'])
        self.assertIs(self.resolve(text, 'Figure', 'tsx')['resolved'], False)
        self.assertIs(self.resolve('export { type Shape as Figure };', 'Figure', 'ts')['resolved'], False)

    def test_unreadable(self):
        path = self.root / 'file.js'
        path.write_text('function f() {}')
        with mock.patch.object(Path, 'open', side_effect=PermissionError('denied')):
            result = capabilities.resolve_pointer(self.repo, 'file.js::f')
        self.assertIsNone(result['resolved'])
        self.assertEqual(result['reason'], 'unreadable')

    def test_large_file_bounded(self):
        path = self.root / 'file.js'
        path.write_bytes(b' ' * (capabilities.JS_MAX_BYTES + 1))
        tracemalloc.start()
        start = time.monotonic()
        result = capabilities.resolve_pointer(self.repo, 'file.js::f')
        elapsed = time.monotonic() - start
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        self.assertIsNone(result['resolved'])
        self.assertEqual(result['reason'], 'file-too-large')
        self.assertLess(elapsed, 5)
        self.assertLess(peak, 10_000_000)

    def test_time_budget(self):
        with mock.patch.object(capabilities.time, 'monotonic', side_effect=[0.0, 4.0]):
            self.assertIsNone(self.resolve('function f() {}', 'f')['resolved'])

    def test_unsupported_syntax_is_unknown(self):
        self.assertIsNone(self.resolve('const pattern = /x/;', 'missing')['resolved'])
