import assert from 'node:assert/strict';
const [shimUrl, apiUrl, viewUrl, base, pid, token, tid, mode] = process.argv.slice(2);
await import(shimUrl);
const storage = new Map();
globalThis.sessionStorage = { getItem: k => storage.get(k) ?? null,
  setItem: (k,v) => storage.set(k,String(v)), removeItem: k => storage.delete(k) };
const { createApi } = await import(apiUrl);
const { lifecyclePanel } = await import(viewUrl);
let inFlight=0, lose=true;const posts=[];
const transport=async(method,path,headers,body)=>{
  inFlight++;
  try {
    const response=await fetch(base+path,{method,headers:{...headers,Authorization:'Bearer '+token},body});
    const result={status:response.status,data:await response.json()};
    if(method==='POST')posts.push({path,key:headers['Idempotency-Key'],body});
    if(method==='POST' && path.endsWith('/scope') && lose){lose=false;throw new Error('synthetic lost response after HTTP commit');}
    return result;
  } finally {inFlight--;}
};
async function settle(){for(let i=0;i<200;i++){await new Promise(r=>setTimeout(r,20));if(!inFlight){await new Promise(r=>setTimeout(r,30));if(!inFlight)return;}}throw new Error('Lifecycle page did not settle');}
const project={id:pid,role:'superuser',archived:false},task={id:tid};
const context=api=>({api,href:path=>path});
const byLabel=(panel,label,tag)=>Array.from(panel.querySelectorAll(tag)).find(e=>e.getAttribute('aria-label')===label);
const button=(panel,text)=>Array.from(panel.querySelectorAll('button')).find(e=>e.textContent===text);
let api=createApi(transport),panel=lifecyclePanel(context(api),project,task);
document.body.append(panel);await settle();
for(const [name,value] of Object.entries({source_commit:'a'.repeat(40),integration_commit:'b'.repeat(40),release_id:'browser-proof',environment:'<img src=x onerror=alert(1)>'}))byLabel(panel,name,'input').value=value;
byLabel(panel,'Evidence pointers','textarea').value='<script>literal evidence</script>';
await button(panel,'Set scope').dispatch('click');await settle();
assert.equal(posts.length,1);assert.equal(api.pendingLifecycle(pid,tid).length,1);
assert.equal(JSON.parse(posts[0].body).expected_scope_token,null);
// Reload with the now-changed CAS token: the old request is still available.
api=createApi(transport);panel.remove();panel=lifecyclePanel(context(api),project,task);document.body.append(panel);await settle();
const pending=api.pendingLifecycle(pid,tid);assert.equal(pending.length,1);assert.equal(pending[0].body.expected_scope_token,null);
assert(panel.textContent.includes('Requests to reconcile'));
await button(panel,'Retry saved identical request').dispatch('click');await settle();
assert.equal(posts.length,2);assert.equal(posts[0].key,posts[1].key);assert.equal(posts[0].body,posts[1].body);
assert.equal(api.pendingLifecycle(pid,tid).length,0);
byLabel(panel,'Lifecycle dimension','select').value='tested';await byLabel(panel,'Lifecycle dimension','select').dispatch('change');
byLabel(panel,'Evidence pointers','textarea').value='<svg onload=alert(1)>literal pointer</svg>';
await button(panel,'Record fact').dispatch('click');await settle();
assert(panel.textContent.includes('<svg onload=alert(1)>literal pointer</svg>'));
assert.equal(panel.querySelector('img'),null);assert.equal(panel.querySelector('svg'),null);assert.equal(panel.querySelector('script'),null);
byLabel(panel,'Lifecycle dimension','select').value='integrated';await byLabel(panel,'Lifecycle dimension','select').dispatch('change');
assert.equal(button(panel,'Record fact').disabled,true);
assert(panel.textContent.includes(mode === 'native' ? 'current independent approval' : 'host integration mark writer'));
const state=await api.lifecycle(pid,tid);assert.equal(state.facts.tested.value,'passed');
console.log(JSON.stringify({sameKey:posts[0].key===posts[1].key,sameBody:posts[0].body===posts[1].body,
  persistedRetry:true,escaped:true,integrationDisabled:true,tested:state.facts.tested.value,scopeToken:state.scope_token}));
