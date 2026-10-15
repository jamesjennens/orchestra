import assert from 'node:assert/strict';
const [shim, moduleUrl] = process.argv.slice(2);
await import(shim);
const { checkpointForm, commentForm } = await import(moduleUrl);
const items = Array.from({ length: 35 }, (_,i) => ({id:'item-'+i,kind:'dependency',text:'remaining '+i,source:'native:source-'+i}));
const source = {checkpoint:{id:'cp-old'}, checkpoint_template:{carried_open_items:35,open_item:{kind:['dependency','blocker']},body:{schema_version:1,previous:'cp-old',activity_cursor:{events:'exact-cursor'},intent:'preserved intent',acceptance:'preserved acceptance',summary:'',next_action:'',source_commit:'a'.repeat(40),branch:'feature',open_items:items,resolved:[]}}};
const copy = v => JSON.parse(JSON.stringify(v));
function setup(reply = async () => ({ok:true}), initial = source) {
 const seen={sends:[],renders:0};
 const ctx={api:{checkpoint:async(p,t,b)=>{seen.sends.push(copy(b));return reply();},brief:async()=>copy(source)},render(){seen.renders++;},setDirty(){}};
 const section=checkpointForm(ctx,'demo','demo-1',copy(initial));
 const form=section.querySelector('form');
 form.querySelector('#cp-summary').value='own summary'; form.querySelector('#cp-next_action').value='next action';
 return {seen,ctx,section,form};
}
let tested=0;
{
 const {seen,form}=setup();assert.equal(seen.sends.length,0);
 form.querySelector('#cp-lane').value='waiting-on-review';form.querySelector('#cp-waiting').value='reviewer';
 await form.dispatch('submit');const b=seen.sends[0];
 assert.deepEqual(b.open_items,items);assert.deepEqual(b.activity_cursor,source.checkpoint_template.body.activity_cursor);
 assert.equal(b.previous,'cp-old');assert.deepEqual(b.resolved,[]);assert.equal(b.intent,'preserved intent');assert.equal(b.acceptance,'preserved acceptance');
 assert.deepEqual(b.lane_state,{schema_version:1,task:'demo-1',state:'waiting-on-review',waiting_on:'reviewer'});assert.equal(seen.renders,1);tested++;
}
{
 const {seen,form}=setup();form.querySelector('#carry-3').value='resolve';form.querySelector('#why-3').value='done explicitly';form.querySelector('#proof-3').value='test:proof';await form.dispatch('submit');
 assert.equal(seen.sends[0].open_items.length,34);assert.deepEqual(seen.sends[0].resolved,[{id:'item-3',reason:'done explicitly',evidence:'test:proof'}]);assert.deepEqual(items[3],source.checkpoint_template.body.open_items[3]);tested++;
}
{
 const {seen,form}=setup(async()=>{throw Object.assign(new Error('stale context'),{status:409});});await form.dispatch('submit');assert.equal(seen.renders,0);assert.equal(form.querySelector('#cp-summary').value,'own summary');assert.match(form.textContent,/reconcile/);tested++;
}
{
 const missing=copy(source);missing.checkpoint_template.carried_open_items=null;missing.checkpoint_template.body.open_items=[];
 const {seen,form}=setup(undefined,missing);assert.equal(form.querySelector('button[type=submit]').disabled,true);await form.dispatch('submit');assert.equal(seen.sends.length,0);tested++;
}
{
 const {seen,ctx,form}=setup();ctx.api.brief=async()=>{throw new Error('read failed');};
 const refresh=form.all(e=>e.tagName==='BUTTON'&&e.textContent==='Refresh checkpoint context')[0];await refresh.dispatch('click');assert.equal(form.querySelector('button[type=submit]').disabled,true);await form.dispatch('submit');assert.equal(seen.sends.length,0);assert.equal(form.querySelector('#cp-summary').value,'own summary');tested++;
}
{
 const seen={sends:[],renders:0};const ctx={api:{comment:async(p,t,text)=>{seen.sends.push(text);throw Object.assign(new Error('unknown outcome'),{status:503});}},render(){seen.renders++;},setDirty(){}};
 const form=commentForm(ctx,'demo','demo-1').querySelector('form');const text='--help\n<img onerror=alert(1)>';form.querySelector('#comment-text').value=text;assert.equal(seen.sends.length,0);await form.dispatch('submit');assert.deepEqual(seen.sends,[text]);assert.equal(form.querySelector('#comment-text').value,text);assert.equal(seen.renders,0);tested++;
}
console.log(JSON.stringify({tested,success:true,native:false,realHttp:false}));
