'use strict';
window.ipGrowthGraph=(()=>{
  const remembered=new Map();
  function mount(ctx){
    const {graphData,data,query,url,e,icon,tagPath,pillarIcon,goalCount,api}=ctx;
    const svg=document.getElementById('growth-graph'),detail=document.getElementById('g-graph-detail'),status=document.getElementById('g-graph-count');
    const abort=new AbortController(),signal=abort.signal,NS='http://www.w3.org/2000/svg';
    const reduced=matchMedia('(prefers-reduced-motion: reduce)'),mobileMedia=matchMedia('(max-width: 600px)');
    let coarse=mobileMedia.matches;
    let paused=reduced.matches,active=true,simulation,scale=1,tx=0,ty=0,selection='',nodes=[],links=[],elements=[],edges=[],mode=query.get('goal')?'all':'overview',focusBranch='';
    const allNodes=graphData.nodes.map(n=>({...n})),lookup=new Map(allNodes.map(n=>[n.id,n]));
    const add=(tag,attrs,parent)=>{const el=document.createElementNS(NS,tag);for(const [k,v]of Object.entries(attrs))el.setAttribute(k,v);parent.appendChild(el);return el;};
    const viewport=add('g',{},svg);
    const radius=n=>n.kind==='fragment'?(coarse?9:7):n.level==='root'?(coarse?39:29):n.level==='pillar'?(coarse?27:20):(coarse?15:11);
    const graphRoot=allNodes.find(n=>n.level==='root'),pillars=allNodes.filter(n=>n.level==='pillar');
    const anchors=new Map();if(graphRoot)anchors.set(graphRoot.id,{x:500,y:330});
    pillars.forEach((p,i)=>{const angle=-Math.PI/2+i*2*Math.PI/Math.max(1,pillars.length);anchors.set(p.id,{x:500+205*Math.cos(angle),y:330+190*Math.sin(angle)});});
    function anchor(n,seen=new Set()){
      if(anchors.has(n.id))return anchors.get(n.id);if(seen.has(n.id))return {x:500,y:330};seen.add(n.id);
      if(n.kind==='fragment'){
        const parents=(n.goal_ids||[]).map(id=>lookup.get(id)).filter(Boolean);if(!parents.length)return {x:130,y:530};
        const points=parents.map(p=>anchor(p,new Set(seen)));return {x:points.reduce((s,p)=>s+p.x,0)/points.length,y:points.reduce((s,p)=>s+p.y,0)/points.length};
      }
      const p=lookup.get(n.parent_id);return p?anchor(p,seen):{x:500,y:330};
    }
    function transformed(){viewport.setAttribute('transform',`translate(${tx} ${ty}) scale(${scale})`);svg.dataset.zoom=scale<.8?'far':scale>1.45?'near':'middle';}
    function highlight(id){
      const connected=new Set(id?[id]:[]);
      for(const link of links)if(link.source.id===id||link.target.id===id){connected.add(link.source.id);connected.add(link.target.id);}
      for(const {n,g}of elements){g.classList.toggle('g-faded',!!id&&!connected.has(n.id));g.classList.toggle('g-related',!!id&&connected.has(n.id));g.classList.toggle('g-selected-node',n.id===selection);}
      for(const {link,el}of edges){const near=link.source.id===id||link.target.id===id;el.classList.toggle('g-faded',!!id&&!near);el.classList.toggle('g-related',!!id&&near);}
    }
    function paint(){
      if(!active)return;
      for(const n of nodes){n.x=Math.max(coarse?180:45,Math.min(coarse?820:955,n.x));n.y=Math.max(coarse?90:45,Math.min(coarse?570:615,n.y));remembered.set(n.id,{x:n.x,y:n.y});}
      for(const {n,g}of elements)g.setAttribute('transform',`translate(${n.x} ${n.y})`);
      for(const {link,el}of edges){el.setAttribute('x1',link.source.x);el.setAttribute('y1',link.source.y);el.setAttribute('x2',link.target.x);el.setAttribute('y2',link.target.y);}
    }
    function pauseLabel(){const button=document.querySelector('[data-g=graph-pause]');button.innerHTML=icon(paused?'play':'pause')+(paused?'开启动效':'暂停动效');button.setAttribute('aria-pressed',String(paused));svg.dataset.motion=paused?'paused':'settling';}
    function selectedIds(){
      if(mode==='overview')return new Set(allNodes.filter(n=>n.level==='root'||n.level==='pillar').map(n=>n.id));
      if(!focusBranch)return new Set(allNodes.map(n=>n.id));
      const branch=new Set([focusBranch]);let size;
      do{size=branch.size;for(const n of allNodes)if(branch.has(n.parent_id))branch.add(n.id);}while(size!==branch.size);
      const ids=new Set(branch);
      for(const n of allNodes)if(n.kind==='fragment'&&n.goal_ids.some(id=>branch.has(id))){ids.add(n.id);for(const id of n.goal_ids)if(lookup.has(id))ids.add(id);}
      for(const key of [...ids]){let n=lookup.get(key);const seen=new Set();while(n?.parent_id&&!seen.has(n.id)){seen.add(n.id);ids.add(n.parent_id);n=lookup.get(n.parent_id);}}
      return ids;
    }
    function rebuild(){
      simulation?.stop();svg.setAttribute('viewBox',coarse?'160 60 680 540':'0 0 1000 660');const ids=selectedIds(),budget=coarse?100:200;
      nodes=allNodes.filter(n=>ids.has(n.id)).slice(0,budget).map((n,i)=>{const a=anchor(n),saved=remembered.get(n.id),angle=i*2.399963;return {...n,x:saved?.x??a.x+Math.cos(angle)*28,y:saved?.y??a.y+Math.sin(angle)*28};});
      const shown=new Set(nodes.map(n=>n.id));links=graphData.edges.filter(l=>shown.has(l.source)&&shown.has(l.target)).map(l=>({...l}));
      const root=nodes.find(n=>n.level==='root');if(root){root.fx=500;root.fy=330;}
      viewport.replaceChildren();edges=links.map(link=>({link,el:add('line',{'class':'g-graph-edge'},viewport)}));
      elements=nodes.map(n=>{
        const g=add('g',{'class':'g-graph-node '+n.kind+' '+(n.level||''),tabindex:0,role:'button','aria-label':n.title,'data-node-id':n.id},viewport);
        add('circle',{r:coarse?Math.max(66,radius(n)):Math.max(22,radius(n)),fill:'transparent',stroke:'none','class':'g-node-hit'},g);
        add('circle',{r:radius(n),fill:n.kind==='fragment'?'var(--surface)':n.level==='root'?'var(--gold)':n.color||'var(--gold)',stroke:n.kind==='fragment'?'var(--gold)':'var(--surface)','stroke-width':n.kind==='fragment'?1.7:2,'data-core':'true'},g);
        const label=add('text',{y:radius(n)+20,'text-anchor':'middle'},g);label.textContent=n.title.length>15?n.title.slice(0,14)+'…':n.title;
        const title=add('title',{},g);title.textContent=n.title;return {n,g};
      });
      const d=window.d3;
      simulation=d.forceSimulation(nodes).stop().force('link',d.forceLink(links).id(n=>n.id).distance(l=>l.target.kind==='fragment'?70:l.target.level==='pillar'?195:92).strength(.24))
        .force('charge',d.forceManyBody().strength(n=>n.kind==='fragment'?-75:-340).distanceMax(600))
        .force('collide',d.forceCollide().radius(n=>radius(n)+16).iterations(2))
        .force('x',d.forceX(n=>anchor(n).x).strength(.055)).force('y',d.forceY(n=>anchor(n).y).strength(.055)).alphaDecay(.045).velocityDecay(.36)
        .on('tick',paint).on('end',()=>{svg.dataset.motion=paused?'paused':'settled';});
      if(paused){for(let i=0;i<90;i++)simulation.tick();}else if(!document.hidden)simulation.alpha(.65).restart();
      paint();transformed();highlight(selection);pauseLabel();
      const prefix=mode==='overview'?'主线视图':focusBranch?'分支视图':'当前范围';
      status.textContent=`${prefix} · 显示 ${nodes.length} / ${ids.size} 个节点${nodes.length<ids.size?'，已达显示上限，请聚焦分支':''} · 数据范围 ${graphData.total_goals} 个目标、${graphData.total_fragments} 条碎片${graphData.shown_goals<graphData.total_goals||graphData.shown_fragments<graphData.total_fragments?'（接口已限量，请筛选）':''}`;
    }
    async function show(n){
      selection=n.id;highlight(selection);detail.hidden=false;detail.innerHTML=`<button class="g-drawer-close" data-g="graph-close" aria-label="关闭节点详情">${icon('close')}</button><span class="g-eyebrow">${icon(n.kind==='fragment'?'text':pillarIcon(data.goals.find(g=>g.id===n.id)))}${n.kind==='fragment'?'碎片':'目标'}</span><h3>${e(n.title)}</h3><p class="g-note">正在读取…</p>`;
      if(n.kind==='fragment'){
        try{const item=await api('/growth/fragments/'+encodeURIComponent(n.id));if(!active||selection!==n.id)return;
          detail.innerHTML=`<button class="g-drawer-close" data-g="graph-close" aria-label="关闭节点详情">${icon('close')}</button><span class="g-eyebrow">${icon('text')}碎片</span><h3>${e(item.title)}</h3><p class="g-drawer-excerpt">${e(item.text||'已保存来源链接。')}</p><p class="g-note">${e(item.goal_ids.map(id=>data.goals.find(g=>g.id===id)?.title||'历史目标').join(' · ')||'待整理')}</p><button data-g="fragment" data-id="${e(n.id)}">查看完整内容 ${icon('arrow')}</button>`;
        }catch(err){if(active&&selection===n.id)detail.querySelector('.g-note').textContent=err.message;}
      }else{
        const goal=data.goals.find(g=>g.id===n.id);
        detail.innerHTML=`<button class="g-drawer-close" data-g="graph-close" aria-label="关闭节点详情">${icon('close')}</button><span class="g-eyebrow">${icon(pillarIcon(goal))}目标</span><h3>${e(n.title)}</h3><p class="g-note">${e(tagPath(n.id))}</p><p class="g-drawer-excerpt">${e(goal?.description||'沿着这个方向，看看已经留下的积累。')}</p><p><strong>${goalCount(n.id)}</strong> 条积累</p><div class="g-heading-actions"><button class="g-primary" data-g="graph-expand" data-id="${e(n.id)}">展开关联</button><button data-g="goal-detail" data-id="${e(n.id)}">查看目标</button></div>`;
      }
    }
    function point(clientX,clientY){const p=svg.createSVGPoint();p.x=clientX;p.y=clientY;return p.matrixTransform(svg.getScreenCTM().inverse());}
    let drag=null,pinch=null;const pointers=new Map();
    svg.addEventListener('pointerdown',ev=>{
      if(ev.button!==0&&ev.pointerType==='mouse')return;
      pointers.set(ev.pointerId,{x:ev.clientX,y:ev.clientY});svg.setPointerCapture(ev.pointerId);
      if(pointers.size===2){if(drag?.n&&drag.n.level!=='root'){drag.n.fx=null;drag.n.fy=null;}const p=[...pointers.values()];pinch={distance:Math.max(1,Math.hypot(p[0].x-p[1].x,p[0].y-p[1].y)),scale,tx,ty,center:point((p[0].x+p[1].x)/2,(p[0].y+p[1].y)/2)};drag=null;return;}
      const target=ev.target.closest('[data-node-id]'),n=nodes.find(n=>n.id===target?.dataset.nodeId);
      drag={n,start:point(ev.clientX,ev.clientY),clientX:ev.clientX,clientY:ev.clientY,tx,ty,x:n?.x,y:n?.y,moved:false};
    },{signal});
    svg.addEventListener('pointermove',ev=>{
      if(pointers.has(ev.pointerId))pointers.set(ev.pointerId,{x:ev.clientX,y:ev.clientY});
      if(pinch&&pointers.size===2){const p=[...pointers.values()],center=point((p[0].x+p[1].x)/2,(p[0].y+p[1].y)/2);scale=Math.max(.35,Math.min(4,pinch.scale*Math.hypot(p[0].x-p[1].x,p[0].y-p[1].y)/pinch.distance));tx=center.x-(pinch.center.x-pinch.tx)*scale/pinch.scale;ty=center.y-(pinch.center.y-pinch.ty)*scale/pinch.scale;transformed();return;}
      if(!drag)return;const p=point(ev.clientX,ev.clientY),dx=p.x-drag.start.x,dy=p.y-drag.start.y;
      if(Math.hypot(ev.clientX-drag.clientX,ev.clientY-drag.clientY)>5)drag.moved=true;
      if(drag.n&&drag.n.level!=='root'){
        const n=drag.n;n.fx=n.x=drag.x+dx/scale;n.fy=n.y=drag.y+dy/scale;
        if(!paused&&!document.hidden)simulation.alpha(.25).restart();paint();
      }else if(!drag.n){tx=drag.tx+dx;ty=drag.ty+dy;transformed();}
    },{signal});
    const release=ev=>{
      pointers.delete(ev.pointerId);
      if(pinch){pinch=null;drag=null;return;}
      if(drag?.n){const n=drag.n;if(n.level!=='root'){n.fx=null;n.fy=null;}if(!drag.moved&&ev.type!=='pointercancel')show(n);else if(!paused&&!document.hidden)simulation.alpha(.25).restart();}
      drag=null;
    };
    svg.addEventListener('pointerup',release,{signal});svg.addEventListener('pointercancel',release,{signal});
    svg.addEventListener('pointerover',ev=>{if(!selection&&!drag)highlight(ev.target.closest('[data-node-id]')?.dataset.nodeId||'');},{signal});
    svg.addEventListener('pointerleave',()=>{if(!selection)highlight('');},{signal});
    svg.addEventListener('focusin',ev=>{highlight(ev.target.closest('[data-node-id]')?.dataset.nodeId||selection);},{signal});
    svg.addEventListener('focusout',()=>highlight(selection),{signal});
    svg.addEventListener('keydown',ev=>{
      const n=nodes.find(n=>n.id===ev.target.closest('[data-node-id]')?.dataset.nodeId);
      if(n&&(ev.key==='Enter'||ev.key===' ')){ev.preventDefault();show(n);}else if(ev.key==='Escape')control('graph-close');
    },{signal});
    function zoom(next,p={x:500,y:330}){next=Math.max(.35,Math.min(4,next));tx=p.x-(p.x-tx)*next/scale;ty=p.y-(p.y-ty)*next/scale;scale=next;transformed();}
    svg.addEventListener('wheel',ev=>{ev.preventDefault();zoom(scale*(ev.deltaY<0?1.12:1/1.12),point(ev.clientX,ev.clientY));},{passive:false,signal});
    function control(cmd,id){
      if(cmd==='zoom-in'||cmd==='zoom-out')zoom(scale*(cmd==='zoom-in'?1.2:1/1.2));
      if(cmd==='reset-graph'){scale=1;tx=ty=0;transformed();}
      if(cmd==='graph-close'){selection='';detail.hidden=true;highlight('');}
      if(cmd==='graph-overview'||cmd==='graph-all'||cmd==='graph-expand'){
        selection='';detail.hidden=true;mode=cmd==='graph-overview'?'overview':'all';focusBranch=cmd==='graph-expand'?id:'';rebuild();
      }
      if(cmd==='graph-pause'){paused=!paused;remembered.size>2000&&remembered.clear();if(paused)simulation.stop();else if(!document.hidden)simulation.alpha(.25).restart();pauseLabel();}
    }
    function visibility(){if(document.hidden)simulation?.stop();else if(!paused)simulation?.alpha(.2).restart();}
    document.addEventListener('visibilitychange',visibility,{signal});
    reduced.addEventListener('change',()=>{if(reduced.matches){paused=true;simulation?.stop();pauseLabel();}},{signal});
    mobileMedia.addEventListener('change',()=>{coarse=mobileMedia.matches;rebuild();},{signal});
    rebuild();
    return {control,cleanup(){active=false;simulation?.stop();abort.abort();svg.dataset.motion='stopped';}};
  }
  return {mount};
})();
