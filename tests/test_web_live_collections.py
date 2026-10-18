"""The web pages never call an array-only method on a live DOM collection.

In a browser `childNodes` is a NodeList and `children` an HTMLCollection: neither has slice,
map or filter (an HTMLCollection has no forEach either). tests/web_dom_shim.mjs gives both as
plain arrays, so such a call passes every Node test and breaks only in a real browser. That
is how the set-up wizard (kittrial-5bb.329) shipped with `bodyNode.childNodes.slice()`, which
stopped it before it hid steps 2 and 3. Copy first: `Array.from(node.childNodes)` or `[...node.children]`.
"""
import re
import unittest
from pathlib import Path

WEB_JS = Path(__file__).resolve().parents[1] / 'web' / 'js'
ARRAY_ONLY = ('slice', 'map', 'filter', 'reduce', 'reduceRight', 'some', 'every', 'find', 'findIndex',
              'findLast', 'findLastIndex', 'indexOf', 'lastIndexOf', 'includes', 'concat', 'join',
              'flat', 'flatMap', 'at', 'reverse', 'sort', 'splice', 'push', 'pop', 'shift', 'unshift')
CALL = re.compile(r'\.(childNodes|children)\s*\.\s*(%s|forEach)\s*\(' % '|'.join(ARRAY_ONLY))


def offenders(text):
    found = []
    for number, line in enumerate(text.splitlines(), 1):
        for match in CALL.finditer(line):
            collection, method = match.groups()
            if collection == 'childNodes' and method == 'forEach':
                continue  # NodeList has forEach; HTMLCollection does not
            found.append((number, match.group(0)))
    return found


class LiveCollectionTests(unittest.TestCase):
    def test_no_page_script_calls_an_array_method_on_a_live_collection(self):
        files = sorted(WEB_JS.rglob('*.js'))
        self.assertTrue(files, 'no web scripts found under %s' % WEB_JS)
        bad = ['%s:%d %s' % (path.relative_to(WEB_JS.parent).as_posix(), number, call)
               for path in files for number, call in offenders(path.read_text(encoding='utf-8'))]
        self.assertEqual([], bad)

    def test_the_check_finds_the_shipped_mistake_and_allows_the_copies(self):
        self.assertEqual([(1, '.childNodes.slice(')], offenders('const kids = bodyNode.childNodes.slice();'))
        self.assertEqual(1, len(offenders('el.children.forEach((c) => c.remove());')))
        self.assertEqual([], offenders('el.childNodes.forEach((c) => c.remove());'))
        self.assertEqual([], offenders('const kids = Array.from(bodyNode.childNodes); [...el.children].map(f);'))


if __name__ == '__main__':
    unittest.main()
