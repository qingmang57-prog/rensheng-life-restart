'use strict';
/* 2026-11-14 分类合并：11 类 -> 5 类；2026-11-16 从认知底座拆出「人性关系」，共 6 类。
   旧词（三观、驾驭金钱等）仍由后端别名命中；历史数据万一还是旧分类，用 aliasCategory 归到新类显示。 */
const knowledgeCategories={
  '认知底座':{className:'cat-human',note:'看世界、看人、看关系的底层判断',shape:'<circle cx="12" cy="12" r="8"/><path d="m15 9-2 4-4 2 2-4Z"/>'},
  '人性关系':{className:'cat-human',note:'和谁在一起、关系怎么处',shape:'<circle cx="9" cy="9" r="3"/><circle cx="16.5" cy="10" r="2.5"/><path d="M3.5 19c0-3 2.4-5 5.5-5s5.5 2 5.5 5M14 19c.2-2.2 1.7-3.7 3.7-3.7 2 0 3.5 1.5 3.8 3.7"/>'},
  '赚钱逻辑':{className:'cat-wealth',note:'钱怎么来、怎么留，账号怎么定位',shape:'<circle cx="12" cy="12" r="8.5"/><path d="M12 7v10M9.4 9.4h4a1.9 1.9 0 0 1 0 3.8h-2.8a1.9 1.9 0 0 0 0 3.8h4.2"/>'},
  '操盘手法':{className:'cat-ops',note:'生意怎么跑起来',shape:'<rect x="4" y="4" width="16" height="16" rx="2"/><path d="M9.3 4v16M14.7 4v16M4 9.3h16M4 14.7h16"/>'},
  '成功心法':{className:'cat-success',note:'习惯、判断、走多远',shape:'<path d="M5 19V9m7 10V4m7 15v-6"/><path d="M3 21h18"/>'},
  '随手记':{className:'cat-other',note:'随手记下的一句话、链接与灵感',shape:'<path d="M5 4h11l3 3v13H5Z"/><path d="M8 9h8M8 13h8M8 17h5"/>'}
};
const knowledgeAliasToCategory={'三观':'认知底座','人性关系':'人性关系','驾驭金钱':'赚钱逻辑','账号观':'赚钱逻辑','事业':'操盘手法',
  '视频积累':'随手记','链接积累':'随手记','成长积累':'随手记','第二大脑':'随手记','个人积累':'随手记'};
function aliasCategory(category){return knowledgeCategories[category]?category:(knowledgeAliasToCategory[category]||category);}
function categoryVisual(category){const x=knowledgeCategories[aliasCategory(category)]||{className:'cat-other',note:'持续积累的个人理解',shape:'<path d="M12 3v18M3 12h18"/><circle cx="12" cy="12" r="7"/>'};return x;}
function categorySymbol(category){const x=categoryVisual(category);return `<span class="category-symbol ${x.className}" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">${x.shape}</svg></span>`;}
function plainClaim(text){return String(text||'').replace(/\*\*/g,'').replace(/^[-#>]+\s*/gm,'').trim();}
/* 2026-11-16（性能 E-2 方案1）：认知卡分批渲染。
   227 张卡一次拼 HTML + 首次布局约 250ms；改为首屏先出前 24 张，其余每帧追加 50 张。
   只在网格末尾 append，不插占位、不替换已有节点，所以滚动位置不动，
   卡片从第一帧起就是完整可点的 <a>，不存在「先占位后填充」。 */
const BRAIN_FIRST=24,BRAIN_CHUNK=50;
let brainGridSeq=0;
/* 2026-11-16（CSP）：这些块原来用内联 style，被 style-src 'self' 拦掉（#brain-chat-log 实测变成 block/gap normal）。
   改为 class + 同源样式表 CSSOM 插入规则，不新增 CSS 文件、不碰 styles.css。 */
function ensureBrainCss(){
  if(ensureBrainCss.done)return;ensureBrainCss.done=true;
  try{
    const sheet=document.styleSheets[0];if(!sheet)return;
    const rules=[
      /* 2026-11-25：对话记录（.brain-chat-log / .brain-chat-turn / .brain-refs*）与「搜认知/问知识库」
         切换（.brain-mode-switch）已随问答页取消，规则一并摘除；.brain-mode-hint 搜索页仍在用，保留。 */
      '.brain-mode-hint{margin:0}',
      /* 2026-11-17（t19 行业热点）：摘要内显＋来源/原文链接。选择器加 .brain-news-list 前缀，
         保证压过 styles.css 的 .small/p 与 readability.css 的 .brain-news-list a（不新增 CSS 文件）。 */
      '.brain-news-item{min-width:0}',
      '.brain-news-list .brain-news-summary{margin:2px 0 0;font-size:11px;line-height:1.5;color:var(--muted);word-break:break-word}',
      '.brain-news-list .brain-news-meta{margin:2px 0 0;font-size:10px;color:var(--muted)}',
      '.brain-news-list a.brain-news-src{display:inline;font-size:10px;color:var(--gold)}'
    ,
    /* 2026-11-17（t64）：删除动作的危险色与回收站卡片弱化，走同源 CSSOM，不用内联 style。 */
    '.danger{border-color:var(--error);color:var(--error)}',
    '.danger:hover:not(:disabled){border-color:var(--error);background-image:linear-gradient(rgba(178,74,60,.16),rgba(178,74,60,.16))}',
    '.deleted-card{opacity:.94}',
    '.deleted-card .knowledge-card-foot{flex-wrap:wrap;gap:8px}'];
    rules.forEach(r=>{try{sheet.insertRule(r,sheet.cssRules.length);}catch(e){}});
  }catch(e){}
}
/* 2026-11-17（t64）：回收站卡片——已删除的认知可恢复，或彻底删除（二次确认在 action 里）。 */
function deletedCardHTML(x){return `<div class="knowledge-card deleted-card"><div class="knowledge-card-top">${categorySymbol(x.category)}<span class="knowledge-card-id">${esc(x.legacy_id||x.category)}</span></div><h3>${esc(x.title)}</h3><p class="claim">${esc(plainClaim(x.claim||x.body))}</p><div class="knowledge-card-foot"><span class="usage-warning">已删除</span><span class="density-note">可恢复；彻底删除需二次确认</span></div><div class="row space"><button class="small-button" type="button" data-action="knowledge-restore" data-id="${esc(x.id)}">恢复</button><button class="small-button danger" type="button" data-action="knowledge-purge" data-id="${esc(x.id)}">彻底删除</button><a class="link small" href="#/knowledge/${encodeURIComponent(x.id)}">查看</a></div></div>`;}
function deletedGridHTML(items){return `<div class="knowledge-grid">${items.map(deletedCardHTML).join('')}</div>`;}
function brainCardHTML(x,expanded){return `<a class="knowledge-card ${expanded?'expanded':''}" href="#/knowledge/${encodeURIComponent(x.id)}"><div class="knowledge-card-top">${categorySymbol(x.category)}<span class="knowledge-card-id">${esc(x.legacy_id||x.category)}</span></div><h3>${esc(x.title)}</h3><p class="claim">${esc(plainClaim(x.claim||x.body))}</p><div class="knowledge-card-foot"><span class="state-indicator ${x.endorsement==='accepted'?'accepted':x.markers?.length?'pending':''}" aria-hidden="true"></span><span>${esc(endorsements[x.endorsement]||'待核对')}</span><span>·</span><span>${esc(validations[x.validation]||'验证待补')}</span>${x.content_use==='review'?'<span class="usage-warning">用途需复核</span>':''}${x.markers?.length?'<span class="usage-warning">待核对</span>':''}${x.lifecycle==='archived'?'<span class="usage-warning">已归档</span>':x.lifecycle==='superseded'?'<span class="usage-warning">已被替代</span>':''}<span class="card-arrow" aria-hidden="true">↗</span></div></a>`;}
function exampleCardHTML(x,expanded){return `<a class="knowledge-card ${expanded?'expanded':''}" href="#/knowledge/${encodeURIComponent(x.id)}"><div class="knowledge-card-top">${categorySymbol(x.category)}<span class="knowledge-card-id">${esc(x.legacy_id||x.category)}</span></div><h3>${esc(x.title)}</h3><p class="claim">${esc(plainClaim(x.claim||x.body))}</p><div class="knowledge-card-foot"><span class="state-indicator" aria-hidden="true"></span><span>示例 · 仅供参考</span><span class="card-arrow" aria-hidden="true">↗</span></div></a>`;}
function brainGridHTML(items,expanded,template){
  const token='bc'+(++brainGridSeq);
  const rest=items.slice(BRAIN_FIRST);
  if(rest.length){
    let i=0;
    const step=()=>{
      // 用 token 找当时那个网格：路由已切换或重渲染时直接停，不往旧节点追加。
      const grid=document.querySelector('.knowledge-grid[data-chunk-token="'+token+'"]');
      if(!grid||!grid.isConnected)return;
      const part=rest.slice(i,i+BRAIN_CHUNK);i+=part.length;
      if(part.length)grid.insertAdjacentHTML('beforeend',part.map(x=>template(x,expanded)).join(''));
      if(i<rest.length)requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  }
  return `<div class="knowledge-grid ${expanded?'expanded':''}" data-chunk-token="${token}">${items.slice(0,BRAIN_FIRST).map(x=>template(x,expanded)).join('')}</div>`;
}
function renderBrain(data,query){
  ensureBrainCss();
  const counts=data.category_counts||{},categories=Array.from(new Set([...Object.keys(knowledgeCategories),...Object.keys(counts)]));
  const total=Object.values(counts).reduce((a,b)=>a+b,0)||data.total;
  const expanded=localStorage.getItem('ip-brain-density')==='expanded';
  const current=query.get('category')||'';
  /* 2026-11-25：「问知识库」对话页已取消（用户决定：省对话时间，数据层不动）。
     旧书签若带 mode=ask，其中的 q 是当年的一句提问而不是搜索词，一并忽略，
     避免落进「暂未找到匹配内容」。ask 固定为 false，下方残留的 ask 分支不再出现。 */
  const ask=false;
  const staleAsk=query.get('mode')==='ask';
  const q=staleAsk?'':(query.get('q')||'');
  const showInactive=query.get('include_archived')==='1';
  const showDeleted=query.get('include_deleted')==='1';
  const archivedCount=Number(data.archived_count||0);
  const deletedCount=Number(data.deleted_count||0);
  const filtered=Boolean(current||q);
  /* 归档口径：默认只显示 active，与认知图谱一致；切换时保留当前搜索/分类/模式。 */
  const toggleParams=new URLSearchParams();
  if(q)toggleParams.set('q',q);
  if(current)toggleParams.set('category',current);
  if(!showInactive)toggleParams.set('include_archived','1');
  const archiveToggleURL='#/brain'+(toggleParams.toString()?'?'+toggleParams:'');
  if(query.get('scope')==='example'){
    // 示例库：某类账号的做法，仅供参考。不进起题、写稿、检索的默认路径。
    return `<section class="brain-heading"><div><div class="eyebrow">EXAMPLE LIBRARY</div><h1>示例库</h1><p class="lead example-lead">这是某类账号的做法，不一定适合你。</p></div><div class="actions"><a class="button-link" href="#/brain">← 回我的认知</a></div></section>
    ${notice('示例库里的内容不会进入你的起题和写稿；只是给你看看别人怎么讲。觉得哪条有用，可以参考它的讲法，别照抄结论。')}
    <div class="brain-tools"><p class="small muted">示例 · ${data.total} 条<span class="density-note">　点击卡片查看原文</span></p></div>
    ${data.items.length?brainGridHTML(data.items,expanded,exampleCardHTML):empty('示例库是空的','当前没有标成示例的内容。')}`;
  }
  const categoryURL=c=>'#/brain?'+new URLSearchParams({...(q?{q}:{}),...(showInactive?{include_archived:'1'}:{}),category:c});
  return `<section class="brain-heading"><div><div class="eyebrow">KNOWLEDGE</div><h1>知识库</h1><p class="lead">搜你的认知卡。</p></div><div class="actions"><button class="primary" data-action="new-knowledge">＋ 记一条认知</button><button data-action="import-brain"><span class="desktop-label">导入 Markdown</span><span class="mobile-label">导入</span></button></div></section>
  <div class="brain-search-hero">
    <form class="brain-chat-hero" id="brain-chat-form" data-mode="search">
      <span class="brain-chat-icon" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg></span>
    <input name="question" type="text" value="${esc(q)}" placeholder="输入关键词，搜标题、主张和原文…" autocomplete="off" aria-label="搜索认知卡">
      <button type="submit" class="primary brain-send-btn"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg><span>搜索</span></button>
    </form>
    <p class="small muted brain-mode-hint">搜认知：按标题、主张和原文关键词筛选，不调用 AI、不消耗额度。</p>
  </div>
  <section class="brain-overview" aria-label="认知库概览">
    <div class="brain-map"><div class="brain-orbit" aria-hidden="true"><svg viewBox="0 0 140 140" fill="none" stroke="currentColor"><circle cx="70" cy="70" r="48" opacity=".25"/><circle cx="70" cy="70" r="64" stroke-dasharray="2 8" opacity=".6"/><path d="M24 24 37 37m66 66 13 13M24 116l13-13m66-66 13-13" opacity=".5"/><circle cx="24" cy="24" r="4" fill="currentColor"/><circle cx="116" cy="24" r="4" fill="currentColor"/><circle cx="24" cy="116" r="4" fill="currentColor"/><circle cx="116" cy="116" r="4" fill="currentColor"/></svg><div class="brain-orbit-count"><strong>${total}</strong><span>条认知</span></div></div><div class="brain-map-copy"><h2>从积累，到自己的判断。</h2><p>收藏是起点。认同、验证和适用边界，留给你慢慢确认。</p><span class="small muted">用上方输入框按关键词筛卡片 ↑</span></div></div>
    <div class="brain-side">
      <div class="category-grid">${categories.filter(c=>counts[c]).map(c=>`<a class="category-tile ${current===c?'selected':''}" href="${esc(categoryURL(c))}" ${current===c?'aria-current="true"':''}>${categorySymbol(c)}<span class="category-label"><strong>${esc(c)}</strong><small>${esc(categoryVisual(c).note)}</small></span><span class="category-number">${counts[c]||0}</span></a>`).join('')}</div>
      ${(state.brainNews||[]).length ? `<div class="brain-news-card"><div class="brain-news-head"><span>行业热点</span><small>按你的赛道匹配</small></div><div class="brain-news-list">${state.brainNews.slice(0,4).map(n=>{const u=n.url||n.link||'#',ext=/^https?:\/\//i.test(u);return `<div class="brain-news-item"><a href="${esc(u)}" ${ext?'target="_blank" rel="noopener"':''} title="${esc(n.title||'')}">${esc(n.title||'')}</a>${n.summary?`<p class="small muted brain-news-summary">${esc(n.summary)}</p>`:''}${(n.source||ext)?`<p class="small muted brain-news-meta">${n.source?esc(n.source):''}${n.source&&ext?' · ':''}${ext?`<a class="brain-news-src" href="${esc(u)}" target="_blank" rel="noopener">原文↗</a>`:''}</p>`:''}</div>`;}).join('')}</div></div>` : ''}
    </div>
  </section>
  <section id="interview-card" class="iv-card" aria-label="深挖访谈"><p class="iv-muted">正在读访谈记录…</p></section>
:''}
  <div class="brain-tools"><p class="small muted">${filtered?`找到 ${data.total} 条${q?`含“${esc(q)}”`:''} <a class="link" href="#/brain">查看全部</a>`:`全部认知 · ${data.total} 条`}${archivedCount?`<span class="muted">（${showInactive?'含':'不含'} ${archivedCount} 条已归档${showInactive?'':'，与认知图谱同一口径'}）</span>`:''}${archivedCount?` <a class="link" href="${esc(archiveToggleURL)}">${showInactive?'隐藏已归档':'显示已归档'}</a>`:''}${deletedCount?` <a class="link" href="#/brain?include_deleted=1">显示已删除（${deletedCount}）</a>`:''}${showDeleted?' <a class="link" href="#/brain">← 回我的认知</a>':''}<span class="density-note">　点击卡片查看原文与边界</span></p><div class="view-switch" role="group" aria-label="认知卡片阅读方式"><button data-action="brain-view" data-mode="compact" class="${expanded?'':'selected'}" aria-pressed="${!expanded}">精简</button><button data-action="brain-view" data-mode="expanded" class="${expanded?'selected':''}" aria-pressed="${expanded}">展开</button></div></div>
  ${showDeleted?notice('这里是你删掉的认知卡：随时可以恢复；彻底删除后不能找回。'):''}
  ${data.items.length?(showDeleted?deletedGridHTML(data.items):brainGridHTML(data.items,expanded,brainCardHTML)):empty(showDeleted?'回收站是空的':'暂未找到匹配内容',showDeleted?'没有已删除的认知卡。':'更换关键词，或记下一条新的认知。')}
  <p class="small muted space"><a class="link" href="#/brain?scope=example">示例库 → 某类账号的做法，仅供参考，不进你的默认路径</a></p>`;
}
