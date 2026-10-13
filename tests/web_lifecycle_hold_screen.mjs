import assert from 'node:assert/strict';
const [shim, apiUrl, viewUrl, base, pid, token, tid, raw] = process.argv.slice(2);
await import(shim);
const {createApi} = await import(apiUrl);
const {lifecyclePanel} = await import(viewUrl);
const expected = JSON.parse(raw);
let pending = 0, posts = 0;
const transport = async (method, path, headers, body) => {
  pending++;
  try {
    if (method === 'POST') posts++;
    const response = await fetch(base+path, {method, headers:{...headers, Authorization:'Bearer '+token}, body});
    return {status:response.status, data:await response.json()};
  } finally {pending--;}
};
const panel = lifecyclePanel({api:createApi(transport), href:x=>x}, {id:pid}, {id:tid});
document.body.append(panel);
for (let i=0;i<200;i++) {
  await new Promise(r=>setTimeout(r,20));
  if (!pending && panel.textContent.includes('Lifecycle facts')) break;
}
const dimension = panel.querySelectorAll('select').find(x=>x.getAttribute('aria-label')==='Lifecycle dimension');
const record = panel.querySelectorAll('button').find(x=>x.textContent==='Record fact');
for (const value of ['reviewed','integrated']) {
  dimension.value = value;await dimension.dispatch('change');
  assert.equal(record.disabled, true);
  assert(panel.textContent.includes(expected[value]));
}
assert.equal(posts, 0);
console.log(JSON.stringify({completeNativeSentences:true, disabled:true, posts}));
