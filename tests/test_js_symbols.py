"""Real-file checks for JS and TS capability pointers."""
import tempfile
import time
import tracemalloc
import unittest
from pathlib import Path
from unittest import mock
from types import SimpleNamespace

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

    def test_declaration_after_closed_statement(self):
        self.assertTrue(self.resolve('run()\nfunction f() {}', 'f')['resolved'])

    def test_ternary_function_expression(self):
        self.assertIs(self.resolve('const x = c ? function ghost(){} : null;', 'ghost')['resolved'], False)

    def test_and_class_expression(self):
        self.assertIs(self.resolve('c && class Ghost{};', 'Ghost')['resolved'], False)

    def test_negated_function_expression(self):
        self.assertIs(self.resolve('!function ghost(){}();', 'ghost')['resolved'], False)

    def test_arrow_function_expression(self):
        self.assertIs(self.resolve('const x = () => function ghost(){};', 'ghost')['resolved'], False)

    def test_typeof_class_expression(self):
        self.assertIs(self.resolve('typeof class Ghost{};', 'Ghost')['resolved'], False)

    def test_interface(self):
        self.assertTrue(self.resolve('export interface I {}', 'I', 'ts')['resolved'])

    def test_type_alias(self):
        self.assertTrue(self.resolve('export type X = string;', 'X', 'ts')['resolved'])

    def test_enum(self):
        self.assertTrue(self.resolve('export enum E { A }', 'E', 'ts')['resolved'])

    def test_const_enum(self):
        self.assertTrue(self.resolve('export const enum E { A }', 'E', 'ts')['resolved'])

    def test_export_var(self):
        self.assertTrue(self.resolve('export var w = 1;', 'w', 'js')['resolved'])

    def test_declare_function(self):
        self.assertTrue(self.resolve('declare function f(x: string): void;', 'f', 'ts')['resolved'])

    def test_string_named_export(self):
        self.assertTrue(self.resolve('const a = 1; export { a as "b-c" };', 'b-c')['resolved'])

    def test_block_comment(self):
        self.assertIs(self.resolve('/* function ghost() {} */ function real() {}', 'ghost')['resolved'], False)

    def test_template_interpolation_brace_depth(self):
        text = 'const t = `${ { x: "}" }.x + `inside {` }`; function real() {}'
        self.assertTrue(self.resolve(text, 'real')['resolved'])
        self.assertIs(self.resolve(text, 'ghost')['resolved'], False)

    def test_token_cap(self):
        with mock.patch.object(capabilities, 'JS_MAX_TOKENS', 5):
            self.assertIsNone(self.resolve('const one = 1; function f() {}', 'f')['resolved'])

    def test_division(self):
        self.assertTrue(self.resolve('const ratio = total / count; export function render() {}', 'render')['resolved'])

    def test_regex(self):
        self.assertTrue(self.resolve('const re = /[{}\\/]+/gi; export function render() {}', 'render')['resolved'])

    def test_jsx(self):
        text = 'export const App = () => <div className="x"><A value={item.name} />Hello {name}</div>; export function later() {}'
        self.assertTrue(self.resolve(text, 'App', 'tsx')['resolved'])
        self.assertTrue(self.resolve(text, 'later', 'tsx')['resolved'])

    def test_slash_inside_expressions(self):
        text = 'const t = `${value / count} ${/[{}]/.test(x)}`; const el = <div>{value / count}</div>; function done() {}'
        self.assertTrue(self.resolve(text, 'done', 'tsx')['resolved'])

    def test_jsx_in_js(self):
        self.assertTrue(self.resolve('const x = <div>hello</div>; function after() {}', 'after', 'jsx')['resolved'])

    def test_asi_number(self):
        self.assertTrue(self.resolve('const a = 1\nfunction foo(){}', 'foo', 'ts')['resolved'])

    def test_asi_string(self):
        self.assertTrue(self.resolve("const a = 'x'\nfunction foo(){}", 'foo', 'ts')['resolved'])

    def test_asi_increment(self):
        self.assertTrue(self.resolve('x++\nfunction foo(){}', 'foo', 'ts')['resolved'])

    def test_asi_sum(self):
        self.assertTrue(self.resolve('b + 1\nfunction foo(){}', 'foo', 'ts')['resolved'])

    def test_asi_class(self):
        self.assertTrue(self.resolve('let a = b\nclass Bar{}', 'Bar', 'ts')['resolved'])

    def test_asi_import(self):
        self.assertTrue(self.resolve("import x from 'y'\nfunction bar(){}", 'bar', 'ts')['resolved'])

    def test_label(self):
        self.assertTrue(self.resolve('lbl: function foo(){}', 'foo', 'ts')['resolved'])

    def test_decorated_export(self):
        self.assertTrue(self.resolve('@dec\nexport class A{}', 'A', 'ts')['resolved'])

    def test_decorated_class(self):
        self.assertTrue(self.resolve('@dec class A{}', 'A', 'ts')['resolved'])

    def test_asi_type(self):
        self.assertTrue(self.resolve('export type X = string | number\nexport function f(){}', 'f', 'ts')['resolved'])

    def test_asi_var(self):
        self.assertTrue(self.resolve('export var w\nexport function f(){}', 'f', 'ts')['resolved'])

    def test_asi_division(self):
        self.assertTrue(self.resolve('x = a / b / c\nexport function f(){}', 'f', 'ts')['resolved'])

    def test_namespace(self):
        self.assertTrue(self.resolve('namespace N {}', 'N', 'ts')['resolved'])

    def test_export_namespace(self):
        self.assertTrue(self.resolve('export namespace N {}', 'N', 'ts')['resolved'])

    def test_module(self):
        self.assertTrue(self.resolve('module M {}', 'M', 'ts')['resolved'])

    def test_declare_module(self):
        self.assertTrue(self.resolve("declare module 'm' {}", 'm', 'ts')['resolved'])

    def test_declare_global(self):
        self.assertTrue(self.resolve('declare global {}', 'global', 'ts')['resolved'])

    def test_abstract_class(self):
        self.assertTrue(self.resolve('abstract class A {}', 'A', 'ts')['resolved'])

    def test_export_abstract_class(self):
        self.assertTrue(self.resolve('export abstract class A {}', 'A', 'ts')['resolved'])

    def test_default_abstract(self):
        self.assertTrue(self.resolve('export default abstract class A {}', 'A', 'ts')['resolved'])

    def test_default_interface(self):
        self.assertTrue(self.resolve('export default interface I {}', 'I', 'ts')['resolved'])

    def test_export_import(self):
        self.assertTrue(self.resolve('export import a = b.c;', 'a', 'ts')['resolved'])

    def test_star_export(self):
        self.assertTrue(self.resolve("export * as ns from 'x';", 'ns', 'ts')['resolved'])

    def test_after_type_export(self):
        self.assertTrue(self.resolve("export type { T } from 'y'\nfunction q(){}", 'q', 'ts')['resolved'])

    def test_overload(self):
        self.assertTrue(self.resolve('export function f(a:number):void; export function f(a:any) {}', 'f', 'ts')['resolved'])

    def test_declare_overloads(self):
        self.assertTrue(self.resolve('export declare function f(a:number):void; export declare function f(a:any):void;', 'f', 'ts')['resolved'])

    def test_mid_statement_block_comment(self):
        text = 'const a = 1 /* function ghost(){} */ ; export function real(){}'
        self.assertIs(self.resolve(text, 'ghost')['resolved'], False)
        self.assertIs(self.resolve(text, 'real')['resolved'], True)

    def test_unbalanced_brace_in_template_text(self):
        self.assertTrue(self.resolve('const t = `unbalanced { text`; function real(){}', 'real')['resolved'])

    def test_jsx_after_and(self):
        self.assertTrue(self.resolve('const x = ok && <p>hi</p>; function real(){}', 'real', 'tsx')['resolved'])

    def test_generic_arrow_tsx(self):
        self.assertTrue(self.resolve('const x = <T,>(x:T) => x; function real(){}', 'real', 'tsx')['resolved'])

    def test_increment_division(self):
        self.assertTrue(self.resolve('const n = a++ / 2; function real(){}', 'real')['resolved'])

    def test_graph_jsx_rechecked(self):
        self.resolve('export function real() {}', 'real', 'jsx')
        index = SimpleNamespace(code_source='graphify', repo=self.repo)
        view = {'source': 'graphify', 'file': 'file.jsx', 'id': 'file.jsx::real', 'verified': None}
        capabilities.verify_matches(index, [view])
        self.assertIs(view['verified'], True)

    def test_invalid_regex_is_unknown(self):
        self.assertIsNone(self.resolve('const pattern = /unterminated', 'missing')['resolved'])
