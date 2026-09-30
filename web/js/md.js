// Minimal Markdown to DOM for requirement and decision text: headings, paragraphs,
// bullet/numbered lists, `code`, **bold** and linked record IDs. Builds nodes
// directly (no innerHTML), so record text can never inject markup.
import { h } from './dom.js';

const ID = /\b(kittrial-[a-z0-9]+(?:\.\d+)*|[a-z]+-[a-z0-9]{3}(?:\.\d+)+|R\d{2})\b/g;

function inline(text, link) {
  const out = [];
  const pattern = /(`[^`]+`)|(\*\*[^*]+\*\*)/g;
  let last = 0; let m;
  const plain = (s) => {
    if (!link) { out.push(s); return; }
    let i = 0; let id;
    ID.lastIndex = 0;
    while ((id = ID.exec(s))) {
      out.push(s.slice(i, id.index));
      out.push(link(id[1]) || id[1]);
      i = id.index + id[1].length;
    }
    out.push(s.slice(i));
  };
  while ((m = pattern.exec(text))) {
    plain(text.slice(last, m.index));
    if (m[1]) out.push(h('code', null, m[1].slice(1, -1)));
    else out.push(h('strong', null, m[2].slice(2, -2)));
    last = m.index + m[0].length;
  }
  plain(text.slice(last));
  return out;
}

export function markdown(text, { link, headingBase = 3 } = {}) {
  const root = h('div', { class: 'md' });
  const lines = String(text || '').replace(/\r\n?/g, '\n').split('\n');
  let para = []; let list = null;
  const flush = () => {
    if (para.length) { root.append(h('p', null, inline(para.join(' '), link))); para = []; }
    if (list) { root.append(list); list = null; }
  };
  for (const raw of lines) {
    const line = raw.trimEnd();
    const heading = /^(#{1,4})\s+(.*)$/.exec(line);
    const bullet = /^\s*[-*]\s+(.*)$/.exec(line);
    const numbered = /^\s*\d+[.)]\s+(.*)$/.exec(line);
    if (!line.trim()) { flush(); continue; }
    if (heading) {
      flush();
      const level = Math.min(6, headingBase + heading[1].length - 1);
      root.append(h('h' + level, { class: 'md-h' }, inline(heading[2], link)));
    } else if (bullet || numbered) {
      if (para.length) { root.append(h('p', null, inline(para.join(' '), link))); para = []; }
      const tag = bullet ? 'ul' : 'ol';
      if (!list || list.tagName.toLowerCase() !== tag) { if (list) root.append(list); list = h(tag); }
      list.append(h('li', null, inline((bullet || numbered)[1], link)));
    } else {
      if (list) { root.append(list); list = null; }
      para.push(line.trim());
    }
  }
  flush();
  return root;
}

// Pulls one "## Heading" section out of a Markdown body (e.g. Rationale).
export function section(text, name) {
  const re = new RegExp('^##\\s+' + name + '\\s*$', 'mi');
  const m = re.exec(text || '');
  if (!m) return null;
  const rest = text.slice(m.index + m[0].length);
  const next = /^##\s+/m.exec(rest);
  return (next ? rest.slice(0, next.index) : rest).trim();
}

export function mentions(text) {
  const found = new Set(); let m;
  ID.lastIndex = 0;
  while ((m = ID.exec(String(text || '')))) found.add(m[1]);
  return [...found];
}
