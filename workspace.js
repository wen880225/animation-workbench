/* Application chrome. Image processing and preview data remain in app.js / loop.js. */
(()=>{
 'use strict';
 const byId=id=>document.getElementById(id),preferenceKey='animation-workbench:preferences:v3';
 const themes={
  graphite:{name:'石墨深灰',kind:'深色 · 中性',surface:'#1c1e22',panel:'#272a2f',accent:'#c6cbd3'},
  ocean:{name:'深海藍',kind:'深色 · 清冷',surface:'#151e30',panel:'#202c43',accent:'#86bcff'},
  violet:{name:'暮光紫',kind:'深色 · 柔和',surface:'#231d2c',panel:'#30273c',accent:'#d0aeff'},
  sand:{name:'暖砂琥珀',kind:'淺色 · 溫暖',surface:'#f4eee4',panel:'#fffcf5',accent:'#855711'},
  celadon:{name:'霧白青瓷',kind:'淺色 · 清爽',surface:'#edf3f0',panel:'#fcfffd',accent:'#2b745b'},
  rose:{name:'櫻霧玫瑰',kind:'淺色 · 柔暖',surface:'#f8eff2',panel:'#fff9fc',accent:'#984465'}
 };
 let preferences={theme:'system',focus:false,tab:'tabPlayback',background:'#000',zoom:'fit'};
 try{const saved=JSON.parse(localStorage.getItem(preferenceKey)||'null');if(saved&&typeof saved==='object')preferences={...preferences,...saved}}catch(_){}
 if(!themes[preferences.theme]&&preferences.theme!=='system')preferences.theme='system';
 let currentJob=null,lastJobId=null,currentVideo=null,editStatuses=new Map(),draftStates=new Map(),transitionContext={project:null,status:{},dirty:false};
 function savePreferences(){try{localStorage.setItem(preferenceKey,JSON.stringify(preferences))}catch(_){byId('themeCurrent').textContent+=' · 此次選擇未能寫入瀏覽器'}}
 function setText(id,text){const node=byId(id);if(node&&node.textContent!==text)node.textContent=text}
 function openDialog(id){const dialog=byId(id);if(!dialog||dialog.open)return;for(const other of document.querySelectorAll('dialog[open]'))other.close();dialog.showModal();if(id==='taskDrawer')byId('openTasks').setAttribute('aria-expanded','true')}
 function closeDialog(id){byId(id)?.close();if(id==='taskDrawer')byId('openTasks').setAttribute('aria-expanded','false')}
 for(const button of document.querySelectorAll('[data-close]'))button.addEventListener('click',()=>closeDialog(button.dataset.close));
 for(const dialog of document.querySelectorAll('dialog')){dialog.addEventListener('click',e=>{if(e.target!==dialog)return;const r=dialog.getBoundingClientRect();if(e.clientX<r.left||e.clientX>r.right||e.clientY<r.top||e.clientY>r.bottom)dialog.close()});dialog.addEventListener('close',()=>{if(dialog.id==='taskDrawer')byId('openTasks').setAttribute('aria-expanded','false')})}
 function openTasksAt(section){openDialog('taskDrawer');requestAnimationFrame(()=>{byId(section)?.scrollIntoView({block:'nearest'});byId(section==='importArea'?'upload':'queueStart')?.focus({preventScroll:true})})}
 byId('openImport').onclick=()=>openTasksAt('importArea');byId('openTasks').onclick=()=>openTasksAt('queueArea');
 window.workbenchOpenImports=()=>openTasksAt('importArea');window.workbenchCloseTasks=()=>closeDialog('taskDrawer');
 byId('openTheme').onclick=()=>openDialog('themeDialog');byId('openResults').onclick=byId('statusDetails').onclick=()=>openDialog('resultsDialog');
 for(const[key,t]of Object.entries(themes)){
  const b=document.createElement('button');b.type='button';b.className='theme-choice';b.dataset.theme=key;b.setAttribute('aria-pressed','false');b.setAttribute('aria-label',t.name+'，'+t.kind);
  b.style.setProperty('--sample-surface',t.surface);b.style.setProperty('--sample-panel',t.panel);b.style.setProperty('--sample-accent',t.accent);
  b.innerHTML='<span class="palette-sample" aria-hidden="true"><span></span><span></span><span></span></span><strong>'+t.name+'</strong><small>'+t.kind+'</small>';
  byId('themeChoices').append(b);
 }
 function applyTheme(){document.documentElement.dataset.theme=preferences.theme;setText('themeCurrent','目前：'+(themes[preferences.theme]?.name||'跟隨系統'));for(const b of document.querySelectorAll('button[data-theme]'))b.setAttribute('aria-pressed',String(b.dataset.theme===preferences.theme))}
 for(const b of document.querySelectorAll('button[data-theme]'))b.onclick=()=>{preferences.theme=b.dataset.theme;applyTheme();savePreferences()};
 applyTheme();
 function applyFocus(){document.body.classList.toggle('focus-canvas',preferences.focus===true);byId('focusCanvas').setAttribute('aria-pressed',String(preferences.focus===true));setText('focusCanvas',preferences.focus?'⛶ 返回設定欄':'⛶ 專注畫布');requestAnimationFrame(()=>window.dispatchEvent(new Event('resize')))}
 byId('focusCanvas').onclick=()=>{preferences.focus=!preferences.focus;applyFocus();savePreferences()};applyFocus();
 const stateNames={ready:'待開始',running:'處理中',paused:'已暫停',review:'待確認',complete:'去背完成',attention:'需檢查'};
 function setProgress(done,total){const p=byId('globalProgress');p.max=Math.max(1,total||1);p.value=Math.max(0,done||0)}
 function transitionVisible(){return byId('tabTransitions')?.getAttribute('aria-selected')==='true'}
 function updateContext(){
  const multi=transitionVisible(),p=transitionContext.project;
  byId('prepareExport').hidden=byId('openResults').hidden=byId('leDraftState').hidden=multi;byId('statusDetails').disabled=multi;
  if(multi){setText('title',p?.name||'接點修整');setText('documentMeta',p?(p.clips?.length||0)+' 段動畫 · '+(p.route_mode==='open'?'依序播完':'循環播放')+' · 固定畫布':'加入已去背的動畫，開始接點修整')}
  else{setText('title',currentJob?.name||'動畫去背工作台');setText('documentMeta',currentJob?(currentJob.dimensions?.join(' × ')||'準備拆幀')+' · '+currentJob.config.fps+' FPS · '+currentJob.frames.length+' 幀':'建立任務，開始檢查你的動畫')}
 }
 function singleArtifactStatus(j,s){
  const draft=draftStates.get(j.id),editing=byId('tabEdit')?.getAttribute('aria-selected')==='true',playback=byId('tabPlayback')?.getAttribute('aria-selected')==='true';
  if(s?.state==='error')return '修整輸出失敗：'+s.phase;
  if(playback&&currentVideo?.jobId===j.id){const f=currentVideo.freshness;return '正在檢查成品：'+currentVideo.label+(f?.state==='stale'?' · 此為先前輸出，尚未包含更新後的來源':f?.state==='unknown'?' · 此版本與目前來源尚未核對':'')}
  if(s?.state==='complete'){
   const name=s.version||'修整版本';
   if(j.frames.some(f=>f.status!=='done'))return name+' 為先前輸出；來源影格仍待處理，請完成後再檢查及輸出。';
   if(s.sourceRevision!=null&&s.sourceRevision!==Number(j.media_revision||0))return name+' 已輸出，但來源已更新；請重新檢查及輸出。';
   if(s.sourceRevision==null||!s.recipeSignature||!draft?.recipeSignature)return name+' 已輸出；目前草稿與此版本尚未核對。';
   return draft.recipeSignature===s.recipeSignature?'✓ '+name+' 已輸出 · 設定與目前草稿相同；仍需檢查成品':name+' 已輸出，但目前草稿已變更；請重新檢查及輸出。';
  }
  if(editing)return '目前顯示修整草稿；儲存方案不等於輸出成品。';
  if(Object.values(j.export_freshness||{}).some(f=>f.state==='stale'))return '來源已更新 · 先前影片尚未重新合成，請至任務操作重新合成。';
  return j.state==='complete'?'✓ 原始去背完成 · '+j.frames.filter(f=>f.status==='done').length+' 幀':j.phase||stateNames[j.state];
 }
 function renderStatus(){
  updateContext();
  const j=currentJob,s=j?editStatuses.get(j.id):null,running=s?.state==='running';
  const ts=transitionContext.status||{};
  byId('prepareExport').disabled=!j||j.frames.length<2||j.state==='running'||running||ts.state==='running';
  if(transitionVisible()||ts.state==='running'){
   const active=ts.state==='running';byId('globalProgress').hidden=!active;
   setText('documentState',active?'動畫輸出中':transitionContext.dirty?'接點草稿有變更':'接點修整');
   byId('documentState').dataset.state=active?'running':'ready';
   setText('globalStatus',active?(ts.phase||'正在輸出銜接動畫'):ts.state==='error'?'輸出失敗：'+ts.phase:transitionContext.completionText|| (transitionContext.dirty?'設定已變更，請重新檢查受影響接點':ts.state==='complete'?'已有輸出版本；目前草稿與此版本尚未核對':'目前顯示接點草稿；正式成品需另行輸出及驗收'));
   setText('globalEta',active&&ts.progress&&typeof progressText==='function'?progressText(ts.progress):active?'正在估算剩餘時間…':'');setProgress(ts.progress?.done||ts.done,ts.progress?.total||ts.total);return;
  }
  byId('globalProgress').hidden=!running&&j?.state!=='running';
  if(!j){setText('globalStatus','建立任務，或從任務紀錄選擇素材');setText('globalEta','');return}
  const done=j.frames.filter(f=>f.status==='done').length;
  if(running){
   setText('documentState','修整輸出中');byId('documentState').dataset.state='running';
   setText('globalStatus',(s.version||'修整版本')+' · '+(s.progress?.stage||s.phase||'正在準備'));
   setText('globalEta',s.progress&&typeof progressText==='function'?progressText(s.progress):'正在估算剩餘時間…');setProgress(s.progress?.done||s.done,s.progress?.total||j.frames.length);
  }else{
   setText('documentState',j.state!=='running'&&byId('tabEdit')?.getAttribute('aria-selected')==='true'?'修整草稿':stateNames[j.state]||j.state);byId('documentState').dataset.state=j.state;
   const status=j.state==='running'?j.phase||'正在處理素材':singleArtifactStatus(j,s);
   setText('globalStatus',status);setText('globalEta',j.state==='running'?byId('eta').textContent:'');setProgress(done,j.frames.length);
  }
 }
 window.workbenchExportStatus=s=>{if(!s?.jobId)return;const previous=editStatuses.get(s.jobId);let snapshot={...s};if(s.recipeSignature){snapshot.sourceRevision=s.source_revision}else if(s.generation&&s.version===previous?.version&&s.generation===previous.generation){snapshot.recipeSignature=previous.recipeSignature;snapshot.sourceRevision=s.source_revision??previous.sourceRevision}editStatuses.set(s.jobId,snapshot);renderStatus();if(s.state==='running'&&previous?.state!=='running')closeDialog('exportDialog')};
 window.addEventListener('workbench:draft',e=>{const d=e.detail;if(!d?.jobId||typeof d.recipeSignature!=='string')return;draftStates.set(d.jobId,d);if(currentJob?.id===d.jobId)renderStatus()});
 window.addEventListener('workbench:transitions',e=>{transitionContext=e.detail||{project:null,status:{}};renderStatus()});
 window.addEventListener('workbench:job',e=>{
  currentJob=e.detail;const j=currentJob;
  if(j){setText('documentMeta',(j.dimensions?.join(' × ')||'準備拆幀')+' · '+j.config.fps+' FPS · '+j.frames.length+' 幀');lastJobId=j.id;byId('editorEmpty').hidden=false;if(j.frames.some(f=>f.status==='done'))byId('editorEmpty').hidden=true}
  else setText('documentMeta','建立任務，開始檢查你的動畫');
  renderStatus();
 });
 window.addEventListener('workbench:video',e=>{currentVideo=e.detail;renderStatus()});
 window.addEventListener('workbench:tab',e=>{preferences.tab=e.detail.id;savePreferences();document.body.dataset.mode=e.detail.id;setText('prepareExport','輸出目前修整');if(byId('resultsDialog').open)closeDialog('resultsDialog');renderStatus()});
 function exportSummary(){
  const j=currentJob;if(!j)return;const get=k=>Number(byId('le'+k).value);const start=get('Start'),end=get('End'),count=end-start+1;
  const data=[['來源',j.name],['固定畫布',j.dimensions.join(' × ')+' px'],['範圍',start+'–'+end+' 幀，共 '+count+' 幀'],['播放規格',j.config.fps+' FPS · '+(count/(typeof fpsNumber==='function'?fpsNumber(j.config.fps):24)).toFixed(2)+' 秒'],['明暗／對比',get('Tone')+' ／ '+get('Contrast')],['尾段變形','左右 '+get('Dx')+' px，上下 '+get('Dy')+' px，寬高 '+get('Sx')+'% / '+get('Sy')+'%'],['另存內容','透明 PNG 序列 ＋ 透明 WebM']];
  const aligned=byId('leAlignEnabled')?.checked===true,mode=byId('leRegionMode')?.value||'legacy',scope=mode==='split'?'框內／框外分別調整':mode==='inside'?'只修框內':mode==='outside'?'保護框內':mode==='edge'?'貼邊固定（中間修整）':mode==='all'?'整個畫面':byId('leProtect').checked?'舊版保護圈':'整個畫面';
  if(aligned){data[5]=['尾段修整','自動局部對位 · 強度 '+Number(byId('leAlignStrength')?.value||0)+'%'];data.splice(6,0,['漸進範圍',get('Ramp')+' → '+end+' 幀']);}
  else data.splice(6,0,['作用範圍',scope+(['inside','outside','split'].includes(mode)?' · '+(byId('leRegionShape').value==='ellipse'?'橢圓':'矩形')+' · 羽化 '+byId('leRegionFeather').value+'%':'')]);
  if(!aligned&&mode==='split'){
   data[5][0]='尾段框內變形';data[5][1]+='，旋轉 '+get('Angle')+'°，中心 '+get('Cx')+'% / '+get('Cy')+'%';
   const outer=k=>Number(byId('leRegionOutside'+k)?.value);
   data.splice(6,0,['尾段框外變形','左右 '+outer('Dx')+' px，上下 '+outer('Dy')+' px，寬高 '+outer('Sx')+'% / '+outer('Sy')+'%，旋轉 '+outer('Angle')+'°，中心 '+outer('Cx')+'% / '+outer('Cy')+'%']);
  }
  if(byId('leClosureEnabled')?.checked){
   data.splice(data.length-1,0,['首尾共同基準','第 '+get('ClosureReference')+' 幀 · 首尾使用同一張基準圖'],['首尾過渡','開頭 '+get('ClosureHead')+' 幀 ／ 結尾 '+get('ClosureTail')+' 幀'],['輸出核對','回讀首尾 PNG，確認像素一致；動作仍需播放檢查']);
  }
  byId('exportSummary').replaceChildren();for(const[label,value]of data){const dt=document.createElement('dt'),dd=document.createElement('dd');dt.textContent=label;dd.textContent=value;byId('exportSummary').append(dt,dd)}
 }
 let exportRequested=false;
 byId('prepareExport').onclick=()=>{if(!currentJob)return;window.workbenchSelectTab?.('tabEdit');exportSummary();exportRequested=false;byId('leExportStatus').hidden=true;openDialog('exportDialog')};
 byId('leExport').addEventListener('click',()=>{exportRequested=true;byId('leExportStatus').hidden=false;setText('leExportStatus','正在檢查修整設定並準備輸出…')});
 new MutationObserver(()=>{if(exportRequested&&byId('exportDialog').open){byId('leExportStatus').hidden=false;setText('leExportStatus',byId('leMessage').textContent)}}).observe(byId('leMessage'),{childList:true,characterData:true,subtree:true});
 byId('jobs').addEventListener('click',e=>{if(e.target.closest('[data-job]'))closeDialog('taskDrawer')});
 new MutationObserver(renderStatus).observe(byId('eta'),{childList:true,characterData:true,subtree:true});
 byId('lePurpose').addEventListener('change',()=>{for(const[id,value]of [['leReferenceControls','reference'],['leLoopControls','loop']]){const node=byId(id);if(node&&node.tagName==='DETAILS'&&byId('lePurpose').value===value)node.open=true}});
 function syncFrameBackdrop(value){byId('frameBackdrop').value=value;preferences.background=value;savePreferences()}
 byId('frameBackdrop').onchange=()=>{const value=byId('frameBackdrop').value;byId('bg').value=value;if(typeof applyBackground==='function')applyBackground();syncFrameBackdrop(value)};
 byId('bg').addEventListener('change',()=>syncFrameBackdrop(byId('bg').value));
 function syncZoom(source,target){const value=byId(source).value;if(Array.from(byId(target).options).some(o=>o.value===value)&&byId(target).value!==value){byId(target).value=value;byId(target).dispatchEvent(new Event(target==='leZoom'?'input':'change'))}preferences.zoom=value;savePreferences()}
 byId('imageZoom').addEventListener('change',()=>syncZoom('imageZoom','leZoom'));byId('leZoom').addEventListener('input',()=>syncZoom('leZoom','imageZoom'));
 byId('fitFrames').onclick=()=>{for(const n of document.querySelectorAll('.still,.compare-viewport'))n.style.removeProperty('height');byId('imageZoom').value='fit';byId('imageZoom').dispatchEvent(new Event('change'))};
 let space=false,pan=null;
 function editable(target){return target?.matches?.('input,select,textarea,button,a,[contenteditable=true]')}
 window.addEventListener('keydown',e=>{if(e.code==='Space'&&!editable(e.target)&&!byId('framesPanel').hidden){space=true;e.preventDefault()}});
 window.addEventListener('keyup',e=>{if(e.code==='Space')space=false});window.addEventListener('blur',()=>{space=false;pan=null});
 for(const view of [byId('compareViewport'),...document.querySelectorAll('.still')]){
  view.addEventListener('pointerdown',e=>{if(!space&&e.button!==1)return;pan={view,x:e.clientX,y:e.clientY,left:view.scrollLeft,top:view.scrollTop};view.setPointerCapture(e.pointerId);e.preventDefault();e.stopPropagation()},true);
  view.addEventListener('pointermove',e=>{if(pan?.view===view){view.scrollLeft=pan.left+pan.x-e.clientX;view.scrollTop=pan.top+pan.y-e.clientY}});
  view.addEventListener('pointerup',()=>pan=null);view.addEventListener('pointercancel',()=>pan=null);
 }
 document.addEventListener('DOMContentLoaded',()=>{
  if(Array.from(byId('bg').options).some(o=>o.value===preferences.background)){byId('bg').value=preferences.background;byId('frameBackdrop').value=preferences.background;applyBackground()}
  if(Array.from(byId('imageZoom').options).some(o=>o.value===preferences.zoom)){byId('imageZoom').value=preferences.zoom;resizeStillImages()}
  window.workbenchSelectTab?.(['tabPlayback','tabFrames','tabEdit','tabTransitions'].includes(preferences.tab)?preferences.tab:'tabPlayback');
 });
})();
