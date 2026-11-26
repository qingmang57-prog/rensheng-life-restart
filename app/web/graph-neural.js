'use strict';
/* 成长图谱 · NEURAL 暗色渲染器（20261117 · 独立文件）
 * 预览定稿：http://127.0.0.1:8090/（_graph_redesign_preview_20261117/index.html）
 * 按参考稿（XUAN / NEURAL）独立实现，只服务暗色主题：星野底 + 发光核心 + 外扩光线
 * + 三角网格立体球（逐帧 3D 旋转）+ 曲线光点 + 右侧五张计数卡（点卡进二级）。
 * 与旧渲染器 graph-cards.js（第一版，浅色用）共存，互不引用。
 * 契约：window.ipGrowthNeural.mount(ctx) -> {control, cleanup}。
 */
window.ipGrowthNeural = (() => {
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
    svg.dataset.skin='neural';

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
    // 2026-11-17 t99：色点改 class/data-color + CSSOM，去掉内联 style（CSP style-src 'self'）。
    const dotColors = new Set();
    function ensureDotColor(hex){
      const h = String(hex || '').trim();
      if (!h || dotColors.has(h)) return;
      dotColors.add(h);
      const rule = '.g-sd-card i.g-sd-dot[data-color="' + h + '"]{background:' + h + '}';
      try {
        if (typeof ipAddCssRules === 'function') { ipAddCssRules([rule]); return; }
        const sheet = new CSSStyleSheet();
        sheet.insertRule(rule, 0);
        document.adoptedStyleSheets = [...document.adoptedStyleSheets, sheet];
      } catch (err) { /* 注入失败时色点退化为无色，不影响布局与点击 */ }
    }
    function pillarHex(p, i){
      return (p&&p.color) || PILLAR_COLOR[p&&p.title] || ['#6366f1','#2563eb','#a855f7','#0d9488','#c2410c'][i%5];
    }

    /* 背景远星（氛围） */
    function drawBackDust(){
      bgLayer.replaceChildren();
      const r=seeded('firmament'), col='#cdd5ea';
      for(let i=0;i<150;i++){
        el('circle',{cx:(r()*LW).toFixed(0),cy:(r()*LH).toFixed(0),
          r:(0.35+r()*1.1).toFixed(2),fill:col,opacity:(0.06+r()*0.42).toFixed(2)},bgLayer);
      }
      for(let i=0;i<6;i++){
        el('circle',{cx:(r()*LW).toFixed(0),cy:(r()*LH).toFixed(0),r:1.7,fill:'#fff',opacity:.9},bgLayer);
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
      CX = LW*0.40; CY = LH*0.5;   /* 核心偏左，右边让给计数卡 */
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
      drawBackDust(); renderCards();
      renderStatus();
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
      CX = LW*0.40; CY = LH*0.5;
      const now=performance.now();
      hideCards(); drawBackDust();
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
          hex:recent?mix(hex,'#ffffff',.3+z*.1):mix(hex,themeHex('--bg','#f7f6f3'),.55),
          x:CX+rad*Math.cos(turn),y:CY+rad*Math.sin(turn),
          r:(2.6+Math.sqrt(c.degree||0)*1.55)*z,z});
        f.home={x:CX,y:CY};               /* 归位指向中心：连力+反发决定半径（Obsidian 局部图）*/
        f.born=now+140+i*40; f.style0=0;
        const rest=120+42*Math.sqrt(Math.min(1,(c.degree||0)/10));   /* 辐条更长，给编织留空间 */
        edges.push({a:center,b:f,kind:'own',rest,op:.3+z*.1,w:1});
      });
      const shown=new Set(items.map(c=>c.id));
      for(const e of rawEdges){
        if(!shown.has(e.source)||!shown.has(e.target)) continue;
        const a=nodeById.get('f:'+e.source), b=nodeById.get('f:'+e.target);
        if(!a||!b||a===b) continue;
        const co=Number(e.cooccur||e.weight||0)||0;
        edges.push({a,b,kind:e.type==='mention'?'mention':'co',rest:e.type==='mention'?78:88,
          op:e.type==='mention'?.22:.14,w:co?Math.min(3,.9+co*.4):.9,cooccur:co,
          label:co?('共现 '+co+' 次'):''});
      }
      nodes.forEach(draw); flushEdges();
      drawBread(p.title);
      if(pages>1) drawPager(pages);
      renderStatus(pages);
    }
    /* ============ 右侧计数卡（NEURAL 构图：核心偏左、卡在右） ============ */
    const EN={认知:'Cognition',价值:'Value',意义:'Meaning',关系:'Relations',能量:'Energy'};
    function cardsHost(){
      const stage=svg.parentElement; if(!stage) return null;
      let host=stage.querySelector('.g-sd-cards');
      if(!host){ host=document.createElement('div'); host.className='g-sd-cards'; stage.appendChild(host); }
      return host;
    }
    function hideCards(){
      const h=svg.parentElement&&svg.parentElement.querySelector('.g-sd-cards');
      if(h) h.style.display='none';
    }
    function renderCards(){
      const host=cardsHost(); if(!host) return;
      host.style.display=''; host.replaceChildren();
      pillarGoals.forEach((p,i)=>{
        const list=pillarCards(p.id);
        const total=list.length||p.card_count||0;
        const recent=list.filter(c=>isRecent(c.recorded_at)).length;
        const b=document.createElement('button');
        b.type='button'; b.className='g-sd-card';
        const hex = pillarHex(p, i);
        ensureDotColor(hex);
        b.innerHTML='<span class="n">'+total+'</span><span class="t"><b>'+esc(p.title)+'</b><small>'
          +esc(EN[p.title]||'')+'</small><em>'+total+' 条 · 近 7 天 '+recent+'</em></span>'
          +'<i class="g-sd-dot" data-color="'+esc(hex)+'" aria-hidden="true"></i>';
        b.addEventListener('click',()=>{ openPillar=p.id; page=0; drawPillarLevel(); });
        host.appendChild(b);
      });
    }
    function flushEdges(){
      const hair=themeHex('--border-strong','#c9c4ba');
      for(const e of edges){                         /* 直线 + 中点光点 */
        e.el=el('line',{class:'g-sd-edge',stroke:hair,
          'stroke-width':e.w||1.2,'stroke-opacity':e.op,'stroke-linecap':'round'},edgeLayer);
        if(e.kind==='own') e.gem=el('circle',{r:1.8,fill:'#ffffff',opacity:.85,'pointer-events':'none'},edgeLayer);
        if(e.label){const t=document.createElementNS('http://www.w3.org/2000/svg','title');t.textContent=e.label;e.el.appendChild(t);}
      }
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

    /* ============ 画星 ============ */
    /* ============ 网格球（3D 线框，逐帧旋转） ============ */
    function makeMesh(n){
      const rnd=seeded('mesh-'+(n.id||'x')), N=n.meshN||38, V=[];
      for(let i=0;i<N;i++){
        const y=1-2*(i+.5)/N, rad=Math.sqrt(Math.max(0,1-y*y)), th=i*2.39996+rnd()*.5;
        const jit=.80+rnd()*.30;                    /* 半径抖动 → 有机“脑”感 */
        V.push([Math.cos(th)*rad*jit, y*jit, Math.sin(th)*rad*jit]);
      }
      const E=[];
      for(let i=0;i<N;i++) for(let j=i+1;j<N;j++){
        const a=V[i], b=V[j], d2=(a[0]-b[0])*(a[0]-b[0])+(a[1]-b[1])*(a[1]-b[1])+(a[2]-b[2])*(a[2]-b[2]);
        if(d2<(n.meshT||.32)) E.push([i,j]);        /* 只连最近的邻居 */
      }
      return {V,E};
    }
    function drawMesh(n,t){
      const V=n.mesh.V, E=n.mesh.E, r=n.r;
      const ca=Math.cos(t), sa=Math.sin(t), ct=Math.cos(.36), st=Math.sin(.36);
      const P=new Array(V.length);
      for(let i=0;i<V.length;i++){
        const v=V[i];
        const x=v[0]*ca-v[2]*sa, z=v[0]*sa+v[2]*ca;
        const y=v[1]*ct-z*st, z2=v[1]*st+z*ct;
        P[i]=[x*r,y*r,z2];
      }
      let df='', db='';
      for(let k=0;k<E.length;k++){
        const a=P[E[k][0]], b=P[E[k][1]];
        const seg='M'+a[0].toFixed(1)+' '+a[1].toFixed(1)+'L'+b[0].toFixed(1)+' '+b[1].toFixed(1);
        if((a[2]+b[2])<0) db+=seg; else df+=seg;
      }
      n.meshB.setAttribute('d',db); n.meshF.setAttribute('d',df);
    }
    function coreGrad(hex){                          /* NEURAL 发光核心渐变 */
      const id='core-'+hex.slice(1);
      if(!gc.has(id)){ gc.set(id,1); const g=el('radialGradient',{id,cx:'50%',cy:'50%',r:'50%'},defs);
        el('stop',{offset:'0%','stop-color':'#ffffff','stop-opacity':1},g);
        el('stop',{offset:'38%','stop-color':hex,'stop-opacity':.95},g);
        el('stop',{offset:'70%','stop-color':hex,'stop-opacity':.35},g);
        el('stop',{offset:'100%','stop-color':hex,'stop-opacity':0},g); }
      return 'url(#'+id+')';
    }
    function neuralArt(n, art){                      /* 暗色·NEURAL：发光核心 + 光线 + 网格球 */
      n.phase=String(n.id||'').split('').reduce((a,c)=>a+c.charCodeAt(0),0)%628/100;
      if(n.kind==='sun'){
        n.haloR=n.r*3.4;
        n.halo=el('circle',{class:'g-halo',r:n.haloR,fill:glow(n.hex,false),opacity:.9,'pointer-events':'none'},art);
        n.rayG=el('g',{class:'g-rays','pointer-events':'none'},art);   /* 外扩光线 */
        const rr=seeded('ray-'+(n.id||'x'));
        for(let k=0;k<20;k++){
          const a=k/20*Math.PI*2+rr()*.26, l=n.r*(1.55+rr()*1.5);
          el('line',{x1:(Math.cos(a)*n.r*1.12).toFixed(1),y1:(Math.sin(a)*n.r*1.12).toFixed(1),
            x2:(Math.cos(a)*l).toFixed(1),y2:(Math.sin(a)*l).toFixed(1),
            stroke:'rgba(255,255,255,'+(0.08+rr()*0.30).toFixed(2)+')','stroke-width':.7},n.rayG);
        }
        n.bodyC=el('circle',{r:0,fill:coreGrad(n.hex),'data-core':'1','pointer-events':'none'},art);
        el('circle',{r:(n.r*.26).toFixed(1),fill:'#fff',opacity:.95,'pointer-events':'none'},art);
      }else if(n.kind==='pillar'){
        n.haloR=n.r*2.4;
        n.halo=el('circle',{class:'g-halo',r:n.haloR,fill:glow(n.hex,false),opacity:.5,'pointer-events':'none'},art);
        n.bodyC=el('circle',{r:0,fill:'rgba(9,9,13,.58)',stroke:'rgba(255,255,255,.24)',
          'stroke-width':.8,'data-core':'1','pointer-events':'none'},art);
        el('circle',{r:(n.r*.34).toFixed(1),fill:n.hex,opacity:.85,'pointer-events':'none'},art);
      }else{                                         /* 碎片光点 */
        n.haloR=n.r*3.2;
        n.halo=el('circle',{class:'g-halo',r:n.haloR,fill:glow(n.hex,false),opacity:.55,'pointer-events':'none'},art);
        n.bodyC=el('circle',{r:0,fill:n.hex,opacity:.92,'data-core':'1','pointer-events':'none'},art);
        n.bodyC.style.animation='g-tw 3.4s ease-in-out '+(n.phase*.6).toFixed(2)+'s infinite';
        return;
      }
      n.meshN=n.kind==='sun'?58:42;
      n.meshT=n.kind==='sun'?.32:.34;
      n.mesh=makeMesh(n);
      n.meshB=el('path',{class:'g-mesh g-mesh-b',fill:'none',stroke:'rgba(255,255,255,.14)',
        'stroke-width':.6,'pointer-events':'none'},art);
      n.meshF=el('path',{class:'g-mesh g-mesh-f',fill:'none',stroke:'rgba(255,255,255,.62)',
        'stroke-width':.7,'pointer-events':'none'},art);
    }
    function draw(n){
      const g=el('g',{class:'g-sd-node '+n.kind,'data-sd-key':n.id},nodeLayer); n.g=g;
      const art=el('g',{class:'art'},g);
      el('circle',{r:Math.max(12,n.r*2.2),fill:'transparent'},g);   /* 命中区 */
      neuralArt(n,art);
      if(n.kind==='sun'){
        if(n.count!=null){   /* 二级中心：名字与条数在球下 */
          const t=el('text',{class:'lbl',y:n.r+22,'text-anchor':'middle'},g); t.textContent=n.title;
          const c=el('text',{class:'cnt',y:n.r+38,'text-anchor':'middle'},g); c.textContent=n.count;
        }else{               /* 一级主线：名字在球下（第一版） */
          const t=el('text',{class:'lbl',y:n.r+24,'text-anchor':'middle'},g); t.textContent=n.title;
        }
      }else if(n.kind==='pillar'){
        const t=el('text',{class:'lbl',y:n.r+24,'text-anchor':'middle'},g); t.textContent=n.title;
        const c=el('text',{class:'cnt',y:n.r+40,'text-anchor':'middle'},g); c.textContent=n.count;
      }else{
        const t=el('text',{class:'fname',y:n.r+10,'text-anchor':'middle'},g);
        t.textContent=n.title.length>12?n.title.slice(0,11)+'…':n.title;
        n.fnameEl=t;
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
          const hk=a.kind==='sun'?.06:a.kind==='pillar'?.035:.022;   /* 碎片归位更弱：让互链织成网 */
          a.vx+=(a.home.x-a.x)*hk*dt; a.vy+=(a.home.y-a.y)*hk*dt;
          a.vx+=Math.sin(now/1500+a.phase)*.0045*dt;
          a.vy+=Math.cos(now/1700+a.phase)*.0045*dt;
          a.vx*=Math.pow(.6,dt); a.vy*=Math.pow(.6,dt);   /* velocityKeep 0.6（学自 obsidian-graph-styler）*/
          a.x=clamp(a.x+a.vx*dt,30,LW-30);
          a.y=clamp(a.y+a.vy*dt,30,LH-30);
        }
        a.g.setAttribute('transform','translate('+a.x.toFixed(1)+' '+a.y.toFixed(1)+')');
        if(a.halo&&a.style0){
          const k=(Math.sin(now/(a.kind==='frag'?1400:3800)+a.phase)+1)/2;
          a.halo.setAttribute('r',(a.haloR*(1+k*.14)).toFixed(1));
          a.halo.setAttribute('opacity',(((a.kind==='frag'?(a.recent?.8:.26)*a.z:.75)*(isLight()?.62:1))*(1+k*.22)).toFixed(2));
        }
      }
      for(const n of nodes){                          /* 网格球旋转 + 光线缓转 */
        if(!n.mesh||n.style0!==1) continue;
        drawMesh(n, now*.00045+n.phase);
        if(n.rayG) n.rayG.setAttribute('transform','rotate('+(now*.0035+n.phase*8).toFixed(1)+')');
      }
      for(const e of edges){
        const ax=e.a.x, ay=e.a.y, bx=e.b.x, by=e.b.y;
        e.el.setAttribute('x1',ax.toFixed(1)); e.el.setAttribute('y1',ay.toFixed(1));
        e.el.setAttribute('x2',bx.toFixed(1)); e.el.setAttribute('y2',by.toFixed(1));
        if(e.gem){ e.gem.setAttribute('cx',((ax+bx)/2).toFixed(1)); e.gem.setAttribute('cy',((ay+by)/2).toFixed(1)); }
      }
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
        tx=CX-(CX-tx)*nx/scale; ty=CY-(CY-ty)*nx/scale; scale=nx; applyView();
      }else if(cmd==='reset-graph'){ scale=1; tx=0; ty=0; applyView(); }
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
    if(!paused) simRaf=requestAnimationFrame(step); else simRaf=requestAnimationFrame(step);
    return {
      control,
      cleanup(){
        cancelAnimationFrame(simRaf);
        abort.abort();
        svg.dataset.motion='stopped';
        bgLayer.replaceChildren(); viewport.replaceChildren(); uiLayer.replaceChildren(); defs.replaceChildren();
        gc.clear();
        const ch=svg.parentElement&&svg.parentElement.querySelector('.g-sd-cards'); if(ch) ch.remove();
        delete svg.dataset.skin;
        if(detail) detail.hidden=true;
      }
    };
  }
  return { mount };
})();
