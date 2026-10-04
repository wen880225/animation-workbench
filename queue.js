/* Batch imports retain individual errors; the server owns dispatch and persistence. */
(()=>{
 'use strict';
 const el=id=>document.getElementById(id),extensions=/\.(mp4|mov|mkv|webm|avi|m4v|ogv|gif)$/i;
 let pending=[],importing=false,queueBusy=false,queueState=null,queueSignature='',queueRevision=0,openEpoch=0,polling=false,pollTimer;
 const names={pending:'等待處理',running:'處理中',paused:'已暫停',review:'試跑待確認',complete:'已完成',error:'需要重試'};
 function text(id,value){el(id).textContent=value}
 function explainError(value){return /10061|ECONNREFUSED|connection refused/i.test(value||'')?'無法連線至 ComfyUI。請確認服務已啟動，並檢查模型設定中的位址。':value||''}
 function message(id,value,error=false){text(id,value);el(id).dataset.error=String(error)}
 function config(){const result={};for(const key of ['start_frame','frame_count','preview_frames','fps','comfy','steps','seed','prompt'])result[key]=el(key).value;return result}
 function renderPending(){
  el('pendingFiles').replaceChildren();text('pendingCount',pending.length?'已選 '+pending.length+' 支影片':'尚未選擇影片');
  for(const item of pending){const row=document.createElement('div'),title=document.createElement('strong'),state=document.createElement('small'),remove=document.createElement('button');row.className='pending-file';row.dataset.error=String(!!item.error);title.textContent=item.name;state.textContent=item.error||item.status||(item.file?(item.file.size/1024/1024).toFixed(1)+' MB':'使用原始路徑，不另外複製影片');remove.textContent='移除';remove.setAttribute('aria-label','移除待匯入影片 '+item.name);remove.disabled=importing;remove.onclick=()=>{pending=pending.filter(p=>p!==item);renderPending()};row.append(title,state,remove);el('pendingFiles').append(row)}
  el('enqueueStart').disabled=el('enqueueOnly').disabled=importing||!pending.length;el('clearPending').disabled=importing||!pending.length;el('upload').disabled=el('addSourcePath').disabled=el('queueMode').disabled=importing;
 }
 function addFiles(files){
  if(importing)return;let rejected=0,duplicate=0;
  for(const file of files){if(!extensions.test(file.name)){rejected++;continue}const key='file:'+file.name+':'+file.size+':'+file.lastModified;if(pending.some(p=>p.key===key)){duplicate++;continue}if(pending.length>=100){rejected++;continue}pending.push({key,file,name:file.name})}
  renderPending();message('importStatus',pending.length?'已選 '+pending.length+' 支。可繼續加入，或按「加入隊列並開始」。':'請選擇支援的影片檔案。');if(rejected||duplicate)message('importStatus',(rejected?rejected+' 個檔案不支援或超過本次 100 支上限。 ':'')+(duplicate?duplicate+' 個重複檔案未再次加入。 ':'')+'目前 '+pending.length+' 支。',!!rejected);
 }
 el('upload').onchange=()=>{addFiles(Array.from(el('upload').files));el('upload').value=''};
 const zone=el('videoDropzone');
 zone.addEventListener('dragover',event=>{event.preventDefault();if(!importing)zone.dataset.drag='true'});
 zone.addEventListener('dragleave',event=>{if(!zone.contains(event.relatedTarget))delete zone.dataset.drag});
 zone.addEventListener('drop',event=>{event.preventDefault();delete zone.dataset.drag;addFiles(Array.from(event.dataTransfer.files||[]))});
 // Dropping a file elsewhere in this dialog must not navigate away from the workbench.
 el('taskDrawer').addEventListener('dragover',event=>event.preventDefault());el('taskDrawer').addEventListener('drop',event=>event.preventDefault());
 el('clearPending').onclick=()=>{pending=[];renderPending();message('importStatus','已清空待匯入清單。已加入隊列的任務不受影響。')};
 el('addSourcePath').onclick=()=>{const source=el('source').value.trim().replace(/^"|"$/g,'');if(!source){message('importStatus','請貼上完整影片路徑。',true);return}if(!extensions.test(source)){message('importStatus','這個路徑不是支援的影片格式。',true);return}if(pending.length>=100){message('importStatus','單次最多匯入 100 支影片。',true);return}if(!pending.some(p=>p.key==='path:'+source))pending.push({key:'path:'+source,path:source,name:source.split(/[\\/]/).pop()});renderPending();message('importStatus','影片路徑已加入，準備好後開始批次處理。')};
 el('queueMode').onchange=()=>text('queueModeHint',el('queueMode').value==='full'?'依序完成每支影片，輸出透明 PNG 圖序與 WebM。':'依序試跑每支影片；完成後需逐一確認，才會處理各支剩餘影格。');
 async function enqueue(start){
  if(importing||!pending.length)return;importing=true;const batch=pending.slice(),settings=config(),mode=el('queueMode').value;let added=0,failed=0;renderPending();
  try{
   for(let index=0;index<batch.length;index++){
    const item=batch[index];item.error='';item.status='正在匯入…';renderPending();message('importStatus','正在匯入 '+(index+1)+' / '+batch.length+'：'+item.name);
    try{
     if(!item.path){const response=await fetch('/api/upload?name='+encodeURIComponent(item.file.name),{method:'POST',headers:{'X-Token':window.APP_TOKEN},body:item.file});const result=await response.json();if(!response.ok)throw Error(result.error||'複製影片失敗');item.path=result.path;item.info=result.info}
     if(!item.jobId){item.requestId??=crypto.randomUUID().replace(/-/g,'');item.settings??={...settings,original_name:item.name};item.mode??=mode;const job=await api('create',{source:item.path,config:item.settings,request_id:item.requestId});item.jobId=job.id}
     queueState=await api('queue/add',{job_ids:[item.jobId],mode:item.mode||mode});queueRevision++;pending=pending.filter(p=>p!==item);added++;renderQueue();
    }catch(error){item.error=error.message;item.status='';failed++}
    renderPending();
   }
   await refresh();
   if(start&&added){queueState=await api('queue/start',{});queueRevision++;renderQueue()}
   message('importStatus','已加入 '+added+' 支'+(start&&added?'，隊列已開始。':'。')+(failed?' '+failed+' 支匯入失敗，原因列在各影片下方，可修正後重試。':''),failed>0);
  }catch(error){message('importStatus','影片已保留；隊列未能啟動：'+error.message+'。可按右側「開始隊列」重試。',true)}
  finally{importing=false;renderPending();await pollQueue()}
 }
 el('enqueueStart').onclick=()=>enqueue(true);el('enqueueOnly').onclick=()=>enqueue(false);
 async function command(action,data={}){if(queueBusy)return;queueBusy=true;renderButtons();try{queueState=await api('queue/'+action,data);queueRevision++;renderQueue();await refresh()}catch(error){message('queueNotice',error.message,true)}finally{queueBusy=false;renderButtons()}}
 el('queueStart').onclick=()=>command('start');el('queuePause').onclick=()=>command('pause');
 function renderButtons(){const items=queueState?.items||[],waiting=items.some(i=>['pending','paused'].includes(i.state));el('queueStart').disabled=queueBusy||(!waiting&&!queueState?.active_job_id);el('queuePause').disabled=queueBusy||!queueState||queueState.paused;el('queueStart').textContent=queueState?.paused?'開始／繼續隊列':'繼續隊列'}
 async function openJob(id){const epoch=++openEpoch;await refresh();if(epoch!==openEpoch)return;selectJob(id);window.workbenchSelectTab?.('tabPlayback');window.workbenchCloseTasks?.()}
 function addAction(target,item,label,action,options={}){const button=document.createElement('button');button.textContent=label;button.dataset.queueAction=action+(options.direction?':'+options.direction:'');button.setAttribute('aria-label',label+'：'+item.name);button.onclick=()=>action==='open'?openJob(item.job_id):command(action,{job_id:item.job_id,...options});target.append(button)}
 function renderQueue(){
  if(!queueState)return;const items=queueState.items||[],active=items.find(i=>i.job_id===queueState.active_job_id)||items.find(i=>i.state==='running'),waiting=items.filter(i=>['pending','paused'].includes(i.state)).length,done=items.filter(i=>i.state==='complete').length,review=items.filter(i=>i.state==='review').length,errors=items.filter(i=>i.state==='error').length;
  text('queueBadge',String(waiting+(active?1:0)));el('openTasks').setAttribute('aria-label','任務隊列，'+waiting+' 支等待'+(active?'，1 支處理中':''));
  text('queueSummary',items.length?done+' 支完成 · '+waiting+' 支等待'+(review?' · '+review+' 支待確認':'')+(errors?' · '+errors+' 支需重試':''):'尚無排隊任務，從左側匯入影片開始。');text('queueState',queueState.paused?'已暫停':active?'正在處理':waiting?'等待處理':'隊列閒置');
  text('queueMini',active?'隊列：'+active.name:waiting?'隊列 '+waiting+' 支'+(queueState.paused?' · 已暫停':' · 等待處理'):'');
  message('queueNotice',queueState.error||(!items.length?'':queueState.phase||(queueState.paused?'隊列已暫停。點「開始／繼續隊列」接著處理。':'按順序逐支處理，單支失敗不會阻止後續影片。')),!!queueState.error);
  const signature=JSON.stringify(items.map(i=>[i.job_id,i.name,i.state,i.mode]));
  if(signature!==queueSignature){queueSignature=signature;const focused=document.activeElement,focusJob=focused?.closest('[data-queue-job]')?.dataset.queueJob,focusAction=focused?.dataset.queueAction;el('queueList').replaceChildren();
   if(!items.length){const empty=document.createElement('div');empty.className='queue-empty';empty.textContent='先選影片 → 加入隊列 → 依序去背\n完成後即可檢查與修整';el('queueList').append(empty)}
   for(const [index,item] of items.entries()){const card=document.createElement('article');card.className='queue-card';card.dataset.queueJob=item.job_id;card.dataset.state=item.state;const heading=document.createElement('div');heading.className='queue-card-head';const name=document.createElement('strong'),state=document.createElement('span');name.textContent=(index+1)+'. '+item.name;state.textContent=names[item.state]||item.state;heading.append(name,state);const progress=document.createElement('progress');progress.setAttribute('aria-label',item.name+' 去背進度');const detail=document.createElement('p');detail.dataset.queueDetail='true';const error=document.createElement('p');error.className='queue-error';error.dataset.queueError='true';const actions=document.createElement('div');actions.className='buttons';addAction(actions,item,'檢查素材','open');if(item.state==='review')addAction(actions,item,'確認效果，排入全片去背','retry',{mode:'full'});if(['error','paused'].includes(item.state))addAction(actions,item,'重新排隊','retry');if(item.state==='pending'){addAction(actions,item,'提前','move',{direction:'up'});addAction(actions,item,'延後','move',{direction:'down'})}if(item.state!=='running')addAction(actions,item,'移出隊列','remove');card.append(heading,progress,detail,error,actions);el('queueList').append(card)}
   if(focusJob&&focusAction)el('queueList').querySelector('[data-queue-job="'+focusJob+'"] [data-queue-action="'+focusAction+'"]')?.focus({preventScroll:true});
  }
  for(const item of items){const card=el('queueList').querySelector('[data-queue-job="'+item.job_id+'"]');if(!card)continue;const progress=card.querySelector('progress');progress.max=Math.max(1,item.total||1);progress.value=item.done||0;progress.hidden=!item.total;card.querySelector('[data-queue-detail]').textContent=(item.mode==='preview'?'試跑':'整段')+' · '+(item.total?(item.done||0)+' / '+item.total+' 幀 · ':'')+(item.state==='error'?'處理失敗，可修正後重新排隊':item.phase||'等待開始');const errorNode=card.querySelector('[data-queue-error]');errorNode.textContent=explainError(item.error);errorNode.title=item.error||''}
  renderButtons();window.dispatchEvent(new CustomEvent('workbench:queue',{detail:queueState}));
 }
 async function pollQueue(){clearTimeout(pollTimer);if(polling)return;polling=true;const revision=queueRevision;try{const state=await api('queue');if(revision===queueRevision){queueState=state;renderQueue()}}catch(error){if(revision===queueRevision)message('queueNotice','無法讀取隊列：'+error.message,true)}finally{polling=false;pollTimer=setTimeout(pollQueue,1800)}}
 renderPending();pollQueue();window.addEventListener('pagehide',()=>clearTimeout(pollTimer));
})();
