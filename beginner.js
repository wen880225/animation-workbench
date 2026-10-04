/* Presentation only: no image settings, recipes, or export data are changed. */
(()=>{
 'use strict';
 const $=id=>document.getElementById(id),key='animation-workbench:reading:v1';
 const text=(id,value)=>{const n=$(id);if(n&&n.textContent!==value)n.textContent=value};
 let reading='comfortable';try{if(localStorage.getItem(key)==='large')reading='large'}catch{}
 function applyReading(value){reading=value==='large'?'large':'comfortable';document.documentElement.dataset.reading=reading;for(const b of document.querySelectorAll('[data-reading]'))if(b.tagName==='BUTTON')b.setAttribute('aria-pressed',String(b.dataset.reading===reading));try{localStorage.setItem(key,reading)}catch{}requestAnimationFrame(()=>window.dispatchEvent(new Event('resize')))}
 for(const b of document.querySelectorAll('button[data-reading]'))b.addEventListener('click',()=>applyReading(b.dataset.reading));applyReading(reading);
 const guide={
  start:['第一次使用','先開啟 ComfyUI，再匯入影片。可以一次選多支，讓任務隊列依序去背。'],
  playback:['檢查播放','先看正常速度，再用 0.1× 看接縫。想查某一張的去背邊緣，切到「逐幀檢查」。'],
  single:['修整一段動畫','選「明暗與參考」調亮暗；選「首尾循環」讓結尾接回開頭。先慢播檢查，再按「輸出目前修整」。'],
  multi:['串接多段表情','依畫面上的四個步驟操作。明暗已一致時可直接到下一步；細部變形放在「進階微調」。']
 };
 function openGuide(topic='start'){const item=guide[topic]||guide.start;text('guideContextTitle',item[0]);text('guideContextText',item[1]);for(const d of document.querySelectorAll('dialog[open]'))if(d.id!=='guideDialog')d.close();if(!$('guideDialog').open)$('guideDialog').showModal()}
 $('openGuide').addEventListener('click',()=>openGuide());for(const b of document.querySelectorAll('[data-guide]'))b.addEventListener('click',()=>openGuide(b.dataset.guide));
 for(const b of document.querySelectorAll('[data-guide-route]'))b.addEventListener('click',()=>{$('guideDialog').close();if(b.dataset.guideRoute==='import')window.workbenchOpenImports?.();else{if(document.body.classList.contains('focus-canvas'))$('focusCanvas').click();const single=b.dataset.guideRoute==='single',tab=single?'tabEdit':'tabTransitions';window.workbenchSelectTab?.(tab);if(single)$(tab).focus();else window.workbenchTransitionStep?.('sources')}});
 $('welcomeImport').onclick=()=>window.workbenchOpenImports?.();$('welcomeTasks').onclick=()=>$('openTasks').click();
 function updateWelcome(job){const empty=!job;$('playbackPanel').dataset.empty=String(empty);$('welcomePanel').hidden=!empty}
 window.addEventListener('workbench:job',e=>updateWelcome(e.detail));
 if(typeof current==='function')updateWelcome(current());
 // Move existing nodes, keeping their IDs, listeners, and recipe values intact.
 $('singleSaveDock').append($('leSave'),$('leUndo'));
 $('singleRangeDock').append($('leLoopControls'));$('leLoopControls').open=false;
 const meta=document.querySelector('#loopEditor .preview-meta'),metaBody=document.createElement('div');metaBody.className='meta-popover';metaBody.append($('lePreviewSnapshot'),$('leModeHint'));meta.append(metaBody);meta.querySelector('summary').textContent='畫面資訊';$('lePreview').before(meta);
 $('singleExport').onclick=()=>$('prepareExport').click();
 function changePurpose(p){if($('lePurpose').value!==p){$('lePurpose').value=p;$('lePurpose').dispatchEvent(new Event('change',{bubbles:true}))}if(p==='loop'&&!$('leEndpoints').disabled)$('leEndpoints').click();syncSingle();requestAnimationFrame(()=>window.dispatchEvent(new Event('resize')))}
 for(const b of document.querySelectorAll('[data-single-mode]'))b.addEventListener('click',()=>changePurpose(b.dataset.singleMode));
 $('singleUseReference').onclick=()=>{changePurpose('reference');$('leReference').focus()};$('singleBeforeAfter').onclick=()=>{changePurpose('tone');$('leTone').focus()};
 $('singleSlowPreview').onclick=()=>{changePurpose('loop');$('leSpeed').value='0.1';$('leSpeed').dispatchEvent(new Event('change',{bubbles:true}));$('lePlay').click()};
 $('singleShowApplied').onclick=()=>{changePurpose('loop');if(advancedSummary().length){$('singleAdvanced').open=true;$('singleAdvanced').scrollIntoView({block:'nearest'});$('singleAdvanced').querySelector('summary').focus()}else{$('leClosureCard').scrollIntoView({block:'nearest'});$('leClosureEnabled').focus()}};
 function advancedSummary(){
  const result=[],number=id=>Number($(id)?.value||0);
  if($('leAlignStatus')?.dataset.error==='true')result.push('局部對位需處理');
  else if($('leAlignEnabled')?.checked)result.push('局部對位 '+number('leAlignStrength')+'%');
  else{
   const changed=number('leDx')!==0||number('leDy')!==0||number('leSx')!==100||number('leSy')!==100||number('leAngle')!==0;
   const outside=['Dx','Dy','Sx','Sy','Angle'].some(k=>{const n=$('leRegionOutside'+k);return n&&Number(n.value)!=(['Sx','Sy'].includes(k)?100:0)});
   if(changed||outside)result.push('手動變形');
  }
  return result;
 }
 function syncSingle(){
  const mode=$('lePurpose').value,loop=mode==='loop';
  if($('loopEditor').dataset.singlePurpose!==mode)$('loopEditor').dataset.singlePurpose=mode;
  $('singleToneGroup').hidden=loop;$('singleLoopGroup').hidden=!loop;
  for(const b of document.querySelectorAll('[data-single-mode]'))b.setAttribute('aria-pressed',String((b.dataset.singleMode==='loop')===loop));
  text('singleStageTitle',loop?'讓結尾自然接回開頭':mode==='reference'?'對照參考圖，調整明暗':'調整這段動畫的明暗');
  $('singleBeforeAfter').hidden=mode!=='reference';$('singleUseReference').hidden=mode==='reference';
  const advanced=advancedSummary(),applied=[...advanced];if($('leClosureEnabled').checked)applied.push('首尾共同基準');
  const error=$('leAlignStatus')?.dataset.error==='true';
  $('singleApplied').hidden=!applied.length;$('singleApplied').dataset.error=String(error);text('singleAppliedText',(error?'請檢查：':'已套用：')+applied.join('、'));
  text('singleAdvancedBadge',advanced.length?(error?'需處理 · ':'使用中 · ')+advanced.join('、'):'未套用');
  $('singleExport').disabled=$('prepareExport').disabled;$('singleSlowPreview').disabled=$('lePlay').disabled;
 }
 let scheduled=false;function scheduleSync(){if(scheduled)return;scheduled=true;requestAnimationFrame(()=>{scheduled=false;syncSingle()})}
 for(const event of ['input','change','click'])$('loopEditor').addEventListener(event,scheduleSync);
 for(const event of ['workbench:tab','workbench:job'])window.addEventListener(event,scheduleSync);
 const observer=new MutationObserver(scheduleSync);
 for(const id of ['leClosureStatus','leAlignStatus','leDraftState','lePreviewSnapshot'])observer.observe($(id),{childList:true,subtree:true,characterData:true});
 observer.observe($('prepareExport'),{attributes:true,attributeFilter:['disabled']});
 observer.observe($('leAlignStatus'),{attributes:true,attributeFilter:['data-error'],childList:true,subtree:true,characterData:true});
 syncSingle();
})();
