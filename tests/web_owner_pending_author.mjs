import './web_dom_shim.mjs';
import assert from 'node:assert/strict';
const [url, raw] = process.argv.slice(2);
const { requirement } = await import(url);
const data = JSON.parse(raw);
const ctx = { href: p => '#' + p, api: {}, setDirty() {}, go() {} };
const page = await requirement(ctx, { pid: 'alpha', rid: data.id }, {}, data);
const button = Array.from(page.querySelectorAll('button')).find(b => b.textContent === 'Accept draft');
assert.ok(button, page.textContent);
assert.ok(button.parentNode.textContent.includes('written by a contributor who gave the name <contributor>[U+202E]usr_aaaaaaaaaaaaaaaa (not verified)'));
assert.ok(button.parentNode.textContent.includes('Recorded 2026-09-25T00:00:00Z'));
assert.equal(page.querySelectorAll('contributor').length, 0);
assert.ok(!page.textContent.includes('\u202e'));
const own = await requirement(ctx, { pid: 'alpha', rid: data.id }, {}, {
  ...data, pending_author: {kind:'owner-account', text:'Owner Name', omitted_chars:0,
    is_self:true, at:'2026-09-25T00:00:00Z'}
});
assert.ok(own.textContent.includes('your own edit'));
assert.ok(!own.textContent.includes('(not verified)'));
const other = await requirement(ctx, { pid: 'alpha', rid: data.id }, {}, {
  ...data, pending_author: {kind:'owner-account', text:'Other Owner', omitted_chars:0,
    is_self:false, at:'2026-09-25T00:00:00Z'}
});
assert.ok(other.textContent.includes('written by project owner Other Owner'));
assert.ok(!other.textContent.includes('your own edit'));
const missing = await requirement(ctx, { pid: 'alpha', rid: data.id }, {}, {...data, pending_author:null});
assert.ok(missing.textContent.includes('author could not be confirmed'));
assert.ok(missing.textContent.includes('Revision time unavailable'));
console.log('Pending author kind, time, escaping, own edit and unknown are shown beside Accept.');
