// Actual HTTP + DOM controls. Output contains observations, never credentials.
const [shimUrl, apiUrl, agentsUrl, projectUrl, base, pid, aid, cid, ownerId, ownerToken, adminId, adminToken] = process.argv.slice(2);
const { Element, nodeList } = await import(shimUrl);
const originalQueryAll = Element.prototype.querySelectorAll;
Element.prototype.querySelectorAll = function (selector) {
  if (selector === 'input[name=project]:checked') return nodeList(this.all((e) => e.tagName === 'INPUT' && e.attributes.name === 'project' && e.checked));
  return originalQueryAll.call(this, selector);
};
Element.prototype.showModal = function () { this.open = true; };
Element.prototype.close = function () { this.open = false; };
Element.prototype.reset = function () { for (const e of this.all((e) => ['INPUT','TEXTAREA'].includes(e.tagName))) e.value = ''; };
globalThis.window = { isSecureContext: false };
globalThis.location = { origin: base };
const { createApi } = await import(apiUrl);
const agents = await import(agentsUrl);
const project = await import(projectUrl);
let flight = 0; let drop = true; const sent = [];
function ctx(id, token, superuser) {
  const api = createApi(async (method,path,headers,body) => {
    flight += 1;
    try {
      const response = await fetch(base+path,{method,headers:{...headers,Authorization:'Bearer '+token},body});
      const text = await response.text(); const data = text ? JSON.parse(text) : null;
      if (method !== 'GET') sent.push({method,path,key:headers['Idempotency-Key'],body:body ? JSON.parse(body) : null,status:response.status});
      if (path.endsWith('/extend') && drop && response.status === 200) { drop = false; return {status:0,data:{error:{code:'network',message:'Synthetic lost response; retry this same intent.'}}}; }
      return {status:response.status,data};
    } finally { flight -= 1; }
  });
  return { api, me:{id,superuser}, projects:[{id:pid,name:'Synthetic project',role:superuser?'superuser':'contributor'}],
    href:(p)=>'#'+p, go(){},query:{},render(){},refreshProjects:async()=>{} };
}
const own = ctx(ownerId,ownerToken,false); const root = ctx(adminId,adminToken,true);
const pause = (ms)=>new Promise((resolve)=>setTimeout(resolve,ms));
async function settle() { let quiet=0;for(let i=0;i<1200&&quiet<6;i+=1){await pause(25);quiet=flight===0?quiet+1:0;} if(quiet<6)throw new Error('HTTP page did not settle'); }
const button=(node,label)=>node.all((e)=>e.tagName==='BUTTON'&&e.textContent===label)[0];
async function confirm(control,answer) {
  if(!control)throw new Error('Expected extension control');
  const task=control.dispatch('click');
  let dialog;
  for(let i=0;i<600&&!dialog;i+=1){dialog=document.body.all((e)=>e.tagName==='DIALOG').at(-1);if(!dialog)await pause(25);}
  if(!dialog)throw new Error('Expected explicit confirmation');
  const text=dialog.textContent;await button(dialog,answer).dispatch('click');await task;await settle();return text;
}
const page=await agents.list(own);await settle();
const block=()=>page.all((e)=>e.attributes['data-agent']===aid)[0];
const control=()=>block().all((e)=>e.attributes['data-extend']===cid)[0];
const out={ownerSelector:page.all((e)=>e.attributes.id==='a-owner').length,warning:page.textContent.includes('within seven days'),expiry:page.textContent.includes('Expires ')};
out.visibleExpiryDates=page.all((e)=>e.attributes['data-expiry']===cid).flatMap((row)=>row.all((e)=>e.tagName==='TIME').map((e)=>e.textContent));
const before=sent.length;await confirm(control(),'Cancel');out.cancelSent=sent.length-before;
out.confirmText=await confirm(control(),'Extend existing credential');
out.retainedAfterLost=Boolean(control());
await confirm(control(),'Extend existing credential');
const mutations=sent.filter((x)=>x.path.endsWith('/extend'));
out.sameRetryKey=mutations.length===2&&mutations[0].key===mutations[1].key;
out.extensionBodies=mutations.map((x)=>x.body);out.extensionStatuses=mutations.map((x)=>x.status);
out.setupDialogsAfterExtension=document.body.all((e)=>e.tagName==='DIALOG').length;
const adminPage=await agents.list(root);await settle();
const selector=adminPage.all((e)=>e.attributes.id==='a-owner')[0];
out.targetAvailable=Boolean(selector&&selector.all((e)=>e.tagName==='OPTION'&&e.value===ownerId).length);
selector.value=ownerId;
adminPage.querySelector('#a-name').value='Created by superuser page';
adminPage.querySelector('#a-tool').value='Synthetic tool';
adminPage.querySelector('#a-dir').value='/synthetic/owned-folder';
const createForm=adminPage.all((e)=>e.tagName==='FORM'&&e.querySelector('#a-name'))[0];
await createForm.dispatch('submit');await settle();
out.creationTargets=sent.filter((x)=>x.path==='/v1/agents').map((x)=>x.body.owner);
for(const dialog of document.body.all((e)=>e.tagName==='DIALOG')){dialog.close();dialog.remove();}
const ownerAfter=await agents.list(own);await settle();
out.creatorDisclosure=ownerAfter.textContent.includes('Created by ')&&ownerAfter.textContent.includes('saw the first secret');
const settings=await project.settings(root,{pid});await settle();
const panel=settings.all((e)=>e.attributes['data-panel']==='project-agents')[0];
out.projectExpiry=panel.textContent.includes('Expires ');
const pc=panel.all((e)=>e.attributes['data-extend']===cid)[0];
await confirm(pc,'Extend existing credential');
out.projectExtension=sent.filter((x)=>x.path.includes('/projects/')&&x.path.endsWith('/extend')).map((x)=>({status:x.status,body:x.body}));
out.projectPrivacy=!panel.textContent.includes('/synthetic/owned-folder');
console.log(JSON.stringify(out));
