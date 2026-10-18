"""The web pages never call an array-only method on a live DOM collection, and the test DOM
shim gives the same collections a browser does.

In a browser `childNodes` is a NodeList and `children` an HTMLCollection: neither has slice,
map or filter (an HTMLCollection has no forEach either). A shim that returned plain arrays let
such a call pass every Node test and break only in a real browser: that is how the set-up wizard
(kittrial-5bb.329) shipped with `bodyNode.childNodes.slice()`, which stopped it before it hid
steps 2 and 3 (hotfix 04cd10b; kittrial-5bb.342).

Two nets, because either alone has holes:

* the static scan below reads web/js and reports direct `.childNodes.<array method>(` and
  `.children.<array method>(` calls. It does not see a collection held in a variable, an
  optional-chained or line-broken call, or a bracket access, and it cannot tell a comment from
  code;
* the Node contract test runs tests/web_dom_shim.mjs itself: the collections it hands out have
  no array methods, are live and iterate, and calling slice() on childNodes raises. Every Node
  screen test therefore fails the way a browser would, including the forms the regex misses.

Copy first when an array is needed: `Array.from(node.childNodes)` or `[...node.children]`.
"""
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

WEB_JS = Path(__file__).resolve().parents[1] / 'web' / 'js'
SHIM = Path(__file__).resolve().parent / 'web_dom_shim.mjs'
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


class ShimCollectionContractTests(unittest.TestCase):
    """tests/web_dom_shim.mjs must hand out browser collections and browser child conversion.

    Two mutations this catches: returning a plain Array from querySelectorAll (so a page's
    .map passes under Node and throws in a browser), and silently dropping null in append /
    replaceChildren where a browser inserts the text "null" (kittrial-5bb.342 review item
    static-lists-and-null).
    """

    SCRIPT = r"""
const { Element } = await import(process.argv[1]);
const root = new Element('div');
const first = new Element('span');
const second = new Element('b');
root.appendChild(first);
root.appendChild(second);
const held = root.childNodes;
const out = {
  isArray: Array.isArray(held),
  length: held.length,
  index: held[0] === first,
  item: held.item(1) === second,
  itemPastEnd: held.item(9),
  spread: [...held].length,
  from: Array.from(held).length,
  forEach: typeof held.forEach,
  slice: typeof held.slice,
  map: typeof held.map,
  filter: typeof held.filter,
  childrenLength: root.children.length,
  childrenItem: root.children.item(0) === first,
  childrenForEach: typeof root.children.forEach,
  childrenSlice: typeof root.children.slice,
};
root.appendChild(new Element('i'));
out.liveLength = held.length;
const seen = [];
held.forEach((node, index, list) => seen.push(index + ':' + (list === held)));
out.forEachSeen = seen;
out.sliceThrows = false;
try { held.slice(1); } catch { out.sliceThrows = true; }
out.childrenForEachThrows = false;
try { root.children.forEach(() => {}); } catch { out.childrenForEachThrows = true; }

// querySelectorAll: a STATIC NodeList, never an Array.
const found = root.querySelectorAll('span');
out.qsa = {
  isArray: Array.isArray(found), length: found.length, index: found[0] === first,
  item: found.item(0) === first, itemPastEnd: found.item(9),
  forEach: typeof found.forEach, map: typeof found.map, find: typeof found.find,
  some: typeof found.some, at: typeof found.at, slice: typeof found.slice,
  spread: [...found].length, from: Array.from(found).length,
};
out.qsaMapThrows = false;
try { root.querySelectorAll('b').map((n) => n); } catch { out.qsaMapThrows = true; }
root.appendChild(new Element('span'));
out.qsaStaticLength = found.length;   // a querySelectorAll list does not grow

// append / replaceChildren convert a non-Node with String(), so null is the text "null".
const host = new Element('p');
host.append(null);
out.appendNull = host.textContent;
host.replaceChildren('a', null, undefined, false);
out.replaceNull = host.textContent;
host.replaceChildren(null);
out.replaceOnlyNull = host.textContent;
const replaced = new Element('em');
host.replaceChildren(replaced);
replaced.replaceWith(null);
out.replaceWithNull = host.textContent;
console.log(JSON.stringify(out));
"""

    def test_the_shim_collections_behave_like_a_browser(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('node is not installed; the source scan above still applies')
        out = subprocess.run([node, '-e', self.SCRIPT, SHIM.as_uri()],
                             capture_output=True, text=True, timeout=120)
        self.assertEqual(0, out.returncode, out.stderr)
        data = json.loads(out.stdout)
        self.assertFalse(data['isArray'], 'childNodes is still an Array')
        self.assertEqual(2, data['length'])
        self.assertTrue(data['index'])
        self.assertTrue(data['item'])
        self.assertIsNone(data['itemPastEnd'])
        self.assertEqual(2, data['spread'])
        self.assertEqual(2, data['from'])
        self.assertEqual('function', data['forEach'])           # NodeList has forEach
        self.assertEqual('undefined', data['slice'])            # and none of these
        self.assertEqual('undefined', data['map'])
        self.assertEqual('undefined', data['filter'])
        self.assertEqual(2, data['childrenLength'])
        self.assertTrue(data['childrenItem'])
        self.assertEqual('undefined', data['childrenForEach'])  # HTMLCollection has no forEach
        self.assertEqual('undefined', data['childrenSlice'])
        self.assertEqual(3, data['liveLength'], 'a held childNodes must stay live')
        self.assertEqual(['0:true', '1:true', '2:true'], data['forEachSeen'])
        self.assertTrue(data['sliceThrows'], 'the shipped childNodes.slice() call must now fail')
        self.assertTrue(data['childrenForEachThrows'])
        # querySelectorAll: NodeList-shaped, static, no array methods (the second mutation).
        self.assertFalse(data['qsa']['isArray'], 'querySelectorAll still returns an Array')
        self.assertEqual(1, data['qsa']['length'])
        self.assertTrue(data['qsa']['index'])
        self.assertTrue(data['qsa']['item'])
        self.assertIsNone(data['qsa']['itemPastEnd'])
        self.assertEqual('function', data['qsa']['forEach'])
        for method in ('map', 'find', 'some', 'at', 'slice'):
            self.assertEqual('undefined', data['qsa'][method], 'querySelectorAll().%s exists' % method)
        self.assertEqual(1, data['qsa']['spread'])
        self.assertEqual(1, data['qsa']['from'])
        self.assertTrue(data['qsaMapThrows'], 'a page .map on querySelectorAll would pass under Node')
        self.assertEqual(1, data['qsaStaticLength'], 'a querySelectorAll list must be a snapshot')
        # append / replaceChildren: null is inserted as the text "null" (the third mutation).
        self.assertEqual('null', data['appendNull'])
        self.assertEqual('anullundefinedfalse', data['replaceNull'])
        self.assertEqual('null', data['replaceOnlyNull'])
        self.assertEqual('null', data['replaceWithNull'])


if __name__ == '__main__':
    unittest.main()
