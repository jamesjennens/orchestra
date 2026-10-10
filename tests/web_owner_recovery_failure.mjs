// Production API and view against the disposable loopback recovery failure.
import './web_dom_shim.mjs';
import assert from 'node:assert/strict';
const [apiUrl, viewUrl, base, pid, token] = process.argv.slice(2);
const { createApi } = await import(apiUrl);
const view = await import(viewUrl);
const transport = async (method, path, headers, body) => {
  const response = await fetch(base + path, { method,
    headers: { ...headers, Authorization: 'Bearer ' + token }, body });
  return { status: response.status, data: await response.json() };
};
const ctx = { api: createApi(transport), href: p => '#' + p, go() {}, setDirty() {} };
const data = await ctx.api.requirements(pid);
const page = await view.brd(ctx, { pid }, {}, data);
assert.ok(page.textContent.includes('Owner content'));
assert.ok(page.textContent.includes('Failed creations could not be read'));
assert.ok(page.querySelector('#requirement-text'));
assert.ok(page.querySelectorAll('a').some(a => a.attributes.href.includes('/requirements/')));
assert.ok(page.querySelectorAll('button').some(b => b.textContent === 'Use governed requirements'));
assert.ok(!page.textContent.includes('private-server-path-sentinel'));
console.log(JSON.stringify({ documentStillUsable: true }));
