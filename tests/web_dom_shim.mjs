// A small DOM for running web/js views under Node (no browser): what dom.js and ui.js use.
//
// childNodes and children are LIVE COLLECTION VIEWS, not arrays, because that is what a
// browser gives: childNodes is a NodeList (indexable, length, item(), forEach, iterable) and
// children is an HTMLCollection (indexable, length, item(), iterable; NO forEach). Neither has
// slice, map or filter. A shim that returned arrays let `bodyNode.childNodes.slice()` pass every
// Node test and throw in a real browser (kittrial-5bb.340; the wizard shipped that way). This
// shim must fail the next such call, so a test that needs an array copies first:
// Array.from(node.childNodes) or [...node.children] (kittrial-5bb.342).
function liveCollection(items, { forEach = false } = {}) {
  const surface = {
    get length() { return items.length; },
    item(index) { return items[index] === undefined ? null : items[index]; },
    [Symbol.iterator]() { return items[Symbol.iterator](); },
    entries() { return items.entries(); },
    keys() { return items.keys(); },
    values() { return items.values(); },
  };
  // NodeList has forEach; HTMLCollection does not. Leaving it off children is the point.
  if (forEach) {
    surface.forEach = function forEachItem(callback, thisArg) {
      for (let index = 0; index < items.length; index += 1) {
        callback.call(thisArg, items[index], index, view);
      }
    };
  }
  // The view reads the same array the node mutates, so a held childNodes stays live, as in a
  // browser. Only the indexable/length/iteration surface is exposed: slice, map and filter are
  // absent, not merely undocumented.
  const view = new Proxy(surface, {
    get(target, property, receiver) {
      if (typeof property === 'string' && /^(?:0|[1-9][0-9]*)$/.test(property)) return items[Number(property)];
      return Reflect.get(target, property, receiver);
    },
    has(target, property) {
      if (typeof property === 'string' && /^(?:0|[1-9][0-9]*)$/.test(property)) return Number(property) < items.length;
      return Reflect.has(target, property);
    },
  });
  return view;
}

// querySelectorAll returns a STATIC NodeList in a browser: a snapshot with index, length,
// item(), forEach and iteration, and no array methods. A plain Array here let a page call
// querySelectorAll(...).map(...) pass under Node and throw in a browser (kittrial-5bb.342
// review item static-lists-and-null). Copy first: Array.from(node.querySelectorAll(sel)).
function nodeList(items) {
  return liveCollection(items.slice(), { forEach: true });
}

class Node {
  constructor() { this._childNodes = []; this.parentNode = null; }
  get childNodes() { return liveCollection(this._childNodes, { forEach: true }); }
  appendChild(child) {
    if (child.parentNode) child.parentNode.removeChild(child);
    child.parentNode = this; this._childNodes.push(child); return child;
  }
  removeChild(child) {
    const index = this._childNodes.indexOf(child);
    if (index !== -1) this._childNodes.splice(index, 1);
    child.parentNode = null; return child;
  }
  remove() { if (this.parentNode) this.parentNode.removeChild(this); }
  // The ChildNode.replaceWith the screen tests used to patch onto Element; a browser has it.
  // Like append, a non-Node argument becomes its string ("null" included).
  replaceWith(...nodes) {
    const parent = this.parentNode;
    if (!parent) return;
    const replacement = nodes.map((node) => (node instanceof Node ? node : new Text(String(node))));
    for (const node of replacement) if (node.parentNode) node.parentNode.removeChild(node);
    const index = parent._childNodes.indexOf(this);
    if (index === -1) return;
    this.parentNode = null;
    parent._childNodes.splice(index, 1, ...replacement);
    for (const node of replacement) node.parentNode = parent;
  }
  get firstChild() { return this._childNodes[0] || null; }
  get textContent() { return this._childNodes.map((c) => c.textContent).join(''); }
  set textContent(value) { this._childNodes.length = 0; if (value !== '' && value != null) this.appendChild(new Text(String(value))); }
}
class Text extends Node {
  constructor(data) { super(); this.data = String(data); }
  get textContent() { return this.data; }
  set textContent(value) { this.data = String(value); }
}
function matcher(selector) {
  if (selector.startsWith('#')) return (e) => e.attributes.id === selector.slice(1);
  const m = /^(\w+)(?:\[(\w+)=(\w+)\])?$/.exec(selector);
  return (e) => e.tagName === m[1].toUpperCase() && (!m[2] || e.attributes[m[2]] === m[3]);
}
class Element extends Node {
  constructor(tag) {
    super(); this.tagName = tag.toUpperCase(); this.attributes = {}; this.listeners = {};
    this.className = ''; this.dataset = {}; this.hidden = false; this.disabled = false; this.selected = false;
  }
  // children is the element-only HTMLCollection: no forEach, unlike childNodes.
  get children() { return liveCollection(this._childNodes.filter((child) => child instanceof Element)); }
  get childElementCount() { return this.children.length; }
  setAttribute(key, value) { this.attributes[key] = String(value); }
  getAttribute(key) { return key in this.attributes ? this.attributes[key] : null; }
  removeAttribute(key) { delete this.attributes[key]; }
  addEventListener(type, handler) { (this.listeners[type] = this.listeners[type] || []).push(handler); }
  async dispatch(type) {
    const event = { type, target: this, preventDefault() {} };
    for (const handler of this.listeners[type] || []) await handler(event);
  }
  // A browser converts a non-Node argument with String(), so append(null) inserts the text
  // "null" and append(undefined) inserts "undefined"; it does not skip them.
  append(...children) { for (const child of children) this.appendChild(child instanceof Node ? child : new Text(String(child))); }
  replaceChildren(...children) { this._childNodes.length = 0; this.append(...children); }
  focus() {}
  all(test, out = []) {
    for (const c of this._childNodes) if (c instanceof Element) { if (test(c)) out.push(c); c.all(test, out); }
    return out;
  }
  querySelector(selector) { return this.all(matcher(selector))[0] || null; }
  querySelectorAll(selector) { return nodeList(this.all(matcher(selector))); }
  get value() {
    if (this.tagName === 'SELECT') {
      if (this.chosen !== undefined) return this.chosen;
      const options = this.all((e) => e.tagName === 'OPTION');
      const option = options.find((o) => o.selected) || options[0];
      return option ? option.value : '';
    }
    if (this.stored !== undefined) return this.stored;
    return this.tagName === 'TEXTAREA' ? this.textContent : '';
  }
  set value(v) { if (this.tagName === 'SELECT') this.chosen = String(v); else this.stored = String(v); }
  // A form reset clears what the user typed. The default VALUE attribute is not tracked
  // here, so this returns controls to empty, which is what every test form starts as.
  reset() {
    for (const control of this.all((e) => ['INPUT', 'TEXTAREA', 'SELECT'].includes(e.tagName))) {
      if (control.tagName === 'SELECT') control.chosen = undefined;
      else control.stored = '';
    }
  }
}
class FormData {
  constructor(form) {
    this.pairs = form.all((e) => ['INPUT', 'TEXTAREA', 'SELECT'].includes(e.tagName) && e.attributes.name)
      .map((e) => [e.attributes.name, e.value]);
  }
  entries() { return this.pairs[Symbol.iterator](); }
}
globalThis.Node = Node;
globalThis.FormData = FormData;
globalThis.document = { createElement: (tag) => new Element(tag), createTextNode: (text) => new Text(text),
  createElementNS: (_, tag) => new Element(tag), body: new Element('body') };

export { Node, Text, Element, FormData, liveCollection, nodeList };
