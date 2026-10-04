/* Shared, canvas-relative selection editor. The overlay is never part of an export. */
(()=>{
 'use strict';
 const clamp=(v,a,b)=>Math.max(a,Math.min(b,Number(v)||0));
 const copy=value=>value?JSON.parse(JSON.stringify(value)):undefined;
 const identity=()=>({dx:0,dy:0,sx:100,sy:100,angle:0,cx:50,cy:50});
 const affineFields=[['dx','左右（px）',-100,100],['dy','上下（px）',-100,100],['sx','寬度（%）',80,120],['sy','高度（%）',80,120],['angle','旋轉（°）',-10,10],['cx','中心 X（%）',0,100],['cy','中心 Y（%）',0,100]];
 const defaults=()=>({mode:'inside',shape:'rect',x:25,y:25,w:50,h:50,feather:10});
 class RegionEditor{
  static identity(){return identity()}
  static transform(value){const result=identity();for(const[key,,min,max]of affineFields)if(value?.[key]!==undefined&&Number.isFinite(Number(value[key])))result[key]=clamp(value[key],min,max);return result}
  static changeMode(value,mode,transform){
   const region=RegionEditor.normalize(value)||defaults(),affine=RegionEditor.transform(transform);let next=affine;
   if(mode==='split'&&!region.outside){if(region.mode==='outside'){region.outside=affine;next=identity()}else region.outside=!value||region.mode==='all'?{...affine}:identity()}
   region.mode=mode;return {region:RegionEditor.normalize(region),transform:next};
  }
  static normalize(value){
   if(!value||typeof value!=='object'||Array.isArray(value))return undefined;
   const r={...defaults(),...value};r.mode=['all','inside','outside','split','edge'].includes(r.mode)?r.mode:'all';r.shape=r.shape==='ellipse'?'ellipse':'rect';
   r.x=clamp(r.x,0,99.99);r.y=clamp(r.y,0,99.99);r.w=clamp(r.w,.01,100-r.x);r.h=clamp(r.h,.01,100-r.y);r.feather=clamp(r.feather,0,50);
   if(r.mode==='split'||r.outside)r.outside=RegionEditor.transform(r.outside);
   if(r.mode==='edge'||['edges','edge_left','edge_right'].some(k=>Object.hasOwn(r,k))){r.edges=['left','right','both'].includes(r.edges)?r.edges:'both';for(const key of ['edge_left','edge_right'])r[key]=Number.isFinite(Number(r[key]))&&r[key]!==null&&r[key]!==''?clamp(r[key],1,45):12}
   return Object.fromEntries(Object.entries(r).filter(([k])=>['mode','shape','x','y','w','h','feather','outside','edges','edge_left','edge_right'].includes(k)));
  }
  static edgeDrag(value,handle,delta){const r=RegionEditor.normalize(value);if(!r||r.mode!=='edge')return r;if(handle==='edge-left'&&r.edges!=='right')r.edge_left=clamp(r.edge_left+delta,1,45);if(handle==='edge-right'&&r.edges!=='left')r.edge_right=clamp(r.edge_right-delta,1,45);return r}
  constructor(options){
   this.options=options;this.canvas=options.canvas;this.container=options.container;this.active=false;this.gesture=null;this.session=false;this.context='';this.space=false;
   if(!this.container||!this.canvas)return;
   const p=options.prefix||'region';this.container.classList.add('region-controls');
   this.container.innerHTML=`<div class="region-heading"><h4>局部變形範圍</h4><span class="region-state" role="status"></span></div>
    <label>套用範圍<select data-region="mode" id="${p}Mode"><option value="legacy">沿用目前方案</option><option value="all">整個畫面</option><option value="split">框內／框外分別調整</option><option value="edge">貼邊固定（中間修整）</option><option value="inside">只修框內</option><option value="outside">保護框內</option></select></label>
    <div class="region-edge-options" hidden><label>固定哪一側<select data-region="edges" id="${p}Edges"><option value="both">左右兩側</option><option value="left">只固定左側</option><option value="right">只固定右側</option></select></label><div class="control-grid"><label class="region-edge-left">左側過渡寬度（畫布 %）<input data-region="edge_left" id="${p}EdgeLeft" type="number" min="1" max="45" step="0.1" value="12"></label><label class="region-edge-right">右側過渡寬度（畫布 %）<input data-region="edge_right" id="${p}EdgeRight" type="number" min="1" max="45" step="0.1" value="12"></label></div><p class="hint region-edge-pixels"></p></div>
    <div class="region-toolbar"><label>形狀<select data-region="shape" id="${p}Shape"><option value="rect">矩形</option><option value="ellipse">橢圓</option></select></label><button type="button" class="quiet region-tool" aria-pressed="false">框選範圍</button></div>
    <div class="control-grid region-numbers">${[['x','左側 X'],['y','上方 Y'],['w','寬度'],['h','高度']].map(([key,label])=>`<label>${label}（%）<input data-region="${key}" id="${p}${key.toUpperCase()}" type="number" min="${key==='w'||key==='h'?'.01':'0'}" max="100" step="0.1"></label>`).join('')}</div>
    <label>羽化（範圍短邊 %）<input data-region="feather" id="${p}Feather" type="number" min="0" max="50" step="1" value="10"></label>
    <p class="hint region-help"></p><p class="hint region-scope">僅限制位移、縮放與旋轉；明暗仍整段套用。範圍線不會輸出。</p>`;
   this.inputs=Object.fromEntries([...this.container.querySelectorAll('[data-region]')].map(node=>[node.dataset.region,node]));this.tool=this.container.querySelector('.region-tool');this.help=this.container.querySelector('.region-help');this.state=this.container.querySelector('.region-state');this.edgeOptions=this.container.querySelector('.region-edge-options');this.edgePixels=this.container.querySelector('.region-edge-pixels');this.rectangleControls=[this.inputs.shape.closest('label'),this.container.querySelector('.region-numbers'),this.inputs.feather.closest('label')];
   this.outsideContainer=options.outsideContainer||document.createElement('div');if(!options.outsideContainer)this.container.append(this.outsideContainer);this.outsideContainer.classList.add('region-outside');
   this.outsideContainer.innerHTML=`<div class="region-heading"><h4>框外變形</h4><button type="button" class="quiet region-reset-outside">重設框外</button></div><p class="hint">與框內獨立設定，共用相同漸進幀數。100% 為原尺寸，0 為不位移／不旋轉。</p><div class="control-grid">${affineFields.map(([key,label,min,max])=>`<label>框外${label}<input data-outside="${key}" id="${p}Outside${key[0].toUpperCase()+key.slice(1)}" type="number" min="${min}" max="${max}" step="0.1"></label>`).join('')}</div>`;
   this.outsideInputs=Object.fromEntries([...this.outsideContainer.querySelectorAll('[data-outside]')].map(node=>[node.dataset.outside,node]));this.outsideReset=this.outsideContainer.querySelector('.region-reset-outside');
   for(const[key,input]of Object.entries(this.outsideInputs)){input.addEventListener('focus',()=>this.session=false);input.addEventListener('blur',()=>{this.session=false;input.value=this.value()?.outside?.[key]??identity()[key];this.sync()});input.addEventListener('input',()=>{if(!this.ready()||input.value===''||!Number.isFinite(Number(input.value)))return;const r=this.value();if(r?.mode!=='split')return;if(!this.session){options.onBegin?.();this.session=true}this.commit({...r,outside:{...r.outside,[key]:Number(input.value)}})});input.addEventListener('change',()=>{this.session=false;input.value=this.value()?.outside?.[key]??identity()[key];this.sync()})}
   this.outsideReset.onclick=()=>{const r=this.value();if(!this.ready()||r?.mode!=='split')return;options.onBegin?.();this.commit({...r,outside:identity()})};
   for(const[key,input]of Object.entries(this.inputs)){
    input.addEventListener('focus',()=>this.session=false);input.addEventListener('blur',()=>{this.session=false;if(key.startsWith('edge_'))input.value=this.value()?.[key]??12;this.sync()});
    const change=()=>{if(!this.ready()||input.disabled||input.value===''||key==='mode'&&input.value==='legacy')return;let r=this.value()||defaults(),transform;if(key==='mode'){const changed=RegionEditor.changeMode(this.value(),input.value,options.getTransform?.());r=changed.region;transform=changed.transform}else r={...r,[key]:['shape','edges'].includes(key)?input.value:Number(input.value)};if(!this.session){options.onBegin?.();this.session=true}this.commit(r,transform);if(key==='mode'&&r.mode==='all')this.deactivate();};
    input.addEventListener(input.tagName==='SELECT'?'change':'input',change);input.addEventListener('change',()=>{this.session=false;this.sync()});
   }
   this.tool.onclick=()=>{if(this.active){this.deactivate();return}if(!this.ready())return;options.onActivate?.();this.active=true;const r=this.value();if(!r||r.mode==='all'){options.onBegin?.();this.commit({...r,...(!r?defaults():{}),mode:'inside'})}this.sync();options.onDraw?.()};
   this.canvas.addEventListener('pointerdown',e=>this.down(e),true);this.canvas.addEventListener('pointermove',e=>this.move(e),true);this.canvas.addEventListener('pointerup',e=>this.up(e),true);this.canvas.addEventListener('pointercancel',e=>this.up(e),true);this.canvas.addEventListener('lostpointercapture',()=>{this.gesture=null},true);
   window.addEventListener('keydown',e=>{if(e.code==='Space')this.space=true;if(e.key==='Escape'&&this.active){e.preventDefault();this.deactivate()}});window.addEventListener('keyup',e=>{if(e.code==='Space')this.space=false});window.addEventListener('blur',()=>{this.space=false;this.deactivate()});this.sync();
  }
  value(){return RegionEditor.normalize(this.options.getValue?.())}
  ready(){return !!this.options.isReady?.()}
  commit(value,transform){const r=RegionEditor.normalize(value);this.options.onChange?.(copy(r),transform);this.sync();this.options.onDraw?.()}
  sync(){
   if(!this.container)return;const context=String(this.options.getContext?.()||'');if(this.context!==context){this.context=context;this.deactivate(false)}
   const ready=this.ready(),r=this.value(),shown=r||defaults();if((!ready||!r||r.mode==='all')&&this.active)this.deactivate(false);
   for(const[key,input]of Object.entries(this.inputs)){input.disabled=!ready;if(document.activeElement!==input)input.value=key==='mode'&&!r?'legacy':typeof shown[key]==='number'?Math.round(shown[key]*1000)/1000:shown[key]??(key==='edges'?'both':key.startsWith('edge_')?12:'')}
   const edge=r?.mode==='edge';this.edgeOptions.hidden=!edge;for(const node of this.rectangleControls)node.hidden=edge;this.inputs.edge_left.disabled=!ready||!edge||r.edges==='right';this.inputs.edge_right.disabled=!ready||!edge||r.edges==='left';
   const canvasWidth=this.canvas.width||0;this.edgePixels.textContent=edge&&canvasWidth>1?`畫布寬 ${canvasWidth} px · `+[r.edges!=='right'?`左側約 ${Math.round(canvasWidth*r.edge_left/100)} px`:null,r.edges!=='left'?`右側約 ${Math.round(canvasWidth*r.edge_right/100)} px`:null].filter(Boolean).join('／'):'過渡寬度以原始畫布寬度計算，放大預覽不會改變設定。';
   this.outsideContainer.hidden=r?.mode!=='split';this.outsideReset.disabled=!ready;for(const[key,input]of Object.entries(this.outsideInputs)){input.disabled=!ready;if(document.activeElement!==input)input.value=shown.outside?.[key]??identity()[key]}
   this.options.onSync?.(r);
   this.inputs.mode.querySelector('[value="legacy"]').hidden=!!r;this.tool.disabled=!ready;this.tool.textContent=this.active?(edge?'完成調整':'完成框選'):(edge?'調整過渡範圍':'框選範圍');this.tool.setAttribute('aria-pressed',String(this.active));this.container.dataset.active=String(this.active);this.container.dataset.mode=r?.mode||'legacy';
   this.state.textContent=this.active?(edge?'正在調整過渡':'正在框選'):!r?'沿用原設定':r.mode==='all'?'全部':edge?'貼邊保留':r.mode==='split'?'兩區獨立':r.mode==='inside'?'框內修整':'框內保護';
   this.help.textContent=!ready?'先載入可比較的影格，才能設定範圍。':edge?(this.active?'拖曳青色／橘色直線調整過渡寬度；按「完成調整」恢復拖曳比對。 ':'')+'所選畫布邊緣保持原樣，向內平滑過渡至中間修整量。僅保留原有邊緣，不能自動修好來源首尾不一致。':this.active?'拖空白處建立範圍；拖框內移動，拖控制點或邊線縮放。完成框選後恢復拖曳比對。':!r?(this.options.getLegacy?.()||'目前沒有局部範圍。使用框選後才會改用新設定。'):r.mode==='all'?'目前變形套用整個畫面；範圍形狀保留供下次使用。':r.mode==='split'?'框內與框外可分別微調。邊界內側羽化，由框外數值平滑過渡至框內數值；不會改變畫布大小。':r.mode==='inside'?'實線內側逐漸套用變形，框外保持原樣；虛線示意羽化過渡邊界。':'實線內完全保護，框外逐漸套用變形；虛線示意羽化過渡邊界。';
   if(this.active)this.canvas.style.cursor=edge?'default':'crosshair';
  }
  deactivate(redraw=true){const was=this.active;this.active=false;this.gesture=null;this.session=false;if(was&&this.canvas)this.canvas.style.cursor='';if(this.tool){this.tool.textContent='框選範圍';this.tool.setAttribute('aria-pressed','false');this.container.dataset.active='false'}if(redraw){this.sync();this.options.onDraw?.()}}
  point(e){const rect=this.canvas.getBoundingClientRect();return {x:clamp((e.clientX-rect.left)/rect.width*100,0,100),y:clamp((e.clientY-rect.top)/rect.height*100,0,100),rect}}
  hit(point,r){
   if(r.mode==='edge'){const tolerance=9/point.rect.width*100;if(r.edges!=='right'&&Math.abs(point.x-r.edge_left)<=tolerance)return 'edge-left';if(r.edges!=='left'&&Math.abs(point.x-(100-r.edge_right))<=tolerance)return 'edge-right';return null}
   const tx=9/point.rect.width*100,ty=9/point.rect.height*100,px=point.x,py=point.y;const xs=[r.x,r.x+r.w/2,r.x+r.w],ys=[r.y,r.y+r.h/2,r.y+r.h];
   for(const [x,y,handle]of [[0,0,'nw'],[1,0,'n'],[2,0,'ne'],[2,1,'e'],[2,2,'se'],[1,2,'s'],[0,2,'sw'],[0,1,'w']])if(Math.abs(px-xs[x])<=tx&&Math.abs(py-ys[y])<=ty)return handle;
   if(py>=r.y-ty&&py<=r.y+r.h+ty){if(Math.abs(px-r.x)<=tx)return 'w';if(Math.abs(px-r.x-r.w)<=tx)return 'e'}
   if(px>=r.x-tx&&px<=r.x+r.w+tx){if(Math.abs(py-r.y)<=ty)return 'n';if(Math.abs(py-r.y-r.h)<=ty)return 's'}
   if(r.shape==='ellipse'){if(((px-r.x-r.w/2)/(r.w/2))**2+((py-r.y-r.h/2)/(r.h/2))**2<=1)return 'move'}else if(px>=r.x&&px<=r.x+r.w&&py>=r.y&&py<=r.y+r.h)return 'move';return 'draw';
  }
  consume(e){e.preventDefault();e.stopImmediatePropagation()}
  down(e){if(!this.active||!this.ready()||e.button!==0||this.space)return;this.consume(e);const point=this.point(e),r=this.value()||defaults(),handle=this.hit(point,r);if(!handle)return;this.options.onBegin?.();this.gesture={point,r,handle,id:e.pointerId,context:this.context};this.canvas.setPointerCapture(e.pointerId)}
  move(e){
   if(!this.active||!this.gesture&&(this.space||(e.buttons&4)))return;
   if(!this.gesture){if(this.ready()){const handle=this.hit(this.point(e),this.value()||defaults());this.canvas.style.cursor=!handle?'default':handle.startsWith('edge-')?'ew-resize':handle==='move'?'move':handle==='draw'?'crosshair':handle+'-resize';this.consume(e)}return}
   this.consume(e);const g=this.gesture;if(g.id!==e.pointerId||g.context!==String(this.options.getContext?.()||'')){this.deactivate();return}const p=this.point(e),r={...g.r},dx=p.x-g.point.x,dy=p.y-g.point.y,minW=Math.min(1,100/this.canvas.width),minH=Math.min(1,100/this.canvas.height);
   if(g.r.mode==='edge'){this.commit(RegionEditor.edgeDrag(g.r,g.handle,dx));return}
   if(g.handle==='move'){r.x=clamp(g.r.x+dx,0,100-r.w);r.y=clamp(g.r.y+dy,0,100-r.h)}
   else if(g.handle==='draw'){r.x=Math.min(g.point.x,p.x);r.y=Math.min(g.point.y,p.y);r.w=Math.max(minW,Math.abs(p.x-g.point.x));r.h=Math.max(minH,Math.abs(p.y-g.point.y));r.x=Math.min(r.x,100-r.w);r.y=Math.min(r.y,100-r.h)}
   else{let left=g.r.x,top=g.r.y,right=left+g.r.w,bottom=top+g.r.h;if(g.handle.includes('w'))left=clamp(left+dx,0,right-minW);if(g.handle.includes('e'))right=clamp(right+dx,left+minW,100);if(g.handle.includes('n'))top=clamp(top+dy,0,bottom-minH);if(g.handle.includes('s'))bottom=clamp(bottom+dy,top+minH,100);Object.assign(r,{x:left,y:top,w:right-left,h:bottom-top})}this.commit(r);
  }
  up(e){if(!this.gesture||this.gesture.id!==e.pointerId)return;this.consume(e);this.gesture=null;if(this.canvas.hasPointerCapture(e.pointerId))this.canvas.releasePointerCapture(e.pointerId);this.sync();this.options.onDraw?.()}
  draw(ctx,w,h){
   this.sync();const r=this.value();if(!this.ready()||!r||r.mode==='all')return;const rect=this.canvas.getBoundingClientRect(),scale=Math.max(.001,rect.width/w),x=r.x*w/100,y=r.y*h/100,rw=r.w*w/100,rh=r.h*h/100,feather=Math.min(rw,rh)*r.feather/100,sign=r.mode==='inside'||r.mode==='split'?-1:1;
   if(r.mode==='edge'){this.drawEdges(ctx,w,h,r,scale);return}
   const path=(px,py,pw,ph)=>{if(pw<=0||ph<=0)return;if(r.shape==='ellipse')ctx.ellipse(px+pw/2,py+ph/2,pw/2,ph/2,0,0,Math.PI*2);else ctx.rect(px,py,pw,ph)};
   ctx.save();ctx.globalAlpha=1;ctx.globalCompositeOperation='source-over';ctx.lineWidth=2/scale;
   if(feather>0){ctx.beginPath();path(x,y,rw,rh);path(x-sign*feather,y-sign*feather,rw+sign*feather*2,rh+sign*feather*2);ctx.fillStyle='rgba(46,211,219,.12)';ctx.fill('evenodd');ctx.beginPath();path(x-sign*feather,y-sign*feather,Math.max(0,rw+sign*feather*2),Math.max(0,rh+sign*feather*2));ctx.setLineDash([5/scale,4/scale]);ctx.strokeStyle='#6ee7ed';ctx.stroke();ctx.setLineDash([])}
   for(const[color,width]of [['#071a23',4],['#6ee7ed',2]]){ctx.beginPath();path(x,y,rw,rh);ctx.strokeStyle=color;ctx.lineWidth=width/scale;ctx.stroke()}
   if(this.active){const size=8/scale;ctx.fillStyle='#fff';ctx.strokeStyle='#071a23';ctx.lineWidth=1.5/scale;for(const[px,py]of [[x,y],[x+rw/2,y],[x+rw,y],[x+rw,y+rh/2],[x+rw,y+rh],[x+rw/2,y+rh],[x,y+rh],[x,y+rh/2]]){ctx.fillRect(px-size/2,py-size/2,size,size);ctx.strokeRect(px-size/2,py-size/2,size,size)}}ctx.restore();
  }
  drawEdges(ctx,w,h,r,scale){
   ctx.save();ctx.globalAlpha=1;ctx.globalCompositeOperation='source-over';ctx.setLineDash([]);
   for(const side of ['left','right']){if(r.edges!=='both'&&r.edges!==side)continue;const left=side==='left',width=w*r[left?'edge_left':'edge_right']/100,boundary=left?width:w-width,color=left?'#6ee7ed':'#ffc078';ctx.fillStyle=left?'rgba(46,211,219,.16)':'rgba(255,177,94,.16)';ctx.fillRect(left?0:w-width,0,width,h);
    for(const[stroke,line]of [['#071a23',4],[color,2]]){ctx.strokeStyle=stroke;ctx.lineWidth=line/scale;ctx.beginPath();ctx.moveTo(boundary,0);ctx.lineTo(boundary,h);ctx.stroke()}
    if(this.active){const sw=12/scale,sh=36/scale;ctx.fillStyle='#071a23';ctx.fillRect(boundary-sw/2,h/2-sh/2,sw,sh);ctx.strokeStyle=color;ctx.lineWidth=2/scale;ctx.strokeRect(boundary-sw/2,h/2-sh/2,sw,sh);ctx.beginPath();ctx.moveTo(boundary,h/2-8/scale);ctx.lineTo(boundary,h/2+8/scale);ctx.stroke()}
   }ctx.restore();
  }
 }
 window.RegionEditor=RegionEditor;
})();
