/* 第二大脑 · 深挖访谈面板（2026-11-16）
   独立脚本，不改 app.js 的大 if 链；靠 MutationObserver 挂到 #interview-card 上。
   规则：访谈挖到的东西先落成「待确认」，用户勾选并点「一键应用」才写入三观卡/成长档案/素材。 */
'use strict';
(function(){
  const THEMEKEY='ip-interview-theme';
  let iv=null,busy=false,loading=false,host=null,seq=0;

  function sourceTag(s){return s==='model'?'AI 追问':'题库';}
  function pids(){return Array.from(host.querySelectorAll('.iv-pick:checked')).map(x=>x.value);}

  function pendingHtml(){
    const list=(iv.pending||[]).filter(x=>x.status==='pending');
    if(!list.length)return '';
    return `<div class="iv-section">
      <div class="iv-section-head"><strong>待确认 ${list.length} 条</strong><span>勾选后点「一键应用」才会写进三观卡、成长档案或素材</span></div>
      ${list.map(x=>`<label class="iv-item"><input type="checkbox" class="iv-pick" value="${esc(x.pid)}" checked>
        <span class="iv-item-main"><span class="iv-tag">${esc(x.target_label||x.note||'')}</span>
        <strong>${esc(x.text)}</strong>${x.quote?`<span class="iv-quote">原话：${esc(x.quote)}</span>`:''}</span></label>`).join('')}
      <div class="iv-actions"><button type="button" class="primary" data-iv-apply>一键应用勾选的 <span class="iv-count">${list.length}</span> 条</button><span class="iv-hint">写入后可在下面一键撤销，回到应用前</span></div>
    </div>`;
  }

  function appliedHtml(){
    const list=iv.applied||[];
    if(!list.length)return '';
    return `<div class="iv-section"><div class="iv-section-head"><strong>已应用 ${list.length} 条</strong><span>撤销会删掉写入的那条，并把它退回待确认</span></div>
      ${list.map(x=>`<div class="iv-applied"><span class="iv-tag">${esc(x.target_label||'')}</span><span class="iv-applied-text">${esc(x.text)}</span><button type="button" data-iv-undo="${esc(x.aid)}">撤销</button></div>`).join('')}
    </div>`;
  }

  function rememberedHtml(){
    const list=iv.remembered||[];
    if(!list.length)return '';
    return `<div class="iv-section"><div class="iv-section-head"><strong>它记得我什么</strong><span>只列你点过「一键应用」的条目</span></div>
      <div class="iv-chips">${list.map(x=>`<span class="iv-chip">${esc(x.text)}</span>`).join('')}</div></div>`;
  }

  function bodyHtml(){
    if(!iv)return '<p class="iv-muted">正在读访谈记录…</p>';
    const s=iv.session||{},q=s.current_question||{};
    let top='';
    if(s.status==='active'){
      top=`<div class="iv-question"><span class="iv-level">第 ${s.level||1}/${iv.max_level||3} 层 · ${sourceTag(q.source)}</span>
        <p>${esc(q.text||'')}</p></div>
        <textarea class="iv-answer" rows="3" placeholder="写一句真话就够，不用组织语言。Ctrl+Enter 提交"></textarea>
        <div class="iv-actions"><button type="button" class="primary" data-iv-send>提交，继续挖</button><button type="button" data-iv-close>先收口</button><span class="iv-hint">最多 3 层，第 3 层答完自动收口</span></div>`;
    }else if(s.status==='closed'){
      top=`<div class="iv-summary${s.safety?' iv-safety':''}"><strong>${s.safety?'先停在这里':'这轮收口'}</strong>
        <p>${esc(s.summary||'')}</p><span class="iv-hint">聊了 ${s.turn_count||0} 层 · 新增 ${s.pending_added||0} 条待确认</span></div>
        <div class="iv-actions"><button type="button" data-iv-start>再聊一轮</button></div>`;
    }else{
      top=`<p class="iv-muted">一次只问一个问题，最多 3 层。挖到的原话先放「待确认」，你点「一键应用」才写进三观卡或成长档案。</p>
        <input class="iv-theme" type="text" maxlength="60" placeholder="想挖什么？例如：不敢涨价 / 和父母的关系（可留空）" value="${esc(localStorage.getItem(THEMEKEY)||'')}">
        <div class="iv-actions"><button type="button" class="primary" data-iv-start>开始访谈</button><span class="iv-hint">访谈不扣额度；只把原话留在本机</span></div>`;
    }
    const fb=(iv.fallbacks||[]).length
      ? `<p class="iv-fb">上一次模型问句没通过校验（${esc((iv.fallbacks[iv.fallbacks.length-1].reasons||[]).join('、')||'接口失败')}），已改用题库。</p>` : '';
    return top+fb+pendingHtml()+appliedHtml()+rememberedHtml();
  }

  function render(){
    if(!host)return;
    const s=(iv&&iv.session)||{};
    const cloud=iv&&iv.settings?iv.settings.cloud:true;
    host.className='iv-card';
    host.innerHTML=`<div class="iv-head">
        <div><div class="eyebrow">DEEP INTERVIEW</div><h2>深挖访谈</h2></div>
        <label class="iv-switch" title="${iv&&iv.cloud_available?'关掉后只问题库，内容不出本机':'云端模型未连接，只能走题库'}"><input type="checkbox" data-iv-cloud ${cloud?'checked':''} ${iv&&iv.cloud_available?'':'disabled'}> 云端追问</label>
      </div>
      <p class="iv-mode">${cloud?'云端只负责决定下一问；问句要过服务端校验，不合规就用题库。':'题库模式：不调用云端，内容不出本机。'}</p>
      <div class="iv-body">${bodyHtml()}</div>`;
    const send=host.querySelector('[data-iv-send]');
    if(send&&busy){send.disabled=true;send.textContent='正在想下一个问题…';}
    const applyBtn=host.querySelector('[data-iv-apply]');
    if(applyBtn){const n=pids().length;applyBtn.disabled=!n;const c=applyBtn.querySelector('.iv-count');if(c)c.textContent=n;}
    const ta=host.querySelector('.iv-answer');
    if(ta&&!busy)ta.focus({preventScroll:true});
  }

  async function load(){
    if(loading)return;
    loading=true;
    const my=++seq;
    try{const s=await api('/interview/state');if(my===seq){iv=s;render();}}
    catch(e){if(host&&my===seq)host.innerHTML=`<div class="iv-head"><div><div class="eyebrow">DEEP INTERVIEW</div><h2>深挖访谈</h2></div></div><p class="iv-muted">访谈记录读取失败：${esc(e.message)}</p>`;}
    finally{loading=false;}
  }

  async function act(fn,okMsg){
    if(busy)return;
    busy=true;render();
    seq++;
    try{
      await fn();
      // 统一回读服务端状态：页面自动刷新、新闻刷新都可能把面板重挂，避免旧响应盖掉新状态。
      // 写入后的这次回读是权威结果，直接采用并让在途的旧 load 作废。
      const fresh=await api('/interview/state');
      iv=fresh;seq++;
      if(okMsg)toast(okMsg);
    }
    catch(e){toast(e.message);}
    finally{busy=false;render();}
  }

  async function start(){
    const input=host.querySelector('.iv-theme');
    const theme=input?input.value.trim():'';
    localStorage.setItem(THEMEKEY,theme);
    await act(()=>api('/interview/start','POST',{theme}),'访谈开始，先答这一问。');
  }

  async function send(){
    const ta=host.querySelector('.iv-answer');
    const text=ta?ta.value.trim():'';
    if(!text){toast('先写点内容再提交；一句真话就够。');return;}
    const sid=(iv.session||{}).id;
    await act(()=>api('/interview/answer','POST',{session_id:sid,text}),null);
  }

  async function closeSession(){
    const sid=(iv.session||{}).id;
    await act(()=>api('/interview/close','POST',{session_id:sid}),'已收口，看看下面的待确认条目。');
  }

  async function apply(){
    const list=pids();
    if(!list.length){toast('先勾选要写入的条目。');return;}
    await act(()=>api('/interview/apply','POST',{pids:list}),'已写入 '+list.length+' 条，可在「已应用」里一键撤销。');
  }

  async function undo(aid){
    if(!aid)return;
    await act(()=>api('/interview/undo','POST',{aids:[aid]}),'已撤销，条目退回待确认。');
  }

  function attach(){
    host.addEventListener('click',event=>{
      const t=event.target.closest('[data-iv-send],[data-iv-close],[data-iv-start],[data-iv-apply],[data-iv-undo]');
      if(!t)return;
      event.preventDefault();
      if(t.hasAttribute('data-iv-send'))return send();
      if(t.hasAttribute('data-iv-close'))return closeSession();
      if(t.hasAttribute('data-iv-start'))return start();
      if(t.hasAttribute('data-iv-apply'))return apply();
      if(t.hasAttribute('data-iv-undo'))return undo(t.getAttribute('data-iv-undo'));
    });
    host.addEventListener('change',event=>{
      const box=event.target.closest('[data-iv-cloud]');
      if(box){
        act(()=>api('/interview/settings','POST',{cloud:box.checked}),box.checked?'已开启云端追问。':'已关掉云端，只用题库。');
        return;
      }
      if(event.target.classList.contains('iv-pick'))render();
    });
    host.addEventListener('keydown',event=>{
      if(event.target.classList&&event.target.classList.contains('iv-answer')&&event.key==='Enter'&&(event.ctrlKey||event.metaKey)){
        event.preventDefault();send();
      }
      if(event.target.classList&&event.target.classList.contains('iv-theme')&&event.key==='Enter'){
        event.preventDefault();start();
      }
    });
  }

  function mount(){
    const card=document.getElementById('interview-card');
    if(!card)return;
    if(card.dataset.ivMounted==='1')return;
    card.dataset.ivMounted='1';
    host=card;
    attach();
    if(iv)render();else card.innerHTML='<p class="iv-muted">正在读访谈记录…</p>';
    load();
  }

  const observer=new MutationObserver(()=>mount());
  if(document.readyState==='loading'){
    document.addEventListener('DOMContentLoaded',()=>{observer.observe(document.getElementById('main')||document.body,{childList:true,subtree:true});mount();});
  }else{
    observer.observe(document.getElementById('main')||document.body,{childList:true,subtree:true});
    mount();
  }
  window.ipInterview={reload:load,mount:mount};
})();
