'use strict';
window.ipGrowth = (() => {
  let data, assetsData = null, talentData = null, checkinData = null, query, graphData, graphCards, graphCardsError = '', graphCardsLoading, recorder, stream, recordingTimer, audioBlob, voiceId = '', pending = false, captureGeneration = 0;
  let graphControl, cleanupGraph, cleanupUI, cleanupPillarBar, themeObs = null, mountedHash = '', reviewBaseline = '', modalBaseline = '', modalGeneration = 0;
  const scrollPositions = new Map();
  const e = value => esc(value);
  const shapes = {
    home:'M3 10 12 3l9 7v10H3Z M9 20v-7h6v7',
    target:'M20 12a8 8 0 1 1-8-8 M16 8l5-5 M16 3v5h5 M16 12a4 4 0 1 1-4-4',
    text:'M6 3h9l4 4v14H6Z M14 3v5h5 M9 12h7 M9 16h7',
    graph:'M12 4v6 M5 14v-4h14v4 M2 14h6v6H2Z M9 1h6v6H9Z M16 14h6v6h-6Z',
    calendar:'M4 5h16v16H4Z M8 2v6 M16 2v6 M4 10h16 M8 14h1 M15 14h1 M8 18h1',
    brain:'M9 4a3 3 0 0 0-5 3 4 4 0 0 0-1 7 4 4 0 0 0 6 5 M15 4a3 3 0 0 1 5 3 4 4 0 0 1 1 7 4 4 0 0 1-6 5 M12 3v18 M7 8l2 2 M17 8l-2 2 M6 15l3-1 M18 15l-3-1',
    star:'m12 3 3 6 7 1-5 5 1 7-6-3-6 3 1-7-5-5 7-1Z',
    energy:'m13 2-9 12h7l-1 8 10-13h-8Z',
    heart:'M12 21S2 15 2 8a5 5 0 0 1 10-1 5 5 0 0 1 10 1c0 7-10 13-10 13Z',
    value:'m3 8 4-5h10l4 5-9 14Z M3 8h18 M8 8l4 14 4-14 M7 3l1 5 M17 3l-1 5',
    folder:'M3 6V4h6l2 3h10l-3 13H3Z',
    link:'M10 14a5 5 0 0 0 7 0l3-3a5 5 0 0 0-7-7l-2 2 M14 10a5 5 0 0 0-7 0l-3 3a5 5 0 0 0 7 7l2-2',
    voice:'M9 4a3 3 0 0 1 6 0v8a3 3 0 0 1-6 0Z M5 10v2a7 7 0 0 0 14 0v-2 M12 19v3 M8 22h8',
    inbox:'M3 4h18v16H3Z M3 13h5l2 3h4l2-3h5',
    plus:'M12 4v16 M4 12h16',
    arrow:'M5 12h14 M13 6l6 6-6 6',
    chevron:'m8 4 8 8-8 8',
    dots:'M4 12h.01 M12 12h.01 M20 12h.01',
    search:'M16 16l5 5 M18 10a8 8 0 1 1-16 0 8 8 0 0 1 16 0',
    pause:'M8 4v16 M16 4v16',
    play:'m7 3 14 9-14 9Z',
    close:'M5 5l14 14 M19 5 5 19',
    check:'m4 12 5 5L20 6',
    layers:'m12 2 10 6-10 6L2 8Z M2 12l10 6 10-6 M2 16l10 6 10-6'
  };
  const icon = name => `<svg class="g-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.65" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="${shapes[name]||shapes.target}"/></svg>`;
  window.growthIcon = icon;
  /* ===== 统一悬停释义层（2026-11-17 · t24）=====
   * 文案集中一张常量表；悬停信息卡＝HTML overlay，样式全部走 CSSOM（CSP style-src 'self'，不写内联 style）。
   * 支柱可改名：先按默认名匹配，再按默认顺序 sort_order 兜底，最后给通用释义；计数一律用真实数据。 */
  const GLOSSARY = {
    pillarOrder: ['认知', '意义', '能量', '关系', '价值'],
    pillars: {
      '认知': { meaning: '你怎么看世界——判断、原则、想明白的道理。', use: '记下每次「想通了」的一刻，成为做选择的底气。' },
      '意义': { meaning: '你为什么而做——驱动你的信念与心法。', use: '写下支撑你坚持的「为什么」。' },
      '能量': { meaning: '你的状态与节奏——精力、情绪、可持续性。', use: '记录让你充电或耗竭的事，找到节奏。' },
      '关系': { meaning: '你与人怎么相处——对人性的理解、关系经营。', use: '记下人际里踩过的坑与悟到的规律。' },
      '价值': { meaning: '你怎么创造价值把事做成——赚钱逻辑与做事方法。', use: '存验证过的赚钱思路与可复用打法。' }
    },
    pillarFallback: { meaning: '这是你自己定的方向，把相关的积累挂到这里。', use: '记下与这个方向有关的碎片，慢慢长出你自己的地图。' },
    pillarZero: '这里还空着。想到什么就记一条，第一条就从此刻开始。',
    stats: {
      'stat-goals': { title: '有效目标', text: '你立的主线＋下面的小目标数量（不含已归档）。目标给积累一个方向。' },
      'stat-fragments': { title: '全部积累', text: '知识池里有效碎片的总数：随手记、链接转写、语音与关联进来的旧认知都算。' },
      'stat-month': { title: '本月记录', text: '本月新增的碎片数。持续记录，积累才会慢慢连成网。' },
      'stat-inbox': { title: '等待归位', text: '还没关联到任何目标的碎片。点数字可跳到待整理清单，逐条归位。' }
    },
    terms: {
      'goal-system': { title: '目标体系', text: '主线 → 五支柱 → 小目标的树：把日常碎片挂上去，积累就有方向。' },
      'count-manual': { title: '手动', text: '你亲手标注的碎片：记录时自己选择关联到哪个目标。' },
      'count-auto': { title: '知识库', text: '按分类自动归进来的知识内容，计入对应目标；不会改写原文。' }
    },
    overviewValue: '记下日常碎片，挂到你的人生主线上，慢慢连成一张网——让积累不白费。'
  };
  const pillarGloss = (title, sortOrder) => GLOSSARY.pillars[title]
    || (Number.isInteger(sortOrder) && GLOSSARY.pillars[GLOSSARY.pillarOrder[sortOrder]])
    || GLOSSARY.pillarFallback;
  function pillarGlossHTML(title, sortOrder, count) {
    const g = pillarGloss(title, sortOrder);
    const n = Number(count) || 0;
    return '<strong>' + e(title) + '</strong>'
      + '<p>' + e(g.meaning) + '</p>'
      + '<p class="g-gloss-count">' + (n > 0 ? '你这里已积累 ' + n + ' 条' : e(GLOSSARY.pillarZero)) + '</p>'
      + '<p class="g-gloss-use">怎么用好：' + e(g.use) + '</p>';
  }
  let glossEl = null, glossRule = null, glossTrigger = null;
  function glossSheet() {
    if (glossRule) return glossRule;
    /* 构造式样式表（纯 CSSOM）：CSP style-src 'self' 下连空 <style> 元素都会被拦（空串哈希），
     * 所以不向 DOM 插 <style>，用 CSSStyleSheet + adoptedStyleSheets，定位改规则不改元素。 */
    const sheet = new CSSStyleSheet();
    [
      '.g-gloss-card{position:fixed;left:0;top:0;z-index:80;width:max-content;max-width:min(320px,calc(100vw - 16px));padding:13px 15px;background:var(--surface);border:1px solid var(--border-strong);border-radius:12px;box-shadow:0 12px 35px #0003;pointer-events:none;opacity:0;visibility:hidden;transition:opacity .12s ease;color:var(--text);font-size:12px;line-height:1.8}',
      '.g-gloss-card.g-gloss-on{opacity:1;visibility:visible}',
      '.g-gloss-card strong{display:block;font-size:13px;font-weight:650;color:var(--gold);margin-bottom:4px}',
      '.g-gloss-card p{margin:0;color:var(--text);overflow-wrap:anywhere}',
      '.g-gloss-card .g-gloss-count{color:var(--gold);font-weight:600;margin-top:5px}',
      '.g-gloss-card .g-gloss-use{color:var(--muted);margin-top:5px}',
      '.g-overview-value{margin:14px 2px 0;font-size:12px;color:var(--muted)}',
      '.g-stats strong a{color:inherit;text-decoration:none;cursor:pointer}',
      '.g-stats strong a:hover{color:var(--gold)}',
      '@media(prefers-reduced-motion:reduce){.g-gloss-card{transition:none}}'
    ].forEach((text, i) => sheet.insertRule(text, i));
    document.adoptedStyleSheets = [...document.adoptedStyleSheets, sheet];
    glossRule = sheet.cssRules[0];
    return glossRule;
  }
  function showGloss(anchor, html) {
    if (!anchor || !anchor.getBoundingClientRect) return;
    const rule = glossSheet();
    if (!glossEl) {
      glossEl = document.createElement('div');
      glossEl.id = 'g-gloss-card';
      glossEl.className = 'g-gloss-card';
      glossEl.setAttribute('role', 'tooltip');
      document.body.appendChild(glossEl);
    }
    glossEl.innerHTML = html;
    glossEl.classList.add('g-gloss-on');
    const rect = anchor.getBoundingClientRect();
    const vw = document.documentElement.clientWidth, vh = document.documentElement.clientHeight;
    const box = glossEl.getBoundingClientRect();
    let left = rect.left + rect.width / 2 - box.width / 2;
    left = Math.max(8, Math.min(left, vw - box.width - 8));
    let top = rect.bottom + 10;
    if (top + box.height > vh - 8) top = rect.top - box.height - 10;
    top = Math.max(8, Math.min(top, vh - box.height - 8));
    rule.style.left = Math.round(left) + 'px';
    rule.style.top = Math.round(top) + 'px';
  }
  function hideGloss() {
    glossTrigger = null;
    if (glossEl) glossEl.classList.remove('g-gloss-on');
  }
  function glossContent(trigger) {
    const key = trigger.dataset.gloss;
    if (key === 'pillar') {
      const goal = data && data.goals.find(g => g.id === trigger.dataset.id);
      if (!goal) return '';
      return pillarGlossHTML(goal.title, goal.sort_order, goalCount(goal.id));
    }
    const entry = GLOSSARY.stats[key] || GLOSSARY.terms[key];
    if (!entry) return '';
    return '<strong>' + e(entry.title) + '</strong><p>' + e(entry.text) + '</p>';
  }
  document.addEventListener('pointerover', ev => {
    const trigger = ev.target.closest ? ev.target.closest('[data-gloss]') : null;
    if (!trigger) { if (glossTrigger) hideGloss(); return; }
    if (trigger === glossTrigger) return;
    const html = glossContent(trigger);
    if (!html) { hideGloss(); return; }
    glossTrigger = trigger;
    showGloss(trigger, html);
  });
  document.addEventListener('pointerout', ev => {
    if (!glossTrigger) return;
    if (ev.relatedTarget && glossTrigger.contains(ev.relatedTarget)) return;
    hideGloss();
  });
  document.addEventListener('focusin', ev => {
    const trigger = ev.target.closest ? ev.target.closest('[data-gloss]') : null;
    if (!trigger) return;
    const html = glossContent(trigger);
    if (html) { glossTrigger = trigger; showGloss(trigger, html); }
  });
  document.addEventListener('focusout', () => { if (glossTrigger) hideGloss(); });
  document.addEventListener('pointerdown', () => hideGloss(), true);
  document.addEventListener('keydown', ev => { if (ev.key === 'Escape') hideGloss(); }, true);
  window.addEventListener('scroll', () => { if (glossTrigger) hideGloss(); }, { capture: true, passive: true });
  window.addEventListener('hashchange', () => hideGloss());
  const pref = (key, fallback) => {try { return JSON.parse(localStorage.getItem('ip-growth-'+key)) ?? fallback; } catch {return fallback;}};
  const remember = (key,value) => {try {localStorage.setItem('ip-growth-'+key,JSON.stringify(value));}catch {}};
  let closed = new Set(pref('closed', []));
  const url = (changes = {}) => {
    const next = new URLSearchParams(query || '');
    for (const [key,value] of Object.entries(changes)) value === '' || value === null ? next.delete(key) : next.set(key,value);
    return '#/restart?' + next;
  };
  const tagPath = key => {
    const parts=[],seen=new Set();
    while(key&&!seen.has(key)){seen.add(key);const g=data.goals.find(x=>x.id===key);if(!g)break;parts.unshift(g.title);key=g.parent_id;}
    return parts.join(' / ');
  };
  const date = value => new Date(value).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'});
  const dayOf = value => new Intl.DateTimeFormat('sv-SE',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date(value));
  const activeGoals = () => data.goals.filter(g=>!data.archived_goal_ids.includes(g.id));
  const goalCount = id => data.goal_counts?.[id]||0;
  const goalManual = id => data.goal_counts_manual?.[id] ?? goalCount(id);
  const goalAuto = id => data.goal_counts_auto?.[id]||0;
  const pillarFor = goal => {const seen=new Set();while(goal&&!seen.has(goal.id)){seen.add(goal.id);if(goal.level==='pillar')return goal;goal=data.goals.find(g=>g.id===goal.parent_id);}return null;};
  const pillarIcon = goal => goal?.level==='root'?'target':(['brain','star','energy','heart','value'][pillarFor(goal)?.sort_order]||'folder');
  const orderedGoals = () => [...data.goals].sort((a,b)=>(a.sort_order||0)-(b.sort_order||0)||a.created_at.localeCompare(b.created_at)||a.id.localeCompare(b.id));
  const options = (selected='',excluded=[],includeArchived=false) => orderedGoals().filter(g=>!excluded.includes(g.id)&&(includeArchived||!data.archived_goal_ids.includes(g.id))).map(g=>`<option value="${e(g.id)}" ${selected===g.id?'selected':''}>${e(tagPath(g.id))}${data.archived_goal_ids.includes(g.id)?'（已归档）':''}</option>`).join('');
  function tags(selected=[]) {
    const all=orderedGoals().filter(g=>!data.archived_goal_ids.includes(g.id)||selected.includes(g.id));
    const roots=all.filter(g=>g.level==='root');
    const groups=[...roots,...all.filter(g=>g.level==='pillar')];
    return `<div class="g-picker"><div class="g-picker-top"><label class="g-searchbox">${icon('search')}<input type="search" data-g-tag-search placeholder="搜索目标或路径" aria-label="搜索关联目标"></label><button type="button" data-g="clear-tags">清空选择</button></div><div class="g-selected-tags" aria-live="polite"></div><div class="g-picker-groups">${groups.map(group=>{
      const members=all.filter(g=>group.level==='root'?g.level==='root':pillarFor(g)?.id===group.id);
      return `<details class="g-tag-group" ${selected.some(id=>members.some(g=>g.id===id))?'open':''}><summary>${icon(pillarIcon(group))}<span>${e(group.title)}</span><small>${members.length}</small></summary><div class="g-tags">${members.map(g=>`<label class="g-tag" data-path="${e(tagPath(g.id).toLowerCase())}"><input type="checkbox" name="goal_ids" value="${e(g.id)}" ${selected.includes(g.id)?'checked':''}><span title="${e(tagPath(g.id))}">${g.id===group.id?'':icon('target')}${e(g.title)}${data.archived_goal_ids.includes(g.id)?' · 已归档':''}</span></label>`).join('')}</div></details>`;
    }).join('')}</div><small class="g-note">可跨支柱多选，也可以先不归类。</small></div>`;
  }
  function sourceKinds(item){return [...new Set([...(item.text?['text']:[]),...(item.source_url||item.type==='link'?['link']:[]),...(item.voice_id||item.type==='voice'?['voice']:[])])];}
  const sourceName = item => {try {return new URL(item.source_url).hostname;}catch{return '';}};
  function card(item){
    const kinds=sourceKinds(item);const first=kinds.includes('voice')?'voice':kinds.includes('link')?'link':'text';
    return `<article class="g-fragment"><div class="g-fragment-top"><span class="g-type-icon">${icon(first)}</span><div class="g-fragment-meta"><span>${kinds.map(k=>({text:'文字',link:'链接',voice:'语音'})[k]).join(' · ')}</span><time datetime="${e(item.recorded_at)}">${e(date(item.recorded_at))}</time></div><span class="g-endorsement">${item.archived?'已归档':item.endorsement==='accepted'?icon('check')+' 本人认可':'仅作参考'}</span></div><button class="g-card-title" data-g="fragment" data-id="${e(item.id)}">${e(item.title)}</button><p class="g-excerpt">${e(item.text||(['queued','running'].includes(item.transcription?.status)?'正在将视频转成文字稿…':item.transcription?.status==='failed'?'文字稿尚未生成，请查看下方原因后重试。':'视频尚未转写，点“转文字稿”开始。'))}</p><div class="g-card-tags">${item.goal_ids.length?item.goal_ids.slice(0,2).map(id=>`<a href="${e(url({view:'library',goal:id,page:'',day:''}))}" title="${e(tagPath(id))}">${e(data.goals.find(g=>g.id===id)?.title||'历史目标')}</a>`).join(''):'<span>'+icon('inbox')+' 待整理</span>'}${item.goal_ids.length>2?`<button data-g="fragment" data-id="${e(item.id)}" aria-label="查看全部 ${item.goal_ids.length} 个关联目标">+${item.goal_ids.length-2}</button>`:''}</div>${item.source_url?transcriptionState(item):''}${manuscriptActions(item)}${item.source_url?`<a class="g-source" href="${e(item.source_url)}" target="_blank" rel="noopener noreferrer">${icon('link')}${e(sourceName(item))}${icon('arrow')}</a>`:''}</article>`;
  }
  function filters(){return `<details class="g-filter-disclosure" open><summary>${icon('search')}筛选与搜索<span>目标 · 来源 · 日期</span></summary><form data-g-form="filter" class="g-filters"><label>关联目标<select name="goal"><option value="">全部目标（含子目标）</option>${options(query.get('goal'),[],true)}</select></label><label>来源类型<select name="type"><option value="">全部类型</option>${['text','link','voice'].map(k=>`<option value="${k}" ${query.get('type')===k?'selected':''}>${({text:'文字',link:'链接',voice:'语音'})[k]}</option>`).join('')}</select></label><label>月份<input name="month" type="month" value="${e(query.get('month')||'')}" aria-label="筛选月份"></label><label class="g-search">搜索<input type="search" name="q" placeholder="找标题、正文或来源" value="${e(query.get('q')||'')}"></label><button type="submit">筛选</button><a class="g-text-link" href="${e(url({goal:'',month:'',day:'',q:'',type:'',inbox:'',archived:'',page:''}))}">重置</a><label class="g-inline-check"><input name="inbox" type="checkbox" ${query.get('inbox')==='1'?'checked':''}>待整理</label><label class="g-inline-check"><input name="archived" type="checkbox" ${query.get('archived')==='1'?'checked':''}>包含归档</label>${query.get('day')?`<a class="g-day-filter" href="${e(url({day:'',page:''}))}">${e(query.get('day'))} ×</a>`:''}</form></details>`;}
  function header(view){return `<div class="g-heading"><div><span class="g-eyebrow">${icon('layers')} 个人成长</span><h1>重启系统</h1><p>把每一次积累，连回自己的方向。</p></div><div class="g-heading-actions"><span class="g-local">● 本机保存</span><button class="g-primary" data-g="new-fragment" aria-label="记录碎片">${icon('plus')}记录碎片</button></div></div><nav class="g-tabs" aria-label="成长系统页面">${[['overview','成长总览','home'],['goals','目标体系','target'],['library','碎片记录','text'],['ask','问问自己','brain'],['search','认知搜索','search'],['graph','成长图谱','graph'],['review','月度复盘','calendar']].map(([key,title,glyph])=>`<a href="${e(url({view:key,page:'',goal:'',day:'',q:'',type:'',inbox:'',branch:''}))}" ${view===key?'aria-current="page"':''}${key==='goals'?' data-gloss="goal-system"':''}>${icon(glyph)}<span>${title}</span></a>`).join('')}</nav>`;}
  function stats(){const s=data.stats;return `<div class="g-stats">${[['有效目标',s.goals,'target','stat-goals'],['全部积累',s.fragments,'layers','stat-fragments'],['本月记录',s.month,'calendar','stat-month'],['等待归位',s.inbox,'inbox','stat-inbox']].map(([label,n,glyph,key])=>`<div data-gloss="${key}"><span>${icon(glyph)}${label}</span><strong>${key==='stat-inbox'?`<a href="${e(url({view:'inbox',goal:'',month:'',day:'',q:'',type:'',page:''}))}" aria-label="查看等待归位的 ${n} 条内容">${n}</a>`:n}</strong></div>`).join('')}</div>`;}
  function assetStrip() {
    if (!assetsData) return '';
    const c = assetsData.counts || {};
    const total = Number(assetsData.total || 0);
    const voice = `${Number(c.voices || 0)} 段${c.voice_chars ? ` · ${c.voice_chars} 字` : ''}`;
    const days = query.get('days') === '90' ? 90 : 30;
    const series = (assetsData.series && assetsData.series[days]) || [];
    const first = assetsData.first_day || '';
    const today = assetsData.today || '';
    const toggle = `<div class="g-segment" role="group" aria-label="趋势范围"><a href="${e(url({ view: 'overview', days: '30' }))}" ${days === 30 ? 'aria-current="true"' : ''}>近30天</a><a href="${e(url({ view: 'overview', days: '90' }))}" ${days === 90 ? 'aria-current="true"' : ''}>近90天</a></div>`;
    return `<section class="g-panel g-space g-asset-panel">
      <div class="g-section-head"><div><h2>这些是你的资产</h2><p class="g-note">认知卡＝当前使用的判断；作品＝真正建过的作品；声音样本＝去AI味用的真实口播语料；积累天数＝有记录的天数。</p></div><span class="g-note">资产越用越值钱 · 带得走 · 抄不走</span></div>
      <p class="g-asset-thickness">${e(assetsData.thickness || '')}</p>
      <div class="g-stats g-asset-stats">
        <div><span>认知卡</span><strong>${Number(c.knowledge || 0)}</strong></div>
        <div><span>作品</span><strong>${Number(c.works || 0)}</strong></div>
        <div><span>声音样本 · 去AI味</span><strong>${e(voice)}</strong></div>
        <div><span>积累天数</span><strong>${Number(c.days || 0)}</strong></div>
      </div>
      <p class="g-note">声音样本：你的真实口播样本，写稿时压成你的口吻，去掉机器腔——这是别人抄不走的部分。</p>
      ${(()=>{const m=assetsData.morning||{};if(!m.items||!m.items.length)return '<p class="g-note g-morning-empty">今日晨报：没有新内容，不硬凑。</p>';return `<section class="g-panel g-space g-morning-card"><div class="row spread"><strong>今日晨报</strong><button type="button" class="small-button" data-g="morning-seen">${m.already_seen?'已读':'知道了'}</button></div>${m.header?`<p class="g-note">${e(m.header)}</p>`:''}<ul>${m.items.map(x=>`<li><b>${e(x.label)}：</b>${e(x.text)}</li>`).join('')}</ul></section>`;})()}
      <div class="row spread g-asset-maint"><button type="button" class="small-button" data-g="brain-consolidate">整理我的大脑</button><span class="g-note">已巩固 ${Number(c.consolidated||0)} 条 · 已沉底 ${Number(c.dormant||0)} 条（沉底不删，可找回）</span></div>
      ${(()=>{const m=assetsData.milestones||{};const a=(m.achieved||[]).slice(-2).map(x=>x.label).join('、');const n=m.next;return '<p class="g-note g-milestone">里程碑：'+(a?'已达成 '+a:'还没有里程碑')+(n?' · '+n.text:' · 继续积累，下一块会自己出现')+'。只按真实记录算。</p>';})()}
      ${total === 0 ? '<div class="g-asset-empty"><p>资产从第一条开始：先记一条认知，这里就会长出第一笔。</p></div>' : ''}
      <div class="g-section-head g-space"><div><h2>复利趋势 · 累计资产</h2><p class="g-note">按每条记录的时间戳累计；没有记录的日子就平着，不补点、不美化。</p></div>${toggle}</div>
      ${assetChart(series)}
      <div class="g-asset-axis"><span>${e(first || '还没有记录')}</span><span>${e(today)}</span></div>
      <p class="g-note">终点是今天真实拥有的条目数；曲线只说明积累节奏，不预测收入。</p>
    </section>`;
  }

  async function openBrainMaintenance() {
    let data = null;
    try { data = await api('/growth/consolidate/run', 'POST', {}); }
    catch (err) { toast('没整理成：' + (err.message || '读取失败')); return; }
    const status = data.status || {};
    const dup = data.duplicates || [];
    const themes = data.themes || [];
    const dormant = data.dormant_cards || [];
    const empty = (data.empty || (!dup.length && !themes.length && !dormant.length));
    const dupHtml = dup.length ? dup.map((x, i) => `<div class="g-fragment"><div class="g-fragment-top"><span class="g-type-icon">${icon('layers')}</span><div class="g-fragment-meta"><span>相似 ${Math.round(Number(x.similarity || 0) * 100)}%</span><small>${e((x.titles || []).join(' / '))}</small></div></div><p class="g-excerpt">${e((x.claims || []).join('｜'))}</p><button type="button" class="small-button" data-g="brain-merge" data-ids="${e((x.ids || []).join(','))}">确认合并（保留原文，重复卡归档）</button></div>`).join('') : '<p class="g-note">没有发现需要合并的重复卡。</p>';
    const themeHtml = themes.length ? themes.map((t, i) => `<div class="g-fragment"><div class="g-fragment-top"><span class="g-type-icon">${icon('star')}</span><div class="g-fragment-meta"><span>${e(t.category)} · ${Number(t.count || 0)} 条</span><small>反复出现的主题</small></div></div><label class="field"><span>提炼成你的一句原则（可改；确认才入库）</span><textarea data-theme-input="${i}" rows="2" maxlength="300">${e(t.draft || '')}</textarea></label><button type="button" class="small-button" data-g="brain-principle" data-idx="${i}" data-sources="${e((t.source_ids || []).join(','))}">确认这句，入库</button></div>`).join('') : '<p class="g-note">还没有反复出现、够提炼的主题。</p>';
    const dormantHtml = dormant.length ? dormant.map(x => `<div class="row spread small"><span>${e(x.title || x.id)}</span><button type="button" class="small-button" data-g="brain-restore" data-id="${e(x.id)}">恢复</button></div>`).join('') : '<p class="g-note">没有沉底的认知。</p>';
    window.__brainThemeData = themes;
    modal('整理我的大脑',
      `<p class="g-note">已巩固 ${Number(status.consolidated || 0)} 条 · 已沉底 ${Number(status.dormant || 0)} 条 · 本次沉底 ${Number(data.sunk || 0)} 条（只降权，不删除；随时可恢复）。合并与提炼都要你确认，系统不自动断言。</p>` +
      (empty ? '<p class="g-note">库里还没有可整理的认知；先记几条，再回来。</p>' : '') +
      `<section class="g-panel g-space"><h3>合并重复认知（需确认）</h3>${dupHtml}</section>` +
      `<section class="g-panel g-space"><h3>提炼主题原则（需确认）</h3>${themeHtml}</section>` +
      `<section class="g-panel g-space"><h3>沉底的认知（可恢复）</h3>${dormantHtml}</section>`);
  }

  function talentForm(){
    const via=(talentData&&talentData.via)||[];
    const p=talentData&&talentData.profile;
    const oldVia=new Set((p&&p.via)||[]);
    const oldIk=(p&&p.ikigai)||{};
    const oldBest=(p&&p.best)||[];
    return `<form data-g-form="talent" class="space">
      <p class="g-note">不用做 240 题：只勾出你身上比较像的 5–10 项，再各写一句 Ikigai 和 3 个“你最好的时刻”。系统会去你的真实记录里找证据，没有证据就不硬夸。</p>
      <div class="g-talent-via">${via.map(x=>`<label class="check"><input type="checkbox" name="via" value="${e(x.key)}" ${oldVia.has(x.key)?'checked':''}> <strong>${e(x.label)}</strong><small>${e(x.hint)}</small></label>`).join('')}</div>
      <div class="space"><h3>Ikigai 四圈</h3>${[['love','你热爱什么'],['good','你擅长什么'],['needs','世界/别人需要什么'],['paid','可以换取什么']].map(([k,l])=>`<label class="field"><span>${l}（一句就够）</span><textarea name="${k}" rows="2" maxlength="500">${e(oldIk[k]||'')}</textarea></label>`).join('')}</div>
      <div class="space"><h3>最佳自我 · 3 个时刻</h3>${[0,1,2].map(i=>`<label class="field"><span>第 ${i+1} 个“我最好的时刻”</span><textarea name="best${i}" rows="2" maxlength="1200">${e(oldBest[i]||'')}</textarea></label>`).join('')}</div>
      <button class="g-primary" type="submit">生成我的天赋画像</button><p class="g-form-status" role="status"></p>
    </form>`;
  }
  function talentPanel(){
    if(!talentData)return '';
    const p=talentData.profile, m=talentData.mining||{};
    if(!p){
      if(talentData.need_evidence)return `<section class="g-panel g-space" id="talent-discover"><div class="g-section-head"><div><h2>发现我的天赋</h2><p class="g-note">先从你已有的记录里挖真实信号；现在还没有记录。</p></div></div><p class="g-note">先记 3 条认知或碎片，再来做轻评估；不编、不猜。</p><a class="g-text-link" href="${e(url({view:'library'}))}">去记一条 →</a></section>`;
      return `<section class="g-panel g-space" id="talent-discover"><div class="g-section-head"><div><h2>发现我的天赋</h2><p class="g-note">已从你的记录里找到 ${m.texts||0} 条可回源线索（认知 ${m.counts?.knowledge||0} · 碎片 ${m.counts?.fragments||0} · 打卡 ${m.counts?.checkins||0}）。</p></div></div>${talentForm()}</section>`;
    }
    const pt=p.portrait||{};const talents=pt.talents||[];
    return `<section class="g-panel g-space" id="talent-discover"><div class="g-section-head"><div><h2>你的天赋画像</h2><p class="g-note">从 ${pt.texts||0} 条真实记录＋你的轻评估里长出来；每条证据都可回源。</p></div></div>
      <div class="g-talent-list">${talents.map((t,i)=>`<article class="g-fragment"><div class="g-fragment-top"><span class="g-type-icon">${icon('star')}</span><div class="g-fragment-meta"><span>Top ${i+1} · ${e(t.label)}</span><small>${e(t.why)}</small></div></div><p class="g-excerpt">${e(t.direction)}</p><div class="g-card-tags">${(t.evidence||[]).map(x=>`<span title="${e(x.quote)}">${e(x.source==='self'?'最佳自我':x.title||x.source)}${x.date?' · '+e(x.date):''}</span>`).join('')}</div><div class="g-card-controls"><button class="g-primary" data-g="talent-topic" data-idx="${i}">把这个方向写成选题 →</button></div></article>`).join('')}</div>
      <details class="g-space"><summary>重新做轻评估</summary>${talentForm()}</details></section>`;
  }


  /* ===== 访谈诊断（t179 / t178 API）：240 问是弹药库不是问卷 ===== */
  let diag = { state: null, batch: null, profile: null, msg: '', busy: false, lastNo: null };
  function diagSection() {
    return `<section id="diagnosis" class="g-panel g-space" aria-live="polite"><div class="g-section-head"><div><h2>访谈诊断</h2><p class="g-note">240 问是弹药库，不是问卷：不答满也能出画像。每批最多 8 问，可跳过，进度自动保存；上一阶段画像卡确认后才进下一阶段。</p></div></div><div id="diag-body"><p class="g-note">正在读取进度…</p></div></section>`;
  }
  function diagEsc(v) { return String(v == null ? '' : v).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])); }
  function diagCardLabel(kind) {
    const map = { debt_profile: '债务画像卡', ability_profile: '能力与失败画像卡', diagnosis_report: '诊断报告', action_plan: '行动方案' };
    return map[kind] || '画像卡';
  }
  function diagCardHtml(card) {
    const facts = (card.facts || []).map(f => { const t = typeof f === 'string' ? f : (f.answer || f.text || f.title || ''); const q = typeof f === 'object' ? (f.question || '') : ''; return `<li>${diagEsc(q ? (q + '：') : '')}${diagEsc(t)}</li>`; }).join('');
    const sig = card.signals || []; const signals = (Array.isArray(sig) ? sig.map(x => `${x.label || x.key || ''}：${x.value != null ? x.value : ''}`) : Object.entries(sig).map(([k, v]) => `${k}：${v}`)).map(x => `<span class="g-note">${diagEsc(x)}</span>`).join(' ');
    const items = (card.items || card.actions || []).map(it => { const label = typeof it === 'string' ? it : (it.text || it.title || ''); const basis = typeof it === 'object' ? (it.basis || it.evidence || '') : ''; const pending = typeof it === 'object' ? (it.pending || (!basis && !it.answer)) : false; return `<li>${diagEsc(label)}${pending ? ' <span class="g-note">待补充</span>' : ''}</li>`; }).join('');
    const corrRaw = card.correct && card.correct.items;
    const corrList = Array.isArray(corrRaw) ? corrRaw : (corrRaw && Array.isArray(corrRaw.items) ? corrRaw.items : []);
    const corrections = corrList.map(it => `<li>第 ${diagEsc(it.question_no)} 题：${diagEsc(it.note || '')}</li>`).join('');
    return `<article class="g-panel g-space" data-diag-card="${diagEsc(card.kind || '')}"><div class="g-section-head"><div><h3>${diagEsc(diagCardLabel(card.kind))}</h3><p class="g-note">已回答 ${diagEsc(card.basis && card.basis.answered != null ? card.basis.answered : '')} 条 · 阶段性整理，不是测评结论。</p></div></div>${facts ? `<ul>${facts}</ul>` : ''}${items ? `<ul>${items}</ul>` : ''}${signals ? `<p>${signals}</p>` : ''}${corrections ? `<div class="g-space"><p class="g-note">你补充的修正：</p><ul>${corrections}</ul></div>` : ''}${card.note ? `<p class="g-note">${diagEsc(card.note)}</p>` : ''}${card.disclaimer ? `<p class="g-note">${diagEsc(card.disclaimer)}</p>` : ''}<div class="g-heading-actions g-space"><button class="g-primary" data-g="diag-ack" data-stage="${diagEsc(card.stage || '')}">确认这份画像，进入下一阶段</button><button data-g="diag-show-correct" data-stage="${diagEsc(card.stage || '')}">修正：补充一句</button><button data-g="diag-continue" data-stage="${diagEsc(card.stage || '')}">继续补充</button></div><div id="diag-correct"></div></article>`;
  }
  function diagLastKey(sid) { return 'ip-diag-last-' + String(sid || 'none'); }
  function diagRememberLast(no, sid) {
    try { localStorage.setItem(diagLastKey(sid), String(no)); } catch (e) {}
  }
  function diagRecallLast() {
    try {
      const n = parseInt(localStorage.getItem(diagLastKey(diag.state && diag.state.session_id)), 10);
      return n > 0 && n <= 240 ? n : null;
    } catch (e) { return null; }
  }
  function diagBatchHtml(b) {
    const prog = b.progress || {};
    const qs = (b.questions || []).map(q => q.gate
      ? `<div class="g-space" data-diag-gate="${diagEsc(q.gate_id || '')}"><p><strong>前置确认</strong> · ${diagEsc(q.text)}</p><div class="g-heading-actions"><button class="g-primary" data-g="diag-gate" data-gate="${diagEsc(q.gate_id || '')}" data-no="1">是</button><button data-g="diag-gate" data-gate="${diagEsc(q.gate_id || '')}" data-no="0">否，这部分不适用</button></div></div>`
      : `<div class="g-space" data-diag-question="${q.no}"><p>${q.representative ? '<span class="g-note">代表题</span> ' : ''}<strong>${diagEsc(q.family_name || '')}</strong> · ${diagEsc(q.text)}</p><label class="field"><span>你的回答（可跳过）</span><textarea name="diag-answer-${q.no}" rows="2" maxlength="4000" placeholder="用你自己的话写；不想说就点「不想说」。"></textarea></label><div class="g-heading-actions"><button class="g-primary" data-g="diag-answer" data-no="${q.no}">提交这题</button><button data-g="diag-skip" data-no="${q.no}">不想说</button></div></div>`).join('');
    return `<div class="g-section-head"><div><h3>${diagEsc(b.stage_name || '访谈诊断')}</h3><p class="g-note">已回答 ${prog.answered || 0} 条 · 可选 240${prog.representatives_total ? (' · 代表题 ' + (prog.representatives_handled || 0) + '/' + prog.representatives_total) : ''}${prog.skipped ? (' · 不想说 ' + prog.skipped) : ''}${prog.na ? (' · 不适用 ' + prog.na) : ''} · 本批 ${(b.questions || []).length} 题</p></div><div class="g-heading-actions"><button data-g="diag-report">出报告</button></div></div>${qs}<p class="g-note">进度保存在本机；中途离开，下次回来会从当前进度继续。</p>`;
  }
  function diagBody() {
    const st = diag.state;
    const msg = diag.msg ? `<p class="g-form-status" role="status">${diagEsc(diag.msg)}</p>` : '';
    if (!st || st.status === 'idle') {
      return msg + `<p class="g-note">还没有开始。答几批就能先看到画像；不想答的题可以直接跳过，不影响继续。</p><button class="g-primary" data-g="diag-start">开始访谈诊断</button>`;
    }
    if (diag.profile && (diag.profile.cards || []).length && (!diag.batch || diag.batch.card_ready)) {
      return msg + `<p class="g-note">${diagEsc(diag.profile.message || '')}</p>` + diag.profile.cards.map(diagCardHtml).join('') + (diag.profile.status === 'complete' ? '<p class="g-note">四阶段已完成，画像卡可随时回看。</p>' : '');
    }
    if (diag.batch && (diag.batch.questions || []).length) return msg + diagBatchHtml(diag.batch);
    return msg + `<p class="g-note">这一批还没读出来。可以重新读取进度，或继续回答。</p><div class="g-heading-actions"><button class="g-primary" data-g="diag-boot">重新读取进度</button><button data-g="diag-continue">继续回答</button><button data-g="diag-start">重新开始诊断</button></div>`;
  }
  function diagPaint() {
    const host = document.getElementById('diag-body');
    if (host) host.innerHTML = diagBody();
  }
  async function diagBoot() {
    try {
      diag.msg = '';
      const st = await api('/diagnosis/state');
      diag.state = st;
      diag.lastNo = diagRecallLast();
      if (st.status === 'idle') { diag.batch = null; diag.profile = null; diagPaint(); return; }
      diag.profile = await api('/diagnosis/profile');
      const cur = st.stage || 1;
      const b = await api('/diagnosis/questions?session_id=' + encodeURIComponent(st.session_id) + '&stage=' + cur);
      diag.batch = b;
      diagPaint();
    } catch (err) {
      diag.msg = '读取诊断进度失败：' + (err.message || err);
      diagPaint();
    }
  }
  async function diagStart() {
    diag.busy = true; diag.msg = '';
    try {
      const r = await api('/diagnosis/start', 'POST', {});
      diag.state = r; diag.profile = null; diag.batch = null;
      await diagBoot();
    } catch (err) { diag.msg = '开始失败：' + (err.message || err); diagPaint(); }
    diag.busy = false;
  }
  async function diagAnswer(no, skip) {
    if (diag.busy) return;
    const st = diag.state; if (!st) return;
    const text = skip ? '' : (document.querySelector('[name="diag-answer-' + no + '"]') || {}).value || '';
    if (!skip && !String(text).trim()) { diag.msg = '这题还没写；不想说可以点「不想说」。'; diagPaint(); return; }
    diag.busy = true; diag.msg = '';
    try {
      const r = await api('/diagnosis/answer', 'POST', { session_id: st.session_id, question_no: Number(no), text: String(text), skip: !!skip });
      diag.lastNo = Number(no);
      diagRememberLast(no, st.session_id);
      if (r.followup) diag.msg = '再补一句：' + r.followup;
      if (r.card_ready) {
        diag.profile = await api('/diagnosis/profile');
        diag.batch = null;
      } else {
        const b = await api('/diagnosis/questions?session_id=' + encodeURIComponent(st.session_id) + '&stage=' + (st.stage || 1));
        diag.batch = b;
      }
      diagPaint();
    } catch (err) {
      if (err && (err.code === 'stage_locked' || /stage_locked/.test(err.message || ''))) {
        diag.msg = '上一阶段的画像卡还没确认，先确认后再继续。';
        try { diag.profile = await api('/diagnosis/profile'); } catch (e2) {}
      } else { diag.msg = '提交失败：' + (err.message || err); }
      diagPaint();
    }
    diag.busy = false;
  }
  async function diagAck(stage) {
    if (diag.busy) return;
    const st = diag.state; if (!st) return;
    diag.busy = true; diag.msg = '';
    try {
      await api('/diagnosis/confirm-card', 'POST', { session_id: st.session_id, stage: Number(stage), action: 'confirm' });
      // 确认状态位已记录；再尝试推进到下一阶段（gate/代表题未处理完时后端会如实拒绝）
      await api('/diagnosis/ack-card', 'POST', { session_id: st.session_id, stage: Number(stage) });
      diag.profile = null; diag.batch = null;
      await diagBoot();
    } catch (err) {
      try { diag.profile = await api('/diagnosis/profile'); } catch (e2) {}
      diag.msg = '已记录确认；还不能进入下一阶段：' + (err.message || err);
      diagPaint();
    }
    diag.busy = false;
  }
  function diagShowCorrect(stage) {
    const host = document.getElementById('diag-correct'); if (!host) return;
    host.innerHTML = `<label class="field"><span>要修正的题号（上一题已自动填；重载后也可手填）</span><input id="diag-correct-no" type="number" min="1" max="240" value="${diag.lastNo || ''}"></label><label class="field"><span>补充/修正说明（会记在这张卡上）</span><textarea id="diag-correct-text" rows="2" maxlength="4000" placeholder="补充事实或修正这一题的回答。"></textarea></label><div class="g-heading-actions"><button class="g-primary" data-g="diag-correct-save" data-stage="${diagEsc(stage)}">保存修正</button></div>`;
  }
  async function diagCorrectSave(stage) {
    const st = diag.state; if (!st) return;
    const text = (document.getElementById('diag-correct-text') || {}).value || '';
    if (!String(text).trim()) { diag.msg = '先写一句再保存。'; diagPaint(); return; }
    const noEl = document.getElementById('diag-correct-no');
    const no = Number((noEl && noEl.value) || diag.lastNo || 0);
    if (!no || no < 1 || no > 240) { diag.msg = '填一下要修正的题号（1–240）。'; diagPaint(); return; }
    diag.busy = true;
    try {
      await api('/diagnosis/confirm-card', 'POST', { session_id: st.session_id, stage: Number(stage || st.stage || 1), action: 'correct', corrections: [{ question_no: Number(no), note: String(text) }] });
      diag.profile = await api('/diagnosis/profile');
      diag.msg = '已保存你的修正。';
      diagPaint();
    } catch (err) { diag.msg = '修正失败：' + (err.message || err); diagPaint(); }
    diag.busy = false;
  }
  async function diagGate(gateId, no) {
    if (diag.busy) return;
    const st = diag.state; if (!st) return;
    diag.busy = true; diag.msg = '';
    try {
      const r = await api('/diagnosis/answer', 'POST', { session_id: st.session_id, gate_id: gateId, no: !!no });
      const na = (r.na_applied && r.na_applied.length) ? r.na_applied : (r.na_applied_families || []);
      if (na.length) diag.msg = '已把 ' + na.length + ' 道不适用题标为不适用，不会逐题追问。';
      if (r.card_ready) { diag.profile = await api('/diagnosis/profile'); diag.batch = null; }
      else { diag.batch = r.next; }
      diagPaint();
    } catch (err) { diag.msg = '提交失败：' + (err.message || err); diagPaint(); }
    diag.busy = false;
  }
  async function diagReport() {
    try { diag.profile = await api('/diagnosis/profile'); diag.batch = null; diagPaint(); }
    catch (err) { diag.msg = '读取报告失败：' + (err.message || err); diagPaint(); }
  }
  async function diagContinue() {
    const st = diag.state; if (!st) return;
    try {
      const b = await api('/diagnosis/questions?session_id=' + encodeURIComponent(st.session_id) + '&stage=' + (st.stage || 1));
      diag.profile = null;
      diag.batch = b;
      if (!(b.questions || []).length) diag.msg = '当前阶段的可选题已答完；可出报告，或去下一阶段。';
      diagPaint();
    } catch (err) { diag.msg = '读取可选题失败：' + (err.message || err); diagPaint(); }
  }
  function diagClick(ev) {
    const el = ev.target.closest('[data-g]'); if (!el) return;
    const act = el.dataset.g;
    if (act === 'diag-start') { ev.preventDefault(); diagStart(); }
    else if (act === 'diag-boot') { ev.preventDefault(); diagBoot(); }
    else if (act === 'diag-answer') { ev.preventDefault(); diagAnswer(el.dataset.no, false); }
    else if (act === 'diag-skip') { ev.preventDefault(); diagAnswer(el.dataset.no, true); }
    else if (act === 'diag-ack') { ev.preventDefault(); diagAck(el.dataset.stage); }
    else if (act === 'diag-show-correct') { ev.preventDefault(); diagShowCorrect(el.dataset.stage); }
    else if (act === 'diag-correct-save') { ev.preventDefault(); diagCorrectSave(el.dataset.stage); }
    else if (act === 'diag-gate') { ev.preventDefault(); diagGate(el.dataset.gate, el.dataset.no === '0'); }
    else if (act === 'diag-report') { ev.preventDefault(); diagReport(); }
    else if (act === 'diag-continue') { ev.preventDefault(); diagContinue(); }
  }
  function diagMount() {
    if (!document.getElementById('diagnosis')) return;
  if (!window.__t179DiagBound) { document.addEventListener('click', diagClick); window.__t179DiagBound = true; }
  // 2026-11-25（方案 A）：对话首页的引导卡带 ?diag=1 进来时，一条都没答过就直接开出第一批题，不用再点一次「开始」。
  const auto = new URLSearchParams(location.hash.split('?')[1] || '').get('diag') === '1';
  diagBoot().then(() => { if (auto && diag.state && diag.state.status === 'idle') diagStart(); });
}

  function overview(){
    const root=data.goals.find(g=>g.level==='root');
    return `${diagSection()}<section class="g-north g-panel"><div><span class="g-eyebrow">${icon('target')} 我的主线</span><h2>${e(root.title)}</h2><p>${e(root.description||'让日常的学习、行动与灵感，有一个共同的方向。')}</p>${root.milestone?`<div class="g-axis-line g-milestone">${(()=>{const st=(checkinData&&checkinData.stats)||{};const days=(checkinData&&checkinData.days)||[];const checked=Number(st.checked||0),total=Number(st.total||60)||60;const pct=Math.max(0,Math.min(100,Math.round(checked*100/total)));const state=pct>=100?'已完成':checked?'进行中':'未开始';const knowledge=days.filter(d=>d.state==='checked'&&d.card_id).length;const content=days.filter(d=>d.video||d.publish_url).length;return `<p>主业目标：${e(root.milestone)}</p><div class="g-goal-progress"><progress class="ck-bar-track" value="${pct}" max="100" aria-label="主业目标进度 ${pct}%"></progress><span class="g-note">${pct}% · ${state} · 60 天打卡 ${checked}/${total} · 积累 ${knowledge} 条 · 出片 ${content} 条</span></div>`;})()}</div>`:''}${root.thesis?`<p class="g-axis-line">命题：${e(root.thesis)}</p>`:`<p class="g-axis-line">还没有命题。命题是更深的可选方向——目标照旧，想补可在「查看与调整」里写。</p>`}<button class="g-text-link" data-g="goal-detail" data-id="${e(root.id)}">查看与调整 ${icon('arrow')}</button></div><div class="g-north-mark" aria-hidden="true">${icon('target')}</div></section>${thesisPanel()}<p class="g-overview-value">${e(GLOSSARY.overviewValue)}</p>${stats()}${assetStrip()}${talentPanel()}<div class="g-section-head g-space"><div><h2>五大支柱</h2><p class="g-note">从一个方向，进入你的积累。</p></div><a class="g-text-link" href="${e(url({view:'goals'}))}">目标体系 ${icon('arrow')}</a></div><div class="g-pillars">${data.pillars.map(p=>`<a class="g-pillar-card" data-pillar-color="${e(p.color)}" data-gloss="pillar" data-id="${e(p.id)}" href="${e(url({view:'goals',branch:p.id}))}"><span class="g-pillar-arrow">${icon('arrow')}</span><span class="g-pillar-symbol">${icon(pillarIcon(p))}</span><strong>${e(p.title)}</strong><span>${activeGoals().filter(g=>g.id!==p.id&&pillarFor(g)?.id===p.id).length} 个小目标 <i>·</i> ${goalCount(p.id)} 条积累</span></a>`).join('')}</div><div class="g-section-head g-space"><h2>最近留下的积累</h2><a class="g-text-link" href="${e(url({view:'library'}))}">查看全部 ${icon('arrow')}</a></div>${data.items.length?`<div class="g-cards">${data.items.slice(0,6).map(card).join('')}</div>`:emptyCard()}`;
  }
  function emptyCard(){return `<div class="g-empty"><span>${icon('layers')}</span><h2>从一条碎片开始</h2><p>一个想法、一段观点，或值得留下的链接。</p><button class="g-primary" data-g="new-fragment">${icon('plus')}记录一条碎片</button></div>`;}
  function library(){
    const pages=Math.ceil(data.total/data.page_size),layout=query.get('layout')||'cards';
    const body=layout==='timeline'?`<div class="g-daily-stream">${[...new Set(data.items.map(f=>dayOf(f.recorded_at)))].map(day=>`<section><h3>${icon('calendar')}${e(day)}</h3><div class="g-cards">${data.items.filter(f=>dayOf(f.recorded_at)===day).map(card).join('')}</div></section>`).join('')}</div>`:`<div class="g-cards">${data.items.map(card).join('')}</div>`;
    return `<div class="g-capture-tiles">${[['text','随手记','留下一点想法'],['link','视频转文字','原稿保存在本机'],['voice','说一段','把灵感转成文字']].map(([kind,title,hint])=>`<button data-g="new-fragment" data-kind="${kind}">${icon(kind)}<span><strong>${title}</strong><small>${hint}</small></span>${icon('plus')}</button>`).join('')}</div>${filters()}<div class="g-section-head"><span class="g-note">${data.total} 条碎片 · 含子目标，按条去重</span><div class="g-heading-actions"><div class="g-segment" aria-label="记录布局"><a href="${e(url({layout:'cards'}))}" ${layout==='cards'?'aria-current="true"':''}>卡片</a><a href="${e(url({layout:'timeline'}))}" ${layout==='timeline'?'aria-current="true"':''}>时间线</a></div><button data-g="import">关联旧认知</button><button data-g="export">导出</button></div></div>${data.items.length?body:emptyCard()}${pages>1?`<div class="g-pagination">${data.page>1?`<a href="${e(url({page:data.page-1}))}">上一页</a>`:''}<span>${data.page} / ${pages}</span>${data.page<pages?`<a href="${e(url({page:data.page+1}))}">下一页</a>`:''}</div>`:''}`;
  }
  function goals(){return `<section class="g-panel g-tree-panel"><div class="g-section-head"><div><h2 data-gloss="goal-system">我的目标树</h2><p class="g-note">共 ${data.goals.length} 个目标 · 左侧数字为你手动标注的碎片，右侧为知识库自动归类的内容</p></div><div class="g-heading-actions"><button data-g="tree-expand">全部展开</button><button data-g="tree-collapse">收起分支</button><button data-g="import">关联旧认知</button></div></div><label class="g-searchbox g-tree-search">${icon('search')}<input type="search" id="g-goal-search" placeholder="搜索目标，保留完整路径" aria-label="搜索目标体系"></label><div id="g-tree"></div></section>`;}
  function paintTree(){
    const host=document.getElementById('g-tree');if(!host)return;
    const needle=document.getElementById('g-goal-search').value.trim().toLowerCase();
    const order=orderedGoals(),visible=new Set(),lookup=new Map(order.map(g=>[g.id,g]));
    if(needle)for(const g of order.filter(g=>g.title.toLowerCase().includes(needle))){let n=g;const seen=new Set();while(n&&!seen.has(n.id)){seen.add(n.id);visible.add(n.id);n=lookup.get(n.parent_id);}}
    const branch=(parent,depth=0,seen=new Set())=>order.filter(g=>g.parent_id===parent&&(!needle||visible.has(g.id))).map(g=>{
      if(seen.has(g.id))return '';const next=new Set(seen).add(g.id);const hasChildren=order.some(n=>n.parent_id===g.id);const expanded=!!needle||!closed.has(g.id);const archived=data.archived_goal_ids.includes(g.id);
      return `<li><div class="g-goal-row ${archived?'g-archived':''}" data-depth="${Math.min(depth,3)}"><button class="g-tree-toggle" data-g="tree-toggle" data-id="${e(g.id)}" ${hasChildren?`aria-expanded="${expanded}" aria-label="${expanded?'收起':'展开'}${e(g.title)}"`:'disabled aria-label="无下级目标"'}>${hasChildren?icon('chevron'):'<span class="g-leaf-dot"></span>'}</button><span class="g-tree-symbol" data-goal-color="${g.level==='root'?'var(--gold)':e(pillarFor(g)?.color||g.color)}">${icon(hasChildren?'folder':g.level==='pillar'?pillarIcon(g):'target')}</span><button class="g-goal-name" data-g="goal-detail" data-id="${e(g.id)}" title="${e(tagPath(g.id))}"><strong>${e(g.title)}</strong>${archived?'<small>已归档</small>':''}</button>${(()=>{const manual=goalManual(g.id),auto=goalAuto(g.id);return `<span class="g-goal-counts"><a class="g-goal-count" href="${e(url({view:'library',goal:g.id,month:'',day:'',q:'',inbox:'',type:'',page:''}))}" data-gloss="count-manual" aria-label="查看${e(g.title)}手动标注的 ${manual} 条碎片">${manual}<small>手动</small></a>${auto?`<a class="g-goal-count g-goal-count-auto" href="${e(url({view:'goal-auto',goal:g.id,month:'',day:'',q:'',inbox:'',type:'',page:''}))}" data-gloss="count-auto" aria-label="查看${e(g.title)}来自知识库的 ${auto} 条内容">${auto}<small>知识库</small></a>`:''}</span>`;})()}<button class="g-more" data-g="goal-detail" data-id="${e(g.id)}" aria-label="${e(g.title)}的更多操作">${icon('dots')}</button></div>${hasChildren&&expanded?`<ul>${branch(g.id,depth+1,next)}</ul>`:''}</li>`;
    }).join('');
    host.innerHTML=`<ul class="g-goal-tree">${branch(null)||'<li class="g-note g-space">没有找到匹配目标。</li>'}</ul>`;
    applyVisualStyles(host);
  }
  function calendar(){
    const [y,m]=data.month.split('-').map(Number),length=new Date(Date.UTC(y,m,0)).getUTCDate(),offset=(new Date(Date.UTC(y,m-1,1)).getUTCDay()+6)%7,days=data.daily_counts||{},max=Math.max(1,...Object.values(days));
    return `<section class="g-panel"><div class="g-section-head"><h2>${icon('calendar')}记录日历</h2><span class="g-note">${Object.keys(days).length} 天留下记录</span></div><div class="g-calendar-week">${['一','二','三','四','五','六','日'].map(s=>`<span>${s}</span>`).join('')}</div><div class="g-calendar">${'<span></span>'.repeat(offset)}${Array.from({length},(_,i)=>{const d=data.month+'-'+String(i+1).padStart(2,'0'),n=days[d]||0,level=n?Math.min(4,Math.ceil(n/max*4)):0;return `<a class="g-day g-day-${level}" href="${e(url({view:'library',day:d,month:data.month,goal:'',q:'',type:'',inbox:'',page:''}))}" aria-label="${d}，${n} 条记录" title="${d} · ${n} 条"><span>${i+1}</span><small>${n||'·'}</small></a>`;}).join('')}</div><div class="g-calendar-key"><span>记录数</span><i class="g-day-0"></i><i class="g-day-1"></i><i class="g-day-2"></i><i class="g-day-3"></i><i class="g-day-4"></i><span>少 → 多</span></div></section>`;
  }
  function review(){
    const r=data.review||{};
    return `<div class="g-review-toolbar"><label>复盘月份<input id="g-review-month" type="month" value="${data.month}" aria-label="复盘月份"></label><button data-g="review-month">查看这个月</button><button data-g="export-review">导出本月记录与复盘</button></div>${stats()}<div class="g-review-grid">${calendar()}<section class="g-panel"><div class="g-section-head"><div><h2>${icon('layers')}支柱分布</h2><p class="g-note">左侧为你手动标注的碎片，右侧为知识库自动归类的内容；累计口径，与目标树一致，点进去都是清单。</p></div></div><div class="g-pillar-list">${data.pillars.map(p=>{const manual=goalManual(p.id),auto=goalAuto(p.id);return `<div class="g-goal-row" data-depth="0"><span class="g-tree-symbol" data-goal-color="${e(p.color)}">${icon(pillarIcon(p))}</span><button class="g-goal-name" data-g="goal-detail" data-id="${e(p.id)}" title="${e(p.title)}"><strong>${e(p.title)}</strong></button><span class="g-goal-counts">${manual?`<a class="g-goal-count" href="${e(url({view:'library',goal:p.id,month:'',day:'',q:'',type:'',inbox:'',page:''}))}" data-gloss="count-manual" aria-label="查看${e(p.title)}手动标注的 ${manual} 条碎片">${manual}<small>手动</small></a>`:`<span class="g-goal-count" data-gloss="count-manual">0<small>手动</small></span>`}${auto?`<a class="g-goal-count g-goal-count-auto" href="${e(url({view:'goal-auto',goal:p.id,month:'',day:'',q:'',type:'',inbox:'',page:''}))}" data-gloss="count-auto" aria-label="查看${e(p.title)}来自知识库的 ${auto} 条内容">${auto}<small>知识库</small></a>`:''}</span></div>`;}).join('')}</div><p class="g-note g-space">${e(data.count_note)}</p></section></div><section class="g-panel g-space"><div class="g-section-head"><div><h2>把记录变成下一步</h2><p class="g-note">先看积累，再写自己的判断。</p></div><span class="g-note" id="g-review-save-state">${r.revision?'已保存':'尚未填写'}</span></div><form data-g-form="review"><input name="month" type="hidden" value="${data.month}"><input name="revision" type="hidden" value="${r.revision||0}">${[['observation','本月观察','我反复关注了什么？有什么变化？','search'],['action','已做的行动','哪条积累在生活或创作中用上了？','check'],['next','下期关注','下一步准备尝试的一件小事。','arrow']].map(([key,title,hint,glyph],i)=>`<details class="g-review-step" ${i===0||r[key]?'open':''}><summary><span class="g-step-icon">${icon(glyph)}</span><span>${title}<small>${hint}</small></span></summary><label class="field"><span class="g-sr-only">${title}</span><textarea name="${key}" rows="3" maxlength="12000" placeholder="${hint}">${e(r[key]||'')}</textarea></label></details>`).join('')}<div class="g-heading-actions g-space"><button class="g-primary" type="submit">${icon('check')}保存这次复盘</button><span class="g-form-status" role="status"></span></div></form></section>`;
  }
  function loadGraphCards(){
    if(window.ipGrowthCards&&window.ipGrowthNeural)return Promise.resolve();
    const load=(src,next)=>{const s=document.createElement('script');s.src=src;s.onload=next;s.onerror=next;document.head.appendChild(s);};
    if(!graphCardsLoading)graphCardsLoading=new Promise(resolve=>{
      load('/graph-cards.js?v=20261117j',()=>load('/graph-neural.js?v=20261117d',resolve));
    });
    return graphCardsLoading;
  }
  async function graph(){
    graphData=await api('/growth/graph?'+query);
    graphCards=null;graphCardsError='';
    await loadGraphCards();
    try{graphCards=await api('/graph/cards');}catch(err){graphCardsError=err.message||String(err);}
    return `${filters()}<section class="g-panel g-graph-panel"><div class="g-section-head"><div><h2>${icon('graph')}你的积累，正在连成一张网</h2><p class="g-note">悬停看邻居 · 点选看详情 · 滚轮缩放</p></div><div class="g-heading-actions"><button data-g="graph-pause">${icon('pause')}暂停动效</button><button data-g="zoom-in" aria-label="放大图谱">＋</button><button data-g="zoom-out" aria-label="缩小图谱">－</button><button data-g="reset-graph">复位</button></div></div><div class="g-graph-modes"><button data-g="graph-overview">主线视图</button><button data-g="graph-all">展开当前范围</button><span id="g-graph-count" class="g-note" aria-live="polite"></span></div><div id="g-pillar-bar" class="g-pillar-bar" hidden></div><div class="g-graph-stage"><svg id="growth-graph" viewBox="0 0 1000 660" role="group" aria-label="可交互的目标与碎片图谱"></svg><aside id="g-graph-detail" class="g-graph-detail" aria-label="节点详情" hidden></aside></div><p class="g-note">实线=互链 · 虚线=归属</p></section>`;
  }
  /* ===== 五支柱横条（2026-11-25 · 需求 C / 方案 A）=====
   * 打开脉络图不点任何东西就看到五大类各多少条（+未归类）；节点大小∝条数；
   * 点支柱展开分类清单（带 +N 计数徽标），点分类看条目清单。
   * 数据来自 /graph/cards（graph_view.build）：clusters 按分类、goals.pillar 带 card_count。
   * 无内联 style（CSP）：颜色与字号走 CSS 类。每次 mount 重画，返回清理函数。 */
  function paintPillarBar(payload){
    const bar=document.getElementById('g-pillar-bar');
    if(!bar)return null;
    const clusters=(payload&&payload.clusters)||[];
    const cards=(payload&&payload.cards)||[];
    const goals=(payload&&payload.goals)||[];
    const pillars=goals.filter(g=>g.level==='pillar');
    if(!pillars.length){bar.hidden=true;return null;}
    const byPillar={};let uncat=0;
    clusters.forEach(c=>{const p=c.pillar||'';if(p){(byPillar[p]=byPillar[p]||[]).push(c);}else{uncat+=c.count||0;}});
    const maxc=Math.max(1,...pillars.map(p=>p.card_count||0),uncat);
    const chip=(title,count)=>{
      const n=count||0;
      const sz=n>=Math.round(maxc*0.66)?'ipb-xl':n>=Math.round(maxc*0.33)?'ipb-lg':'ipb-sm';
      return '<button class="ipb-chip ipb-p-'+e(title)+' '+sz+'" data-ipb-pillar="'+e(title)+'" type="button">'
        +'<span class="ipb-dot" aria-hidden="true"></span><strong>'+e(title)+'</strong><span class="ipb-count">'+n+' 条</span></button>';
    };
    let html='<div class="ipb-row" role="list" aria-label="五大支柱条数总览">';
    pillars.forEach(p=>{html+=chip(p.title,p.card_count);});
    if(uncat)html+=chip('未归类',uncat);
    html+='</div><div class="ipb-panel" hidden></div>';
    bar.innerHTML=html;
    bar.hidden=false;
    const panel=bar.querySelector('.ipb-panel');
    let openPillar='',openCat='';
    const catsOf=p=>clusters.filter(c=>p==='未归类'?!(c.pillar):c.pillar===p)
      .map(c=>({id:c.id,title:c.title,count:c.count||0,items:cards.filter(x=>x.category===c.id)}));
    const paintPanel=()=>{
      if(!openPillar){panel.hidden=true;panel.innerHTML='';return;}
      const cats=catsOf(openPillar);
      const total=openPillar==='未归类'?uncat:((pillars.find(p=>p.title===openPillar)||{}).card_count||0);
      const catsHtml=cats.map(c=>{
        const items=openCat===c.id
          ? '<ul class="ipb-items">'+c.items.slice(0,50).map(x=>'<li><span class="ipb-item-title">'+e(x.title||'未命名')+'</span><span class="ipb-item-date">'+e((x.recorded_at||'').slice(0,10))+'</span></li>').join('')
            +(c.items.length>50?'<li class="ipb-note">还有 '+(c.items.length-50)+' 条未列出</li>':'')+'</ul>'
          : '';
        return '<div class="ipb-cat"><button class="ipb-cat-btn'+(openCat===c.id?' on':'')+'" data-ipb-cat="'+e(c.id)+'" type="button">'
          +e(c.title)+'<span class="ipb-badge">+'+(c.count||0)+'</span></button>'+items+'</div>';
      }).join('');
      panel.innerHTML='<p class="ipb-panel-title">'+e(openPillar)+' · '+total+' 条 · 点分类看清单，再点收起</p>'
        +(catsHtml||'<p class="ipb-note">这一支柱下还没有积累</p>');
      panel.hidden=false;
    };
    const onClick=ev=>{
      const chipBtn=ev.target.closest('.ipb-chip');
      if(chipBtn){
        const p=chipBtn.getAttribute('data-ipb-pillar')||'';
        if(openPillar===p){openPillar='';openCat='';paintPanel();chipBtn.classList.remove('on');}
        else{openPillar=p;openCat='';paintPanel();
          bar.querySelectorAll('.ipb-chip').forEach(b=>b.classList.toggle('on',b===chipBtn));}
        return;
      }
      const catBtn=ev.target.closest('.ipb-cat-btn');
      if(catBtn){const c=catBtn.getAttribute('data-ipb-cat')||'';openCat=openCat===c?'':c;paintPanel();}
    };
    bar.addEventListener('click',onClick);
    return ()=>{bar.removeEventListener('click',onClick);bar.hidden=true;bar.innerHTML='';};
  }
  function autoContent(detail,problem){
    const back=e(url({view:'goals',goal:'',day:'',q:'',type:'',inbox:'',page:'',branch:''}));
    if(problem||!detail)return `<section class="g-panel"><div class="g-section-head"><h2>没能读取这部分内容</h2></div><p class="g-note">${e(problem||'未知错误')}</p><a class="g-text-link" href="${back}">返回目标树</a></section>`;
    const goal=detail.goal||{},items=detail.items||[],cats=detail.categories||{};
    const manual=e(url({view:'library',goal:goal.id||'',month:'',day:'',q:'',inbox:'',type:'',page:''}));
    return `<section class="g-panel g-auto-panel"><div class="g-section-head"><div><h2>${e(goal.title||'')} · 积累来源</h2><p class="g-note">知识库自动归类 ${detail.auto_count} 条 · 你手动标注 ${detail.manual_count} 条</p></div><div class="g-heading-actions"><a class="g-text-link" href="${manual}">手动标注的 ${detail.manual_count} 条</a><a class="g-text-link" href="${back}">返回目标树</a></div></div><div class="g-auto-cats">${Object.entries(cats).map(([name,n])=>`<a href="#/brain?${new URLSearchParams({category:name})}">${icon('layers')}${e(name)}<strong>${n}</strong></a>`).join('')}</div><p class="g-note">这些是知识库里的知识，不是成长碎片；点一条可看原文。</p><div class="g-auto-list">${items.map(x=>`<a class="g-auto-row" href="#/knowledge/${encodeURIComponent(x.id)}"><span class="g-auto-main"><strong>${e(x.title||'未命名')}</strong><small>${e(x.excerpt||'')}</small></span><span class="g-auto-meta">${e(x.category||'')}<time>${e(x.recorded_at?dayOf(x.recorded_at):'')}</time></span>${icon('arrow')}</a>`).join('')||'<p class="g-note">这个目标暂时没有自动归类的内容。</p>'}</div></section>`;
  }
  /* ===== 立轴引导（2026-11-17 · t27）=====
   * 三观卡＝镜子；命题候选可编辑、留空也能开始；目标含赚钱正当，命题是可选方向。
   * 样式仍走 CSSOM（CSP style-src 'self'），不写内联 style。 */
  let axisSheetRule = null;
  function axisSheet() {
    if (axisSheetRule) return axisSheetRule;
    const sheet = new CSSStyleSheet();
    [
      '.g-axis-mirror{margin:14px 0;padding:13px 15px;border:1px solid var(--border-strong);border-radius:12px;background:var(--surface);text-align:left}',
      '.g-axis-mirror h3{margin:0 0 8px;font-size:13px;color:var(--gold);display:flex;gap:6px;align-items:center}',
      '.g-axis-mirror ul{list-style:none;margin:0;padding:0;display:grid;gap:8px}',
      '.g-axis-mirror li span{font-size:11px;color:var(--muted)}',
      '.g-axis-mirror li p{margin:2px 0 0;font-size:13px;color:var(--text);line-height:1.65}',
      '.g-axis-candidates{margin:16px 0 4px;text-align:left}',
      '.g-axis-candidates h3{margin:0 0 6px;font-size:13px;color:var(--text);display:flex;gap:6px;align-items:center}',
      '.g-axis-cand{display:flex;gap:9px;align-items:flex-start;padding:9px 11px;border:1px solid var(--border);border-radius:10px;margin:7px 0;cursor:pointer}',
      '.g-axis-cand input{margin-top:3px}',
      '.g-axis-cand strong{font-size:13px;line-height:1.6;color:var(--text)}',
      '.g-axis-cand small{display:block;color:var(--muted);font-size:11px;margin-top:3px;line-height:1.5}',
      '.g-axis-connect{margin:8px 0 0;font-size:12px;color:var(--gold);line-height:1.6}',
      '.g-axis-line{margin:6px 0 0;font-size:12px;color:var(--gold);line-height:1.6}',
      '.g-inbox-row{display:flex;gap:10px;align-items:center;justify-content:space-between;padding:10px 2px;border-bottom:1px solid var(--border)}',
      '.g-inbox-main{min-width:0;display:grid;gap:2px}',
      '.g-inbox-main strong{font-size:13px;color:var(--text)}',
      '.g-inbox-main small{font-size:11px;color:var(--muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}'
    ].forEach((text, i) => sheet.insertRule(text, i));
    document.adoptedStyleSheets = [...document.adoptedStyleSheets, sheet];
    axisSheetRule = sheet.cssRules[0];
    return axisSheetRule;
  }
  let voiceGuideCss = false;
  function voiceGuideSheet() {
    if (voiceGuideCss) return;
    voiceGuideCss = true;
    try {
      const sheet = new CSSStyleSheet();
      [
        '.g-voice-guide{margin:8px 0 0;padding:12px 14px;border:1px solid var(--border-strong);border-left:3px solid var(--gold);border-radius:10px;background:var(--surface);text-align:left}',
        '.g-voice-guide p{margin:0 0 6px;font-size:12.5px;line-height:1.8;color:var(--text)}',
        '.g-voice-guide ol{margin:4px 0 8px;padding-left:19px;font-size:12.5px;line-height:1.85;color:var(--text)}',
        '.g-voice-guide .gvg-why{color:var(--gold);font-size:12px}',
        '.g-voice-guide .gvg-soft{color:var(--muted);font-size:12px;margin:0}'
      ].forEach((t, i) => sheet.insertRule(t, i));
      document.adoptedStyleSheets = [...document.adoptedStyleSheets, sheet];
    } catch (err) { /* 样式失败不影响文字 */ }
  }
  function voiceGuideHtml() {
    voiceGuideSheet();
    return `<div class="g-voice-guide" role="note">
      <p><strong>这台电脑没有可用的麦克风，也能用「说」的：</strong></p>
      <ol><li>拿手机打开微信，按住说话，把想法说出来（想到哪说到哪，30 秒也行）</li>
      <li>长按那条语音 → 点「转文字」→ 复制文字</li>
      <li>回到这里粘贴到上面「想到什么，先写下来」框里，保存就行</li></ol>
      <p class="gvg-why">为什么值得费这一步：你的真实声音是去 AI 味的金矿——系统要学你本人说话的口气和节奏，之后写的稿子才像你说的，而不是机器腔。</p>
      <p class="gvg-soft">说不好、说断了、没头绪都没关系，AI 会帮你修饰；碎片和半句也有用。实在不想说就直接打字，内容一样能保存。</p>
    </div>`;
  }
  const voiceInputReady = (ready) => !!(ready && navigator.mediaDevices && navigator.mediaDevices.getUserMedia && window.MediaRecorder);
  window.growthVoiceGuideHtml = voiceGuideHtml; // 2026-11-18：供问问自己页复用同一份「微信转文字」引导

  let axisGuide = null;
  let thesisState = null;   // t71：命题修订候选＋版本史（GET /api/growth/thesis/proposals）
  async function loadAxisGuide() {
    if (axisGuide) return axisGuide;
    axisSheet();
    let flow = null, suggest = null, error = '';
    try { flow = await api('/flow'); } catch (err) { error = err.message || String(err); }
    if (flow && flow.card) {
      try { suggest = await api('/growth/thesis/suggest'); } catch (err) { error = err.message || String(err); }
    }
    axisGuide = { flow, suggest, error };
    return axisGuide;
  }
  let thesisSheetRule = null;
  function thesisSheet() {
    if (thesisSheetRule) return thesisSheetRule;
    const sheet = new CSSStyleSheet();
    [
      '.g-thesis-cand{margin:12px 0;padding:13px 15px;border:1px solid var(--border-strong);border-left:3px solid var(--gold);border-radius:10px;background:var(--surface)}',
      '.g-thesis-cand h3{margin:0 0 8px;font-size:13px;color:var(--gold)}',
      '.g-thesis-row{display:grid;gap:4px;margin:6px 0}',
      '.g-thesis-row span{font-size:11px;color:var(--muted)}',
      '.g-thesis-row p{margin:0;font-size:13px;line-height:1.7;color:var(--text)}',
      '.g-thesis-cand textarea{width:100%;min-height:62px;padding:9px 11px;border:1px solid var(--border);border-radius:9px;background:var(--bg);font-size:13px;line-height:1.7;color:var(--text)}',
      '.g-thesis-actions{display:flex;gap:10px;flex-wrap:wrap;margin-top:9px}',
      '.g-thesis-history{margin-top:10px;font-size:12px;color:var(--muted);line-height:1.8}'
    ].forEach((text, i) => sheet.insertRule(text, i));
    document.adoptedStyleSheets = [...document.adoptedStyleSheets, sheet];
    thesisSheetRule = sheet.cssRules[0];
    return thesisSheetRule;
  }
  function thesisPanel() {
    if (!thesisState) return '';
    const proposals = (thesisState.proposals || []).filter(p => p && p.status === 'pending');
    const history = thesisState.history || [];
    if (!proposals.length && !history.length) return '';
    thesisSheet();
    const cand = p => `<article class="g-thesis-cand" data-proposal="${e(p.id)}">
      <h3>${icon('target')} 命题修订候选</h3>
      <div class="g-thesis-row"><span>原命题</span><p>${e(p.original || thesisState.thesis || '（此前没有命题）')}</p></div>
      <div class="g-thesis-row"><span>候选命题（可改到你认领）</span><textarea data-thesis-edit maxlength="600">${e(p.proposed || '')}</textarea></div>
      ${p.reason ? `<div class="g-thesis-row"><span>为什么提这个候选</span><p>${e(p.reason)}</p></div>` : ''}
      <p class="g-note">来源：${p.source === 'review_insight' ? '复盘洞察' : '手动登记'}${p.evidence_count ? ` · 证据 ${p.evidence_count} 条` : ''}${p.insight_id ? ` · ${e(p.insight_id)}` : ''}</p>
      <div class="g-thesis-actions"><button class="g-primary" data-g="thesis-accept" data-id="${e(p.id)}">${icon('check')}采纳并更新命题</button><button data-g="thesis-reject" data-id="${e(p.id)}">拒绝</button></div>
    </article>`;
    const acceptedIds = new Set((thesisState.proposals || []).filter(p => p && p.status === 'accepted').map(p => p.id));
    const hist = history.length ? `<details class="g-thesis-history"><summary>命题版本史（${history.length}）</summary>${history.slice().reverse().map(h => `<p>${e(h.at || '')}<br>${e(h.thesis || '')}${h.previous ? `<br><span>上一版：${e(h.previous)}</span>` : ''}${h.from_proposal && acceptedIds.has(h.from_proposal) ? `<br><button class="g-text-link" data-g="thesis-undo" data-id="${e(h.from_proposal)}">${h.decided_by === 'reflow_auto' ? '撤销这次自动采纳' : '回滚到上一版命题'}</button>` : ''}</p>`).join('')}</details>` : '';
    const head = `<div class="g-section-head"><div><h2>${icon('target')}命题修订候选${proposals.length?`<span class="g-note">（待审 ${proposals.length} 条）</span>`:''}</h2><p class="g-note">AI 只提候选，命题永远由你拍板：可逐条改写/采纳/拒绝，也可以攒几条一起看后一键采纳。${history.length?` 已采纳 ${history.length} 版，可回看/回滚。`:''}</p></div>${proposals.length>1?`<div class="g-heading-actions"><button class="g-primary" data-g="thesis-accept-all">${icon('check')}一键采纳全部（${proposals.length}）</button></div>`:''}</div>`;
    return `<section class="g-panel g-space" id="g-thesis-panel">${head}${proposals.map(cand).join('')}${hist}</section>`;
  }
  function axisMirror(axis) {
    const card = (axis.flow && axis.flow.card) || {};
    const rows = [['世界观', card.worldview], ['人生观', card.life], ['价值观', card.values]].filter(([, t]) => t);
    if (!rows.length) return '';
    return `<section class="g-axis-mirror"><h3>${icon('brain')}这是你刚才说的你自己</h3><ul>${rows.map(([k, t]) => `<li><span>${e(k)}</span><p>${e(t)}</p></li>`).join('')}</ul><p class="g-note">三观卡来自你答的题；下面的话只是候选，改到你认领为止。</p></section>`;
  }
  function axisCandidates(axis) {
    const list = (axis.suggest && axis.suggest.candidates) || [];
    if (!list.length) return '';
    return `<section class="g-axis-candidates"><h3>${icon('star')}你的命题（候选，可改；留空＝先不立命题）</h3>${list.map((c, i) => `<label class="g-axis-cand"><input type="radio" name="thesis_pick" value="${i}" data-thesis="${e(c.thesis)}" ${i === 0 ? 'checked' : ''}><span><strong>${e(c.thesis)}</strong>${c.source || c.boundary ? `<small>${c.source ? '来自：' + e(c.source) : ''}${c.boundary ? ` · 不成立的时候：${e(c.boundary)}` : ''}</small>` : ''}</span></label>`).join('')}<label class="field"><span>你认领的命题（可编辑）</span><textarea name="thesis" rows="2" maxlength="600" placeholder="我相信……，我要用……通过内容验证它">${e(list[0].thesis)}</textarea></label>${list[0].connect ? `<p class="g-axis-connect">${e(list[0].connect)}</p>` : ''}</section>`;
  }
  function welcome(axis) {
    const flow = axis && axis.flow;
    const hasCard = !!(flow && flow.card);
    /* 2026-11-25（方案 A）：还没立主线的新用户，这一页原来没有访谈诊断。对话首页的引导卡带 ?diag=1 进来时，把诊断放到立主线表单前面并直接开出第一批题；平时不带参数进来顺序不变（先立主线，诊断在表单下面）。 */ const diagFirst = new URLSearchParams(location.hash.split('?')[1] || '').get('diag') === '1'; return `<div class="growth">${header('overview')}${diagFirst ? diagSection() : ''}<section class="g-welcome"><span class="g-welcome-icon">${icon('target')}</span><h2>先把主线立起来，碎片才有地方去。</h2><p>目标可以很实际（比如赚钱、涨粉、出片数）；命题是更深的可选方向，不填也能开始。</p>${hasCard ? axisMirror(axis) : ''}<form data-g-form="setup"><input type="hidden" name="track" value="${e((flow && flow.track) || '')}">${axisCandidates(axis)}<label class="field"><span>你的主线：今年想走向哪里</span><input type="text" name="title" required maxlength="160" placeholder="比如：今年靠内容稳定月入 1 万 / 先做 100 条片子"></label><label class="field"><span>主业目标（可选，写具体数字或结果）</span><input type="text" name="milestone" maxlength="160" placeholder="比如：主业收入 10 万 / 涨粉 5000"></label><button class="g-primary" type="submit">${icon('plus')}开始我的重启系统</button></form><p class="g-note">认知 · 意义 · 能量 · 关系 · 价值，之后都可以改名；命题以后也能在「调整目标」里补。${hasCard ? '' : '<br>想要更像你的目标？先去完成建档（12 题），回来这里会先给你看三观卡和命题候选。'}</p></section>${diagFirst ? '' : diagSection()}</div>`;
  }
  function inboxView(box, problem) {
    if (problem) return `<section class="g-panel"><div class="g-section-head"><h2>没能读取等待归位的清单</h2></div><p class="g-note">${e(problem)}</p></section>`;
    const items = (box && box.items) || [], total = (box && box.total) || 0;
    return `<section class="g-panel"><div class="g-section-head"><div><h2>${icon('inbox')}等待归位 · ${total} 条</h2><p class="g-note">${e((box && box.note) || '统一内容池里还没有关联目标的条目。')}</p></div><div class="g-heading-actions"><a class="g-text-link" href="${e(url({view: 'library', inbox: '1'}))}">只看成长碎片的待整理</a><a class="g-text-link" href="${e(url({view: 'overview'}))}">回总览</a></div></div>${items.length ? `<div class="g-inbox-list">${items.map(x => `<div class="g-inbox-row"><span class="g-inbox-main"><strong>${e(x.title)}</strong><small>${e([x.category, x.recorded_at ? dayOf(x.recorded_at) : '', x.excerpt].filter(Boolean).join(' · '))}</small></span><span class="g-heading-actions"><a class="g-text-link" href="#/knowledge/${encodeURIComponent(x.id)}">看原文</a><button data-g="import-one" data-id="${e(x.id)}">归到目标</button></span></div>`).join('')}</div>` : '<p class="g-note g-space">都归位了，没有等待整理的条目。</p>'}</section>`;
  }

  let assetsSheetReady = false;
  function assetsSheet() {
    if (assetsSheetReady) return;
    assetsSheetReady = true;
    const sheet = new CSSStyleSheet();
    ['.g-asset-hero{display:grid;gap:8px;margin-bottom:14px}',
     '.g-asset-hero h2{margin:0;font-size:22px}',
     '.g-asset-thickness{margin:0;font-size:15px;line-height:1.85;color:var(--text)}',
     '.g-asset-cards{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin:12px 0}',
     '.g-asset-card{padding:14px 16px;border:1px solid var(--border);border-radius:12px;background:var(--panel)}',
     '.g-asset-card small{display:block;font-size:11px;letter-spacing:.08em;color:var(--muted)}',
     '.g-asset-card strong{display:block;margin-top:6px;font-size:28px;line-height:1.1;color:var(--gold)}',
     '.g-asset-card span{display:block;margin-top:4px;font-size:11px;color:var(--muted)}',
     '.g-asset-trend{margin-top:12px}',
     '.g-asset-trend svg{display:block;width:100%;height:auto;margin-top:8px}',
     '.g-asset-line{fill:none;stroke:var(--gold);stroke-width:2;stroke-linejoin:round;stroke-linecap:round}',
     '.g-asset-area{fill:var(--gold);opacity:.08}',
     '.g-asset-dot{fill:var(--gold)}',
     '.g-asset-axis{display:flex;justify-content:space-between;font-size:11px;color:var(--muted);margin-top:4px}',
     '.g-asset-toggle{display:flex;gap:8px}',
     '.g-asset-panel{margin-top:14px}',
     '.g-asset-stats{grid-template-columns:repeat(4,minmax(0,1fr))}',
     '.g-asset-empty{padding:18px;border:1px dashed var(--border);border-radius:12px;margin-top:10px}',
     '@media(max-width:720px){.g-asset-cards{grid-template-columns:repeat(2,minmax(0,1fr))}}'
    ].forEach((text, i) => sheet.insertRule(text, i));
    document.adoptedStyleSheets = [...document.adoptedStyleSheets, sheet];
  }

  function assetChart(points) {
    const width = 640, height = 180, left = 18, right = 14, top = 16, bottom = 18;
    const list = points || [];
    const max = Math.max(1, ...list.map(p => p.total));
    const usable = width - left - right;
    const plotH = height - top - bottom;
    const xy = list.map((p, i) => {
      const x = left + (list.length <= 1 ? usable / 2 : usable * i / (list.length - 1));
      const y = top + plotH - (p.total / max) * plotH;
      return [x, y];
    });
    const line = xy.map(p => p[0].toFixed(1) + ',' + p[1].toFixed(1)).join(' ');
    const area = xy.length ? (left + ',' + (top + plotH) + ' ' + line + ' ' + xy[xy.length - 1][0].toFixed(1) + ',' + (top + plotH)) : '';
    const last = xy[xy.length - 1] || [left, top + plotH];
    return `<svg viewBox="0 0 ${width} ${height}" role="img" aria-label="按记录时间累计的资产曲线">` +
      (area ? `<polygon class="g-asset-area" points="${area}"/>` : '') +
      (line ? `<polyline class="g-asset-line" points="${line}"/>` : '') +
      (xy.length ? `<circle class="g-asset-dot" cx="${last[0].toFixed(1)}" cy="${last[1].toFixed(1)}" r="3.5"/>` : '') +
      `</svg>`;
  }

  async function render(){
    query=new URLSearchParams(location.hash.split('?')[1]||'');const view=query.get('view')||'overview';
    const titles={overview:'成长总览',goals:'目标体系',library:'碎片记录',ask:'问问自己',search:'认知搜索',graph:'成长图谱',review:'月度复盘','goal-auto':'目标积累来源',inbox:'等待归位'};
    document.getElementById('breadcrumb').textContent='重启系统 · '+(titles[view]||titles.overview);
    if(view==='ask')return `<div class="growth">${header(view)}${await window.ipGrowthAsk.render()}</div>`;
    if(view==='search')return `<div class="growth">${header(view)}${await window.ipGrowthSearch.render(query)}</div>`;
    if(view==='goal-auto'){
      let detail=null,problem='';
      try{detail=await api('/growth/goal-content?goal='+encodeURIComponent(query.get('goal')||''));}catch(err){problem=err.message||String(err);}
      return `<div class="growth">${header(view)}${autoContent(detail,problem)}<p class="g-footer-note">这些内容来自知识库，按分类自动计入，不会改写原文。</p></div>`;
    }
    if(view==='inbox'){
      let box=null,problem='';
      try{box=await api('/growth/inbox');}catch(err){problem=err.message||String(err);}
      return `<div class="growth">${header(view)}${inboxView(box,problem)}<p class="g-footer-note">与首页「等待归位」同一批数据（统一内容池里没关联目标的条目）。</p></div>`;
    }
    data=await api('/growth/overview?'+query);
    try{checkinData=await api('/checkin/state');}catch(err){checkinData=null;}
    try{thesisState=await api('/growth/thesis/proposals');}catch(err){thesisState=null;}
    try{assetsData=await api('/growth/assets');}catch(err){assetsData=null;}
    try{talentData=await api('/growth/talent');}catch(err){talentData=null;}
    if(data.goals.length&&!data.goal_counts)return `<div class="growth"><section class="g-welcome"><h2>界面已更新</h2><p>请重新启动工作台，以加载本次统计与图谱升级。</p></section></div>`;
    if(!data.goals.length)return welcome(await loadAxisGuide());
    return `<div class="growth">${header(view)}${view==='goals'?goals():view==='library'?library():view==='graph'?await graph():view==='review'?review():overview()}<p class="g-footer-note">当前积累保存在这台工作台。${data.month} · 成长由你定义。</p></div>`;
  }

  function goalForm(key='',parent='') {
    const g = data.goals.find(x=>x.id===key) || {parent_id:parent,title:'',description:'',color:getComputedStyle(document.documentElement).getPropertyValue('--gold').trim()};
    const axisFields = g.level==='root' ? `<label class="field"><span>命题（可选；目标照旧，命题是更深的方向）</span><textarea name="thesis" maxlength="600" rows="2" placeholder="我相信……，我要用……通过内容验证它">${e(g.thesis||'')}</textarea></label><label class="field"><span>主业目标（可选，写具体数字或结果）</span><input type="text" name="milestone" maxlength="160" value="${e(g.milestone||'')}" placeholder="比如：主业收入 10 万 / 涨粉 5000"></label><p class="g-note">命题不否定目标：赚多少钱、涨多少粉都算正当目标；命题只是更深的一层，可以以后再补。</p>` : '';
    const excluded = new Set(key?[key]:[]);
    let size;
    do {size=excluded.size;data.goals.filter(x=>excluded.has(x.parent_id)).forEach(x=>excluded.add(x.id));}while(size!==excluded.size);
    growthModal(key?'调整目标':'添加一个目标',`<form data-g-form="goal" data-id="${e(key)}"><input name="revision" type="hidden" value="${g.revision||0}"><label class="field"><span>目标名称</span><input type="text" name="title" maxlength="160" required value="${e(g.title)}"></label>${g.level==='root'?'':`<label class="field"><span>上级目标</span><select name="parent_id" required>${options(g.parent_id,[...excluded],true)}</select></label>`}${axisFields}<label class="field"><span>想达到什么，为什么</span><textarea name="description" maxlength="4000" rows="3">${e(g.description||'')}</textarea></label><label class="field"><span>标记颜色</span><input type="color" name="color" value="${e(g.color)}"></label>${key&&g.level!=='root'?`<label class="check"><input type="checkbox" name="archived" ${g.archived?'checked':''}>归档这个目标（保留全部历史）</label>`:''}<button class="g-primary" type="submit">保存目标</button><p class="g-form-status" role="status"></p></form>`);
  }
  const activeTranscriptions=new Map();
  let transcriptTimer=null,pollingTranscript=false,pollGeneration=0,refreshAfterModal=false;
  function transcriptionState(item){
    const t=item.transcription;
    return `<div class="g-transcription-state ${t?.status==='failed'?'g-transcription-error':''}" data-g-job="${e(item.id)}" role="status">${item.transcript_available?icon('check')+' 本地文字稿已保存':t?e(t.status==='failed'?'转写未完成：'+(t.error||'请重试'):t.stage||'等待转写'):'尚未生成文字稿'}</div>`;
  }
  function manuscriptActions(item){
    const busy=['queued','running'].includes(item.transcription?.status);
    return `<div class="g-card-controls">${item.transcript_available?`<button data-g="manuscript" data-id="${e(item.id)}">${icon('text')}查看文字稿</button>`:item.source_url?`<button data-g="video-transcribe" data-id="${e(item.id)}" ${busy?'disabled':''}>${icon('text')}${busy?'正在转写':item.transcription?.status==='failed'?'重试转写':'转文字稿'}</button>`:''}<button class="g-delete-link" data-g="delete-fragment" data-id="${e(item.id)}" ${busy?'disabled title="正在转写，请结束后再删除"':''}>删除</button></div>`;
  }
  function rememberTranscription(item){
    if(['queued','running'].includes(item.transcription?.status))activeTranscriptions.set(item.id,item);
    if(transcriptTimer||!activeTranscriptions.size)return;
    transcriptTimer=setInterval(async()=>{
      if(pollingTranscript||document.hidden||!mountedHash)return;
      pollingTranscript=true;const generation=pollGeneration;
      try{
        for(const key of [...activeTranscriptions.keys()]){
          const fresh=await api('/growth/fragments/'+encodeURIComponent(key));
          if(generation!==pollGeneration)return;
          Object.assign(data.items.find(i=>i.id===key)||{},fresh);
          document.querySelectorAll('[data-g-job="'+key+'"]').forEach(el=>{el.outerHTML=transcriptionState(fresh);});
          if(!['queued','running'].includes(fresh.transcription?.status)){
            activeTranscriptions.delete(key);const d=document.getElementById('modal');
            if(d.open){refreshAfterModal=true;if(d.dataset.gFragment===key&&!d.querySelector('form'))await fragmentDetail(key);}
            else if(!reviewDirty()){await route();return;}
          }
        }
      }catch(err){/* A foreground refresh/retry reports the actionable error. */}
      finally{pollingTranscript=false;if(!activeTranscriptions.size){clearInterval(transcriptTimer);transcriptTimer=null;}}
    },2500);
  }
  async function fragmentForm(item={},kind='text'){
    await ensureData();
    captureGeneration++;voiceId=item.voice_id||'';audioBlob=null;
    const localReady=state.boot?.capabilities?.local_asr_ready;
    growthModal(item.id?'编辑碎片':kind==='link'?'视频链接转文字稿':'留下一条碎片',`<form data-g-form="fragment" data-original-type="${e(item.type||'text')}" data-id="${e(item.id||'')}"><input name="revision" type="hidden" value="${item.revision||0}"><label class="field"><span>来源链接（可粘贴整段分享文字）</span><input name="source_url" type="text" inputmode="url" maxlength="12000" placeholder="粘贴抖音视频链接或分享文字" value="${e(item.source_url||'')}"></label><div class="g-video-options"><label class="check"><input name="transcribe_video" type="checkbox" ${!item.transcript_available?'checked':''}>保存后转成文字稿，保存在这台电脑</label><p class="g-note">目前支持抖音公开链接。本机识别，不自动调用付费转写；登录、验证或环境问题会显示原因。</p></div><label class="field"><span>标题（可稍后再补）</span><input type="text" name="title" maxlength="160" value="${e(item.title||'')}" placeholder="留空时根据视频资料补上标题"></label><label class="field"><span>${kind==='link'?'我的备注（可选，原稿会单独保留）':'想到什么，先写下来'}</span><textarea name="text" id="g-fragment-text" maxlength="40000" rows="4" placeholder="一段观点、一个方法，或自己的备注…">${e(item.text||'')}</textarea></label><div class="g-voice-controls" ${kind==='link'?'hidden':''}><button type="button" data-g="record" ${voiceInputReady(localReady)?'':'disabled'}>开始录音</button><button type="button" data-g="transcribe" disabled>转成文字</button><span id="g-voice-status" role="status">${voiceInputReady(localReady)?'仅用本机转写。':'本地录音不可用；可以按下面「微信转文字」把说的话贴进来，或直接打字。'}</span>${voiceInputReady(localReady)?'':voiceGuideHtml()}</div><span class="g-field-title">关联目标</span>${tags(item.goal_ids||[])}<label class="check"><input name="accepted" type="checkbox" ${item.endorsement==='accepted'?'checked':''}>我认可这条内容，可选作创作素材</label>${item.id?`<label class="check"><input type="checkbox" name="archived" ${item.archived?'checked':''}>归档这条碎片（保留内容）</label>`:''}<button class="g-primary" type="submit">保存碎片</button><p class="g-form-status" role="status"></p></form>`);
    updateVideoEntry(document.querySelector('[data-g-form="fragment"]'));
    modalBaseline=snapshotForm(document.querySelector('[data-g-form="fragment"]'));
    const focus=document.querySelector(kind==='link'?'#modal [name="source_url"]':kind==='voice'?'#modal [data-g="record"]':'#g-fragment-text');if(focus&&!focus.disabled)focus.focus();
  }
  function updateVideoEntry(form){
    if(!form)return;
    const hasLink=!!form.elements.source_url.value.trim();form.querySelector('.g-video-options').hidden=!hasLink;
    form.querySelector('[type="submit"]').textContent=hasLink&&form.elements.transcribe_video.checked?'保存并转文字稿':'保存碎片';
  }
  async function ensureData(){
    if(!data)data=await api('/growth/overview?'+query);
  }
  async function fragmentDetail(key){
    await ensureData();
    const item=await api('/growth/fragments/'+encodeURIComponent(key));rememberTranscription(item);
    growthModal('这一次积累',`<article class="g-detail"><span class="g-note">${e(date(item.recorded_at))} · ${item.endorsement==='accepted'?'本人认可':'仅作参考'}</span><h2>${e(item.title)}</h2>${item.source_url?transcriptionState(item):''}<pre>${e(item.text||'视频口播文字稿生成后，会保存在这里。')}</pre>${manuscriptActions(item)}<div class="g-detail-paths">${item.goal_ids.map(id=>`<p>${e(tagPath(id))}</p>`).join('')||'<p>暂未关联目标，可稍后整理。</p>'}</div>${item.source_url?`<a class="g-text-link" href="${e(item.source_url)}" target="_blank" rel="noopener noreferrer">打开原始来源 ↗</a>`:''}${item.origin_answer_id?`<p><button class="g-text-link" data-g="answer-detail" data-id="${e(item.origin_answer_id)}">查看原回答 ↗</button></p>`:''}${item.origin_knowledge_id?`<p><a href="#/knowledge/${encodeURIComponent(item.origin_knowledge_id)}" data-g="close">查看原认知及用途限制 ↗</a></p>`:''}<div class="g-heading-actions g-space"><button class="g-primary" data-g="edit-fragment" data-id="${e(key)}">编辑 / 整理</button><button data-g="create" data-id="${e(key)}">用于内容创作 ↗</button></div><p class="g-note g-space">未认可的收藏不会自动成为你的观点。</p></article>`);
    document.getElementById('modal').dataset.gFragment=key;
  }
  async function answerDetail(key){
    const item=await api('/growth/answers/'+encodeURIComponent(key)),snapshot=item.question_snapshot||{};
    growthModal('原回答',`<article class="g-detail"><span class="g-note">${e(date(item.answered_at||item.updated_at))} · ${e(snapshot.pillar||'')}</span><h2>${e(snapshot.text||item.question_id)}</h2><pre>${e(item.text||'')}</pre>${(item.probes||[]).map(p=>`<p class="g-note">追问：${e(p.question)}<br>回答：${e(p.answer)}</p>`).join('')}<p class="g-note g-space">出处：问问自己 · ${e(item.id)}${item.fragment_id?' · 已投影为碎片':''}</p><div class="g-heading-actions g-space"><button class="g-primary" data-g="close">关闭</button>${item.fragment_id?`<button data-g="fragment" data-id="${e(item.fragment_id)}">查看碎片</button>`:''}</div></article>`);
  }
  async function manuscriptView(key){
    const item=await api('/growth/fragments/'+encodeURIComponent(key));
    if(!item.transcript_available)throw Error('原稿尚未生成，或本地文件不存在，请查看转写状态。');
    growthModal('本地文字稿',`<article class="g-detail"><span class="g-eyebrow">${icon('check')}原稿已保存在本机 · ${item.transcript_text.length} 字</span><h2>${e(item.title)}</h2><div class="g-heading-actions"><a class="button-link" href="${e(media('/api/growth/fragments/'+key+'/transcript?format=txt'))}">下载 TXT</a><a class="button-link" href="${e(media('/api/growth/fragments/'+key+'/transcript?format=md'))}">下载 Markdown</a></div><details class="g-local-proof"><summary>查看本地保存位置与来源</summary><code>${e(item.transcript_local_path)}</code><p class="g-note">原稿来源：${e(item.transcript_source_url)}</p></details><pre>${e(item.transcript_text)}</pre></article>`);
  }
  async function deleteFragmentForm(key){
    const item=await api('/growth/fragments/'+encodeURIComponent(key));const busy=['queued','running'].includes(item.transcription?.status);
    growthModal('删除碎片',`<form data-g-form="delete-fragment" data-id="${e(key)}"><input name="revision" type="hidden" value="${item.revision}"><h3>${e(item.title)}</h3><p class="g-note">将移除这条碎片及它生成的本地文字稿，相关目标的积累数量会随之更新。删除前会保留本机备份；已关联的旧认知和创作内容保留。</p>${busy?'<p class="g-form-status">正在转写，请等任务结束后再删除。</p>':''}<label class="check"><input name="confirm" type="checkbox" required>确认删除这条碎片及生成的文字稿</label><button type="submit" class="g-danger" ${busy?'disabled':''}>删除这条碎片</button><p class="g-form-status" role="status"></p></form>`);
  }

  async function importForm(preselect='') {
    const old=await api('/knowledge');
    growthModal('把旧认知关联到成长目标',`<form data-g-form="import"><p class="g-note">保留旧分类、原文与认同状态；相同认知重复关联不会产生第二条碎片。导入时间沿用原记录日期。</p><label class="field"><span>选择一条已有认知</span><input id="g-old-search" type="search" placeholder="搜索标题"><select name="knowledge_id" size="6" required id="g-old-list">${old.items.map(x=>`<option value="${e(x.id)}" ${x.id===preselect?'selected':''}>${e(x.category)} · ${e(x.title)}</option>`).join('')}</select></label><span class="g-field-title">关联目标</span>${tags()}<button class="g-primary" type="submit" ${old.items.length?'':'disabled'}>关联这条认知</button><p class="g-form-status" role="status"></p></form>`);
    document.getElementById('g-old-search').addEventListener('input',ev=>{for(const option of document.getElementById('g-old-list').options)option.hidden=!option.textContent.includes(ev.target.value)});
  }
  function stopRecording() {
    clearTimeout(recordingTimer);
    if(recorder?.state==='recording') recorder.stop();
    stream?.getTracks().forEach(t=>t.stop());stream=null;
  }
  async function record(button) {
    if(recorder?.state==='recording'){stopRecording();return;}
    if(!window.isSecureContext||!navigator.mediaDevices?.getUserMedia||!window.MediaRecorder)throw Error('当前浏览器无法录音。请在电脑本机打开，或使用 HTTPS；也可以用系统键盘语音输入。');
    const generation=captureGeneration;
    stream=await navigator.mediaDevices.getUserMedia({audio:true});
    const localStream=stream;
    if(generation!==captureGeneration){localStream.getTracks().forEach(t=>t.stop());return;}
    const parts=[];recorder=new MediaRecorder(localStream);const localRecorder=recorder;
    recorder.addEventListener('dataavailable',ev=>{if(ev.data.size)parts.push(ev.data)});
    recorder.addEventListener('stop',()=>{localStream.getTracks().forEach(t=>t.stop());if(generation!==captureGeneration)return;audioBlob=new Blob(parts,{type:localRecorder.mimeType});const status=document.getElementById('g-voice-status');if(status)status.textContent='录音已留在当前窗口，点“转成文字”后再保存碎片。';const b=document.querySelector('[data-g="transcribe"]');if(b)b.disabled=false;button.textContent='重新录音';});
    recorder.start();button.textContent='结束录音';document.getElementById('g-voice-status').textContent='正在录音，最长 90 秒；关闭弹窗会结束录音。';recordingTimer=setTimeout(stopRecording,90000);
  }
  async function transcribe() {
    if(!audioBlob)throw Error('请先录一段语音。');
    if(audioBlob.size>8*1024*1024)throw Error('录音超过 8MB，请缩短后重录。');
    const generation=captureGeneration;
    const audio=await new Promise((resolve,reject)=>{const r=new FileReader();r.onload=()=>resolve(r.result.split(',')[1]);r.onerror=reject;r.readAsDataURL(audioBlob)});
    const status=document.getElementById('g-voice-status');status.textContent='正在本机转写，首次需要加载模型，请保留这个窗口…';
    const result=await api('/growth/voice','POST',{audio,mime:audioBlob.type});
    const field=document.getElementById('g-fragment-text');if(!field||generation!==captureGeneration){toast('先前录音的转写已保存在本机；没有写入新打开的碎片。');return;}
    field.value+=(field.value?'\n\n':'')+result.text;voiceId=result.voice_id;status.textContent='转写文字已填入，请核对后保存。';audioBlob=null;
  }
  async function download(reviewOnly=false) {
    const params=new URLSearchParams(query);
    if(reviewOnly){for(const key of ['goal','q','inbox','archived','day','type'])params.delete(key);params.set('month',data.month);}
    params.delete('page');params.delete('view');
    growthModal('导出成长积累',`<p class="g-note">包含所选目标的全部后代目标，跨标签内容只导出一次。${reviewOnly?'范围为 '+data.month+' 全月。':'按当前筛选范围导出。'}</p><div class="g-heading-actions"><a class="button-link" href="${e(media('/api/growth/export?'+params+'&format=md'))}">下载 Markdown</a><a class="button-link" href="${e(media('/api/growth/export?'+params+'&format=txt'))}">下载 TXT 文本</a></div>`);
  }

  function applyVisualStyles(scope=document){
    const valid=color=>/^#[0-9a-f]{6}$/i.test(color)||color==='var(--gold)';
    for(const el of scope.querySelectorAll('[data-pillar-color]'))if(valid(el.dataset.pillarColor))el.style.setProperty('--pillar',el.dataset.pillarColor);
    for(const el of scope.querySelectorAll('[data-depth]'))el.style.setProperty('--depth',String(Math.max(0,Math.min(3,Number(el.dataset.depth)||0))));
    for(const el of scope.querySelectorAll('[data-goal-color]'))if(valid(el.dataset.goalColor))el.style[el.tagName==='I'?'backgroundColor':'color']=el.dataset.goalColor;
    for(const el of scope.querySelectorAll('[data-bar-width]'))el.style.width=Math.max(0,Math.min(100,Number(el.dataset.barWidth)||0))+'%';
  }
  const snapshotForm = form => form ? JSON.stringify([...new FormData(form)]) : '';
  function updateSelected(picker){
    if(!picker)return;
    const chosen=[...picker.querySelectorAll('[name=goal_ids]:checked')];
    picker.querySelector('.g-selected-tags').innerHTML=chosen.length?chosen.map(input=>`<button type="button" data-g="remove-tag" data-id="${e(input.value)}" aria-label="取消关联${e(data.goals.find(g=>g.id===input.value)?.title)}">${e(data.goals.find(g=>g.id===input.value)?.title)} ${icon('close')}</button>`).join(''):'<span>尚未选择目标 · 可先保存到待整理</span>';
  }
  function growthModal(title,html){
    modalGeneration++;modal(title,html);const dialog=document.getElementById('modal');dialog.dataset.growth='true';delete dialog.dataset.gFragment;
    applyVisualStyles(dialog);updateSelected(dialog.querySelector('.g-picker'));modalBaseline=snapshotForm(dialog.querySelector('form[data-g-form]'));
  }
  function modalDirty(){const d=document.getElementById('modal');return d.open&&d.dataset.growth==='true'&&modalBaseline&&modalBaseline!==snapshotForm(d.querySelector('form[data-g-form]'));}
  function reviewDirty(){const form=document.querySelector('form[data-g-form=review]');return form&&reviewBaseline!==snapshotForm(form);}
  function dirty(){return !!(modalDirty()||reviewDirty());}
  // 2026-11-18 t143：原生 confirm → 复用 confirmBar 页内二次确认（放弃修改仍要确认）。
  async function permitLeave(){
    if(!dirty())return true;
    const dialog=document.getElementById('modal');
    const host=(dialog&&dialog.open?document.getElementById('modal-body'):null)||document.getElementById('main');
    return await confirmBar(host,'还有未保存的内容。确定离开并放弃这些修改吗？',{okText:'离开',cancelText:'留下',danger:true});
  }
  function goalDetail(key){
    const g=data.goals.find(x=>x.id===key);if(!g)return;
    const axis = g.level==='root' ? `<p class="g-axis-line">${g.thesis?('命题：'+e(g.thesis)):'还没有命题——命题是可选方向，不影响目标成立。'}</p>${g.milestone?`<p class="g-axis-line">主业目标：${e(g.milestone)}</p>`:''}` : '';
    growthModal('目标与积累',`<div class="g-goal-detail"><span class="g-detail-icon" data-goal-color="${e(pillarFor(g)?.color||'var(--gold)')}">${icon(pillarIcon(g))}</span><p class="g-note">${e(tagPath(g.id))}</p><h2>${e(g.title)}</h2><p class="g-detail-copy">${e(g.description||'还没有补充目标说明。')}</p>${axis}<a class="g-detail-total" href="${e(url({view:'library',goal:g.id,month:'',q:'',type:'',day:'',inbox:'',page:''}))}" data-g="close"><strong>${goalCount(g.id)}</strong> 条积累（含小目标） ${icon('arrow')}</a><div class="g-heading-actions g-space"><button class="g-primary" data-g="goal" data-id="${e(g.id)}">${g.level==='root'&&!g.thesis?'补命题 / 编辑目标':'编辑目标'}</button>${!data.archived_goal_ids.includes(g.id)?`<button data-g="new-goal" data-parent="${e(g.id)}">${icon('plus')}添加${g.level==='root'?'支柱':'小目标'}</button>`:''}</div></div>`);
  }
  document.addEventListener('submit',async ev=>{
    const form=ev.target;if(!form.dataset.gForm)return;
    ev.preventDefault();ev.stopImmediatePropagation();
    const values=Object.fromEntries(new FormData(form)),type=form.dataset.gForm;
    if(type==='filter'){location.hash=url({goal:values.goal,month:values.month,type:values.type,q:values.q,inbox:values.inbox?'1':'',archived:values.archived?'1':'',day:'',page:''});return;}
    const button=form.querySelector('[type=submit]'),message=form.querySelector('.g-form-status');
    if(button?.disabled||pending)return;
    if(recorder?.state==='recording'){if(message)message.textContent='请先结束录音并转成文字，再保存。';return;}
    pending=true;if(button)button.disabled=true;
    try{
      if(type==='setup')await api('/growth/setup','POST',{title:values.title,thesis:values.thesis||'',milestone:values.milestone||'',track:values.track||'',thesis_source:(axisGuide&&axisGuide.suggest&&axisGuide.suggest.inputs&&axisGuide.suggest.inputs.knowledge_ids)||[]});
      if(type==='goal')await api('/growth/goals'+(form.dataset.id?'/'+form.dataset.id:''),form.dataset.id?'PATCH':'POST',{...values,revision:+values.revision,archived:!!values.archived});
      if(type==='fragment'){
        const editing=!!form.dataset.id,wantsTranscript=!!values.source_url&&!!values.transcribe_video;
        const saved=await api('/growth/fragments'+(editing?'/'+form.dataset.id:''),editing?'PATCH':'POST',{...values,transcribe:!editing&&wantsTranscript,revision:+values.revision,goal_ids:new FormData(form).getAll('goal_ids'),endorsement:values.accepted?'accepted':'reference_only',archived:!!values.archived,voice_id:voiceId,type:voiceId||form.dataset.originalType==='voice'?'voice':values.source_url?'link':'text'});
        form.dataset.id=saved.id;form.elements.revision.value=saved.revision;modalBaseline=snapshotForm(form);
        if(editing&&wantsTranscript)await api('/growth/fragments/'+saved.id+'/transcribe','POST',{revision:saved.revision});
      }
      if(type==='delete-fragment'){
        await api('/growth/fragments/'+form.dataset.id+'/delete','POST',{revision:+values.revision,confirm:!!values.confirm});
        modalBaseline='';document.getElementById('modal').close();toast('已删除，并保留本机备份');await route();return;
      }
      if(type==='import'){const r=await api('/growth/from-knowledge','POST',{knowledge_id:values.knowledge_id,goal_ids:new FormData(form).getAll('goal_ids')});if(r.duplicate){if(message)message.textContent='已经关联过这条认知，请在原碎片中调整目标。';return;}}
      if(type==='talent'){
        const fd=new FormData(form);
        await api('/growth/talent/assess','POST',{via:fd.getAll('via'),ikigai:{love:values.love||'',good:values.good||'',needs:values.needs||'',paid:values.paid||''},best:[values.best0||'',values.best1||'',values.best2||''].filter(Boolean)});
        toast('天赋画像已生成，已存进本机资产');
        await route();return;
      }
      if(type==='review'){const r=await api('/growth/reviews','POST',{...values,revision:+values.revision});form.elements.revision.value=r.revision;reviewBaseline=snapshotForm(form);if(message)message.textContent='复盘已保存。';document.getElementById('g-review-save-state').textContent='已保存';toast('复盘已保存');return;}
      modalBaseline='';document.getElementById('modal').close();toast(type==='setup'?'成长系统已建立':'已保存');await route();
    }catch(err){if(message)message.textContent=err.message;else toast(err.message)}finally{pending=false;if(button)button.disabled=false;}
  },true);
  document.addEventListener('input',ev=>{
    const input=ev.target;
    if(input.id==='g-goal-search')paintTree();
    if(input.matches('[data-g-form=fragment] [name=source_url]'))updateVideoEntry(input.closest('form'));
    if(input.matches('[data-g-tag-search]')){
      const picker=input.closest('.g-picker'),needle=input.value.trim().toLowerCase();
      for(const group of picker.querySelectorAll('.g-tag-group')){
        if(needle&&group.dataset.previousOpen===undefined)group.dataset.previousOpen=String(group.open);
        let found=false;for(const label of group.querySelectorAll('.g-tag')){label.hidden=!!needle&&!label.dataset.path.includes(needle);if(!label.hidden)found=true;}
        group.hidden=!found;
        if(needle&&found)group.open=true;else if(!needle&&group.dataset.previousOpen!==undefined){group.open=group.dataset.previousOpen==='true';delete group.dataset.previousOpen;}
      }
    }
    if(input.closest('[data-g-form=review]'))document.getElementById('g-review-save-state').textContent=reviewDirty()?'有未保存的修改':'已保存';
  });
  document.addEventListener('change',ev=>{if(ev.target.matches('[name=transcribe_video]'))updateVideoEntry(ev.target.closest('form'));if(ev.target.matches('.g-picker [name=goal_ids]'))updateSelected(ev.target.closest('.g-picker'));if(ev.target.matches('[name=thesis_pick]')){const form=ev.target.closest('form'),area=form&&form.querySelector('[name=thesis]');if(area)area.value=ev.target.dataset.thesis||'';}});
  document.addEventListener('click',async ev=>{
    const button=ev.target.closest('[data-g]');if(!button||button.disabled)return;
    const cmd=button.dataset.g;
    if(cmd==='close'){document.getElementById('modal').close();return;}
    if(cmd==='morning-seen'){
      button.disabled=true;
      try{await api('/growth/morning/seen','POST',{});button.textContent='已读';}
      catch(err){button.disabled=false;toast('没记住已读：'+err.message);}
      return;
    }
    if(cmd==='brain-consolidate'){return openBrainMaintenance();}
    if(cmd==='brain-merge'){
      const ids=String(button.dataset.ids||'').split(',').filter(Boolean);
      if(ids.length<2){toast('至少要两条才能合并');return;}
      button.disabled=true;
      try{const r=await api('/growth/consolidate/merge','POST',{ids});toast('已合并 '+(r.merged_ids||[]).length+' 条，原文都保留');await openBrainMaintenance();}
      catch(err){button.disabled=false;toast('没合并成：'+err.message);}
      return;
    }
    if(cmd==='brain-principle'){
      const themes=window.__brainThemeData||[];
      const idx=Number(button.dataset.idx||0);
      const area=document.querySelector('[data-theme-input="'+idx+'"]');
      const text=area?String(area.value||'').trim():'';
      if(!text){toast('先写一句原则');return;}
      button.disabled=true;
      try{await api('/growth/consolidate/principle','POST',{text,source_ids:String(button.dataset.sources||'').split(',').filter(Boolean)});toast('已入库：这句原则归你');document.getElementById('modal').close();await route();}
      catch(err){button.disabled=false;toast('没入库：'+err.message);}
      return;
    }
    if(cmd==='brain-restore'){
      button.disabled=true;
      try{await api('/growth/consolidate/restore','POST',{ids:[button.dataset.id]});toast('已恢复');await openBrainMaintenance();}
      catch(err){button.disabled=false;toast('没恢复成：'+err.message);}
      return;
    }
    if(cmd==='new-fragment'){fragmentForm({},button.dataset.kind||'text');return;}
    if(cmd==='thesis-accept-all'){
      const ids=(thesisState.proposals||[]).filter(p=>p&&p.status==='pending').map(p=>p.id);
      if(!ids.length){toast('没有待审的命题候选');return;}
      button.disabled=true;
      let done=0;
      try{
        for(const id of ids){
          const card=document.querySelector('[data-proposal="'+id+'"]');
          const area=card&&card.querySelector('[data-thesis-edit]');
          const thesis=area?String(area.value||'').trim():undefined;
          await api('/growth/thesis/proposals/'+encodeURIComponent(id)+'/decide','POST',{accept:true,thesis});
          done+=1;
        }
        toast('已采纳 '+done+' 条命题候选（版本史可回看/回滚）');
        await route();
      }catch(err){toast('第 '+(done+1)+' 条没采纳成功：'+err.message);button.disabled=false;}
      return;
    }
    if(cmd==='thesis-undo'){
      const id=button.dataset.id;
      button.disabled=true;
      try{
        await api('/growth/thesis/proposals/'+encodeURIComponent(id)+'/undo','POST',{});
        toast('已回滚到上一版命题，版本史已留痕');
        await route();
      }catch(err){toast(err.message);button.disabled=false;}
      return;
    }
    if(cmd==='thesis-accept'||cmd==='thesis-reject'){
      const id=button.dataset.id,accept=cmd==='thesis-accept';
      const card=button.closest('[data-proposal]');
      const area=card&&card.querySelector('[data-thesis-edit]');
      const thesis=accept&&area?String(area.value||'').trim():undefined;
      if(accept&&!thesis){toast('候选命题不能为空');return;}
      button.disabled=true;
      try{
        await api('/growth/thesis/proposals/'+encodeURIComponent(id)+'/decide','POST',accept?{accept:true,thesis}:{accept:false});
        toast(accept?'已采纳，命题已更新并写入版本史':'已拒绝这条候选，命题未改');
        await route();
      }catch(err){toast(err.message);button.disabled=false;}
      return;
    }
    if(cmd==='talent-topic'){
      const idx=Number(button.dataset.idx)||0;
      const t=talentData&&talentData.profile&&talentData.profile.portrait&&talentData.profile.portrait.talents[idx];
      if(!t){toast('天赋画像已更新，请刷新再试。');return;}
      button.disabled=true;
      try{const r=await api('/growth/talent/topic','POST',{title:t.direction,talent:t.label});toast('已写进选题池：'+(r.title||t.label));await route();}
      catch(err){toast(err.message);button.disabled=false;}
      return;
    }
    if(cmd==='goal-detail'){goalDetail(button.dataset.id);return;}
    if(cmd==='goal'){goalForm(button.dataset.id);return;}
    if(cmd==='new-goal'){goalForm('',button.dataset.parent);return;}
    if(cmd==='review-month'){location.hash=url({view:'review',month:document.getElementById('g-review-month').value,page:'',day:''});return;}
    if(cmd==='tree-toggle'){closed.has(button.dataset.id)?closed.delete(button.dataset.id):closed.add(button.dataset.id);remember('closed',[...closed]);paintTree();return;}
    if(cmd==='tree-expand'||cmd==='tree-collapse'){closed=cmd==='tree-expand'?new Set():new Set(data.goals.filter(g=>g.level!=='root').map(g=>g.id));remember('closed',[...closed]);paintTree();return;}
    if(cmd==='clear-tags'||cmd==='remove-tag'){
      const picker=button.closest('.g-picker');for(const input of picker.querySelectorAll('[name=goal_ids]'))if(cmd==='clear-tags'||input.value===button.dataset.id)input.checked=false;updateSelected(picker);return;
    }
    if(['zoom-in','zoom-out','reset-graph','graph-pause','graph-overview','graph-all','graph-close','graph-expand'].includes(cmd)){graphControl?.(cmd,button.dataset.id);return;}
    button.disabled=true;
    try{
      if(cmd==='fragment')await fragmentDetail(button.dataset.id);
      if(cmd==='answer-detail')await answerDetail(button.dataset.id);
      if(cmd==='manuscript')await manuscriptView(button.dataset.id);
      if(cmd==='delete-fragment')await deleteFragmentForm(button.dataset.id);
      if(cmd==='video-transcribe'){
        const fresh=await api('/growth/fragments/'+button.dataset.id);
        const updated=await api('/growth/fragments/'+button.dataset.id+'/transcribe','POST',{revision:fresh.revision});rememberTranscription(updated);
        if(document.getElementById('modal').open)await fragmentDetail(button.dataset.id);else await route();
      }
      if(cmd==='edit-fragment'){const item=await api('/growth/fragments/'+button.dataset.id);fragmentForm(item);}
      if(cmd==='import')await importForm();
      if(cmd==='import-one')await importForm(button.dataset.id);
      if(cmd==='record')await record(button);
      if(cmd==='transcribe')await transcribe();
      if(cmd==='export'||cmd==='export-review')await download(cmd==='export-review');
      if(cmd==='create'){const r=await api('/growth/fragments/'+button.dataset.id+'/knowledge','POST',{});modalBaseline='';document.getElementById('modal').close();createDialog(r.knowledge_id);}
    }catch(err){toast(err.message);const s=document.getElementById('g-voice-status');if((cmd==='record'||cmd==='transcribe')&&s)s.textContent=err.message;}finally{button.disabled=false;}
  });
  const dialog=document.getElementById('modal');
  // 2026-11-18 t143：关闭按钮/ESC 的原生 confirm → 页内确认条；确认后才真正关闭。
  document.getElementById('close-modal').addEventListener('click',ev=>{
    if(!modalDirty())return;
    ev.preventDefault();ev.stopImmediatePropagation();
    const host=document.getElementById('modal-body')||dialog;
    confirmBar(host,'碎片或目标尚未保存，确定放弃修改吗？',{okText:'放弃修改',cancelText:'继续编辑',danger:true}).then(ok=>{
      if(ok){modalBaseline='';dialog.close();}
    });
  },true);
  dialog.addEventListener('cancel',ev=>{
    if(!modalDirty())return;
    ev.preventDefault();
    const host=document.getElementById('modal-body')||dialog;
    confirmBar(host,'内容尚未保存，确定放弃修改吗？',{okText:'放弃修改',cancelText:'继续编辑',danger:true}).then(ok=>{
      if(ok){modalBaseline='';dialog.close();}
    });
  });
  dialog.addEventListener('close',()=>{captureGeneration++;modalGeneration++;stopRecording();audioBlob=null;modalBaseline='';delete dialog.dataset.growth;if(refreshAfterModal&&mountedHash){refreshAfterModal=false;route();}});
  let allowLeaveOnce=false;
  window.addEventListener('hashchange',ev=>{
    if(allowLeaveOnce){allowLeaveOnce=false;return;}
    if(mountedHash&&dirty()){
      ev.stopImmediatePropagation();
      const target=location.hash;
      history.replaceState(null,'',mountedHash);
      permitLeave().then(ok=>{
        if(!ok)return;
        allowLeaveOnce=true;
        if(dialog.open&&dialog.dataset.growth==='true'){modalBaseline='';dialog.close();}
        location.hash=target;
      });
      return;
    }
    if(dialog.open&&dialog.dataset.growth==='true'){modalBaseline='';dialog.close();}
  },true);
  window.addEventListener('beforeunload',ev=>{if(dirty()){ev.preventDefault();ev.returnValue='';}});
  function unmount(){
    hideGloss();
    if(mountedHash)scrollPositions.set(mountedHash,document.getElementById('main').scrollTop);
    window.ipGrowthAsk?.unmount();window.ipGrowthSearch?.unmount();
    pollGeneration++;clearInterval(transcriptTimer);transcriptTimer=null;activeTranscriptions.clear();
    themeObs?.disconnect();themeObs=null;
    cleanupGraph?.();cleanupUI?.();cleanupPillarBar?.();cleanupUI=null;cleanupGraph=null;cleanupPillarBar=null;graphControl=null;mountedHash='';reviewBaseline='';
  }
  function mount(){
    applyVisualStyles();
    const media=matchMedia('(max-width:760px)'),filters=document.querySelector('.g-filter-disclosure');
    const adaptFilters=()=>{if(filters)filters.open=!media.matches||['goal','q','type','month','day','inbox','archived'].some(key=>query.get(key));};
    adaptFilters();media.addEventListener('change',adaptFilters);cleanupUI=()=>media.removeEventListener('change',adaptFilters);
    mountedHash=location.hash;reviewBaseline=snapshotForm(document.querySelector('form[data-g-form=review]'));
    if(query.get('view')==='ask')window.ipGrowthAsk?.mount();
    if(query.get('view')==='search')window.ipGrowthSearch?.mount();
    if(query.get('view')==='goals'){
      if(pref('closed',null)===null)closed=new Set(data.goals.filter(g=>g.level==='goal').map(g=>g.id));
      const focus=query.get('branch');if(focus){let g=data.goals.find(n=>n.id===focus);const seen=new Set();while(g&&!seen.has(g.id)){seen.add(g.id);closed.delete(g.id);g=data.goals.find(n=>n.id===g.parent_id);}}
      paintTree();
    }
    if(document.getElementById('growth-graph')){
      cleanupPillarBar?.();cleanupPillarBar=null;
      cleanupPillarBar=paintPillarBar(graphCards);
      const mountViewer=()=>{
        cleanupGraph?.();cleanupGraph=null;graphControl=null;
        const isLightNow=document.documentElement.classList.contains('light');
        const viewer=(!isLightNow&&window.ipGrowthNeural)?window.ipGrowthNeural:(window.ipGrowthCards||window.ipGrowthGraph);
        if(!viewer)return;
        const result=viewer.mount({graphData,data,query,url,e,icon,tagPath,pillarIcon,goalCount,api,fragmentDetail,goalDetail,cards:graphCards,cardsError:graphCardsError,glossary:{pillarHTML:pillarGlossHTML,show:showGloss,hide:hideGloss}});
        graphControl=result.control;cleanupGraph=result.cleanup;
      };
      mountViewer();
      themeObs?.disconnect();
      themeObs=new MutationObserver(()=>{if(document.getElementById('growth-graph'))mountViewer();});
      themeObs.observe(document.documentElement,{attributes:true,attributeFilter:['class']});
    }
    data?.items?.forEach(rememberTranscription);
    const saved=scrollPositions.get(mountedHash);if(saved!==undefined)document.getElementById('main').scrollTop=saved;
    diagMount();
  }
  return {render,mount,unmount};
})();
