'use strict';
/* 成长图谱 · 星尘终稿（20261117）
 * 预览定稿：http://127.0.0.1:8090/（_graph_redesign_preview_20261117/index.html）
 * 一级 = 柔金太阳 + 五支柱等大；二级 = 支柱居中 + 它的积累成环。
 * 三条编码（用真实数据）：大小=关联数 degree · 呼吸=recorded_at 近7天 · 线=真实 mention/co 互链。
 * 契约不变：window.ipGrowthCards.mount(ctx) -> {control, cleanup}。
 * 上一版备份：graph-cards.js.bak-20261117-移植前
 */
window.ipGrowthCards = (() => {
  const NS = 'http://www.w3.org/2000/svg';

  /* ============ 定稿配色：Galaxy 紫云 + 白太阳（截图定稿） ============ */
  const PILLAR_COLOR = { '认知':'#5eead4', '能量':'#67e8f9', '意义':'#f472b6', '关系':'#fb7185', '价值':'#e879f9' };
  const SUN = '#ffffff';
  const PAGE_SIZE = 44;
  const RINGS = [8, 16, 20];

  function seeded(key){ let h=2166136261; for (let i=0;i<key.length;i++){ h^=key.charCodeAt(i); h=Math.imul(h,16777619); }
    return () => { h^=h<<13; h^=h>>>17; h^=h<<5; return ((h>>>0)%10000)/10000; }; }
  const clamp=(v,a,b)=>Math.max(a,Math.min(b,v));
  const h2r=h=>[parseInt(h.slice(1,3),16),parseInt(h.slice(3,5),16),parseInt(h.slice(5,7),16)];
  const mix=(h,k,t)=>{ const a=h2r(h),b=h2r(k); return '#'+a.map((v,i)=>Math.round(v+(b[i]-v)*t).toString(16).padStart(2,'0')).join(''); };
  function hash0(n){ n=(n*2654435761)>>>0; n^=n>>>13; n=(n*1597334677)>>>0; return ((n>>>16)&1023)/1024; }
  function isRecent(s){ if(!s) return false;
    const d=new Date(String(s).replace(' ','T')); if(isNaN(d.getTime())) return false;
    return Date.now()-d.getTime() <= 7*864e5; }

  function mount(ctx) {
    const svg = document.getElementById('growth-graph');
    const detail = document.getElementById('g-graph-detail');
    const status = document.getElementById('g-graph-count');
    if (!svg) return { control(){}, cleanup(){} };

    const esc = (ctx && ctx.e) || (v => String(v==null?'':v).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])));
    const icon = (ctx && ctx.icon) || (() => '');
    const payload = (ctx && ctx.cards) || null;
    const cardsError = (ctx && ctx.cardsError) || '';
    const goals = (payload && payload.goals) || ((ctx && ctx.data && ctx.data.goals) || []);
    const rootGoal = goals.find(g => g.level === 'root') || null;
    const pillarGoals = goals.filter(g => g.level === 'pillar');
    const cards = (payload && payload.cards) || [];
    const rawEdges = (payload && payload.edges) || [];
    const byIdMap = new Map(cards.map(c => [c.id, c]));
    const reduced = matchMedia('(prefers-reduced-motion: reduce)');
    const abort = new AbortController();
    const signal = abort.signal;
    const isLight = () => document.documentElement.classList.contains('light');

    /* ============ 画布与层 ============ */
    let LW = 1000, LH = 660, CX = 500, CY = 318;
    const defs = el('defs', {}, svg);
    const bgLayer = el('g', {}, svg);
    const viewport = el('g', {}, svg);
    const edgeLayer = el('g', {}, viewport);
    const nodeLayer = el('g', {}, viewport);
    const uiLayer = el('g', {}, svg);
    const gc = new Map();
    function el(t, a, parent){ const n=document.createElementNS(NS,t);
      for(const [k,v] of Object.entries(a||{})) n.setAttribute(k,v);
      if(parent) parent.appendChild(n); return n; }
    function glow(hex, light){ const id='w-'+hex.slice(1)+(light?'L':'D');
      if(!gc.has(id)){ gc.set(id,1); const g=el('radialGradient',{id,cx:'50%',cy:'50%',r:'50%'},defs);
        const s=light?.34:.55;
        el('stop',{offset:'0%','stop-color':hex,'stop-opacity':s},g);
        el('stop',{offset:'42%','stop-color':hex,'stop-opacity':s*.36},g);
        el('stop',{offset:'100%','stop-color':hex,'stop-opacity':0},g); }
      return 'url(#'+id+')'; }
    function body(hex){ const id='b-'+hex.slice(1);
      if(!gc.has(id)){ gc.set(id,1); const g=el('radialGradient',{id,cx:'38%',cy:'30%',r:'78%'},defs);
        el('stop',{offset:'0%','stop-color':mix(hex,'#ffffff',.42)},g);
        el('stop',{offset:'55%','stop-color':hex},g);
        el('stop',{offset:'100%','stop-color':mix(hex,'#05060a',.4)},g); }
      return 'url(#'+id+')'; }

    /* ============ 状态 ============ */
    let level='all', openPillar='', page=0;
    let nodes=[], edges=[], nodeById=new Map();
    let scale=1, tx=0, ty=0, paused=false;
    let simRaf=0, lastT=performance.now();
    let drag=null, pan=null, downPt=null, moved=false, selection='';

    function canvasBox(){
      LW = 1000; LH = 660; CX = LW/2; CY = LH*0.5;   /* 第一版固定画布，随面板等比缩放 */
      svg.setAttribute('viewBox', '0 0 1000 660');
    }
    function themeHex(name, fb){
      try{ const v=getComputedStyle(document.documentElement).getPropertyValue(name).trim(); return v||fb; }
      catch(e){ return fb; }
    }
    function pillarHex(p, i){
      return (p&&p.color) || PILLAR_COLOR[p&&p.title] || ['#6366f1','#2563eb','#a855f7','#0d9488','#c2410c'][i%5];
    }

    /* 背景远星（氛围） */
    function drawBackDust(){
      bgLayer.replaceChildren();
      const r=seeded('firmament'), col=isLight()?'#5a5f75':'#cdd5ea';
      for(let i=0;i<46;i++){
        const c=el('circle',{cx:(r()*LW).toFixed(0),cy:(r()*LH).toFixed(0),
          r:(0.5+r()*0.9).toFixed(2),fill:col,opacity:(0.04+r()*0.1).toFixed(2)},bgLayer);
        if(!reduced.matches && !paused){
          const blink=()=>{ if(paused||!c.isConnected) return;
            c.setAttribute('opacity',(0.03+Math.random()*0.12).toFixed(2));
            setTimeout(blink,1800+Math.random()*3800); };
          setTimeout(blink, r()*4000);
        }
      }
    }

    /* ============ 建场景 ============ */
    function mk(o){
      const n=Object.assign({vx:0,vy:0,phase:Math.random()*6.28,born:0,style0:1},o);
      n.home={x:n.x,y:n.y}; nodes.push(n); nodeById.set(n.id,n); return n;
    }
    function clearScene(){
      nodes=[]; edges=[]; nodeById=new Map();
      edgeLayer.replaceChildren(); nodeLayer.replaceChildren(); uiLayer.replaceChildren();
    }
    function buildAll(){
      level='all'; openPillar=''; page=0; clearScene(); selection='';
      if(detail) detail.hidden=true;
      const now=performance.now();
      const sun=mk({id:'root',kind:'sun',title:rootGoal?rootGoal.title:'主线',goalId:rootGoal?rootGoal.id:'',
        x:CX,y:CY,r:29,hex:themeHex('--gold','#b45309'),recent:true});
      sun.born=now+80; sun.style0=0;
      pillarGoals.forEach((p,i)=>{
        const a=-Math.PI/2+i*2*Math.PI/Math.max(1,pillarGoals.length);
        const n=mk({id:'p:'+p.id,kind:'pillar',goalId:p.id,pillarId:p.id,title:p.title,count:p.card_count||0,
          hex:pillarHex(p,i),x:CX+205*Math.cos(a),y:CY+190*Math.sin(a),r:20,recent:true});
        n.born=now+220+i*120; n.style0=0;
        edges.push({a:sun,b:n,kind:'own',op:.48,w:1.2});
      });
      nodes.forEach(draw); flushEdges(); drawBread('');
      renderStatus();
      queueLabels();
    }
    function pillarCards(pid){
      const p=pillarGoals.find(x=>x.id===pid)||{};
      const t=p.title||'';
      const list=cards.filter(c=> c.pillar_id===pid ||
        (t && (c.pillar===t || c.category===t || c.group===t || c.pillar_title===t)));
      return list.sort((a,b)=>(b.degree||0)-(a.degree||0));
    }
    function drawPillarLevel(){
      clearScene(); selection=''; if(detail) detail.hidden=true;
      level='pillar';
      const now=performance.now();
      const p=pillarGoals.find(x=>x.id===openPillar);
      if(!p){ buildAll(); return; }
      const hex=pillarHex(p, pillarGoals.indexOf(p));
      const all=pillarCards(p.id);
      const pages=Math.max(1,Math.ceil(all.length/PAGE_SIZE));
      page=clamp(page,0,pages-1);
      const items=all.slice(page*PAGE_SIZE, page*PAGE_SIZE+PAGE_SIZE);
      const rs=Math.min(1,Math.min(LW,LH)/640);
      const center=mk({id:'p:'+p.id,kind:'sun',goalId:p.id,pillarId:p.id,title:p.title,count:all.length,
        x:CX,y:CY,r:20,hex,recent:true});
      const golden=Math.PI*(3-Math.sqrt(5));
      items.forEach((c,i)=>{
        const z=.55+Math.pow(hash0(i+1),1.4)*.85;
        const recent=isRecent(c.recorded_at);
        const turn=i*golden, rad=20+Math.sqrt(i)*14;      /* 螺旋初始位，随后由力导自然摊开 */
        const f=mk({id:'f:'+c.id,kind:'frag',cardId:c.id,title:c.title,pillar:p.title,
          deg:c.degree||0,days:0,recent,
          /* t63b：浅色主题下 frag 不再往白色/背景混（发白根治）——取二级主球同色，仅轻微提亮；深色保持原样 */
          hex:isLight()?mix(hex,'#ffffff',recent?Math.min(.18,.06+z*.05):0)
                      :(recent?mix(hex,'#ffffff',.3+z*.1):mix(hex,themeHex('--bg','#f7f6f3'),.55)),
          x:CX+rad*Math.cos(turn),y:CY+rad*Math.sin(turn),
          r:(2.6+Math.sqrt(c.degree||0)*1.55)*z,z});
        f.home={x:CX,y:CY};               /* 归位指向中心：连力+反发决定半径（Obsidian 局部图）*/
        f.born=now+140+i*40; f.style0=0;
        const rest=88+34*Math.sqrt(Math.min(1,(c.degree||0)/8));
        edges.push({a:center,b:f,kind:'own',rest,op:.3+z*.1,w:1});
      });
      const shown=new Set(items.map(c=>c.id));
      for(const e of rawEdges){
        if(!shown.has(e.source)||!shown.has(e.target)) continue;
        const a=nodeById.get('f:'+e.source), b=nodeById.get('f:'+e.target);
        if(!a||!b||a===b) continue;
        edges.push({a,b,kind:e.type==='mention'?'mention':'co',op:e.type==='mention'?.18:.12,w:.8});
      }
      nodes.forEach(draw); flushEdges();
      drawBread(p.title);
      if(pages>1) drawPager(pages);
      renderStatus(pages);
    }
    function flushEdges(){
      const hair=themeHex('--border-strong','#c9c4ba');   /* 第一版线条：主题边线色 */
      for(const e of edges) e.el=el('line',{class:'g-sd-edge',stroke:hair,
        'stroke-width':e.w||1.2,'stroke-opacity':e.op,'stroke-linecap':'round'},edgeLayer);
    }
    function drawBread(title){
      uiLayer.replaceChildren();
      if(level==='all'){ return; }
      const t=el('text',{x:22,y:30,'font-size':'12.5','data-sd-crumb':'1',cursor:'pointer',
        fill:'var(--gold)'},uiLayer);
      t.textContent='‹ 全部';
      t.addEventListener('click',ev=>{ev.stopPropagation();buildAll();},{signal});
      const s=el('text',{x:64,y:30,'font-size':'12.5',fill:'var(--muted)'},uiLayer);
      s.textContent=title;
    }
    function drawPager(pages){
      const mk2=(x,label,fn,aria)=>{
        const g=el('g',{transform:'translate('+x+' '+(LH-30)+')','data-sd-page':'1',cursor:'pointer',
          role:'button',tabindex:'0','aria-label':aria},uiLayer);
        el('circle',{r:15,fill:'var(--surface2)','stroke':'var(--border)','stroke-width':1},g);
        const t=el('text',{y:5,'text-anchor':'middle','font-size':'13',fill:'var(--text)'},g);
        t.textContent=label;
        g.addEventListener('click',ev=>{ev.stopPropagation();fn();},{signal});
        g.addEventListener('keydown',ev=>{if(ev.key==='Enter'||ev.key===' '){ev.preventDefault();fn();}},{signal});
      };
      if(page>0) mk2(CX-40,'‹',()=>{page--;drawPillarLevel();},'上一批');
      mk2(CX+40,'›',()=>{page++;drawPillarLevel();},'下一批');
      const info=el('text',{x:CX,y:LH-30+26,'text-anchor':'middle','font-size':'10.5',fill:'var(--muted)'},uiLayer);
      info.textContent='第 '+(page+1)+' / '+pages+' 批';
    }
    function renderStatus(pages){
      if(!status) return;
      if(cardsError){ status.textContent='碎片接口没接上（'+cardsError+'），图谱暂用目标线显示。'; return; }
      if(level==='all'){
        status.textContent='一个主题 · '+pillarGoals.length+' 个支柱 · 点支柱看它的积累';
      }else{
        const p=pillarGoals.find(x=>x.id===openPillar)||{title:''};
        const n=pillarCards(p.id).length;
        status.textContent=p.title+' · 共 '+n+' 条'+(pages&&pages>1?'（本页 '+Math.min(PAGE_SIZE,n-page*PAGE_SIZE)+' 条 · 第 '+(page+1)+'/'+pages+' 批）':'')+
          ' · 亮的为近七天碰过';
      }
    }

    /* ============ t63：标签碰撞避让 ============ */
    const LBL_GAP = 2;            /* 两个标签包围盒之间至少留 2px */
    const forced = new Set();     /* 悬停/聚焦/选中的节点：标签强制显示 */
    let lastLabelT = 0;

    function labelRank(n){ return n.kind==='sun'?0:n.kind==='pillar'?1:2; }

    /* 估包围盒：宽度用实测（getComputedTextLength）优先，取不到再按字数估 */
    function labelBox(n){
      if(!n.lblEl) return null;
      const size = n.lblSize || 11.5;
      let w = 0;
      try { w = n.lblEl.getComputedTextLength ? n.lblEl.getComputedTextLength() : 0; } catch(e){ w = 0; }
      if(!w) w = (n.lblEl.textContent||'').length * size * .62;
      const h = size + 3;
      const y = (n.lblY || (n.r+10)) - h;     /* 文本基线在 y，盒子往上抬一个字高 */
      const box = {x:n.x - w/2, y:n.y + y, w:w, h:h};
      if(n.cntEl){
        const cs = n.cntSize || 10.5;
        let cw = 0;
        try { cw = n.cntEl.getComputedTextLength ? n.cntEl.getComputedTextLength() : 0; } catch(e){ cw = 0; }
        if(!cw) cw = (n.cntEl.textContent||'').length * cs * .62;
        const cTop = n.y + ((n.lblY||n.r) + cs + 2) - cs;
        box.x = Math.min(box.x, n.x - cw/2); box.w = Math.max(box.w, cw);
        box.h = Math.max(box.y + box.h, cTop + cs + 2) - box.y;
      }
      return box;
    }
    function boxHit(a,b){
      return !(a.x + a.w + LBL_GAP <= b.x || b.x + b.w + LBL_GAP <= a.x ||
               a.y + a.h + LBL_GAP <= b.y || b.y + b.h + LBL_GAP <= a.y);
    }
    /* 优先级：太阳/支柱恒定显示 → 碎片按 degree、半径降序贪心占位，撞了就隐藏 */
    function layoutLabels(){
      const cands = nodes.filter(n=>n.lblEl);
      if(!cands.length) return;
      cands.sort((a,b)=> (labelRank(a)-labelRank(b)) || ((b.deg||0)-(a.deg||0)) || ((b.r||0)-(a.r||0)));
      const taken = [];
      let shown = 0, hidden = 0;
      for(const n of cands){
        const box = labelBox(n);
        if(!box) continue;
        const hub = (n.kind==='sun' || n.kind==='pillar');
        const inCanvas = box.x >= -2 && box.y >= -2 && box.x + box.w <= LW + 2 && box.y + box.h <= LH + 2;
        const ok = hub || forced.has(n.id) || (inCanvas && !taken.some(b=>boxHit(box,b)));
        n.lblShown = ok;
        if(n.lblEl) n.lblEl.style.visibility = ok ? '' : 'hidden';
        if(n.cntEl) n.cntEl.style.visibility = ok ? '' : 'hidden';
        if(ok){ taken.push(box); shown++; } else { hidden++; }
      }
      svg.dataset.lblShown = String(shown);
      svg.dataset.lblHidden = String(hidden);
    }
    function queueLabels(){ requestAnimationFrame(()=>{ layoutLabels(); }); }

    /* ============ 画星 ============ */
    function draw(n){
      const g=el('g',{class:'g-sd-node '+n.kind,'data-sd-key':n.id,tabindex:'0',role:'button'},nodeLayer); n.g=g;
      /* t63：悬停/键盘聚焦时强制显示标签（其余按碰撞避让隐藏） */
      const _f=()=>{ forced.add(n.id); layoutLabels(); };
      const _b=()=>{ forced.delete(n.id); layoutLabels(); };
      g.addEventListener('pointerenter',_f,{signal}); g.addEventListener('pointerleave',_b,{signal});
      g.addEventListener('focus',_f,{signal}); g.addEventListener('blur',_b,{signal});
      g.addEventListener('keydown',ev=>{ if(ev.key==='Enter'||ev.key===' '){ev.preventDefault();click(n);} },{signal});
      const art=el('g',{class:'art'},g);
      el('circle',{r:Math.max(12,n.r*2.2),fill:'transparent'},g);   /* 命中区 */
      n.bodyC=el('circle',{r:0,fill:n.hex,
        stroke:n.kind==='frag'?'none':themeHex('--surface','#ffffff'),
        'stroke-width':n.kind==='frag'?0:2,'data-core':'1'},art);
      if(n.kind==='sun'){
        if(n.count!=null){   /* 二级中心：名字与条数在球下 */
          const t=el('text',{class:'lbl',y:n.r+22,'text-anchor':'middle'},g); t.textContent=n.title;
          const c=el('text',{class:'cnt',y:n.r+38,'text-anchor':'middle'},g); c.textContent=n.count;
          n.lblEl=t; n.cntEl=c; n.lblY=n.r+22; n.lblSize=13; n.cntSize=10.5;
        }else{               /* 一级主线：名字在球下（第一版） */
          const t=el('text',{class:'lbl',y:n.r+24,'text-anchor':'middle'},g); t.textContent=n.title;
          n.lblEl=t; n.lblY=n.r+24; n.lblSize=13;
        }
      }else if(n.kind==='pillar'){
        const t=el('text',{class:'lbl',y:n.r+24,'text-anchor':'middle'},g); t.textContent=n.title;
        const c=el('text',{class:'cnt',y:n.r+40,'text-anchor':'middle'},g); c.textContent=n.count;
        n.lblEl=t; n.cntEl=c; n.lblY=n.r+24; n.lblSize=13; n.cntSize=10.5;
      }else{
        const t=el('text',{class:'fname',y:n.r+10,'text-anchor':'middle'},g);
        t.textContent=n.title.length>12?n.title.slice(0,11)+'…':n.title;
        n.lblEl=t; n.lblY=n.r+10; n.lblSize=11.5;
      }
      if(n.born>performance.now()) g.classList.add('enter'); else grow(n);
    }
    function grow(n){ if(n.bodyC) n.bodyC.setAttribute('r',n.r); }
    function entrance(now){
      for(const n of nodes){
        if(n.style0===0 && (!n.born||now>=n.born)){
          n.style0=1; n.g.classList.remove('enter'); grow(n);
        }
      }
    }

    /* ============ 物理 ============ */
    function step(now){
      simRaf=requestAnimationFrame(step);
      const dt=Math.min(32,now-lastT)/16.67; lastT=now;
      entrance(now);
      if(paused) return;
      for(const e of edges){
        const a=e.a,b=e.b;
        let dx=b.x-a.x,dy=b.y-a.y,d=Math.hypot(dx,dy)||.01;
        const rest=(e.rest!=null)?e.rest:Math.hypot(b.home.x-a.home.x,b.home.y-a.home.y);
        const f=(d-rest)*.05*dt; dx/=d; dy/=d;
        if(!a.pinned){a.vx+=dx*f;a.vy+=dy*f;}
        if(!b.pinned){b.vx-=dx*f;b.vy-=dy*f;}
      }
      for(let i=0;i<nodes.length;i++){
        const a=nodes[i];
        for(let j=i+1;j<nodes.length;j++){
          const b=nodes[j];
          let dx=b.x-a.x,dy=b.y-a.y,d2=dx*dx+dy*dy;
          if(d2>190*190) continue;
          if(d2<1){d2=1;dx=.5;dy=.3;}
          const d=Math.sqrt(d2),f=Math.min(1.9,1400/d2)*dt; dx/=d; dy/=d;
          if(!a.pinned){a.vx-=dx*f;a.vy-=dy*f;}
          if(!b.pinned){b.vx+=dx*f;b.vy+=dy*f;}
        }
        if(!a.pinned){
          const hk=a.kind==='sun'?.06:a.kind==='pillar'?.035:.04;   /* gravity 0.06（学自 obsidian-graph-styler）*/
          a.vx+=(a.home.x-a.x)*hk*dt; a.vy+=(a.home.y-a.y)*hk*dt;
          a.vx+=Math.sin(now/1500+a.phase)*.0045*dt;
          a.vy+=Math.cos(now/1700+a.phase)*.0045*dt;
          a.vx*=Math.pow(.6,dt); a.vy*=Math.pow(.6,dt);   /* velocityKeep 0.6（学自 obsidian-graph-styler）*/
          a.x=clamp(a.x+a.vx*dt,30,LW-30);
          a.y=clamp(a.y+a.vy*dt,30,LH-30);
        }
        a.g.setAttribute('transform','translate('+a.x.toFixed(1)+' '+a.y.toFixed(1)+')');
        if(a.halo&&a.style0&&(a.recent||a.kind==='sun')){
          const k=(Math.sin(now/(a.kind==='frag'?1400:3800)+a.phase)+1)/2;
          a.halo.setAttribute('r',(a.haloR*(1+k*.14)).toFixed(1));
          a.halo.setAttribute('opacity',(((a.kind==='frag'?(a.recent?.8:.26)*a.z:.75)*(isLight()?.62:1))*(1+k*.22)).toFixed(2));
        }
      }
      for(const e of edges){
        e.el.setAttribute('x1',e.a.x.toFixed(1)); e.el.setAttribute('y1',e.a.y.toFixed(1));
        e.el.setAttribute('x2',e.b.x.toFixed(1)); e.el.setAttribute('y2',e.b.y.toFixed(1));
        e.el.setAttribute('stroke-opacity',e.op);
      }
      if(now - lastLabelT > 260){ lastLabelT = now; layoutLabels(); }
    }

    /* ============ 详情 ============ */
    function neighborsOf(cardId){
      const out=[];
      for(const e of rawEdges){
        if(e.source===cardId&&byIdMap.has(e.target)) out.push(byIdMap.get(e.target));
        if(e.target===cardId&&byIdMap.has(e.source)) out.push(byIdMap.get(e.source));
      }
      return out;
    }
    function showCard(n){
      if(!detail) return;
      const c=byIdMap.get(n.cardId);
      selection=n.cardId;
      if(!c){ detail.hidden=false; detail.innerHTML='<p class="g-note">'+esc(n.title)+'</p>'; return; }
      const around=neighborsOf(c.id).sort((a,b)=>(b.degree||0)-(a.degree||0)).slice(0,4);
      const kinds={}; for(const x of neighborsOf(c.id)) kinds[x.pillar]=(kinds[x.pillar]||0)+1;
      detail.hidden=false;
      detail.innerHTML='<button class="g-drawer-close" data-g="graph-close" aria-label="关闭节点详情">'+icon('close')+'</button>'
        +'<span class="g-eyebrow">'+icon('text')+'碎片 · '+(isRecent(c.recorded_at)?'近 7 天碰过':'久未触碰')+'</span>'
        +'<h3>'+esc(c.title)+'</h3>'
        +'<p class="g-drawer-excerpt">'+esc(c.summary||'这条还没有摘要，点下面看完整内容。')+'</p>'
        +'<p class="g-note">'+(c.degree||0)+' 条真实关联'+(around.length?' · 最近邻：'+esc(around.map(x=>x.title).join('、')):'')+'</p>'
        +(c.keywords&&c.keywords.length?'<p class="g-note">关键词：'+esc(c.keywords.slice(0,6).join('、'))+'</p>':'')
        +'<p class="g-note">旧分类 '+esc(c.group||c.category)+' · '+(c.pillar?'归入 '+esc(c.pillar):'还没归入支柱')+'</p>'
        +'<a class="g-text-link" href="/#/knowledge/'+encodeURIComponent(c.id)+'">查看完整内容 '+icon('arrow')+'</a>';
    }
    function showGoal(n){
      if(!detail) return;
      const g=(ctx.data&&ctx.data.goals||goals).find(x=>x.id===n.goalId);
      selection=''; detail.hidden=false;
      detail.innerHTML='<button class="g-drawer-close" data-g="graph-close" aria-label="关闭节点详情">'+icon('close')+'</button>'
        +'<span class="g-eyebrow">'+icon('target')+(n.kind==='sun'&&n.levelGoal?'目标':'支柱')+'</span>'
        +'<h3>'+esc(n.title)+'</h3>'
        +'<p class="g-note">'+esc(g&&g.description||(n.count?'归到这里的积累 '+n.count+' 条':'沿着这个方向，看看已经留下的积累。'))+'</p>'
        +(n.kind==='pillar'&&n.pillarId?'<div class="g-heading-actions"><button class="g-primary" data-g="graph-expand" data-id="'+esc(n.pillarId)+'">展开积累</button></div>':'');
    }
    function click(n){
      if(level==='pillar'){
        if(n.kind==='sun'){ buildAll(); return; }
        if(n.kind==='frag') showCard(n);
        return;
      }
      if(n.kind==='pillar'){ openPillar=n.pillarId; page=0; drawPillarLevel(); return; }
      if(n.kind==='sun'){ showGoal(n); }
    }

    /* ============ 交互 ============ */
    function toWorld(x,y){ const p=svg.createSVGPoint(); p.x=x; p.y=y;
      const m=viewport.getScreenCTM(); return m?p.matrixTransform(m.inverse()):{x,y}; }
    function applyView(){ viewport.setAttribute('transform','translate('+tx+' '+ty+') scale('+scale+')'); }
    function nodeAt(ev){ const g=ev.target.closest&&ev.target.closest('[data-sd-key]');
      return g?nodeById.get(g.getAttribute('data-sd-key')):null; }
    svg.addEventListener('pointerdown',ev=>{
      if(ev.button!==0&&ev.pointerType==='mouse') return;
      ev.preventDefault();                           /* 不触发文字选择（去掉蓝色选框） */
      svg.setPointerCapture(ev.pointerId);
      downPt={x:ev.clientX,y:ev.clientY}; moved=false;
      const n=nodeAt(ev);
      if(n){
        const w=toWorld(ev.clientX,ev.clientY);
        drag={n,ox:n.x-w.x,oy:n.y-w.y,last:[{x:w.x,y:w.y,t:performance.now()}]};
        n.pinned=true; n.vx=0; n.vy=0; svg.classList.add('dragging');
      }else{
        pan={tx,ty,start:toWorld(ev.clientX,ev.clientY)}; svg.classList.add('dragging');
      }
    },{signal});
    svg.addEventListener('pointermove',ev=>{
      if(!downPt) return;
      if(Math.hypot(ev.clientX-downPt.x,ev.clientY-downPt.y)>5) moved=true;
      const w=toWorld(ev.clientX,ev.clientY);
      if(drag){
        const n=drag.n;
        n.x=clamp(w.x+drag.ox,30,LW-30); n.y=clamp(w.y+drag.oy,30,LH-30);
        drag.last.push({x:n.x,y:n.y,t:performance.now()});
        if(drag.last.length>6) drag.last.shift();
      }else if(pan){
        tx=pan.tx+(w.x-pan.start.x); ty=pan.ty+(w.y-pan.start.y); applyView();
      }
    },{signal});
    function release(){
      svg.classList.remove('dragging');
      if(drag){
        const n=drag.n; n.pinned=false;
        const L=drag.last;
        if(L.length>=2){ const a=L[0],b=L[L.length-1],dt=Math.max(1,b.t-a.t);
          n.vx=clamp((b.x-a.x)/dt*15,-11,11); n.vy=clamp((b.y-a.y)/dt*15,-11,11); }
        if(!moved) click(n);
        drag=null;
      }else if(pan){
        if(!moved){ selection=''; if(detail) detail.hidden=true; if(level==='pillar') buildAll(); }
        pan=null;
      }
      downPt=null;
      layoutLabels();
    }
    svg.addEventListener('pointerup',release,{signal});
    svg.addEventListener('pointercancel',release,{signal});

    /* Obsidian 式悬停：自己+邻居高亮，其余变淡 */
    function highlightNeighbors(id){
      const near=new Set(); if(id) near.add(id);
      if(id) for(const e of edges){ if(e.a.id===id) near.add(e.b.id); else if(e.b.id===id) near.add(e.a.id); }
      const dim=level==='pillar';                     /* 一级不压暗其他节点 */
      for(const n of nodes){ if(!n.g) continue;
        n.g.classList.toggle('g-related', !!id && near.has(n.id));
        n.g.classList.toggle('g-faded', dim && !!id && !near.has(n.id)); }
      for(const e of edges){ if(!e.el) continue;
        const on=!!id && (e.a.id===id||e.b.id===id);
        e.el.classList.toggle('g-related', on);
        e.el.classList.toggle('g-faded', dim && !!id && !on); }
    }
    svg.addEventListener('pointerover',ev=>{ if(drag) return;
      const g=ev.target.closest && ev.target.closest('[data-sd-key]');
      highlightNeighbors(g?g.getAttribute('data-sd-key'):''); },{signal});
    svg.addEventListener('pointerleave',()=>highlightNeighbors(''),{signal});
    svg.addEventListener('wheel',ev=>{
      ev.preventDefault();
      const w=toWorld(ev.clientX,ev.clientY);
      const nx=clamp(scale*(ev.deltaY<0?1.1:1/1.1),.5,2.8);
      tx=w.x-(w.x-tx)*nx/scale; ty=w.y-(w.y-ty)*nx/scale; scale=nx; applyView();
    },{passive:false,signal});

    /* ============ 命令 ============ */
    function pauseLabel(){
      const button=document.querySelector('[data-g=graph-pause]');
      if(button){ button.innerHTML=icon(paused?'play':'pause')+(paused?'开启动效':'暂停动效');
        button.setAttribute('aria-pressed',String(paused)); }
      svg.dataset.motion=paused?'paused':'running';
    }
    function control(cmd,id){
      if(cmd==='zoom-in'||cmd==='zoom-out'){
        const nx=clamp(scale*(cmd==='zoom-in'?1.2:1/1.2),.5,2.8);
        tx=CX-(CX-tx)*nx/scale; ty=CY-(CY-ty)*nx/scale; scale=nx; applyView(); layoutLabels();
      }else if(cmd==='reset-graph'){ scale=1; tx=0; ty=0; applyView(); layoutLabels(); }
      else if(cmd==='graph-pause'){ paused=!paused; pauseLabel(); }
      else if(cmd==='graph-overview'){ buildAll(); }
      else if(cmd==='graph-all'||cmd==='graph-expand'){
        if(level==='pillar'){ page++; drawPillarLevel(); }
        else if(cmd==='graph-expand'&&id){ openPillar=id; page=0; drawPillarLevel(); }
      }
      else if(cmd==='graph-close'){ selection=''; if(detail) detail.hidden=true; }
    }
    reduced.addEventListener('change',()=>{ if(reduced.matches){paused=true;pauseLabel();} },{signal});

    /* ============ 启停 ============ */
    canvasBox(); buildAll(); pauseLabel();
    // t134：图谱首屏只露约 169px（1280×800）。挂载后把图谱舞台对齐到滚动容器顶部，
    // 不靠改全局样式；只在图谱页 mount 时执行一次，不影响总览/标签。
    const graphStage=svg.closest('.g-graph-stage');
    if(graphStage)setTimeout(()=>{try{graphStage.scrollIntoView({block:'start'});}catch(e){}},0);
    if(!paused) simRaf=requestAnimationFrame(step); else simRaf=requestAnimationFrame(step);
    return {
      control,
      cleanup(){
        cancelAnimationFrame(simRaf);
        abort.abort();
        svg.dataset.motion='stopped';
        bgLayer.replaceChildren(); viewport.replaceChildren(); uiLayer.replaceChildren(); defs.replaceChildren();
        gc.clear();
        if(detail) detail.hidden=true;
      }
    };
  }
  return { mount };
})();

// t134：活跃视图可能是 graph-neural（暗色）/graph-cards（亮色）；本文件总会先加载，
// 因此在这里盯住图谱舞台首次出现，挂载后把它对齐到滚动容器顶部（不改全局样式）。
(function(){
  function alignGraphFirstScreen(){
    const svg=document.getElementById('growth-graph');
    if(!svg)return;
    const stage=svg.closest('.g-graph-stage');
    if(!stage||stage.dataset.t134Aligned==='1')return;
    stage.dataset.t134Aligned='1';
    setTimeout(()=>{try{stage.scrollIntoView({block:'start'});}catch(e){}},0);
  }
  const obs=new MutationObserver(alignGraphFirstScreen);
  obs.observe(document.documentElement,{childList:true,subtree:true});
  if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',alignGraphFirstScreen,{once:true});
  else setTimeout(alignGraphFirstScreen,0);
})();
