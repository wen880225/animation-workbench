// Synthetic-only command ownership tests. Deferred API/image promises exercise real UI handlers.
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const source=fs.readFileSync(__dirname+'/transitions.js','utf8');
const take=(start,end)=>{const a=source.indexOf(start),b=source.indexOf(end,a);assert.ok(a>=0&&b>a,`missing source block ${start}`);return source.slice(a,b)};
const deferred=()=>{let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no});return{promise,resolve,reject}};
const tick=()=>new Promise(resolve=>setImmediate(resolve));
function setup(){
 const fields=new Map(),timers=new Map(),calls=[],images=[],draws=[],messages=[];let timerId=0;
 const context={finishInspection:null,finishPlan:null,renderCandidateControls(){},project:{id:'synthetic',clips:[{key:'a',label:'A',start:1,end:8},{key:'b',label:'B',start:1,end:8},{key:'c',label:'C',start:1,end:8}]},aKey:'a',bKey:'b',mode:'check',loadingProject:false,revision:0,loadEpoch:1,comparison:{left:{old:true},valid:false},pairResults:new Map(),compareEpoch:0,compareTimer:null,compareState:'empty',compareFailure:'',previewFrames:[],previewIndex:0,previewTimer:null,previewRunning:false,previewPreparing:false,previewEpoch:0,previewOwner:null,
  clone:v=>JSON.parse(JSON.stringify(v)),inView:()=>true,sequencePairs:()=>[{a:'a',b:'b'}],pairKey:(a,b)=>a+'>'+b,
  el(id){if(!fields.has(id))fields.set(id,{value:id==='PreviewSpeed'?'0.1':id==='Span'?'6':'',disabled:false,dataset:{},textContent:''});return fields.get(id)},
  call(action,data){const task=deferred();calls.push({action,data,...task});return task.promise},loadImage(url){const task=deferred();images.push({url,...task});return task.promise},
  setTimeout(fn,delay){const id=++timerId;timers.set(id,{fn,delay});return id},clearTimeout(id){timers.delete(id)},
  clearFinishInspection(){},renderCompareStatus(){},renderReview(){},renderMatrix(){},renderAlignment(){},renderProjectState(){},metricText:()=>'',drawComparison:image=>draws.push(image||'comparison'),message:(text,error)=>messages.push({text,error}),pauseOutput(){},regionEditor:null,rememberDraft(){},renderPairs(){},
 };
 vm.createContext(context);vm.runInContext(take(' function queueCompare(',' function drawComparison('),context);vm.runInContext(take(' function stopPreview(',' function renderVersions('),context);
 vm.runInContext(take(' function selectPair(',' function setMode('),context);
 async function finishPreview(call){call.resolve({frames:[{image:'synthetic-frame',label:'synthetic A tail',duration_ms:100}]});await tick();const image=images.at(-1);assert.equal(image.url,'synthetic-frame');image.resolve({synthetic:true});await tick()}
 return{context,fields,timers,calls,images,draws,messages,finishPreview};
}
// tolerate repository CRLF while keeping source markers explicit
function blocksAvailable(){return source.includes(' function renderVersions(')}
(async()=>{
 assert.ok(blocksAvailable());
 {
  const h=setup(),s=h.context;s.queueCompare();const p=s.preparePreview();
  assert.equal([...h.timers.values()].filter(t=>t.delay===400).length,0,'slow play must cancel the scheduled static comparison');
  await h.finishPreview(h.calls[0]);await p;assert.equal(s.previewRunning,true);assert.equal(s.el('StopPreview').disabled,false);
 }
 {
  const h=setup(),s=h.context;const comparing=s.compare(),old=h.calls[0];const playing=s.preparePreview();await h.finishPreview(h.calls[1]);await playing;
  old.resolve({left:'late-left',right:'late-right'});await comparing;
  assert.equal(s.previewRunning,true,'late static response must not interrupt playback');assert.ok(!h.images.some(i=>i.url==='late-left'),'superseded compare must not decode or publish');
 }
 {
  const h=setup(),s=h.context;const comparing=s.compare();h.calls[0].resolve({left:'left',right:'right'});await tick();const oldImages=[...h.images];const playing=s.preparePreview();await h.finishPreview(h.calls[1]);await playing;oldImages.forEach(i=>i.resolve({stale:true}));await comparing;
  assert.equal(s.comparison.valid,false,'late decoded comparison must not replace the current playback intent');assert.equal(s.previewRunning,true);
 }
 for(const action of ['stop','pair','edit','project']){
  const h=setup(),s=h.context,pending=s.preparePreview();assert.equal(s.el('StopPreview').disabled,false,'pending preparation must be stoppable');
  if(action==='stop')s.stopPreview();if(action==='pair')s.selectPair('b','c');if(action==='edit'){s.revision++;s.clearPreview()}if(action==='project'){s.project={id:'other',clips:[]};s.clearPreview()}
  h.calls[0].resolve({frames:[{image:'stale',duration_ms:100}]});await pending;assert.equal(s.previewRunning,false,action+' must invalidate pending play');assert.equal(h.images.length,0);
 }
 {
  const h=setup(),s=h.context,first=s.preparePreview(),second=s.preparePreview();await h.finishPreview(h.calls[1]);await second;h.calls[0].reject(Error('stale API failure'));await first;
  assert.equal(s.previewRunning,true);assert.ok(!h.messages.some(m=>m.text==='stale API failure'),'old command failures cannot replace current status');
 }
 {
  const h=setup(),s=h.context,p=s.preparePreview();h.calls[0].reject(Error('synthetic unavailable'));await p;assert.equal(s.previewRunning,false);assert.equal(s.el('StopPreview').disabled,true);assert.ok(h.messages.some(m=>m.error&&/synthetic unavailable/.test(m.text)));
 }
 {
  const h=setup(),s=h.context,p=s.preparePreview();s.project.clips[0].head={dx:0,dy:0};await h.finishPreview(h.calls[0]);await p;assert.equal(s.previewRunning,true,'same-revision canonical save during decode does not cancel playback');assert.equal(s.previewOwner,s.previewIdentity());
 }
 console.log('PASS: pending timer, in-flight response/decode, stop, pair/edit/project invalidation, newest play wins and visible preparation failures');
 {
  const fields=new Map(),identity=()=>({dx:0,dy:0,sx:100,sy:100,angle:0,cx:50,cy:50}),plain=v=>JSON.parse(JSON.stringify(v)),clip={key:'a',label:'A',job_id:'synthetic-a',version:'original',start:1,end:8,head_frames:2,tail_frames:2,tone:0,contrast:0,head:identity(),tail:identity()},project={id:'p',name:'Synthetic',clips:[clip],sequence:['a'],shared_tone:{tone:0,contrast:0},reviews:{},updated:1};
  const s={project,clone:plain,identity,routeKeys:(clips,sequence)=>sequence||clips.map(c=>c.key),el(id){if(!fields.has(id))fields.set(id,{textContent:'',dataset:{},value:''});return fields.get(id)},outputKind:'export',outputReadyVersion:'v001',outputClips:[{frames:8,dimensions:[8,16],preview_dimensions:[8,16]}],outputVersionMeta:null,exportSubmission:null,versions:[],completeVersions(){return s.versions},versionName:v=>v.name||v.version};
  vm.createContext(s);vm.runInContext(take(' function normalizeProject(',' async function loadProject('),s);vm.runInContext(take(' function outputRecipeIdentity(',' async function preloadOutput('),s);vm.runInContext(take(' function exportCompletedText(',' function applyExportStatus('),s);
  const exported={name:'v001',project_snapshot:plain(project),source_freshness:{state:'current'}};s.outputVersionMeta=exported;s.versions=[exported];
  assert.equal(s.outputDraftState(exported).state,'current');project.updated=99;project.reviews['a>a']={verdict:'pass'};assert.equal(s.outputDraftState(exported).state,'current','review and save timestamps do not make rendered output stale');
  project.shared_tone.tone=4;s.renderLoadedOutputState();assert.equal(s.el('OutputInfo').dataset.state,'draft-changed');assert.match(s.el('OutputInfo').textContent,/已載入成品 v001（固定版本）.*草稿.*不同/);assert.match(s.exportCompletedText({version:'v001'}),/目前草稿另有變更/);
  project.shared_tone.tone=0;exported.source_freshness.state='stale';assert.equal(s.outputDraftState(exported).state,'source-changed');exported.source_freshness.state='unknown';assert.equal(s.outputDraftState(exported).state,'unknown');assert.equal(s.outputDraftState({name:'legacy'}).state,'unknown');
  s.outputVersionMeta=exported;s.renderLoadedOutputState();assert.equal(s.el('OutputInfo').dataset.warning,'true');
  const snapshot=plain(project);s.exportSubmission={projectId:'p',version:'v002',snapshot};project.clips[0].tail.dx=3;assert.match(s.exportCompletedText({version:'v002'}),/目前草稿另有變更/);
 }
 {
  const fields=new Map(),plain=v=>JSON.parse(JSON.stringify(v)),s={project:{id:'p',clips:[{key:'a'},{key:'b'}],shared_tone:{tone:0,contrast:0}},revision:2,loadEpoch:1,operation:false,toneBusy:false,seamBusy:false,exportStatus:{},exportSubmission:null,clone:plain,el(id){if(!fields.has(id))fields.set(id,{});return fields.get(id)},renderProjectState(){},saveProject:async()=>plain(s.project),message(text){s.notice=text},applyExportStatus(){},pollStatus(){s.polled=true},call(action,data){s.submitted=data.project;s.task=deferred();return s.task.promise}};
  vm.createContext(s);vm.runInContext(take(" el('Export').onclick="," window.transitionsShow="),s);
  const pending=s.el('Export').onclick();await tick();s.project.shared_tone.tone=9;s.revision++;s.task.resolve({version:'v002',state:'running'});await pending;
  assert.equal(s.submitted.shared_tone.tone,0,'output submission is a fixed snapshot');assert.equal(s.exportSubmission.snapshot.shared_tone.tone,0);assert.equal(s.project.shared_tone.tone,9,'new draft edits survive export');assert.match(s.notice,/不包含在這次成品/);assert.equal(s.operation,false);
 }
 {
  const fields=new Map(),s={project:{id:'p'},loadEpoch:1,versions:[],exportStatus:{state:'complete',version:'v002'},outputReadyVersion:'v001',outputVersionMeta:null,outputRunning:true,outputLoading:false,el(id){if(!fields.has(id))fields.set(id,{value:'v001'});return fields.get(id)},call:async()=>({versions:[{name:'v002'},{name:'v001'}]}),completeVersions(){return s.versions},versionName:v=>v.name,renderVersions(){},releaseOutput(){s.outputReadyVersion='';s.outputRunning=false;s.outputLoading=false;s.released=true},exportCompletedText:()=> 'v002 completed'};
  vm.createContext(s);vm.runInContext(take(' async function reloadVersions(',' async function pollStatus('),s);await s.reloadVersions('p','v002');assert.equal(s.el('Version').value,'v002');assert.equal(s.released,true);assert.equal(s.outputRunning,false);assert.match(s.el('OutputInfo').textContent,/新成品 v002/);
  s.el('Version').value='v001';s.outputReadyVersion='';s.outputLoading=true;s.released=false;await s.reloadVersions('p','v002');assert.equal(s.released,true,'new completed version also cancels pending decode of the previously selected version');
 }
 const css=fs.readFileSync(__dirname+'/transitions.css','utf8');assert.doesNotMatch(css,/\[data-step="output"\]\s+#trMessage\s*\{\s*display:none/,'output stage must not hide errors');assert.match(css,/#trMessage\[data-error="true"\]\{display:block/);
 console.log('PASS: immutable loaded version, draft/source/legacy freshness, review-only changes, draft edits during export, new version selection and visible output errors');
})().catch(error=>{console.error(error);process.exitCode=1});
