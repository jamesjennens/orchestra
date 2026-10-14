import assert from 'node:assert/strict';
const [shim,moduleUrl,apiUrl]=process.argv.slice(2);await import(shim);
const {assignmentForm}=await import(moduleUrl);const {createApi}=await import(apiUrl);
let tested=0;
function formWith(api) {
 const ctx={api,render:async()=>{},setDirty(){}};
 const form=assignmentForm(ctx,'demo','demo-1',{assignee:'old'}).querySelector('form');
 return {form,load:form.all(e=>e.tagName==='BUTTON'&&e.textContent==='Load assignment roster')[0],submit:form.querySelector('button[type=submit]')};
}
{
 const sends=[];let lost=true;
 const api=createApi(async(method,path,headers,body)=>{
  sends.push({method,path,headers,body});
  if(method==='GET')return {status:200,data:{items:[{actor:'new',name:'<img onerror=alert(1)>'}],more:false,next_cursor:null}};
  if(lost){lost=false;return {status:503,data:{error:{code:'uncertain',message:'outcome unknown'}}};}
  return {status:200,data:{changed:true}};
 });
 const {form,load,submit}=formWith(api);assert.equal(submit.disabled,true);await form.dispatch('submit');assert.equal(sends.length,0);
 await load.dispatch('click');assert.equal(submit.disabled,false);form.querySelector('select').value='new';
 const literal='<img onerror=alert(1)>\nFinish this slice';form.querySelector('#reason').value=literal;
 form.querySelector('#allow_pending_review').checked=true;form.querySelector('#allow_held_slot').checked=false;
 await form.dispatch('submit');assert.match(form.textContent,/Reconcile identical assignment/);assert.equal(form.querySelector('#reason').disabled,true);
 form.querySelector('#reason').value='accidental changed input';await form.dispatch('submit');
 const writes=sends.filter(x=>x.method==='POST');assert.equal(writes.length,2);assert.equal(writes[0].headers['Idempotency-Key'],writes[1].headers['Idempotency-Key']);assert.equal(writes[0].body,writes[1].body);
 assert.deepEqual(JSON.parse(writes[0].body),{expected_assignee:'old',assignee:'new',reason:literal,allow_pending_review:true,allow_held_slot:false});tested++;
}
{
 let pages=0;const {form,load,submit}=formWith({assignmentRoster:async()=>++pages===1?{items:[{actor:'old',name:'Old'}],more:true,next_cursor:'next'}:{items:[{actor:'new',name:'New'}],more:false,next_cursor:null}});
 await load.dispatch('click');assert.equal(pages,2);assert.equal(form.querySelector('select').querySelectorAll('option').length,3);assert.equal(submit.disabled,false);tested++;
}
{
 const {load,submit,form}=formWith({assignmentRoster:async()=>({items:[],more:true,next_cursor:null})});await load.dispatch('click');assert.equal(submit.disabled,true);assert.match(form.textContent,/incomplete/);tested++;
}
{
 let sends=0;const {load,submit,form}=formWith({assignmentRoster:async()=>({items:[{actor:'new',name:'New'}],more:false,next_cursor:null}),assignTask:async()=>{sends++;throw Object.assign(new Error('holder changed'),{status:409});}});
 await load.dispatch('click');form.querySelector('#reason').value='preserved reason';await form.dispatch('submit');assert.equal(submit.disabled,true);assert.equal(form.querySelector('#reason').value,'preserved reason');await form.dispatch('submit');assert.equal(sends,1);tested++;
}
console.log(JSON.stringify({tested,success:true,native:false,realHttp:false}));
