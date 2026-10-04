// A small DOM for running web/js views under Node (no browser): what dom.js and ui.js use.
// The same shim as tests/web_proposal_screens.mjs, as a module other screen tests can import.
class Node {
  constructor() { this.childNodes = []; this.parentNode = null; }
  appendChild(child) {
    if (child.parentNode) child.parentNode.removeChild(child);
    child.parentNode = this; this.childNodes.push(child); return child;
  }
  removeChild(child) { this.childNodes = this.childNodes.filter((c) => c !== child); child.parentNode = null; return child; }
  remove() { if (this.parentNode) this.parentNode.removeChild(this); }
  get firstChild() { return this.childNodes[0] || null; }
  get textContent() { return this.childNodes.map((c) => c.textContent).join(''); }
  set textContent(value) { this.childNodes = []; if (value !== '' && value != null) this.appendChild(new Text(String(value))); }
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
  setAttribute(key, value) { this.attributes[key] = String(value); }
  getAttribute(key) { return key in this.attributes ? this.attributes[key] : null; }
  addEventListener(type, handler) { (this.listeners[type] = this.listeners[type] || []).push(handler); }
  async dispatch(type) {
    const event = { type, target: this, preventDefault() {} };
    for (const handler of this.listeners[type] || []) await handler(event);
  }
  append(...children) { for (const c of children) this.appendChild(typeof c === 'string' ? new Text(c) : c); }
  replaceChildren(...children) { this.childNodes = []; this.append(...children); }
  focus() {}
  all(test, out = []) {
    for (const c of this.childNodes) if (c instanceof Element) { if (test(c)) out.push(c); c.all(test, out); }
    return out;
  }
  querySelector(selector) { return this.all(matcher(selector))[0] || null; }
  querySelectorAll(selector) { return this.all(matcher(selector)); }
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

export { Node, Text, Element, FormData };
