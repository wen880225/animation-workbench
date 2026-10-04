(()=>{
const ids={start:'Start',end:'End',ramp:'Ramp',dx:'Dx',dy:'Dy',sx:'Sx',sy:'Sy',angle:'Angle',cx:'Cx',cy:'Cy',protect:'Protect',px:'Px',py:'Py',radius:'Radius',tone:'Tone',contrast:'Contrast'};
const draftPrefix='animation-workbench:draft:v1:',drafts=new Map(),references=new Map();
const purposes=['tone','loop','reference'];
let job=null,history=[],committed=null,images=[],seam=[],seamIndex=0,playing=false,timer=null,epoch=0,working=false,exporting=false,polling=false;
let previewTimer=null,previewPending=false,previewDirty=true,reference=null,referenceName='',draftWarning='',lastSingle=null,pickTarget=null,scaleRatio=1,pendingBounds=null;
let regionValue,regionEditor=null,closureValue;
let alignmentValue,alignmentReport=null,alignmentBusy=false,alignmentEpoch=0,alignmentNotice='',alignmentError=false,alignmentStrengthSession=false;
let shownPreview=null,previewState='pending',previewFailure='',exportChecking=false,jobSerial=0,seamSnapshot=null;
let outputCheck=null,outputEpoch=0;
const workWaiters=[];
const clone=value=>value==null?undefined:JSON.parse(JSON.stringify(value));
const el=k=>$('le'+k),message=s=>{el('Message').textContent=s};
const clamp=(v,min,max,fallback=min)=>v!==null&&v!==undefined&&v!==''&&Number.isFinite(Number(v))?Math.min(max,Math.max(min,Number(v))):fallback;
function purpose(){return purposes.includes(el('Purpose')?.value)?el('Purpose').value:'tone'}
function values(){const r=Object.fromEntries(Object.entries(ids).map(([k,v])=>[k,k==='protect'?el(v).checked:Number(el(v).value)]));if(regionValue)r.region=RegionEditor.normalize(regionValue);if(alignmentValue)r.alignment=clone(alignmentValue);if(closureValue)r.closure=clone(closureValue);return r}
function closureDefaults(r){const count=Math.max(2,Math.min(12,Math.floor((r.end-r.start+1)/2)));return {enabled:false,reference:r.start,head_frames:count,tail_frames:count}}
function cleanClosure(r,value,changed=''){
 const out=closureDefaults(r),n=r.end-r.start+1;if(!value||typeof value!=='object'||Array.isArray(value))return out;
 out.enabled=value.enabled===true;out.reference=Math.round(clamp(value.reference,r.start,Math.max(r.start,r.end),r.start));
 out.head_frames=Math.round(clamp(value.head_frames,2,Math.max(2,n-2),out.head_frames));out.tail_frames=Math.round(clamp(value.tail_frames,2,Math.max(2,n-2),out.tail_frames));
 if(n>=4&&out.head_frames+out.tail_frames>n){if(changed==='tail_frames')out.head_frames=n-out.tail_frames;else out.tail_frames=n-out.head_frames}return out;
}
function closureRangeReason(r=values()){
 if(!job||!Number.isInteger(r.start)||!Number.isInteger(r.end)||r.start<1||r.end>job.frames.length||r.end-r.start+1<4)return '首尾共同基準需要至少 4 幀的有效循環範圍。';
 if(job.frames.slice(r.start-1,r.end).some(f=>f.status!=='done'))return '請先完成循環範圍內所有幀的去背，再啟用首尾共同基準。';return '';
}
function assertClosureReady(){if(!closureValue?.enabled)return;const reason=closureRangeReason();if(reason)throw Error(reason+' 請調整範圍或停用共同基準。')}
function renderClosure(){
 if(!el('ClosureEnabled'))return;const r=values(),c=closureValue||closureDefaults(r),reason=closureRangeReason(r),locked=!job||job.state==='running'||exporting;
 el('ClosureEnabled').checked=c.enabled;el('ClosureEnabled').disabled=locked||(!c.enabled&&!!reason);
 for(const[suffix,key]of [['ClosureReference','reference'],['ClosureHead','head_frames'],['ClosureTail','tail_frames']]){const node=el(suffix);node.disabled=locked||!c.enabled||!!reason;node.min=key==='reference'?r.start:2;node.max=key==='reference'?r.end:Math.max(2,r.end-r.start-1);if(document.activeElement!==node)node.value=c[key]}
 el('ClosureStatus').textContent=reason||(!c.enabled?'尚未啟用；既有方案與輸出不會改變。':`共同基準：第 ${c.reference} 幀 · 頭段 ${r.start}–${r.start+c.head_frames-1}，尾段 ${r.end-c.tail_frames+1}–${r.end}。`);
 el('ClosureStatus').dataset.error=String(c.enabled&&!!reason);el('ClosureCard').dataset.active=String(c.enabled&&!reason);
}
function previewRequest(){const r=values(),p=purpose(),b=Number(el('B').value),request={id:job?.id,serial:jobSerial,recipe:r,purpose:p,a:p==='tone'?b:Number(el('A').value),b,original:el('Original').checked,reference:p==='reference'?reference?.url||referenceName:''};request.signature=JSON.stringify(request);return request}
function freshPreview(request=previewRequest()){return images.length===2&&shownPreview?.signature===request.signature&&previewState==='fresh'}
function renderPreviewState(single=null){
 const shown=single?seamSnapshot:shownPreview,hasImage=single||images.length===2,current=previewRequest(),stale=!!hasImage&&(!shown||shown.signature!==current.signature||(!single&&previewState!=='fresh'));
 const label=el('PreviewSnapshot'),badge=el('PreviewBadge');
 if(label){const names={tone:'調整前後',loop:'首尾銜接',reference:'參考圖與目前幀'},r=shown?.recipe,alignment=r?.alignment,correction=shown?.original?'暫看原始，未套用修整':alignment?.enabled?'局部對位設定 '+alignment.strength+'%（依幀漸進）':'手動修整';label.textContent=shown&&hasImage?'畫面來源：'+(shown.original?'原始去背':'修整草稿（不是已輸出版本）')+' · '+(single?'接縫播放 · 第 '+single.frame+' 幀':(names[shown.purpose]||shown.purpose)+' · A '+(shown.purpose==='reference'?'參考圖':('第 '+shown.aFrame+' 幀'))+' ／ B 第 '+shown.bFrame+' 幀')+' · '+correction+(!shown.original&&r?.closure?.enabled?' · 首尾共同基準 第 '+r.closure.reference+' 幀':''):'畫面來源：尚無成功預覽'}
 if(badge){badge.hidden=!stale;badge.textContent='舊預覽，尚未套用目前設定'+(previewState==='failed'?' · 更新失敗，請重試':previewState==='loading'?' · 正在載入':' · 等待更新');badge.dataset.state=previewState}
 const hint=el('ModeHint');if(hint)hint.textContent=el('Mode').value==='onion'?'半透明疊圖是兩張圖的混合顯示，不是單張輸出；輪廓檢查請用「輪廓差異」或「拖曳比對」。':el('Mode').value==='contour'?'紅色：A 多出的輪廓；青色：B 多出的輪廓；共同區域為淡灰。僅比較透明輪廓（差異 ×4），不代表內部線條一致。':el('Mode').value==='diff'?'RGB 差異放大 4 倍；黑色表示相同，亮色表示差異。':'左側 A、右側 B；拖曳分隔線查看各自的實際畫面。';
}
function waitForWork(){return working?new Promise(resolve=>workWaiters.push(resolve)):Promise.resolve()}
function defaults(j){const n=Math.max(1,j.frames.length);return {start:1,end:n,ramp:Math.max(1,n-11),dx:0,dy:0,sx:100,sy:100,angle:0,cx:50,cy:50,protect:false,px:50,py:35,radius:15,tone:0,contrast:0}}
function cleanRecipe(j,value){
 const out=defaults(j),n=Math.max(1,j.frames.length),limits={dx:[-100,100],dy:[-100,100],sx:[80,120],sy:[80,120],angle:[-10,10],cx:[0,100],cy:[0,100],px:[0,100],py:[0,100],radius:[1,60],tone:[-100,100],contrast:[-50,50]};
 if(!value||typeof value!=='object'||Array.isArray(value))return out;
 for(const[k,[low,high]]of Object.entries(limits))if(typeof value[k]==='number'&&Number.isFinite(value[k]))out[k]=clamp(value[k],low,high,out[k]);
 for(const k of ['start','end','ramp'])if(Number.isInteger(value[k]))out[k]=clamp(value[k],1,n,out[k]);
 if(out.start>=out.end){out.start=1;out.end=n}
 out.ramp=clamp(out.ramp,out.start,Math.max(out.start,out.end-1),out.start);out.protect=value.protect===true;if(value.region&&window.RegionEditor)out.region=RegionEditor.normalize(value.region);if(value.alignment?.model&&typeof value.alignment.model==='object')out.alignment={enabled:value.alignment.enabled===true,strength:clamp(value.alignment.strength,0,100,100),model:clone(value.alignment.model)};if(value.closure&&typeof value.closure==='object'&&!Array.isArray(value.closure))out.closure=cleanClosure(out,value.closure);return out;
}
function alignmentReason(){if(!alignmentValue?.model)return '';const p=alignmentValue.model.provenance;if(!p?.source||!p.reference)return '此對位缺少來源資訊，請重新計算。';if(!job||p.scope!=='loop'||p.source.job_id!==job.id||p.source.frame!==Number(el('End').value))return '來源尾幀已變更，請重新對齊參考幀。';if(p.reference.job_id!==job.id||p.reference.frame<1||p.reference.frame>job.frames.length)return '計算時的參考來源已變更，請重新對齊。';return ''}
function alignmentActive(){return !!alignmentValue?.enabled&&!alignmentReason()}
function assertAlignmentReady(){const reason=alignmentReason();if(alignmentValue?.enabled&&reason)throw Error(reason+' 請重新計算，或清除對位後再儲存／輸出。')}
function alignmentReportText(report){
 if(!report)return '';const out=[],before=report.before||{},after=report.after||{};
 const distance=(a,b)=>{if(!Array.isArray(a)||!Array.isArray(b)||!a.length||a.length!==b.length)return null;let max=0;for(let i=0;i<a.length;i++){if(!Array.isArray(a[i])||!Array.isArray(b[i])||a[i].length!==2||b[i].length!==2)return null;for(let j=0;j<2;j++){if(!Number.isFinite(a[i][j])||!Number.isFinite(b[i][j]))return null;max=Math.max(max,Math.abs(a[i][j]-b[i][j]))}}return max};
 for(const[key,label,unit]of [['silhouette_mismatch_pixels','輪廓差異','像素'],['linework_symmetric_distance_px','線條距離','px']])if(Number.isFinite(before[key])&&Number.isFinite(after[key]))out.push(label+'：'+before[key].toFixed(key==='silhouette_mismatch_pixels'?0:2)+' → '+after[key].toFixed(key==='silhouette_mismatch_pixels'?0:2)+' '+unit);
 for(const[key,label]of [['left','左'],['right','右']]){const b=report.boundaries?.[key];if(!b)continue;const source=b.source_intervals,target=b.target_intervals,candidate=b.candidate_intervals;if([source,target,candidate].every(x=>Array.isArray(x)&&x.length===0)){out.push(label+'側：未接觸畫布邊緣');continue}const initial=distance(source,target),result=distance(candidate,target);out.push(initial!==null&&result!==null?label+'貼邊偏差：'+initial.toFixed(2)+' → '+result.toFixed(2)+' px':label+'側：輪廓對應不足，請檢查')}
 for(const warning of Array.isArray(report.warnings)?report.warnings:[])if(typeof warning==='string')out.push('⚠ '+warning);return out.join('\n')||'已完成量測；請以放大預覽確認細節。';
}
function renderAlignment(){
 if(!el('Align'))return;const reason=alignmentReason(),active=alignmentActive(),ready=!!job&&job.frames.length>1&&job.frames[Number(el('End').value)-1]?.status==='done'&&job.frames[Number(el('A').value)-1]?.status==='done',savedReference=alignmentValue?.model?.provenance?.reference?.frame;
 el('Align').disabled=!ready||alignmentBusy||exporting||job?.state==='running';el('Align').textContent=alignmentBusy?'正在計算局部對位…':alignmentValue?'重新對齊參考幀':'自動對齊參考幀';el('AlignDirection').textContent='尾幀 '+(el('End').value||'—')+' → 參考 A 幀 '+(el('A').value||'—');
 el('AlignReference').disabled=!job||alignmentBusy;el('AlignReference').max=Math.max(1,job?.frames.length||1);if(document.activeElement!==el('AlignReference'))el('AlignReference').value=el('A').value;
 el('AlignEnabled').checked=active;el('AlignEnabled').disabled=!alignmentValue||!!reason||alignmentBusy;el('AlignStrength').disabled=!alignmentValue||!!reason||alignmentBusy;if(document.activeElement!==el('AlignStrength'))el('AlignStrength').value=alignmentValue?.strength??100;el('AlignStrengthValue').textContent=(alignmentValue?.strength??100)+'%';el('AlignClear').disabled=!alignmentValue||alignmentBusy;
 const referenceNote=active&&savedReference!==Number(el('A').value)?'目前對位依計算時第 '+savedReference+' 幀；重新對齊會改用目前參考 A。':'';
 el('AlignStatus').textContent=alignmentBusy?'正在量測輪廓與貼邊差異，完成後會更新尾幀比較。':reason||(alignmentError?alignmentNotice:'')||referenceNote||alignmentNotice||(!alignmentValue?'先選擇參考 A 幀，再自動對齊尾幀。':active?'局部對位已啟用，沿用尾段漸進範圍。':'局部對位已停用，保留方案供再次啟用。');el('AlignStatus').dataset.error=String(!alignmentBusy&&(!!reason||alignmentError));
 const reportMismatch=closureValue?.enabled?'目前已套用首尾共同基準':!active?'局部對位未啟用或需要重新計算':alignmentValue.strength!==100?'目前 '+alignmentValue.strength+'%':el('Original').checked?'目前暫看原始結果':purpose()!=='loop'?'目前不是首尾銜接比較':savedReference!==Number(el('A').value)?'目前參考幀與計算時不同':Number(el('B').value)!==Number(el('End').value)?'目前檢查的 B 幀不是尾幀':'';
 el('AlignReport').textContent=alignmentReport?(reportMismatch?reportMismatch+'，需以更新後畫面為準；100% 候選量測不適用目前設定。'+(Array.isArray(alignmentReport.warnings)?alignmentReport.warnings.filter(x=>typeof x==='string').map(x=>'\n⚠ '+x).join(''):''):'計算候選量測（100% 強度）\n'+alignmentReportText(alignmentReport)):'';el('AlignReport').dataset.mismatch=String(!!alignmentReport&&!!reportMismatch);
 el('ManualNotice').hidden=!active;el('ManualTransform').disabled=active;el('Reset').disabled=active;if(active){el('ManualDetails').open=false;regionEditor?.deactivate(false);if(pickTarget)setPick(pickTarget)}
}
function syncTone(){for(const suffix of ['Tone','Contrast'])if(el(suffix+'Range'))el(suffix+'Range').value=el(suffix).value}
function set(r){closureValue=r.closure?cleanClosure(r,r.closure):undefined;regionValue=r.region?RegionEditor.normalize(r.region):undefined;if(JSON.stringify(alignmentValue?.model)!==JSON.stringify(r.alignment?.model))alignmentReport=null;alignmentValue=clone(r.alignment);alignmentNotice='';alignmentError=false;for(const[k,v]of Object.entries(ids)){if(k==='protect')el(v).checked=!!r[k];else el(v).value=r[k]??defaults(job)[k]}committed=values();scaleRatio=committed.sx/committed.sy;syncTone();updateTimeline();regionEditor?.sync();renderAlignment()}
function showDraftState(restored=false){if(el('DraftState'))el('DraftState').textContent=draftWarning||(referenceName&&!reference?'草稿已保留 · 參考圖需重新選取':restored?'已恢復此任務草稿':'草稿已自動保留')}
function snapshot(){const r=values();if(pendingBounds){for(const k of ['start','end','ramp'])r[k]=pendingBounds[k];if(pendingBounds.closure)r.closure=clone(pendingBounds.closure)}return {version:1,recipe:r,alignmentReport:clone(alignmentReport),a:pendingBounds?.a??Number(el('A').value),b:pendingBounds?.b??Number(el('B').value),purpose:purpose(),mode:el('Mode').value,zoom:el('Zoom').value,backdrop:el('Backdrop').value,split:Number(el('Split').value),original:el('Original').checked,lockRatio:el('LockRatio')?.checked===true,referenceName}}
function saveDraft(){
 if(!job)return;const value=snapshot();drafts.set(job.id,value);
 try{localStorage.setItem(draftPrefix+job.id,JSON.stringify(value));draftWarning=''}catch(_){draftWarning='草稿暫存在本次視窗；請儲存方案以免關閉後遺失'}
 showDraftState();
}
function restoreDraft(j){
 let value=drafts.get(j.id);draftWarning='';
 if(!value)try{const raw=localStorage.getItem(draftPrefix+j.id);if(raw){const parsed=JSON.parse(raw);if(parsed&&parsed.version===1&&typeof parsed.recipe==='object')value=parsed;else draftWarning='草稿格式無法讀取，已使用預設值'}}catch(_){draftWarning='無法讀取瀏覽器草稿；可載入已儲存方案'}
 pendingBounds=null;const stored=value?.recipe;
 if(!j.frames.length&&stored&&['start','end','ramp'].every(k=>Number.isInteger(stored[k]))&&stored.start>=1&&stored.start<stored.end&&stored.ramp>=stored.start&&stored.ramp<stored.end)pendingBounds={start:stored.start,end:stored.end,ramp:stored.ramp,a:value.a,b:value.b,closure:clone(stored.closure)};
 set(cleanRecipe(j,stored));const n=Math.max(1,j.frames.length);
 el('A').value=Math.round(clamp(value?.a,1,n,values().start));el('B').value=Math.round(clamp(value?.b,1,n,values().end));
 if(el('Purpose'))el('Purpose').value=purposes.includes(value?.purpose)?value.purpose:'tone';
 for(const[key,suffix]of [['mode','Mode'],['zoom','Zoom'],['backdrop','Backdrop']]){const select=el(suffix);if(value&&Array.from(select.options).some(o=>o.value===value[key]))select.value=value[key]}
 el('Split').value=clamp(value?.split,0,100,50);el('Original').checked=value?.original===true;el('ToneCompare').checked=false;if(el('LockRatio'))el('LockRatio').checked=value?.lockRatio===true;
 reference=references.get(j.id)||null;referenceName=reference?.name||(typeof value?.referenceName==='string'?value.referenceName.slice(0,250):'');el('Reference').value='';
 alignmentReport=clone(value?.alignmentReport)||null;committed=values();updateReference();updatePurpose();updateTimeline();renderAlignment();showDraftState(!!value);
}
function updateReference(){el('ReferenceName').textContent=reference?reference.name:referenceName?referenceName+'（重新選取後才能比對）':'尚未選取參考圖'}
function updatePurpose(){
 if(el('AControls'))el('AControls').hidden=purpose()!=='loop';
 el('Endpoints').hidden=purpose()!=='loop';el('ToneCompare').checked=false;
 if(el('ReferenceControls'))el('ReferenceControls').hidden=purpose()!=='reference';
 if(el('LoopControls'))el('LoopControls').hidden=purpose()!=='loop';
 updateLabels();
}
function updateLabels(){
 const missing=purpose()==='reference'&&!reference;
 if(el('SourceA'))el('SourceA').textContent=missing?'請先選取參考圖':images[0]?.label||'左側：等待載入';
 if(el('SourceB'))el('SourceB').textContent=missing?'目前幀：尚未比較':images[1]?.label||'右側：等待載入';
}
function updateTimeline(){
 if(!job)return;const n=Math.max(1,job.frames.length),r=values(),b=Math.round(clamp(el('B').value,1,n,1));renderClosure();
 if(el('Timeline')){el('Timeline').max=n;el('Timeline').value=b}
 if(el('TimelineInfo'))el('TimelineInfo').textContent=`第 ${b} / ${n} 幀 · 範圍 ${r.start}–${r.end} · 保留 ${Math.max(0,r.end-r.start+1)} 幀 · 漸進修正 ${r.ramp}–${r.end}`+(r.closure?.enabled?` · 共同基準 ${r.closure.reference} · 頭段 ${r.start}–${r.start+r.closure.head_frames-1} ／ 尾段 ${r.end-r.closure.tail_frames+1}–${r.end}`:'');
 if(el('RampTrack')){const left=clamp((r.ramp-1)/Math.max(1,n-1)*100,0,100),right=clamp((n-r.end)/Math.max(1,n-1)*100,0,100);el('RampTrack').style.left=left+'%';el('RampTrack').style.right=right+'%';el('RampTrack').style.setProperty('--ramp-start',left+'%');el('RampTrack').style.setProperty('--ramp-end',(100-right)+'%')}
 for(const[suffix,first,last]of [['ClosureHeadTrack',r.start,r.start+(r.closure?.head_frames||2)-1],['ClosureTailTrack',r.end-(r.closure?.tail_frames||2)+1,r.end]]){const track=el(suffix);if(track){track.hidden=!r.closure?.enabled||!!closureRangeReason(r);track.style.left=clamp((first-1)/Math.max(1,n-1)*100,0,100)+'%';track.style.right=clamp((n-last)/Math.max(1,n-1)*100,0,100)+'%'}}
}
function emitFrame(){if(job)window.dispatchEvent(new CustomEvent('workbench:frame',{detail:{frame:Number(el('B').value),source:'editor',jobId:job.id}}))}
function changeB(frame,emit=true){if(!job)return;el('B').value=Math.round(clamp(frame,1,Math.max(1,job.frames.length),1));updateTimeline();renderAlignment();saveDraft();epoch++;stop();queuePreview();if(emit)emitFrame()}
window.addEventListener('workbench:frame',event=>{const d=event.detail;if(!job||!d||d.source==='editor'||(d.jobId&&d.jobId!==job.id)||!Number.isFinite(d.frame))return;changeB(d.frame,false)});
async function call(action,data={},id=job?.id){if(!id)throw Error('請先選擇任務');return api('loop/'+action,{id,...data})}
function stop(){playing=false;clearTimeout(timer);lastSingle=null}
function queuePreview(){previewDirty=true;previewState='pending';renderPreviewState(lastSingle);clearTimeout(previewTimer);if(!$('loopEditor').hidden)previewTimer=setTimeout(()=>preview(),300)}
function finishWork(){working=false;for(const resolve of workWaiters.splice(0))resolve();if(previewPending){previewPending=false;queuePreview()}}
function invalidate(){stop();epoch++;seam=[];syncTone();updateTimeline();renderAlignment();saveDraft();draw();message('設定已變更，正在更新比對（保留上次畫面）…');el('Playback').textContent='修正已變更；接縫片段需重新準備';queuePreview()}
function remember(changed){
 if(['Protect','Px','Py','Radius'].includes(changed)&&regionValue){regionValue=undefined;regionEditor?.deactivate(false)}
 if(['Start','End','Ramp'].includes(changed))pendingBounds=null;if(closureValue&&['Start','End'].includes(changed))closureValue=cleanClosure(values(),closureValue);
 if(el('LockRatio')?.checked&&['Sx','Sy'].includes(changed)){
  if(changed==='Sx'){const x=clamp(el('Sx').value,Math.max(80,80*scaleRatio),Math.min(120,120*scaleRatio),committed.sx);el('Sx').value=Number(x.toFixed(3));el('Sy').value=Number((x/scaleRatio).toFixed(3))}
  else{const y=clamp(el('Sy').value,Math.max(80,80/scaleRatio),Math.min(120,120/scaleRatio),committed.sy);el('Sy').value=Number(y.toFixed(3));el('Sx').value=Number((y*scaleRatio).toFixed(3))}
 }
 if(JSON.stringify(values())===JSON.stringify(committed))return;if(committed)history.push({...committed});if(history.length>40)history.shift();committed=values();invalidate();
}
for(const suffix of Object.values(ids))el(suffix).oninput=el(suffix).onchange=()=>remember(suffix);
el('ClosureEnabled').onchange=()=>{
 const enabled=el('ClosureEnabled').checked;if(enabled&&closureRangeReason()){renderClosure();return}history.push(values());closureValue={...cleanClosure(values(),closureValue),enabled};
 if(enabled){el('Purpose').value='loop';el('A').value=el('Start').value;el('B').value=el('End').value;el('Original').checked=false;el('SeamN').value=Math.min(24,Math.max(Number(el('SeamN').value),closureValue.head_frames,closureValue.tail_frames));updatePurpose()}
 committed=values();invalidate();emitFrame();
};
for(const[suffix,key]of [['ClosureReference','reference'],['ClosureHead','head_frames'],['ClosureTail','tail_frames']]){
 el(suffix).oninput=()=>{if(!closureValue||el(suffix).value==='')return;const before=values(),next=cleanClosure(before,{...closureValue,[key]:Number(el(suffix).value)},key);if(JSON.stringify(next)===JSON.stringify(closureValue))return;history.push(before);closureValue=next;committed=values();invalidate()};
 el(suffix).onchange=el(suffix).onblur=()=>{if(closureValue)el(suffix).value=closureValue[key];renderClosure()};
}
el('Align').onclick=async()=>{if(!job||alignmentBusy)return;const id=job.id,referenceFrame=Number(el('A').value),sourceFrame=Number(el('End').value),submitted=values(),signature=JSON.stringify(submitted),token=++alignmentEpoch;alignmentBusy=true;alignmentNotice='';alignmentError=false;renderAlignment();try{const result=await call('align',{recipe:submitted,reference_frame:referenceFrame,source_frame:sourceFrame},id);if(token!==alignmentEpoch||job?.id!==id)return;if(signature!==JSON.stringify(values())||Number(el('A').value)!==referenceFrame){alignmentNotice='計算期間設定已變更，未套用舊結果。請重新對齊。';alignmentError=true;return}if(!result.alignment?.model)throw Error('服務未回傳完整對位方案，請重新計算。');history.push(values());set({...values(),alignment:clone(result.alignment)});alignmentReport=clone(result.report)||null;el('Purpose').value='loop';el('B').value=sourceFrame;el('Original').checked=false;updatePurpose();invalidate();alignmentNotice='對位已套用；目前比較參考 A 幀與修整後尾幀。'}catch(error){if(token===alignmentEpoch&&job?.id===id){alignmentNotice=error.message;alignmentError=true}}finally{if(token===alignmentEpoch){alignmentBusy=false;renderAlignment();saveDraft()}}};
el('AlignEnabled').onchange=()=>{if(!alignmentValue||alignmentBusy)return;if(alignmentReason()){renderAlignment();return}history.push(values());alignmentValue={...alignmentValue,enabled:el('AlignEnabled').checked};alignmentNotice='';alignmentError=false;committed=values();invalidate()};
el('AlignReference').oninput=()=>{if(!job||el('AlignReference').value==='')return;el('A').value=Math.round(clamp(el('AlignReference').value,1,Math.max(1,job.frames.length),1));el('A').oninput()};el('AlignReference').onchange=el('AlignReference').onblur=()=>{el('AlignReference').value=el('A').value};
el('AlignStrength').onfocus=()=>alignmentStrengthSession=false;el('AlignStrength').oninput=()=>{if(!alignmentValue||alignmentBusy||alignmentReason())return;if(!alignmentStrengthSession){history.push(values());alignmentStrengthSession=true}alignmentValue={...alignmentValue,strength:clamp(el('AlignStrength').value,0,100,100)};committed=values();invalidate()};el('AlignStrength').onchange=el('AlignStrength').onblur=()=>alignmentStrengthSession=false;
el('AlignClear').onclick=()=>{if(!alignmentValue||alignmentBusy)return;history.push(values());alignmentValue=undefined;alignmentReport=null;alignmentNotice='已清除對位，恢復原本手動變形設定。';alignmentError=false;committed=values();invalidate()};
if(el('LockRatio'))el('LockRatio').onchange=()=>{const r=values();scaleRatio=clamp(r.sx,80,120,100)/clamp(r.sy,80,120,100);saveDraft();message(el('LockRatio').checked?'已鎖定目前寬高比例，縮放限制為 80–120%。':'已解除寬高比例鎖定。')};
for(const suffix of ['Tone','Contrast'])if(el(suffix+'Range'))el(suffix+'Range').oninput=()=>{el(suffix).value=el(suffix+'Range').value;remember()};
if(el('Purpose'))el('Purpose').onchange=()=>{stop();images=[];updatePurpose();invalidate()};
el('Reference').onchange=async()=>{
 const f=el('Reference').files[0],id=job?.id;if(!f||!id)return;const url=URL.createObjectURL(f);
 try{const im=await img(url),old=references.get(id);if(old)URL.revokeObjectURL(old.url);const item={im,url,name:f.name,frame:'參考圖',clipped:false};references.set(id,item);
  if(job?.id!==id){const d=drafts.get(id);if(d){d.referenceName=f.name;try{localStorage.setItem(draftPrefix+id,JSON.stringify(d))}catch(_){}}return}
  reference=item;referenceName=f.name;if(el('Purpose'))el('Purpose').value='reference';images=[];updateReference();updatePurpose();invalidate();
 }catch(_){URL.revokeObjectURL(url);if(job?.id===id)message('參考圖無法讀取，請使用 PNG、JPG 或 WebP')}
};
el('ReferenceClear').onclick=()=>{if(reference)URL.revokeObjectURL(reference.url);references.delete(job?.id);reference=null;referenceName='';el('Reference').value='';images=[];updateReference();invalidate()};
el('ToneReset').onclick=()=>{history.push(values());set({...values(),tone:0,contrast:0});invalidate()};
el('ToneCompare').onchange=()=>{if(el('Purpose'))el('Purpose').value='tone';updatePurpose();invalidate()};
el('Backdrop').onchange=()=>{saveDraft();renderCanvas()};
el('Undo').onclick=()=>{if(history.length){set(history.pop());invalidate()}};
el('Reset').onclick=()=>{history.push(values());const r=values();if(r.region?.outside)r.region.outside=RegionEditor.identity();set({...r,dx:0,dy:0,sx:100,sy:100,angle:0,cx:50,cy:50,protect:false});invalidate()};
function endpoints(){el('A').value=el('Start').value;el('B').value=el('End').value;epoch++;updateTimeline();renderAlignment();saveDraft();queuePreview();emitFrame()}
function img(src){return new Promise((resolve,reject)=>{const im=new Image();im.onload=()=>resolve(im);im.onerror=()=>reject(Error('圖片載入失敗'));im.src=src})}
async function previewAt(i,r,original,id){const result=await call('preview',{recipe:r,frame:i,original},id);if(r.closure?.enabled&&!original&&result.closure?.enabled!==true)throw Error('目前服務版本尚未支援首尾共同基準，請重新啟動工作台');return {...result,im:await img(result.image)}}
async function preview(options={}){
 clearTimeout(previewTimer);if(!job)return false;if($('loopEditor').hidden&&options.force!==true){previewDirty=true;return false}if(!job.frames.some(f=>f.status==='done')){draw();return false}if(working){previewPending=true;return false}previewPending=false;stop();
 try{assertClosureReady()}catch(error){previewState='failed';previewFailure=error.message;renderPreviewState();message(error.message);return false}
 const request=previewRequest(),selectedPurpose=request.purpose;
 if(selectedPurpose==='reference'&&!reference){images=[];previewState='failed';previewFailure=referenceName?'請重新選取參考圖「'+referenceName+'」，完成後才能比較。':'請先選取 PNG、JPG 或 WebP 參考圖，才能與目前幀比較。';draw();message(previewFailure);return false}
 const token=++epoch,id=request.id,r=request.recipe,aIndex=request.a,bIndex=request.b,original=request.original;
 working=true;previewState='loading';previewFailure='';renderPreviewState();message('正在更新預覽，暫時保留上次畫面與來源標籤…');
 try{
  const a=selectedPurpose==='reference'?{...reference}:await previewAt(aIndex,r,selectedPurpose==='tone'||original,id);if(token!==epoch||request.signature!==previewRequest().signature)return false;
  const b=await previewAt(bIndex,r,original,id);if(token!==epoch||request.signature!==previewRequest().signature)return false;
  a.label=selectedPurpose==='reference'?'參考圖 · '+reference.name:(selectedPurpose==='tone'||original?'原始去背':'修整草稿')+' · 第 '+a.frame+' 幀';
  b.label=(original?'原始去背':'修整草稿')+' · 第 '+b.frame+' 幀';images=[a,b];shownPreview={...request,aFrame:a.frame,bFrame:b.frame};previewDirty=false;previewState='fresh';previewFailure='';draw();
  message(a.clipped||b.clipped?'⚠ 修正可能超出畫布，請減少位移或縮放。':original?'目前暫停套用修正；預覽顯示原始去背結果。':'比對已更新 · 畫布 '+job.dimensions.join(' × ')+' · 明暗 '+r.tone+'／對比 '+r.contrast);
  return true;
 }catch(e){if(token===epoch){previewState='failed';previewFailure=e.message;renderPreviewState();message(e.message+'（保留上次成功畫面）')}return false}
 finally{finishWork()}
}
el('A').oninput=()=>{renderAlignment();saveDraft();epoch++;queuePreview()};el('B').oninput=()=>changeB(Number(el('B').value));
if(el('Timeline'))el('Timeline').oninput=()=>changeB(Number(el('Timeline').value));
el('Preview').onclick=preview;el('Endpoints').onclick=()=>{endpoints();preview()};el('Original').onchange=()=>{invalidate();preview()};
function contourPixels(a,b,background){
 const out=new Uint8ClampedArray(a.length);for(let i=0;i<a.length;i+=4){const aa=a[i+3]/255,ba=b[i+3]/255,common=Math.min(aa,ba),difference=Math.min(1,Math.abs(aa-ba)*4),color=aa>ba?[255,65,85]:[30,220,255],gray=150+(a[i]+a[i+1]+a[i+2]+b[i]+b[i+1]+b[i+2])/30;for(let k=0;k<3;k++)out[i+k]=(background[i+k]*(1-common)+gray*common)*(1-difference)+color[k]*difference;out[i+3]=255}return out;
}
function drawContour(c,a,b,w,h,fit){
 const temp=document.createElement('canvas');temp.width=w;temp.height=h;const context=temp.getContext('2d',{willReadFrequently:true});context.drawImage(a.im,(w-a.im.width*fit)/2,(h-a.im.height*fit)/2,a.im.width*fit,a.im.height*fit);const left=context.getImageData(0,0,w,h);context.clearRect(0,0,w,h);context.drawImage(b.im,0,0);const right=context.getImageData(0,0,w,h);c.fillStyle=el('Backdrop').value;c.fillRect(0,0,w,h);const output=c.getImageData(0,0,w,h);output.data.set(contourPixels(left.data,right.data,output.data));c.putImageData(output,0,0);
}
function draw(single=null){
 regionEditor?.sync();
 renderPreviewState(single);
 const canvas=el('Canvas'),view=el('Viewport'),empty=$('editorEmpty');
 let emptyText=!job?'請先選擇任務':!job.frames.some(f=>f.status==='done')?'此任務尚無完成的透明圖片':purpose()==='reference'&&!reference?'請先選取明暗參考圖':!single&&images.length!==2?'正在準備比對預覽…':'';
 if(empty){empty.hidden=!emptyText;if(emptyText)empty.textContent=emptyText}
 if(!job||!job.dimensions)return;const[w,h]=job.dimensions;if(!w||!h)return;
 const zoom=el('Zoom').value,scale=zoom==='fit'?Math.max(.01,Math.min(view.clientWidth/w,view.clientHeight/h)):Number(zoom)||1;
 canvas.width=w;canvas.height=h;canvas.style.width=w*scale+'px';canvas.style.height=h*scale+'px';
 const c=canvas.getContext('2d');c.fillStyle=el('Backdrop').value;c.fillRect(0,0,w,h);updateLabels();
 if(single){c.drawImage(single.im,0,0);if(el('SourceA'))el('SourceA').textContent='接縫播放 · 第 '+single.frame+' 幀';if(el('SourceB'))el('SourceB').textContent=el('Original').checked?'原始去背':'修整草稿';return}
 if(images.length!==2){if(purpose()==='reference'&&!reference){c.font=`${Math.max(14,16/scale)}px sans-serif`;c.textAlign='center';c.fillStyle=el('Backdrop').value==='#ffffff'?'#222222':'#ffffff';c.fillText('請先選取參考圖',w/2,h/2)}return}
 const[a,b]=images,mode=el('Mode').value;
 if(mode==='onion')c.globalAlpha=.5;const fit=Math.min(w/a.im.width,h/a.im.height);c.drawImage(a.im,(w-a.im.width*fit)/2,(h-a.im.height*fit)/2,a.im.width*fit,a.im.height*fit);c.globalAlpha=1;
 if(mode==='wipe'){
  const split=Number(el('Split').value)/100*w;c.save();c.beginPath();c.rect(split,0,w-split,h);c.clip();c.fillStyle=el('Backdrop').value;c.fillRect(0,0,w,h);c.drawImage(b.im,0,0);c.restore();
  for(const[color,width]of [['#000',4],['#fff',2]]){c.strokeStyle=color;c.lineWidth=width/scale;c.beginPath();c.moveTo(split,0);c.lineTo(split,h);c.stroke()}
 }else if(mode==='onion'){c.globalCompositeOperation='lighter';c.globalAlpha=.5;c.drawImage(b.im,0,0);c.globalAlpha=1;c.globalCompositeOperation='source-over'}
 else if(mode==='contour')drawContour(c,a,b,w,h,fit);
 else{const ac=c.getImageData(0,0,w,h);c.fillStyle=el('Backdrop').value;c.fillRect(0,0,w,h);c.drawImage(b.im,0,0);const bc=c.getImageData(0,0,w,h);for(let i=0;i<bc.data.length;i+=4){for(let k=0;k<3;k++)bc.data[i+k]=Math.min(255,Math.abs(ac.data[i+k]-bc.data[i+k])*4);bc.data[i+3]=255}c.putImageData(bc,0,0)}
 if(!alignmentActive()&&!regionValue&&el('Protect').checked){const r=values();for(const[color,width]of [['#000',4],['#fff',2]]){c.strokeStyle=color;c.lineWidth=width/scale;c.beginPath();c.arc(r.px*(w-1)/100,r.py*(h-1)/100,Math.min(w,h)*r.radius/100,0,Math.PI*2);c.stroke()}}
 regionEditor?.draw(c,w,h);
}
function renderCanvas(){draw(lastSingle)}
function drawOutputCheck(){
 const canvas=el('OutputCanvas'),view=el('OutputViewport');if(!outputCheck){canvas.width=1;canvas.height=1;return}const[a,b]=outputCheck.images,w=b.width,h=b.height,zoom=el('OutputZoom').value,scale=zoom==='fit'?Math.max(.01,Math.min(view.clientWidth/w,view.clientHeight/h)):Number(zoom)||1;
 canvas.width=w;canvas.height=h;canvas.style.width=w*scale+'px';canvas.style.height=h*scale+'px';const c=canvas.getContext('2d'),mode=el('OutputMode').value;c.fillStyle='#000';c.fillRect(0,0,w,h);c.drawImage(a,0,0);
 if(mode==='wipe'){const split=Number(el('OutputSplit').value)/100*w;c.save();c.beginPath();c.rect(split,0,w-split,h);c.clip();c.fillStyle='#000';c.fillRect(0,0,w,h);c.drawImage(b,0,0);c.restore();for(const[color,width]of [['#000',4],['#fff',2]]){c.strokeStyle=color;c.lineWidth=width/scale;c.beginPath();c.moveTo(split,0);c.lineTo(split,h);c.stroke()}}
 else{const ac=c.getImageData(0,0,w,h);c.fillStyle='#000';c.fillRect(0,0,w,h);c.drawImage(b,0,0);const bc=c.getImageData(0,0,w,h);for(let i=0;i<bc.data.length;i+=4){for(let k=0;k<3;k++)bc.data[i+k]=Math.min(255,Math.abs(ac.data[i+k]-bc.data[i+k])*4);bc.data[i+3]=255}c.putImageData(bc,0,0)}
 el('OutputHint').textContent=mode==='wipe'?'左：實際輸出首幀；右：實際輸出尾幀。純黑底色，拖曳下方分隔線檢查。':'實際輸出首尾 PNG 在黑底上的 RGB 差異 ×4；完整 RGBA 像素核對結果以上方紀錄為準。';
}
function closeOutputCheck(){outputEpoch++;outputCheck=null;el('OutputDialog').close();drawOutputCheck()}
async function inspectOutput(j,v){
 const count=Number(v.report?.frames??(v.recipe?v.recipe.end-v.recipe.start+1:0));if(!Number.isInteger(count)||count<2){message('此版本缺少有效幀數，請開啟版本資料夾檢查。');return}
 const token=++outputEpoch,serial=jobSerial;outputCheck=null;el('OutputTitle').textContent='核對輸出首尾 · '+v.name;el('OutputSource').textContent=`實際輸出 ${v.name} · PNG 第 1 幀 ／ 第 ${count} 幀（不套用目前草稿）`;el('OutputStatus').textContent='正在回讀此版本的首尾 PNG…';el('OutputHint').textContent='';el('OutputSplit').value=50;if(!el('OutputDialog').open)el('OutputDialog').showModal();drawOutputCheck();
 try{const revision=String(v.revision||v.generation||Date.now()),path=seq=>{const base=file(j,'loop_edits/'+v.name+'/transparent_png/frame_'+String(seq).padStart(8,'0')+'.png');return base+(base.includes('?')?'&':'?')+'revision='+encodeURIComponent(revision)},loaded=await Promise.all([img(path(1)),img(path(count))]);if(token!==outputEpoch||job?.id!==j.id||jobSerial!==serial)return;if(loaded[0].width!==loaded[1].width||loaded[0].height!==loaded[1].height)throw Error('此版本首尾 PNG 尺寸不同，請檢查輸出資料夾');outputCheck={images:loaded};const report=v.report?.loop_closure;el('OutputStatus').textContent=report?.enabled===true&&report.endpoints_equal===true&&report.max_channel_error===0&&report.changed_pixels===0?'輸出時回讀核對：首尾 RGBA 像素相同。請另播放此版本，確認接縫動作。':report?'輸出時回讀核對：首尾仍有差異，請檢查畫面。':'此版本沒有首尾 RGBA 核對記錄；以下直接顯示輸出的 PNG。';drawOutputCheck()}
 catch(error){if(token===outputEpoch){el('OutputStatus').textContent='無法讀取此版本 PNG：'+error.message;outputCheck=null;drawOutputCheck()}}
}
el('OutputClose').onclick=closeOutputCheck;el('OutputDialog').addEventListener?.('cancel',()=>{outputEpoch++;outputCheck=null});
for(const suffix of ['OutputMode','OutputZoom','OutputSplit'])el(suffix).oninput=drawOutputCheck;new ResizeObserver(drawOutputCheck).observe(el('OutputViewport'));
['Mode','Zoom','Split'].forEach(k=>el(k).oninput=()=>{saveDraft();renderCanvas()});new ResizeObserver(renderCanvas).observe(el('Viewport'));
let dragging=false,panning=null,spaceHeld=false;
function editable(target){return target?.matches?.('input,textarea,select,button,a,[role=button],[role=slider],[contenteditable=true]')}
window.addEventListener('keydown',e=>{if(e.code==='Space'&&!editable(e.target)&&!$('loopEditor').hidden){spaceHeld=true;e.preventDefault()}});
window.addEventListener('keyup',e=>{if(e.code==='Space')spaceHeld=false});window.addEventListener('blur',()=>{spaceHeld=false;dragging=false;panning=null});
function drag(e){const rect=el('Canvas').getBoundingClientRect();el('Split').value=clamp((e.clientX-rect.left)/rect.width*100,0,100,50);renderCanvas()}
function setPick(target){regionEditor?.deactivate(false);pickTarget=pickTarget===target?null:target;for(const[k,value]of [['PickCenter','center'],['PickProtect','protect']])el(k)?.setAttribute('aria-pressed',String(pickTarget===value));el('Canvas').style.cursor=pickTarget?'crosshair':'';if(pickTarget)message(pickTarget==='center'?'點選畫布以設定變形中心。':'點選畫布以設定舊版圓形保護中心；會改回舊版保護方式。')}
if(el('PickCenter'))el('PickCenter').onclick=()=>setPick('center');if(el('PickProtect'))el('PickProtect').onclick=()=>setPick('protect');
el('Canvas').onpointerdown=e=>{
 if(e.button===1||spaceHeld){e.preventDefault();const view=el('Viewport');panning={x:e.clientX,y:e.clientY,left:view.scrollLeft,top:view.scrollTop};el('Canvas').setPointerCapture(e.pointerId);return}
 if(e.button!==0||playing)return;
 if(pickTarget){const rect=el('Canvas').getBoundingClientRect(),x=Math.round(clamp((e.clientX-rect.left)/rect.width*100,0,100)*10)/10,y=Math.round(clamp((e.clientY-rect.top)/rect.height*100,0,100)*10)/10;history.push(values());const r=values();set(pickTarget==='center'?{...r,cx:x,cy:y}:{...r,region:undefined,protect:true,px:x,py:y});setPick(pickTarget);invalidate();return}
 if(el('Mode').value!=='wipe')return;dragging=true;el('Canvas').setPointerCapture(e.pointerId);drag(e);
};
el('Canvas').onpointermove=e=>{if(panning){el('Viewport').scrollLeft=panning.left+panning.x-e.clientX;el('Viewport').scrollTop=panning.top+panning.y-e.clientY}else if(dragging)drag(e)};
el('Canvas').onpointerup=el('Canvas').onpointercancel=()=>{if(dragging)saveDraft();dragging=false;panning=null};el('Canvas').onauxclick=e=>{if(e.button===1)e.preventDefault()};
el('Search').onclick=async()=>{
 if(working||!job)return;working=true;const token=epoch;message('搜尋較佳接點…');
 try{const result=await call('search',{window:Number(el('Window').value)});if(token!==epoch)return;el('Candidates').replaceChildren();
  for(const item of result.candidates){const b=document.createElement('button'),count=item.end-item.start+1;b.textContent=`${item.start}–${item.end} 幀 · 保留 ${count} 幀／裁去 ${job.frames.length-count} 幀`;b.title='差異評分 '+item.score+'（越低越接近）';b.onclick=()=>{history.push(values());set({...values(),start:item.start,end:item.end,ramp:Math.max(item.start,item.end-11)});if(el('Purpose'))el('Purpose').value='loop';updatePurpose();endpoints();invalidate();preview()};el('Candidates').append(b)}message('已列出候選；採用後只改輸出範圍，請播放接縫確認。');
 }catch(e){if(token===epoch)message(e.message)}finally{finishWork()}
};
function displaySeam(){const f=seam[seamIndex];if(!f)return;lastSingle=f;draw(f);el('Playback').textContent=`第 ${f.frame} 幀${f.frame===values().start?' · 尾接頭':''} · ${el('Original').checked?'原始':'修正後'} · ${seamIndex+1}/${seam.length} 檢查片段`;}
function tick(){if(!playing||!seam.length)return;displaySeam();timer=setTimeout(()=>{seamIndex=(seamIndex+1)%seam.length;tick()},1000/(fpsNumber(job.config.fps)*Number(el('Speed').value)))}
el('Play').onclick=async()=>{
 if(working||!job)return;clearTimeout(previewTimer);previewPending=false;stop();working=true;const token=++epoch,id=job.id,r=values(),original=el('Original').checked;seam=[];seamSnapshot=previewRequest();
 try{assertClosureReady();const n=Math.min(Math.max(1,Number(el('SeamN').value)),24,Math.floor((r.end-r.start+1)/2));if(!n)throw Error('接縫檢查至少需要兩幀');const indices=[...Array.from({length:n},(_,i)=>r.end-n+1+i),...Array.from({length:n},(_,i)=>r.start+i)];
  for(const i of indices){message(`準備接縫預覽 ${seam.length+1}/${indices.length}`);const f=await previewAt(i,r,original,id);if(token!==epoch)return;seam.push(f)}
  message(seam.some(f=>f.clipped)?'⚠ 接縫中有幀可能超出畫布':'接縫預覽就緒');playing=true;seamIndex=0;tick();
 }catch(e){if(token===epoch)message(e.message)}finally{finishWork()}
};
el('Stop').onclick=()=>{clearTimeout(previewTimer);previewPending=false;stop();epoch++;draw();el('Playback').textContent='已停止接縫播放'};
for(const[k,d]of [['Prev',-1],['Next',1]])el(k).onclick=()=>{stop();if(!seam.length){changeB(Number(el('B').value)+d);return}seamIndex=(seamIndex+d+seam.length)%seam.length;displaySeam()};
el('Save').onclick=async()=>{try{assertAlignmentReady();assertClosureReady();const id=job.id,r=values();await call('save',{recipe:r},id);if(job?.id===id){saveDraft();message('修整方案已儲存至素材任務')}}catch(e){message(e.message)}};
function exportStatus(s){
 const active=s.state==='running',text=active?'正在輸出「'+(s.version||'修整')+'」：'+(progressText(s.progress)||s.phase):s.state==='complete'?'✓ 修整輸出完成'+(s.elapsed?' · 共用 '+durationText(s.elapsed):''):s.state==='error'?'輸出失敗：'+s.phase:'';
 for(const id of ['editExportStatus','leExportStatus']){const node=$(id);if(node){node.hidden=!text;node.textContent=text}}
 window.workbenchExportStatus?.({...s,jobId:job?.id});
}
function recipeSummary(v){const r=v.recipe,report=v.report;if(!r)return report?`${report.frames} 幀 · ${report.fps} FPS`:'版本記錄';const signed=n=>Number(n)>0?'+'+n:String(n??0),size=r.alignment?.enabled?`局部對位 ${r.alignment.strength??100}%`:r.region?.mode==='split'?`框內 ${r.sx}% × ${r.sy}%／框外 ${r.region.outside?.sx??100}% × ${r.region.outside?.sy??100}%`:r.region?.mode==='edge'?`貼邊固定 · 中間 ${r.sx}% × ${r.sy}%`:`${r.sx}% × ${r.sy}%`;return `${r.start}–${r.end} 幀 · 明暗 ${signed(r.tone)}／對比 ${signed(r.contrast)} · ${size}`+(r.closure?.enabled?` · 共同基準 ${r.closure.reference} · 頭 ${r.closure.head_frames}／尾 ${r.closure.tail_frames} 幀`:'')}
function versions(list){
 const id=job.id,j=job;el('Versions').replaceChildren();
 for(const v of list){const box=document.createElement('div');box.className='export-card';const label=document.createElement('strong');label.textContent=v.name;const summary=document.createElement('small');summary.textContent=recipeSummary(v)+' · '+v.phase;box.append(label,summary);
  if(v.recipe?.closure?.enabled){const verified=document.createElement('small'),report=v.report?.loop_closure;verified.className='closure-verification';verified.textContent=v.state!=='complete'?'尚未完成實際 PNG 核對':report?.enabled===true&&report.endpoints_equal===true&&report.max_channel_error===0&&report.changed_pixels===0?'✓ 實際輸出 PNG 已回讀：首尾像素相同。仍需播放確認動作。':report?'實際輸出 PNG 核對：首尾仍有差異，請檢查此版本。':'此版本沒有實際 PNG 核對記錄。';verified.dataset.verified=String(v.state==='complete'&&report?.enabled===true&&report.endpoints_equal===true&&report.max_channel_error===0&&report.changed_pixels===0);box.append(verified)}
  if(v.state==='complete'){const check=document.createElement('button');check.textContent='核對輸出首尾';check.onclick=()=>inspectOutput(j,v);box.append(check);const play=document.createElement('button');play.textContent='播放此版本';play.onclick=()=>window.workbenchSelectVersion?.(id,v.name);box.append(play);const a=document.createElement('a');a.className='download';a.textContent='下載透明影片';a.href=file(j,'loop_edits/'+v.name+'/loop_transparent.webm');a.download=v.name+'.webm';box.append(a)}
  const b=document.createElement('button');b.textContent='開啟此版本資料夾';b.onclick=()=>call('open',{version:v.name},id).catch(e=>message(e.message));box.append(b);el('Versions').append(box);
 }
 window.workbenchSetVersions?.(id,list);
}
async function load(apply=false){if(!job)return;const id=job.id;try{const result=await call('load',{},id);if(job?.id!==id)return;versions(result.versions||[]);if(apply){if(result.recipe){history.push(values());set(cleanRecipe(job,result.recipe));if(closureValue?.enabled){el('Purpose').value='loop';el('Original').checked=false;updatePurpose()}endpoints();const ref=alignmentValue?.model?.provenance?.reference;if(!closureValue?.enabled&&ref?.job_id===job.id&&Number.isInteger(ref.frame)&&ref.frame>=1&&ref.frame<=job.frames.length)el('A').value=ref.frame;invalidate();preview()}else message('尚未儲存方案')}}catch(e){if(job?.id===id)message('循環修整服務尚未載入，請重啟工作台。'+e.message)}}
el('Load').onclick=()=>load(true);
el('Export').onclick=async()=>{
 if(!job||exportChecking||exporting)return;const id=job.id,serial=jobSerial;exportChecking=true;el('Export').disabled=true;
 try{
  assertAlignmentReady();assertClosureReady();stop();if(el('Original').checked){el('Original').checked=false;invalidate()}draw();saveDraft();const request=previewRequest();
  message('正在確認目前修整預覽；成功後才會開始輸出…');clearTimeout(previewTimer);previewPending=false;
  await waitForWork();clearTimeout(previewTimer);previewPending=false;
  if(request.signature!==previewRequest().signature)throw Error('等待預覽期間設定或素材已變更，本次未輸出；請重新檢查後輸出。');
  if(!freshPreview(request)&&!await preview({force:true}))throw Error('預覽未更新成功，本次未輸出。'+(previewFailure?' '+previewFailure:''));
  if(request.signature!==previewRequest().signature||!freshPreview(request))throw Error('預覽期間設定或素材已變更，本次未輸出；請重新檢查後輸出。');
  assertAlignmentReady();assertClosureReady();const result=await call('export',{recipe:request.recipe},id);if(job?.id!==id||jobSerial!==serial)return;exporting=true;exportStatus(result);message(result.phase);
 }catch(e){if(job?.id===id&&jobSerial===serial)message(e.message)}finally{exportChecking=false;if(job)el('Export').disabled=exporting||job.state==='running'||job.frames.length<2}
};
window.loopRefresh=j=>{
 const previous=job,changedJob=job?.id!==j.id,previousCount=changedJob?0:job.frames.length;
 if(job?.id!==j.id){
  if(el('OutputDialog').open)closeOutputCheck();else{outputEpoch++;outputCheck=null}
  if(job)saveDraft();jobSerial++;alignmentEpoch++;alignmentBusy=false;alignmentNotice='';alignmentError=false;regionEditor?.deactivate(false);job=j;clearTimeout(previewTimer);previewPending=false;previewDirty=true;previewState='pending';shownPreview=null;previewFailure='';stop();epoch++;images=[];seam=[];history=[];pickTarget=null;restoreDraft(j);el('Candidates').replaceChildren();exporting=false;el('Export').disabled=exportChecking||j.frames.length<2;load();draw();if(!$('loopEditor').hidden&&j.frames.length>1)queuePreview();
 }else{
  job=j;
  if(previousCount===0&&j.frames.length>0){
   const old=values(),bounds=pendingBounds;
   if(bounds){const restored=cleanRecipe(j,{...old,...bounds});pendingBounds=null;set(restored);el('A').value=Math.round(clamp(bounds.a,1,j.frames.length,restored.start));el('B').value=Math.round(clamp(bounds.b,1,j.frames.length,restored.end))}
   else if(old.start===1&&old.end<=1){const fresh=defaults(j);set({...old,start:fresh.start,end:fresh.end,ramp:fresh.ramp});el('A').value=clamp(el('A').value,1,j.frames.length,1);el('B').value=clamp(el('B').value,1,j.frames.length,1)}
   previewDirty=true;saveDraft();draw();
  }
 }
 el('Export').disabled=exportChecking||exporting||j.state==='running'||j.frames.length<2;
 if(!changedJob&&previewDirty&&j.state!=='running'&&j.frames.some(f=>f.status==='done')&&(previous?.state==='running'||previousCount===0||!previous?.frames.some(f=>f.status==='done')))queuePreview();
 updateTimeline();renderAlignment();
 if(!polling){polling=true;const id=j.id;call('status',{},id).then(s=>{if(job?.id!==id)return;exportStatus(s);if(s.state==='running'){exporting=true;el('Export').disabled=true}else if(exporting){exporting=false;el('Export').disabled=exportChecking||job.state==='running'||job.frames.length<2;message(s.phase);load()}}).catch(()=>{}).finally(()=>polling=false)}
};
window.loopTabShown=()=>{draw();if((previewDirty||!images.length)&&job?.frames.length>1)preview()};
window.addEventListener('beforeunload',saveDraft);
if(window.RegionEditor&&el('RegionControls'))regionEditor=new RegionEditor({
 container:el('RegionControls'),outsideContainer:el('OutsideControls'),canvas:el('Canvas'),prefix:'leRegion',getContext:()=>[job?.id,el('A').value,el('B').value].join(':'),getValue:()=>regionValue,getTransform:()=>values(),
 getLegacy:()=>el('Protect').checked?'沿用舊版圓形保護圈。使用新框選後會改由新範圍控制。':'沿用整體變形；尚未設定局部範圍。',
 isReady:()=>!!job&&!alignmentActive()&&images.length===2&&!playing&&!lastSingle&&!(purpose()==='reference'&&!reference),
 onBegin:()=>{history.push(values());if(history.length>40)history.shift()},
 onActivate:()=>{stop();if(pickTarget)setPick(pickTarget)},
 onChange:(r,transform)=>{regionValue=r;if(transform){for(const[k,v]of Object.entries(transform))el(ids[k]).value=v;scaleRatio=transform.sx/transform.sy}committed=values();invalidate()},
 onSync:r=>{if(el('TransformHeading'))el('TransformHeading').textContent=r?.mode==='edge'?'中間修整（向所選邊緣漸退）':r?.mode==='split'?'框內變形':r?.mode==='inside'?'框內變形（框外不變）':r?.mode==='outside'?'框外變形（框內不變）':'整體變形';if(el('PickCenter'))el('PickCenter').textContent=r?.mode==='split'?'在畫布上選框內變形中心':'在畫布上選變形中心'},onDraw:()=>draw()
});
window.addEventListener('workbench:tab',()=>{if($('loopEditor').hidden)regionEditor?.deactivate(false)});
if(current())window.loopRefresh(current());
})();
