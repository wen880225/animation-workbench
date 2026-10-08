/* One repair action. All playback remains owned by the normal draft renderer. */
(()=>{
 'use strict';
 const $=id=>document.getElementById(id),button=$('trRepair'),status=$('trRepairStatus');
 if(!button)return;
 let busy=false,notice='',noticeProject='',serial=0;
 const signatures=new Map();
 const stabilityText=p=>{
  const records=p?.seam_repair?.stability?.clips;if(!records)return '這份修復尚未做整段比例穩定，可重新修復。';
  const values=Object.values(records),fixed=values.filter(v=>v.status==='applied').length;
  const warnings=Object.entries(records).filter(([,v])=>v.status==='review'||v.warning).map(([key,v])=>(p.clips.find(c=>c.key===key)?.label||'動畫')+'：'+(v.reason||v.warning));
  return '整段比例：'+fixed+' 段已穩定。'+(warnings.length?'比例待檢查 '+warnings.length+' 段。'+warnings.join('；'):'其餘未量到需要修正的漂移。');
 };
 const signature=p=>JSON.stringify({sequence:p.sequence,route_mode:p.route_mode,shared_tone:p.shared_tone,tone_match:p.tone_match,clips:p.clips.map(({label,...c})=>c),seam_closure:p.seam_closure});
 window.seamRepairUI={
  changed(p){if(!p?.seam_repair)return;const id=p.seam_repair.id,old=signatures.get(id);if(old&&old!==signature(p)){delete p.seam_repair;notice='設定已變更，請重新修復後再驗收。';noticeProject=p.id}},
  update(p,locked=false){
   if(p?.seam_repair)signatures.set(p.seam_repair.id,signature(p));
   button.disabled=busy||locked||!p?.clips?.length;
   button.textContent=busy?'正在重建接縫…':p?.seam_repair?'重新修復全部接點':'一鍵修復全部接點';
   status.textContent=noticeProject===p?.id&&notice?notice:p?.seam_repair?'已套用 '+p.seam_repair.pairs.length+' 個接點。'+stabilityText(p)+' 慢播與輸出使用同一份修復影格。':'先穩定整段比例，再重建接點過渡；保持解析度，原始素材保留。';
  }
 };
 button.onclick=async()=>{
  const bridge=window.workbenchRepair,owner=bridge?.snapshot();if(!owner?.project||busy)return;
  busy=true;noticeProject=owner.project.id;notice='正在準備本地修復…';const token=++serial;bridge.busy(true);
  try{
   const version=await api('version');if(Number(version.features?.seam_repair)<2||!version.features?.seam_repair)throw Error('請重新啟動 v3.12 工作台後使用一鍵修復；目前服務仍是舊版。');
   let result=await api('transitions/repair_start',{project:owner.project});
   while(result.state==='running'){
    notice=result.phase+'（'+(result.done||0)+'／'+(result.total||0)+' '+(result.unit||'個接點')+'）';bridge.refresh();
    await new Promise(resolve=>setTimeout(resolve,500));
    result=await api('transitions/repair_status',{id:result.id,project_id:owner.project.id});
   }
   if(result.state!=='complete')throw Error(result.phase||'修復未完成，原方案保留');
   if(token!==serial||!bridge.belongs(owner))throw Error('修復期間專案已變更，結果未覆蓋目前草稿。');
   const p=result.result.project;signatures.set(p.seam_repair.id,signature(p));
   notice='已套用 '+p.seam_repair.pairs.length+' 個接點。請按「0.1× 慢播修復結果」驗收；可用復原返回原方案。';
   bridge.adopt(p);await bridge.save();notice="";
  }catch(error){notice=error.message;bridge.message(error.message,true)}
  finally{busy=false;bridge.busy(false);bridge.refresh()}
 };
 window.workbenchRepair?.refresh();
})();
