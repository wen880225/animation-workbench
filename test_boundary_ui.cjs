// Exercise navigation through public buttons/events; no browser, files or real media are opened.
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const source=fs.readFileSync(__dirname+'/boundary_workspace.js','utf8');
const html=fs.readFileSync(__dirname+'/ui.html','utf8');
const nav=html.match(/<div class="work-tabs"[\s\S]*?<\/div>/)[0];
const visible=[...nav.matchAll(/<button\b([^>]*)>([^<]*)<\/button>/g)].filter(([,attrs])=>!/(?:^|\s)hidden(?:\s|$)/.test(attrs));
assert.deepEqual(visible.map(([,attrs,text])=>[attrs.match(/id="([^"]+)"/)[1],text]),[['tabMaterials','素材檢查'],['tabTransitions','接點修整']]);
for(const id of ['tabPlayback','tabFrames','tabEdit'])assert.match(nav,new RegExp('id="'+id+'"[^>]* hidden'));
assert.match(html,/<script src="\/boundary_workspace\.js[^>]+><\/script>/);

function harness(job=null){
 const nodes=new Map(),events=new Map(),domEvents=new Map(),calls=[],notices=[],opened=[];
 class Node{constructor(id){Object.assign(this,{id,hidden:false,disabled:false,open:false,dataset:{},attributes:{},tabIndex:0,textContent:''})}setAttribute(key,value){this.attributes[key]=value}getAttribute(key){return this.attributes[key]}focus(){nodes.focused=this.id}click(){return this.disabled?undefined:this.onclick?.()}}
 const $=id=>{if(!nodes.has(id))nodes.set(id,new Node(id));return nodes.get(id)};
 for(const id of ['loopEditor','transitionPanel','framesPanel'])$(id).hidden=true;
 const window={addEventListener(type,fn){if(!events.has(type))events.set(type,[]);events.get(type).push(fn)},dispatchEvent(event){for(const fn of events.get(event.type)||[])fn(event)},notice:text=>notices.push(text),boundaryOpenSource:async value=>{opened.push(value);return{}},workbenchSelectTab(id){calls.push(id);for(const[key,panel]of [['tabPlayback','playbackPanel'],['tabFrames','framesPanel'],['tabEdit','loopEditor'],['tabTransitions','transitionPanel']]){$(key).setAttribute('aria-selected',String(key===id));$(panel).hidden=key!==id}window.dispatchEvent({type:'workbench:tab',detail:{id}})}};
 $('tabTransitions').onclick=()=>window.workbenchSelectTab('tabTransitions');
 const document={body:$('body'),getElementById:$,addEventListener(type,fn){domEvents.set(type,fn)}};
 vm.runInNewContext(source,{window,document,current:()=>job},{filename:'boundary_workspace.js'});
 return{$,window,document,calls,notices,opened,domEvents};
}
const complete={id:'robot',state:'complete',frames:[{status:'done'},{status:'done'}]};
(async()=>{
 const h=harness(complete);
 assert.equal(h.$('boundaryStartCurrent').disabled,false);
 await h.$('boundaryStartCurrent').click();assert.equal(h.opened.length,1);assert.equal(h.opened[0].jobId,'robot');assert.equal(h.opened[0].version,'original');assert.equal(h.calls.at(-1),'tabTransitions');assert.equal(h.$('qaModes').hidden,true);
 h.$('tabMaterials').click();h.$('qaFrames').click();assert.equal(h.calls.at(-1),'tabFrames');assert.equal(h.$('tabMaterials').getAttribute('aria-selected'),'true');assert.equal(h.$('qaFrames').getAttribute('aria-pressed'),'true');h.$('tabTransitions').click();h.$('tabMaterials').click();assert.equal(h.calls.at(-1),'tabFrames','return to source QA retains the chosen inspection view');
 let prevented=0;h.$('tabMaterials').onkeydown({key:'ArrowRight',preventDefault(){prevented++}});assert.equal(h.calls.at(-1),'tabTransitions');assert.equal(h.$('body').dataset.boundaryMode,'seams');h.$('tabTransitions').onkeydown({key:'ArrowRight',preventDefault(){prevented++}});assert.equal(h.calls.at(-1),'tabFrames');assert.equal(prevented,2);assert.equal(h.$('body').dataset.boundaryMode,'materials');
 const before=h.opened.length;h.$('openLegacySingle').click();assert.equal(h.calls.at(-1),'tabEdit');assert.equal(h.$('body').dataset.boundaryMode,'legacy');assert.equal(h.opened.length,before,'compatibility entry must not create or convert a project');
 h.domEvents.get('DOMContentLoaded')();assert.equal(h.calls.at(-1),'tabTransitions','old stored tab does not silently restore the duplicate normal workflow');assert.equal(h.opened.length,before);
 const empty=harness();assert.equal(empty.$('boundaryStartCurrent').disabled,true);assert.equal(empty.$('openLegacySingle').disabled,true);assert.equal(await empty.window.boundaryOpenCurrent(),false);assert.equal(empty.opened.length,0);assert.match(empty.notices.at(-1),/完成去背/);
 empty.window.dispatchEvent({type:'workbench:job',detail:{...complete,state:'running'}});assert.equal(empty.$('boundaryStartCurrent').disabled,true);empty.window.dispatchEvent({type:'workbench:job',detail:complete});assert.equal(empty.$('boundaryStartCurrent').disabled,false);
 let release;h.window.boundaryOpenSource=()=>new Promise(resolve=>release=resolve);h.$('tabMaterials').click();const pending=h.$('boundaryStartCurrent').click();assert.equal(h.$('boundaryStartCurrent').disabled,true);assert.equal(await h.window.boundaryOpenCurrent(),false);release({});await pending;assert.equal(h.$('boundaryStartCurrent').disabled,false);
 h.window.boundaryOpenSource=async()=>{throw Error('synthetic unavailable')};assert.equal(await h.window.boundaryOpenCurrent(),false);assert.equal(h.$('boundaryStartCurrent').disabled,false);assert.match(h.notices.at(-1),/synthetic unavailable/);
 console.log('PASS: two visible workspaces, keyboard routes, source readiness, one-clip handoff, legacy isolation, duplicate action and recoverable errors');
})().catch(error=>{console.error(error);process.exitCode=1});
