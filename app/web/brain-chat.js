'use strict';
/* 2026-11-16 第二大脑 · 教练式对话窗口。
   规则：短句结论先行（默认三行，可展开）、每次回答挂一张真实卡片、反问一次一个、
   不谄媚、结尾留一个动作、开场接上次；错误用独立状态条，不进回答。
   视觉：右侧独立窗口（560-640px），可拖出、Esc 收起；不用聊天气泡；
   卡片做成引文条（支柱色条 + 灰底）；动作沉到底部一行小字。
   一处发问，页面入口与独立窗口共用 state.brainChat。 */
(function(){
  const KEY='ip-brain-chat-v1', LOGKEY='ip-brain-chat-log', GAP=70;
  const MAX_QUESTION=300, HISTORY_TURNS=6, HISTORY_CHARS=600;
  const PILLARS=['认知底座','人性关系','赚钱逻辑','操盘手法','成功心法','随手记'];
  const PILLAR_COLOR={'认知底座':'#c9a227','人性关系':'#b0705f','赚钱逻辑':'#6f8f6a',
    '操盘手法':'#857a6a','成功心法':'#c1874a','随手记':'#8a8f98'};
  let root=null, logEl=null, inputEl=null, sendBtn=null;
  let maximized=false, sending=false, stick=true, rendered=0, streamTimer=null, scrollTimer=null;

  const escHtml=s=>String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const nl2br=s=>escHtml(s).replace(/\n/g,'<br>');
  function pref(){try{return JSON.parse(localStorage.getItem(KEY)||'{}')||{}}catch(e){return {}}}
  function savePref(patch){try{localStorage.setItem(KEY,JSON.stringify({...pref(),...patch}))}catch(e){}}
  function chat(){try{if(!Array.isArray(state.brainChat))state.brainChat=[];return state.brainChat}catch(e){return []}}
  function toast(t){try{window.toast&&window.toast(t);}catch(e){}}
  function pageSync(){
    try{
      if(location.hash.startsWith('#/brain')&&window.route){
        Promise.resolve(window.route()).then(()=>{
          const log=document.getElementById('brain-chat-log');
          if(log)log.scrollTop=log.scrollHeight;
        }).catch(()=>{});
      }
    }catch(e){}
  }

  function persistLog(){
    try{
      sessionStorage.setItem(LOGKEY,JSON.stringify(chat().slice(-60).map(m=>({
        role:m.role,text:m.text,source:m.source||'',sourceLabel:m.sourceLabel||'',ms:m.ms||0,
        question:m.question||'',detail:m.detail||'',question_text:m.question_text||'',
        action:m.action||'',opening:m.opening||'',status:m.status||'',
        opening_brief:m.opening_brief||'',
        card:m.card||null,pick_reason:m.pick_reason||'',exit_pressure:m.exit_pressure||null,proactive_memory:m.proactive_memory||null
      }))));
    }catch(e){}
  }
  function loadLog(){
    try{
      const raw=JSON.parse(sessionStorage.getItem(LOGKEY)||'[]');
      return Array.isArray(raw)?raw.filter(x=>x&&x.role&&x.text):[];
    }catch(e){return []}
  }
  function hydrate(){
    try{
      if(!Array.isArray(state.brainChat)||!state.brainChat.length){
        const saved=loadLog();
        if(saved.length)state.brainChat=saved;
      }
    }catch(e){}
  }

  // ------------------------------------------------------------ 渲染
  function userTurn(text){
    const div=document.createElement('div');
    div.className='bc-turn bc-user';
    div.innerHTML=nl2br(text);
    return div;
  }

  function citeBar(card){
    const a=document.createElement('a');
    const weak=!!card.weak;
    a.className='bc-cite'+(weak?' bc-cite-weak':'');
    a.href=card.link||'#/brain';
    a.style.setProperty('--pillar',weak?'#8a8f98':(PILLAR_COLOR[card.category]||'#8a8f98'));
    a.title=card.title||'';
    const date=(card.date||'').slice(5)||'';
    a.innerHTML=`<span class="bc-cite-main"><span class="bc-cite-text">「${escHtml(card.quote||'')}」</span>
      <span class="bc-cite-meta">${escHtml((date?date+' · ':'')+(card.title||''))}${card.confidence&&card.confidence.label?' · '+escHtml(card.confidence.label):''}${weak?' · 略相关':''}</span></span>`;
    a.addEventListener('click',()=>{if(String(a.getAttribute('href')).startsWith('#'))closeWindow();});
    return a;
  }

  function actionRow(m){
    const row=document.createElement('div');
    row.className='bc-actions';
    const save=document.createElement('button');save.type='button';save.textContent='存成卡';
    const link=document.createElement('button');link.type='button';link.textContent='连到支柱';
    const follow=document.createElement('button');follow.type='button';follow.textContent='继续追问';
    save.addEventListener('click',()=>saveCard(m,row,save));
    link.addEventListener('click',()=>togglePillars(m,row,link));
    follow.addEventListener('click',()=>{
      if(!inputEl)return;
      inputEl.value=m.question_text||'';
      inputEl.focus();
      inputEl.style.height='auto';
      inputEl.style.height=Math.min(96,inputEl.scrollHeight)+'px';
    });
    row.append(save,link,follow);
    return row;
  }

  function statusBar(text,isError){
    const bar=document.createElement('div');
    bar.className='bc-status'+(isError?' error':'');
    bar.textContent=text;
    return bar;
  }

  function assistantTurn(m,animate){
    const art=document.createElement('article');
    art.className='bc-turn bc-assistant';
    if(animate)art.classList.add('bc-in');
    const firstInWindow=!logEl||!logEl.querySelector('.bc-assistant');
    if(m.opening&&firstInWindow){
      const open=document.createElement('div');
      open.className='bc-opening';
      open.textContent=m.opening;
      art.appendChild(open);
      // 2026-11-16：开场下面补一句更短的说明（灰色小字，不抢开场句）。
      // 口径就是后端算好的 opening_pick.brief，不在这里另做判断。
      if(m.opening_brief){
        const why=document.createElement('div');
        why.className='bc-pick bc-opening-note';
        why.textContent=m.opening_brief;
        art.appendChild(why);
      }
    }
    const body=document.createElement('div');
    body.className='bc-body';
    const ans=document.createElement('div');
    ans.className='bc-answer';
    ans.innerHTML=nl2br(m.text||'');
    body.appendChild(ans);
    // 2026-11-17 t124：仅通用回答在气泡下方给小字来源提示，正文不再重复；其他来源不显示。
    const isGeneral=(m.source==='general')||/AI 通用回答/.test(String(m.sourceLabel||''));
    if(isGeneral){
      const note=document.createElement('div');
      note.className='bc-general-note small muted';
      note.textContent='通用知识 · 非你的记录';
      body.appendChild(note);
    }
    if(Array.isArray(m.blind_spots)&&m.blind_spots.length){
      const box=document.createElement('div');box.className='bc-blindspots';
      m.blind_spots.forEach(function(x){
        const card=document.createElement('div');card.className='bc-bs-card';
        const title=document.createElement('div');title.className='bc-bs-lens';title.textContent='从没考虑过 · '+(x.title||'');
        const q=document.createElement('p');q.className='bc-bs-dir';q.textContent=x.question||'';
        card.appendChild(title);card.appendChild(q);
        if(x.reason){const why=document.createElement('p');why.className='bc-bs-why';why.textContent=x.reason;card.appendChild(why);}
        box.appendChild(card);
      });
      body.appendChild(box);
    }
    if(Array.isArray(m.revisions)&&m.revisions.length){
      const box=document.createElement('div');box.className='bc-blindspots';
      m.revisions.forEach(function(x){
        const card=document.createElement('div');card.className='bc-bs-card';
        const lens=document.createElement('div');lens.className='bc-bs-lens';lens.textContent='旧判断 → 新记录';
        const oldp=document.createElement('p');oldp.className='bc-bs-why';oldp.textContent='旧：「'+(x.old_claim||'')+'」'+(x.old_date?'（'+x.old_date+'）':'');
        const newp=document.createElement('p');newp.className='bc-bs-dir';newp.textContent='新：「'+(x.new_claim||'')+'」'+(x.new_date?'（'+x.new_date+'）':'');
        const q=document.createElement('p');q.className='bc-bs-risk';q.textContent=x.question||'';
        const act=document.createElement('div');act.className='bc-bs-actions';
        const b=document.createElement('button');b.type='button';b.className='small-button';b.setAttribute('data-bc-revise',(x.old_id||'')+'|'+(x.new_id||''));b.textContent='确认修订（旧卡保留）';act.appendChild(b);
        card.appendChild(lens);card.appendChild(oldp);card.appendChild(newp);card.appendChild(q);card.appendChild(act);box.appendChild(card);
      });
      body.appendChild(box);
    }
    if(Array.isArray(m.capsules)&&m.capsules.length){
      const box=document.createElement('div');box.className='bc-blindspots';
      m.capsules.slice(0,8).forEach(function(x){
        const card=document.createElement('div');card.className='bc-bs-card';
        const lens=document.createElement('div');lens.className='bc-bs-lens';
        lens.textContent=(x.status==='opened'?'已开封':(x.due?'到期':'封存中 · '+x.open_text));
        const text=document.createElement('p');text.className='bc-bs-dir';text.textContent=x.text||'';
        const meta=document.createElement('p');meta.className='bc-bs-why';
        meta.textContent=(x.reason?'理由：'+x.reason+' · ':'')+(x.status==='opened'?'校准：'+({right:'对',partial:'部分对',wrong:'错'}[x.calibration]||'—'):('开封日 '+x.open_text));
        card.appendChild(lens);card.appendChild(text);card.appendChild(meta);
        if(x.due&&x.status==='sealed'){
          const act=document.createElement('div');act.className='bc-bs-actions';
          [['对','right'],['部分对','partial'],['错','wrong']].forEach(function(pair){
            const b=document.createElement('button');b.type='button';b.className='small-button';
            b.setAttribute('data-bc-capsule',(x.id||'')+'|'+pair[1]);b.textContent=pair[0];act.appendChild(b);
          });
          card.appendChild(act);
        }
        box.appendChild(card);
      });
      if(m.capsule_stats&&m.capsule_stats.note){const st=document.createElement('p');st.className='bc-bs-converge';st.textContent=m.capsule_stats.note;box.appendChild(st);}
      body.appendChild(box);
    }
    if(m.detail){
      const detail=document.createElement('div');
      detail.className='bc-detail';
      detail.innerHTML=nl2br(m.detail);
      body.appendChild(detail);
    }
    if(m.brainstorm&&Array.isArray(m.brainstorm.directions)&&m.brainstorm.directions.length){
      const bs=m.brainstorm;
      const box=document.createElement('div');box.className='bc-brainstorm';
      if(bs.counts){
        const meta=document.createElement('p');meta.className='bc-bs-meta';
        meta.textContent='依据你的 '+Number(bs.counts.knowledge||0)+' 条认知 · '+Number(bs.counts.works||0)+' 个作品 · '+Number(bs.counts.voices||0)+' 段口播语料；方向待你验证。';
        box.appendChild(meta);
      }
      bs.directions.forEach(function(d){
        const card=document.createElement('div');card.className='bc-bs-card';
        const lens=document.createElement('div');lens.className='bc-bs-lens';lens.textContent=d.lens||'';
        const dir=document.createElement('p');dir.className='bc-bs-dir';dir.textContent=d.direction||'';
        const why=document.createElement('p');why.className='bc-bs-why';why.textContent='凭什么：'+(d.why||'');
        const step=document.createElement('p');step.className='bc-bs-step';step.textContent='第一步：'+(d.first_step||'');
        const risk=document.createElement('p');risk.className='bc-bs-risk';risk.textContent='最大风险：'+(d.risk||'');
        const actions=document.createElement('div');actions.className='bc-bs-actions';
        const mk=function(label,kind,text){
          const b=document.createElement('button');b.type='button';b.className='small-button';
          b.setAttribute('data-bc-bs',kind);b.setAttribute('data-bs-n',String(d.n||''));
          b.setAttribute('data-bs-text',text);b.textContent=label;return b;
        };
        actions.appendChild(mk('就选这条','pick',d.direction||''));
        actions.appendChild(mk('转选题','topic',d.direction||''));
        actions.appendChild(mk('转目标','goal',d.direction||''));
        card.appendChild(lens);card.appendChild(dir);card.appendChild(why);card.appendChild(step);card.appendChild(risk);card.appendChild(actions);
        box.appendChild(card);
      });
      if(bs.converge){const c=document.createElement('p');c.className='bc-bs-converge';c.textContent=bs.converge;box.appendChild(c);}
      body.appendChild(box);
    }
    let more=null;
    const longText=(String(m.text||'').split('\n').length>3||String(m.text||'').length>110);
    if(m.detail||longText){
      more=document.createElement('button');
      more.type='button';more.className='bc-more';more.textContent='展开';
      more.addEventListener('click',()=>{
        const open=art.classList.toggle('open');
        more.textContent=open?'收起':'展开';
        if(!open)art.scrollIntoView({block:'nearest'});
      });
      body.appendChild(more);
    }
    art.appendChild(body);
    if(m.card){
      art.appendChild(citeBar(m.card));
      if(m.pick_reason){
        const pick=document.createElement('div');
        pick.className='bc-pick';
        pick.textContent='为什么是这张：'+m.pick_reason;
        art.appendChild(pick);
      }
    }
    if(m.exit_pressure&&m.exit_pressure.candidate){
      const ep=m.exit_pressure,cand=ep.candidate;
      const box=document.createElement('div');box.className='bc-pick bc-exit-pressure';
      const line=document.createElement('div');
      line.textContent='你的记录已经攒了 '+(Number(ep.count)||0)+' 条。挑一条先写成稿，不用一次讲完。';
      box.appendChild(line);
      const title=document.createElement('strong');title.textContent=cand.title||'先写这一条';
      box.appendChild(title);
      const a=document.createElement('a');a.className='button-link';a.textContent='把它写成稿';
      const base=ep.create_url||'#/create';
      a.href=cand.id?base+(base.includes('?')?'&':'?')+'knowledge_id='+encodeURIComponent(cand.id):base;
      box.appendChild(a);
      art.appendChild(box);
    }
    if(m.proactive_memory&&(m.proactive_memory.kind==='contrast'||m.proactive_memory.kind==='change')){
      const pm=m.proactive_memory,box=document.createElement('div');box.className='bc-pick bc-proactive-memory';
      let text='';
      if(pm.kind==='contrast'){
        const o=pm.old||{};text='你以前写过：「'+(o.quote||'')+'」'+(o.date?'（'+o.date+'）':'')+'，对照看看。';
      }else{
        text='你这几个月想法变了：旧「'+(pm.before||'')+'」/ 新「'+(pm.after||'')+'」，两个都留着。';
      }
      box.textContent=text;
      art.appendChild(box);
    }
    art.appendChild(actionRow(m));
    if(m.status)art.appendChild(statusBar(m.status,false));
    return art;
  }

  function renderAll(){
    if(!logEl)return;
    logEl.innerHTML='';rendered=0;
    const msgs=chat();
    if(!msgs.length){
      const hint=document.createElement('div');
      hint.className='bc-empty';
      hint.textContent='问点具体的，比如：上次打卡是哪天？最近有什么成片？我那几条关于三观和价值观的记录。';
      logEl.appendChild(hint);
    }
    msgs.forEach(m=>{
      if(m.role==='user')logEl.appendChild(userTurn(m.text));
      else logEl.appendChild(assistantTurn(m,false));
    });
    rendered=msgs.length;
  }

  function sync(){
    if(!root||root.hidden)return;
    const msgs=chat();
    const empty=logEl.querySelector('.bc-empty');
    if(empty&&msgs.length)empty.remove();
    for(let i=rendered;i<msgs.length;i++){
      const m=msgs[i];
      logEl.appendChild(m.role==='user'?userTurn(m.text):assistantTurn(m,true));
    }
    rendered=msgs.length;
    if(stick)scrollBottom(false);
    persistLog();
  }

  // ------------------------------------------------------------ 动作
  async function saveCard(m,row,btn){
    const title=String(m.question||m.text||'知识库对话').replace(/\s+/g,' ').slice(0,60);
    const claim=String(m.text||'').slice(0,1200);
    if(!title||!claim){statusInline(row,'这条还没有可存的正文。',true);return;}
    btn.disabled=true;btn.textContent='保存中…';
    try{
      const rec=await window.api('/knowledge','POST',{title,claim,category:'随手记',
        boundary:'来自知识库对话，待本人核对'});
      btn.textContent='已存成卡';
      statusInline(row,'已存进「随手记」：'+title.slice(0,24),false);
      if(rec&&rec.id)row.dataset.cardId=rec.id;
    }catch(e){
      btn.disabled=false;btn.textContent='存成卡';
      statusInline(row,'存卡失败：'+(e.message||'接口不可用'),true);
    }
  }

  function statusInline(row,text,isError){
    let bar=row.parentNode.querySelector('.bc-action-status');
    if(!bar){
      bar=statusBar(text,isError);
      bar.classList.add('bc-action-status');
      row.parentNode.appendChild(bar);
    }else{
      bar.textContent=text;
      bar.classList.toggle('error',!!isError);
    }
  }

  function togglePillars(m,row,btn){
    const old=row.parentNode.querySelector('.bc-pillars');
    if(old){old.remove();return;}
    if(!m.card||!m.card.id){statusInline(row,'这条没有可归属的卡片。',true);return;}
    const box=document.createElement('div');
    box.className='bc-pillars';
    PILLARS.forEach(name=>{
      const b=document.createElement('button');
      b.type='button';b.textContent=name;
      b.addEventListener('click',()=>linkPillar(m,row,box,name));
      box.appendChild(b);
    });
    row.parentNode.insertBefore(box,row.nextSibling);
  }

  async function linkPillar(m,row,box,name){
    box.querySelectorAll('button').forEach(b=>b.disabled=true);
    try{
      await window.api('/knowledge/'+encodeURIComponent(m.card.id),'PATCH',
        {category:name,revision:m.card.revision||0});
      box.remove();
      statusInline(row,'已连到「'+name+'」下面。',false);
    }catch(e){
      box.querySelectorAll('button').forEach(b=>b.disabled=false);
      statusInline(row,'连支柱失败：'+(e.message||'接口不可用'),true);
    }
  }

  // 2026-11-17 t100：预设问题 chip 样式（CSSOM，CSP 不写内联 style）
  let presetCss=false;
  function ensurePresetStyle(){
    if(presetCss)return;
    presetCss=true;
    const rules=[
      '.bc-presets{display:flex;flex-wrap:wrap;gap:6px;padding:8px 12px 0}',
      '.bc-chip{border:1px solid var(--border-strong,rgba(255,255,255,.22));background:transparent;color:var(--text,#fbf8f1);border-radius:999px;padding:5px 10px;font-size:12px;line-height:1.3;cursor:pointer;font-family:inherit}',
      '.bc-chip:hover,.bc-chip:focus-visible{border-color:var(--gold,#d3ad70);color:var(--gold,#d3ad70)}',
      '.bc-chip.is-off{opacity:.5;border-style:dashed}',
      '.bc-tones{display:flex;align-items:center;flex-wrap:wrap;gap:6px;padding:6px 12px 0}',
      '.bc-tone-label{font-size:11px;color:var(--muted,#9b968c)}',
      '.bc-tone{border:1px solid var(--border,rgba(255,255,255,.16));background:transparent;color:var(--text,#fbf8f1);border-radius:999px;padding:3px 9px;font-size:11.5px;cursor:pointer;font-family:inherit}',
      '.bc-tone.is-on{border-color:var(--gold,#d3ad70);color:var(--gold,#d3ad70)}',
      '.bc-brainstorm{display:grid;gap:8px;margin-top:10px}',
      '.bc-bs-meta{margin:0;font-size:11px;color:var(--muted,#9b968c)}',
      '.bc-bs-card{border:1px solid var(--border,rgba(255,255,255,.16));border-left:3px solid var(--gold,#d3ad70);border-radius:8px;padding:9px 11px;background:var(--surface-2,rgba(255,255,255,.03))}',
      '.bc-bs-lens{font-size:11px;letter-spacing:.06em;color:var(--gold,#d3ad70)}',
      '.bc-bs-dir{margin:4px 0;font-size:13px;font-weight:600;line-height:1.6}',
      '.bc-bs-why,.bc-bs-step,.bc-bs-risk{margin:2px 0;font-size:12px;line-height:1.65;color:var(--text,#fbf8f1)}',
      '.bc-bs-risk{color:var(--muted,#9b968c)}',
      '.bc-bs-actions{display:flex;flex-wrap:wrap;gap:6px;margin-top:7px}',
      '.bc-bs-converge{margin:2px 0 0;font-size:12px;line-height:1.7;color:var(--muted,#9b968c)}',
      '@media (max-width:480px){.bc-presets{padding:6px 10px 0;gap:5px}.bc-chip{font-size:11.5px;padding:4px 8px}}'
    ];
    try{
      if(typeof ipAddCssRules==='function'){ipAddCssRules(rules);return;}
      const sheet=new CSSStyleSheet();
      rules.forEach((r,i)=>sheet.insertRule(r,i));
      document.adoptedStyleSheets=[...document.adoptedStyleSheets,sheet];
    }catch(err){/* 样式失败不影响提问 */}
  }

  // 2026-11-17 t105：云端预算用完时把「模型类」chip 置灰并说明（本地类不受影响）。
  let budgetChipDone=false;
  // 2026-11-19 t153：语气选择（默认「你的语气」）——只改措辞，不冒充身份。
  const TONES=[['mine','你的语气'],['direct','更直接'],['friend','朋友'],['mentor','导师']];
  let toneName='mine';
  try{toneName=localStorage.getItem('ip-brain-tone')||'mine';}catch(e){}
  if(!TONES.some(x=>x[0]===toneName))toneName='mine';
  function toneChipHtml(){return '<div class="bc-tones" id="bc-tones" role="group" aria-label="回答语气"><span class="bc-tone-label">语气</span>'+
    TONES.map(([k,label])=>'<button type="button" class="bc-tone'+(toneName===k?' is-on':'')+'" data-bc-tone="'+k+'" aria-pressed="'+(toneName===k?'true':'false')+'">'+label+'</button>').join('')+'</div>';}
  function refreshBudgetChips(){
    if(budgetChipDone)return;
    budgetChipDone=true;
    const chips=[...document.querySelectorAll('.bc-chip[data-bc-cost="model"]')];
    if(!chips.length)return;
    fetch('/api/brain/budget',{headers:{'Accept':'application/json'}}).then(r=>r.ok?r.json():null).then(b=>{
      if(!b)return;
      const off=(b.enabled===false)||(typeof b.remaining==='number'&&b.remaining<=0)
        ||(typeof b.remaining_yuan==='number'&&b.remaining_yuan<=0);
      chips.forEach(c=>{
        if(!off){c.classList.remove('is-off');return;}
        c.classList.add('is-off');
        c.title='今日云端额度已用完，先用左边本地问题；额度恢复后可问通用/写作类';
      });
    }).catch(()=>{/* 预算接口暂不可用：保持原样，不阻塞提问 */});
  }

  // ------------------------------------------------------------ 窗口
  function build(){
    if(root)return;
    root=document.createElement('section');
    root.id='brain-chat-window';
    root.className='bc-window';
    root.hidden=true;
    root.setAttribute('role','dialog');
    root.setAttribute('aria-modal','false');
    root.setAttribute('aria-label','知识库对话窗口');
    root.innerHTML=`
      <header class="bc-head"><span class="bc-title">知识库<small>你的记录 · 深度对话</small></span>
        <span class="bc-tools">
          <button type="button" data-bc="dock" aria-label="停靠右侧" title="停靠右侧">⇥</button>
          <button type="button" data-bc="max" aria-label="最大化" title="最大化">⤢</button>
          <button type="button" data-bc="close" aria-label="关闭" title="关闭">✕</button>
        </span>
      </header>
      <div class="bc-log" id="bc-log" role="log" aria-live="polite"></div>
      <div class="bc-presets" id="bc-presets" aria-label="快捷提问">
        <button type="button" class="bc-chip" data-bc-ask="我最近记了哪些想法？">最近记了什么</button>
        <button type="button" class="bc-chip" data-bc-ask="我最独特的那句观点是什么？">我最独特的观点</button>
        <button type="button" class="bc-chip" data-bc-ask="你跟其他AI有什么区别？">和别的 AI 有什么不同</button>
        <button type="button" class="bc-chip" data-bc-ask="什么是复利？" data-bc-cost="model" title="通用知识需要模型额度">什么是复利</button>
        <button type="button" class="bc-chip" data-bc-ask="帮我把一个想法写成口播稿" data-bc-cost="model" title="写作类需要模型额度">写成口播稿</button>
        <button type="button" class="bc-chip" data-bc-ask="未来一年我该往哪走？">一起想未来方向</button>
        <button type="button" class="bc-chip" data-bc-ask="我有什么盲点？">我有什么盲点</button>
        <button type="button" class="bc-chip" data-bc-ask="如果我这么做，三个月后会怎样？">事前验尸</button>
        <button type="button" class="bc-chip" data-bc-ask="我的观点有矛盾吗？">我的观点有矛盾吗</button>
        <button type="button" class="bc-chip" data-bc-ask="今日晨报">今日晨报</button>
        <button type="button" class="bc-chip" data-bc-ask="我的时间胶囊">我的时间胶囊</button>
        <button type="button" class="bc-chip" data-bc-ask="封一个时间胶囊">封一个时间胶囊</button>
      </div>
      ${toneChipHtml()}
      <form class="bc-form" id="bc-form">
        <textarea name="q" rows="1" placeholder="问知识库…（Enter 发送，Shift+Enter 换行）" aria-label="问知识库"></textarea>
        <button type="submit" class="primary bc-send">发送</button>
      </form>
      <p class="bc-sovereignty small muted">记录只存本机 · 模型只收到本次提示词 · 不用于训练 · <a class="link small" href="#/settings">整体导出 / 删除说明</a></p>`;
    document.body.appendChild(root);
    logEl=root.querySelector('#bc-log');
    inputEl=root.querySelector('textarea');
    sendBtn=root.querySelector('.bc-send');
    root.addEventListener('click',e=>{
      const tone=e.target.closest('[data-bc-tone]');
      if(tone){
        toneName=tone.getAttribute('data-bc-tone')||'mine';
        try{localStorage.setItem('ip-brain-tone',toneName);}catch(err){}
        root.querySelectorAll('[data-bc-tone]').forEach(b=>{
          const on=b.getAttribute('data-bc-tone')===toneName;
          b.classList.toggle('is-on',on);b.setAttribute('aria-pressed',on?'true':'false');
        });
        return;
      }
      const caps=e.target.closest('[data-bc-capsule]');
      if(caps){
        if(sending)return;
        send('[[capsule:open|'+caps.getAttribute('data-bc-capsule')+']]');
        return;
      }
      const rev=e.target.closest('[data-bc-revise]');
      if(rev){
        if(sending)return;
        send('[[revise:'+rev.getAttribute('data-bc-revise')+']]');
        return;
      }
      const bsb=e.target.closest('[data-bc-bs]');
      if(bsb){
        if(sending)return;
        const kind=bsb.getAttribute('data-bc-bs'),n=bsb.getAttribute('data-bs-n'),text=bsb.getAttribute('data-bs-text')||'';
        if(kind==='pick')send('选第'+n+'条：'+text);
        else if(kind==='topic')send('转选题：'+text);
        else if(kind==='goal')send('转目标：'+text);
        return;
      }
      const chip=e.target.closest('[data-bc-ask]');
      if(chip){
        const q=chip.getAttribute('data-bc-ask')||'';
        if(!q||sending)return;
        send(q);
        return;
      }
      const b=e.target.closest('[data-bc]');
      if(!b)return;
      if(b.dataset.bc==='max')toggleMax();
      if(b.dataset.bc==='close')closeWindow();
      if(b.dataset.bc==='dock')dock();
    });
    ensurePresetStyle();
    root.querySelector('.bc-head').addEventListener('pointerdown',startDrag);
    root.querySelector('#bc-form').addEventListener('submit',e=>{e.preventDefault();submit();});
    inputEl.addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();submit();}});
    inputEl.addEventListener('input',()=>{
      inputEl.style.height='auto';
      inputEl.style.height=Math.min(96,inputEl.scrollHeight)+'px';
    });
    logEl.addEventListener('scroll',()=>{
      stick=nearBottom();
      clearTimeout(scrollTimer);
      scrollTimer=setTimeout(()=>savePref({scroll:logEl.scrollTop}),180);
    },{passive:true});
    window.addEventListener('resize',()=>{if(!maximized&&root.classList.contains('bc-detached'))clampIntoView();});
    document.addEventListener('keydown',e=>{
      if(e.key==='Escape'&&root&&!root.hidden&&!document.querySelector('dialog[open]'))closeWindow();
    });
  }

  function startDrag(e){
    if(e.target.closest('[data-bc]')||maximized)return;
    const rect=root.getBoundingClientRect();
    const offX=e.clientX-rect.left, offY=e.clientY-rect.top;
    root.classList.remove('bc-max');
    root.classList.add('bc-detached');
    root.style.width=Math.max(520,Math.min(680,rect.width))+'px';
    root.style.left=rect.left+'px';
    root.style.top=rect.top+'px';
    root.style.right='auto';
    root.style.bottom='auto';
    root.style.height=rect.height+'px';
    document.body.classList.add('bc-detached');
    const move=ev=>{
      const left=Math.max(4,Math.min(window.innerWidth-360,ev.clientX-offX));
      const top=Math.max(4,Math.min(window.innerHeight-120,ev.clientY-offY));
      root.style.left=left+'px';root.style.top=top+'px';
    };
    const up=()=>{
      window.removeEventListener('pointermove',move);
      window.removeEventListener('pointerup',up);
      const r=root.getBoundingClientRect();
      savePref({detached:true,left:r.left,top:r.top,width:r.width,height:r.height});
      stick=true;scrollBottom(false);
    };
    window.addEventListener('pointermove',move);
    window.addEventListener('pointerup',up);
    e.preventDefault();
  }

  function clampIntoView(){
    const r=root.getBoundingClientRect();
    if(r.right>window.innerWidth||r.bottom>window.innerHeight){
      root.style.left=Math.max(8,Math.min(window.innerWidth-r.width-8,r.left))+'px';
      root.style.top=Math.max(8,Math.min(window.innerHeight-r.height-8,r.top))+'px';
    }
  }

  function dock(){
    root.classList.remove('bc-detached','bc-max');
    root.style.cssText='';
    document.body.classList.remove('bc-detached');
    savePref({detached:false,left:null,top:null,width:null,height:null});
    stick=true;scrollBottom(false);
    inputEl&&inputEl.focus();
  }

  function applyPref(){
    if(maximized){root.classList.add('bc-max');return;}
    const p=pref();
    if(p.detached){
      root.classList.add('bc-detached');
      document.body.classList.add('bc-detached');
      if(Number.isFinite(p.width))root.style.width=Math.max(520,Math.min(680,p.width))+'px';
      if(Number.isFinite(p.height))root.style.height=Math.max(360,p.height)+'px';
      if(Number.isFinite(p.left))root.style.left=p.left+'px';
      if(Number.isFinite(p.top))root.style.top=p.top+'px';
      clampIntoView();
    }else{
      document.body.classList.remove('bc-detached');
    }
  }

  function toggleMax(){
    maximized=!maximized;
    applyPref();
    savePref({max:maximized});
    stick=true;scrollBottom(false);
  }

  function openWindow(){
    setTimeout(refreshBudgetChips,0);
    build();hydrate();
    try{const modal=document.querySelector('dialog[open]');if(modal)modal.close();}catch(e){}
    root.hidden=false;
    document.body.classList.add('bc-open');
    maximized=!!pref().max;
    applyPref();
    renderAll();
    const saved=pref().scroll;
    requestAnimationFrame(()=>{
      logEl.scrollTop=Number.isFinite(saved)?saved:logEl.scrollHeight;
      stick=nearBottom();
      inputEl.focus();
    });
  }

  function closeWindow(){
    if(!root||root.hidden)return;
    savePref({scroll:logEl.scrollTop});
    root.hidden=true;
    document.body.classList.remove('bc-open','bc-detached');
  }

  // ------------------------------------------------------------ 发送
  function updateSendState(on){
    if(sendBtn){sendBtn.disabled=on;sendBtn.textContent=on?'思考中…':'发送';}
    if(inputEl)inputEl.placeholder=on?'知识库正在查你的记录…':'问知识库…（Enter 发送，Shift+Enter 换行）';
  }

  function nearBottom(){return !logEl||logEl.scrollHeight-logEl.scrollTop-logEl.clientHeight<GAP;}
  function scrollBottom(smooth){if(!logEl)return;logEl.scrollTo({top:logEl.scrollHeight,behavior:smooth?'smooth':'auto'});}

  function submit(){
    const q=inputEl?inputEl.value.trim():'';
    if(!q)return;
    if(q.length>MAX_QUESTION){toast('内容偏长，建议精简后再发送。');return;}
    inputEl.value='';
    inputEl.style.height='auto';
    send(q);
  }

  function streamInto(el,text,done){
    const total=text.length;
    const chunk=Math.max(3,Math.ceil(total/120));
    let i=0;
    el.style.minHeight='calc(3 * 1.75em)';
    const tick=()=>{
      if(!root||root.hidden||!el.isConnected){
        el.innerHTML=nl2br(text);
        el.style.minHeight='';
        done();return;
      }
      i=Math.min(total,i+chunk);
      el.innerHTML=nl2br(text.slice(0,i));
      if(stick)scrollBottom(false);
      if(i>=total){el.style.minHeight='';done();return;}
      streamTimer=setTimeout(tick,20);
    };
    tick();
  }

  async function send(text){
    const q=String(text||'').trim();
    if(!q)return;
    if(sending){toast('上一个问题还在回答，稍等。');return;}
    if(q.length>MAX_QUESTION){toast('内容偏长，建议精简后再发送。');return;}
    if(!root||root.hidden)openWindow();
    if(!root||root.hidden)return;
    sending=true;updateSendState(true);
    const history=chat().slice(-HISTORY_TURNS).map(m=>({role:m.role,text:String(m.text||'').slice(0,HISTORY_CHARS)}));
    const question=q;
    chat().push({role:'user',text:q});
    sync();
    stick=true;scrollBottom(true);
    const typing=statusBar('正在查你的记录…',false);
    logEl.appendChild(typing);scrollBottom(false);
    try{
      const wire=toneName&&toneName!=='mine'?('[[tone:'+toneName+']] '+q):q;
      const r=await window.api('/brain/ask','POST',{question:wire,history});
      typing.remove();
      if(!r||r.ok===false){
        addStatus((r&&(r.message||r.error))||'知识库没有返回结果，稍后再试。',true);
        persistLog();pageSync();
        return;
      }
      // 2026-11-17 t123：聊天里不显示「今日云端额度剩余…」这类技术噪音（backend 已不再拼，这里再兜一层）。
      let cleanAnswer=String(r.answer||'').replace(/[。\s]*(?:·\s*)?今日云端额度剩余[^。\n]*。?/g,'').trim();
      // 通用标签兜底去重：万一后端旧版/模型重复，也只保留一次「（网名：）这是通用知识，不是你的记录。」
      const label=/^([^：:\n]{1,12}[：:])?\s*((?:这是通用知识[，,]?\s*(?:不是你的记录)?[。！!]?\s*)+)/.exec(cleanAnswer);
      if(label&&label[2].length>0){cleanAnswer=(label[1]||'')+'这是通用知识，不是你的记录。'+cleanAnswer.slice(label[0].length).trim();}
      const msg={role:'assistant',text:cleanAnswer,question:q,
        source:r.mode||'',sourceLabel:r.source_label||'',ms:r.ms||0,
        card:r.card||null,pick_reason:r.pick_reason||'',detail:r.detail||'',exit_pressure:r.exit_pressure||null,proactive_memory:r.proactive_memory||null,
        question_text:r.question_text||'',action:r.action||'',opening:r.opening||'',
        opening_brief:(r.opening_pick&&r.opening_pick.brief)||'',
        brainstorm:r.brainstorm||null,
        blind_spots:r.blind_spots||null,
        revisions:r.revisions||null,
        capsules:r.capsules||null,capsule_stats:r.capsule_stats||null,capsule_due:r.capsule_due||null,
        status:r.status||''};
      chat().push(msg);
      const el=assistantTurn(msg,true);
      logEl.appendChild(el);
      rendered=chat().length;
      stick=true;scrollBottom(true);
      const ans=el.querySelector('.bc-answer');
      if(ans)streamInto(ans,msg.text,()=>{persistLog();});
      else persistLog();
    }catch(e){
      typing.remove();
      persistLog();
      addStatus('这次没连上知识库：'+(e.message||'网络或服务异常'),true);
    }finally{
      sending=false;updateSendState(false);persistLog();pageSync();
      // 2026-11-18 云端积分真源：每次问答结束（成功/失败都算）强制刷新云端余额，
      // 后台加减积分与本次扣费当场可见；刷新失败静默，不影响聊天。
      try{if(typeof window.refreshBilling==='function')Promise.resolve(window.refreshBilling(true)).catch(()=>{});}catch(e){}
    }
  }

  function addStatus(text,isError){
    if(!logEl)return;
    const bar=statusBar(text,isError);
    logEl.appendChild(bar);
    if(stick)scrollBottom(false);
  }

  function syncPageLog(){
    try{
      if(location.hash.startsWith('#/brain')&&window.route)Promise.resolve(window.route()).catch(()=>{});
    }catch(e){}
  }

  window.ipBrainChat={
    open:openWindow,
    close:closeWindow,
    toggle:()=>{if(!root||root.hidden)openWindow();else closeWindow();},
    send,
    ask:(text)=>{openWindow();return send(text);}
  };
})();
