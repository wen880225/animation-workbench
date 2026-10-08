const fs=require('fs'),vm=require('vm'),assert=require('assert');
const code=fs.readFileSync(__dirname+'/seam_repair.js','utf8');
function setup(mode='ok'){
 const elements={trRepair:{},trRepairStatus:{}};const original={id:'p',clips:[{key:'a',tone:0}],sequence:['a'],route_mode:'loop'};
 const state={project:structuredClone(original),adopted:0,saved:0};let c;
 const bridge={snapshot:()=>({project:structuredClone(state.project)}),belongs:()=>mode!=='stale',busy:()=>{},refresh:()=>c.window.seamRepairUI.update(state.project),message:m=>state.error=m,adopt:p=>{state.project=p;state.adopted++},save:async()=>{state.saved++}};
 c={document:{getElementById:id=>elements[id]},window:{workbenchRepair:bridge},api:async(action)=>{if(action==='version')return {features:{seam_repair:mode==='old'?1:2}};if(mode==='error')throw Error('模型失敗');return {state:'complete',result:{project:{...structuredClone(original),seam_repair:{id:'r',enabled:true,pairs:[{}]}}}}},setTimeout};vm.createContext(c);vm.runInContext(code,c);return {c,state,elements,original};
}
(async()=>{
 for(const mode of ['ok','stale','error','old']){const x=setup(mode);await x.elements.trRepair.onclick();assert.equal(x.state.adopted,mode==='ok'?1:0);assert.equal(x.state.saved,mode==='ok'?1:0);assert.equal(x.elements.trRepair.disabled,false);if(mode!=='ok')assert.ok(x.state.error);
 if(mode==='ok'){assert.match(x.elements.trRepairStatus.textContent,/已套用/);x.state.project=structuredClone(x.original);x.c.window.seamRepairUI.update(x.state.project);assert.doesNotMatch(x.elements.trRepairStatus.textContent,/已套用/,'undo must clear success notice');}}
 const x=setup();await x.elements.trRepair.onclick();x.state.project.clips[0].label='rename';x.c.window.seamRepairUI.changed(x.state.project);assert.ok(x.state.project.seam_repair);x.state.project.clips[0].tone=3;x.c.window.seamRepairUI.changed(x.state.project);assert.equal(x.state.project.seam_repair,undefined);
 const stable=setup();await stable.elements.trRepair.onclick();stable.state.project.seam_repair.stability={clips:{a:{status:'review',reason:'no stable features'}}};stable.c.window.seamRepairUI.update(stable.state.project);assert.match(stable.elements.trRepairStatus.textContent,/比例待檢查/);
 const src=fs.readFileSync(__dirname+'/transitions.js','utf8');const body=src.slice(src.indexOf(' function assertFeatureRoundTrip('),src.indexOf(' function contourPixels('));const c={};vm.createContext(c);vm.runInContext(body,c);assert.throws(()=>c.assertFeatureRoundTrip({seam_repair:{id:'r'}},{}),/修復影格/);
 console.log('PASS: one-click adopt/save; stale, failed and old-server results never adopted; undo clears applied notice; visual edits invalidate cached repairs; rename preserves repair; save round-trip cannot discard repair');
})().catch(e=>{console.error(e);process.exitCode=1});
