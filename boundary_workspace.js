/* Navigation adapter only. Existing drafts and legacy recipes are never converted here. */
(()=>{
 'use strict';
 const $=id=>document.getElementById(id);
 if(!$('tabMaterials'))return;
 let selectedJob=typeof current==='function'?current():null,lastInspection='tabPlayback',opening=false;
 const inspectionTabs=['tabPlayback','tabFrames'];
 function ready(job){return !!job&&job.state!=='running'&&job.frames?.length>=2&&job.frames.every(frame=>frame.status==='done')}
 function updateSource(){
  const button=$('boundaryStartCurrent'),available=ready(selectedJob);
  button.disabled=opening||!available;button.textContent=opening?'正在開啟修整…':'以目前素材建立修整';
  button.title=available?'以這段已去背素材開始新的接點修整':'請先選擇至少兩幀、已完成去背的素材';
  $('openLegacySingle').disabled=!selectedJob||!selectedJob.frames?.length;
 }
 function syncNavigation(id){
  const inspection=inspectionTabs.includes(id),legacy=id==='tabEdit';
  if(inspection)lastInspection=id;
  document.body.dataset.boundaryMode=legacy?'legacy':inspection?'materials':'seams';
  $('tabMaterials').setAttribute('aria-selected',String(inspection));$('tabMaterials').tabIndex=inspection||legacy?0:-1;
  $('qaModes').hidden=!inspection;$('boundaryStartCurrent').hidden=!inspection;
  $('qaPlayback').setAttribute('aria-pressed',String(id==='tabPlayback'));$('qaFrames').setAttribute('aria-pressed',String(id==='tabFrames'));
  $('qaPlayback').tabIndex=id==='tabFrames'?-1:0;$('qaFrames').tabIndex=id==='tabFrames'?0:-1;
  $('compatibilityMenu').open=false;
 }
 function inspect(id=lastInspection){window.workbenchSelectTab?.(inspectionTabs.includes(id)?id:'tabPlayback')}
 $('tabMaterials').onclick=()=>inspect();$('qaPlayback').onclick=()=>inspect('tabPlayback');$('qaFrames').onclick=()=>inspect('tabFrames');
 $('openLegacySingle').onclick=()=>{if(!selectedJob)return;window.workbenchSelectTab?.('tabEdit');$('singleStageTitle')?.focus?.()};
 window.boundaryOpenCurrent=async()=>{
  if(opening)return false;
  if(!ready(selectedJob)){inspect();window.notice?.('請先選擇已完成去背的素材，再建立修整。');$('boundaryStartCurrent').focus();return false}
  const jobId=selectedJob.id;opening=true;updateSource();
  try{
   if(typeof window.boundaryOpenSource!=='function')throw Error('接點修整尚未載入，請重新整理工作台。');
   const result=await window.boundaryOpenSource({jobId,version:'original'});
   if(result===false)return false;
   window.workbenchSelectTab?.('tabTransitions');return true;
  }catch(error){window.notice?.(error.message||'無法開啟修整，請重試。');return false}
  finally{opening=false;updateSource()}
 };
 $('boundaryStartCurrent').onclick=window.boundaryOpenCurrent;
 // Only the two visible workspaces participate in top-level keyboard navigation.
 for(const[id,index]of [['tabMaterials',0],['tabTransitions',1]])$(id).onkeydown=event=>{
  const direction=event.key==='ArrowRight'?1:event.key==='ArrowLeft'?-1:0;
  const target=event.key==='Home'?0:event.key==='End'?1:direction?(index+direction+2)%2:null;
  if(target===null)return;event.preventDefault();const button=$(['tabMaterials','tabTransitions'][target]);button.click();button.focus();
 };
 for(const[id,index]of [['qaPlayback',0],['qaFrames',1]])$(id).onkeydown=event=>{
  if(!['ArrowLeft','ArrowRight','Home','End'].includes(event.key))return;
  event.preventDefault();const target=event.key==='Home'?0:event.key==='End'?1:1-index,button=$(['qaPlayback','qaFrames'][target]);button.click();button.focus();
 };
 window.addEventListener('workbench:job',event=>{selectedJob=event.detail;updateSource()});
 window.addEventListener('workbench:tab',event=>syncNavigation(event.detail.id));
 document.addEventListener('DOMContentLoaded',()=>{
  // A stored pre-consolidation tab is not a request to enter compatibility mode.
  if(!$('loopEditor').hidden)window.workbenchSelectTab?.('tabTransitions');
  else syncNavigation(!$('transitionPanel').hidden?'tabTransitions':!$('framesPanel').hidden?'tabFrames':'tabPlayback');
 });
 updateSource();syncNavigation('tabPlayback');
})();
