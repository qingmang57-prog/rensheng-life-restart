'use strict';
const jobLabels={queued:'排队中',running:'处理中',succeeded:'已完成',failed:'未完成',needs_action:'需要补充'};
// 20261113 上传区拖拽支持（委托到 document，覆盖首页两个 .dropzone；drop 后以 change 事件复用既有上传链路）
document.addEventListener('dragover',event=>{
  const zone=event.target.closest?event.target.closest('.dropzone'):null;
  if(!zone)return;
  const input=zone.querySelector('input[type=file]');
  if(input&&input.disabled)return;
  event.preventDefault();
  zone.classList.add('dragover');
});
document.addEventListener('dragleave',event=>{
  const zone=event.target.closest?event.target.closest('.dropzone'):null;
  if(zone)zone.classList.remove('dragover');
});
// t78：非 label 的 .dropzone（拍摄上传 div）点击也要能选文件；label 走原生，不重复触发。
document.addEventListener('click',event=>{
  const zone=event.target.closest?event.target.closest('.dropzone'):null;
  if(!zone||zone.tagName==='LABEL')return;
  if(event.target.closest('input,button,a,select,textarea,label'))return;
  const input=zone.querySelector('input[type=file]');
  if(!input||input.disabled)return;
  input.click();
});
document.addEventListener('drop',event=>{
  const zone=event.target.closest?event.target.closest('.dropzone'):null;
  if(!zone)return;
  event.preventDefault();
  zone.classList.remove('dragover');
  const input=zone.querySelector('input[type=file]');
  const files=event.dataTransfer?event.dataTransfer.files:null;
  if(!input||input.disabled||!files||!files.length)return;
  try{input.files=files;}catch(err){return;}
  input.dispatchEvent(new Event('change',{bubbles:true}));
});
function factorySections(active){return '<nav class="factory-sections factory-navigation" aria-label="开始创作导航">'+[['content','#/create','▣','工作总览'],['topics','#/bench','✦','找选题']].map(([key,href,icon,title])=>'<a href="'+href+'" class="factory-section '+(active===key?'active':'')+'" '+(active===key?'aria-current="page"':'')+'><span aria-hidden="true">'+icon+'</span><strong>'+title+'</strong></a>').join('')+'</nav>';}
const HOME_TILE_ORDER_KEY='ip-home-tile-order';
function homeTileOrder(){try{const v=JSON.parse(localStorage.getItem(HOME_TILE_ORDER_KEY)||'[]');return Array.isArray(v)?v:[];}catch(e){return [];}}
function homeOrderTiles(list){const order=homeTileOrder();return [...list].sort((a,b)=>{const ia=order.indexOf(a.key),ib=order.indexOf(b.key);if(ia<0&&ib<0)return 0;if(ia<0)return 1;if(ib<0)return -1;return ia-ib;});}
function renderFactory(boot){
  const videoEnabled=true;
  const projects=[...boot.projects].sort((a,b)=>String(b.updated_at||'').localeCompare(String(a.updated_at||'')));
  const firstLine=p=>{
    const d=Array.isArray(p.draft)?p.draft:[];
    const hit=d.find(x=>x.role==='hook'&&String(x.text||'').trim())||d.find(x=>String(x.text||'').trim());
    return String(hit?hit.text:'').replace(/\s+/g,' ').trim();
  };
  const workCard=p=>{
    const next=p.next_action||{step:'script',label:'继续作品',hint:stages[p.stage]||p.stage};
    const direct=p.mode==='direct';
    const writing=!direct&&(!videoEnabled||['source','script','pack'].includes(next.step));
    const step=!videoEnabled?'script':next.step;
    const line=firstLine(p);
    const thumbText=direct?'<p class="home-work-hook">直剪模式：导入视频自动转写、净剪、加字幕</p>':(line?'<p class="home-work-hook">'+esc(line.length>62?line.slice(0,62)+'…':line)+'</p>':'<p class="home-work-hook is-empty">还没写正文</p>');
    return '<a class="home-work" href="'+purl(p.id,step)+'">'+
      '<div class="home-work-thumb"><span class="home-work-kind">'+(direct?'视频直剪':writing?'文稿创作':'视频制作')+'</span>'+thumbText+'<span class="home-work-go">'+esc(videoEnabled?next.label:'继续写稿')+' →</span></div>'+
      '<div class="home-work-body"><h3>'+esc(p.title)+'</h3><div class="home-work-foot"><span class="home-work-stage">'+esc(videoEnabled?(next.hint||''):'讲稿待确认')+'</span><span>'+stamp(p.updated_at)+'</span></div></div></a>';
  };
  const knowledgeCount=Number(boot.knowledge_count||0);
  const chips='<nav class="home-chips" aria-label="快速入口">'+
    '<a class="home-chip is-on" href="#/create"><span aria-hidden="true">▣</span>工作总览</a>'+
    '<a class="home-chip" href="#/bench"><span aria-hidden="true">✦</span>找选题</a>'+
    '<button class="home-chip" data-action="create"><span aria-hidden="true">✎</span>写稿</button>'+
    '<a class="home-chip" href="#/feedback"><span aria-hidden="true">◍</span>发布复盘</a>'+
    '<a class="home-chip" href="#/matrix"><span aria-hidden="true">▦</span>矩阵</a>'+
    '<a class="home-chip" href="#/brain"><span aria-hidden="true">◎</span>我的经验库</a>'+
    '</nav>';
  const tileList=[
    {key:'direct',html:'<button class="home-tile is-primary" data-action="home-direct" data-tile="direct"><i class="home-tile-glow" aria-hidden="true"></i><span class="home-tile-icon" aria-hidden="true">✂</span><span class="home-tile-body"><strong>文字剪视频</strong><small>上传视频 → 自动转写 → 对着文字点词剪</small></span></button>'},
    {key:'bench',html:'<a class="home-tile is-primary" href="#/bench" data-tile="bench"><i class="home-tile-glow" aria-hidden="true"></i><span class="home-tile-icon" aria-hidden="true">✦</span><span class="home-tile-body"><strong>找选题</strong><small>同类账号的作品与热评，挑一个值得讲的点</small></span></a>'},
    {key:'write',html:'<button class="home-tile" data-action="create" data-tile="write"><i class="home-tile-glow" aria-hidden="true"></i><span class="home-tile-icon" aria-hidden="true">✎</span><span class="home-tile-body"><strong>直接写稿</strong><small>已有主题，从讲稿开始</small></span></button>'},
    {key:'reference',html:'<button class="home-tile" data-action="home-reference" data-tile="reference"><i class="home-tile-glow" aria-hidden="true"></i><span class="home-tile-icon" aria-hidden="true">↗</span><span class="home-tile-body"><strong>从参考视频开始</strong><small>粘贴链接或上传，先转写再拆解</small></span></button>'},
    {key:'feedback',html:'<a class="home-tile" href="#/feedback" data-tile="feedback"><i class="home-tile-glow" aria-hidden="true"></i><span class="home-tile-icon" aria-hidden="true">◍</span><span class="home-tile-body"><strong>发布与复盘</strong><small>看已发内容的真实反馈</small></span></a>'},
    {key:'ask',html:'<a class="home-tile" href="#/ask" data-tile="ask"><i class="home-tile-glow" aria-hidden="true"></i><span class="home-tile-icon" aria-hidden="true">◈</span><span class="home-tile-body"><strong>问我的经验库</strong><small>带着问题查资料，判断仍由你来做</small></span></a>'},
    {key:'matrix',html:'<a class="home-tile" href="#/matrix" data-tile="matrix"><i class="home-tile-glow" aria-hidden="true"></i><span class="home-tile-icon" aria-hidden="true">▦</span><span class="home-tile-body"><strong>多平台矩阵</strong><small>各平台账号、演练与真实发布状态</small></span></a>'},
    {key:'settings',html:'<a class="home-tile" href="#/settings" data-tile="settings"><i class="home-tile-glow" aria-hidden="true"></i><span class="home-tile-icon" aria-hidden="true">⚙</span><span class="home-tile-body"><strong>个人档案</strong><small>说话风格、红线与服务连接</small></span></a>'},
    {key:'brain',html:'<a class="home-tile" href="#/brain" data-tile="brain"><i class="home-tile-glow" aria-hidden="true"></i><span class="home-tile-icon" aria-hidden="true">◎</span><span class="home-tile-body"><strong>我的经验库</strong><small>'+(knowledgeCount?knowledgeCount+' 条经验':'经验库还是空的')+'，写稿前先找依据</small></span></a>'}
  ];
  const tiles=homeOrderTiles(tileList).map(t=>t.html).join('');
  const works=projects.length
    ?'<div class="home-works">'+projects.slice(0,8).map(workCard).join('')+'</div>'+(projects.length>8?'<details class="home-older"><summary>其余 '+(projects.length-8)+' 条作品</summary><div class="home-older-body home-works">'+projects.slice(8).map(workCard).join('')+'</div></details>':'')
    :empty('还没有作品，从一个选题开始','选中后进入写稿，作品会自动保存在这里。');
  const history=factoryHistorySection();
  const reference=videoEnabled?'<details class="home-reference" id="home-reference"><summary><span aria-hidden="true">↗</span>从参考视频开始<small>链接解析 · 视频上传 · 转写后再拆解</small></summary><div class="home-reference-body"><form id="quick-start-form"><label class="field" for="quick-share"><span>参考视频链接或完整分享文字</span><textarea id="quick-share" name="share_text" rows="2" required placeholder="粘贴抖音或视频号的完整分享文字…"></textarea></label><div class="row spread"><span class="small muted">读取真实资料，取不到的会说明。</span><button type="submit">解析视频 →</button></div></form><label class="reference-launch dropzone" id="reference-dropzone"><span class="dropzone-icon" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 16V4"/><path d="m6 10 6-6 6 6"/><path d="M4 20h16"/></svg></span><span class="dropzone-title">拖拽参考视频到此处，或点击选择文件</span><span class="small muted" id="reference-info">转写 → 内容拆解 → 待确认初稿</span><span class="dropzone-formats" aria-hidden="true"><i>.MP4</i><i>.MOV</i><i>.WEBM</i><i>.MKV</i><i>.M4V</i></span><input id="reference-upload" type="file" accept=".mp4,.mov,.webm,.mkv,.m4v" aria-label="上传参考视频"><progress id="reference-progress" value="0" max="100"></progress></label></div></details>':'';
  const directPanel=videoEnabled?'<details class="home-reference home-direct" id="home-direct" open><summary><span aria-hidden="true">✂</span>文字剪视频：导入已有视频，开始剪辑<small>上传视频 → 自动转写 → 对着文字点词剪</small></summary><div class="home-reference-body"><label class="reference-launch dropzone" id="direct-dropzone"><span class="dropzone-icon" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 16V4"/><path d="m6 10 6-6 6 6"/><path d="M4 20h16"/></svg></span><span class="dropzone-title">拖拽视频到此处，或点击选择文件</span><span class="small muted" id="direct-info">上传 → 转写 → 保守净剪 → 字幕，完成后自动进入审片；原始素材保留</span><span class="dropzone-formats" aria-hidden="true"><i>.MP4</i><i>.MOV</i><i>.WEBM</i><i>.MKV</i><i>.M4V</i></span><input id="direct-upload" type="file" accept=".mp4,.mov,.webm,.mkv,.m4v" aria-label="导入已有视频开始剪辑"><progress id="direct-progress" value="0" max="100"></progress></label></div></details>':'';
  const llmReady=Boolean(state.boot.capabilities&&state.boot.capabilities.llm);
  // 20261104 A 方案：首屏只留主干「输入 → 生成」，其余入口全部收进「更多工具」，默认收起。
  // 20261104 理想交互流程：首屏主输入改为「贴链接 → 拆方向 → 定稿」。
  const trunk=typeof flowHomePanel==='function'?flowHomePanel():
    '<section class="home-hero home-trunk">'+
    '<p class="home-kicker">开始创作</p>'+
    '<h1 class="home-title">今天，做一条什么内容？</h1>'+
    '<p class="home-sub">写下你想讲的主题或选区，点生成，直接拿到完整口播逐字稿。</p>'+
    '<form id="home-quick-form" class="home-trunk-form">'+
      '<textarea name="topic" rows="3" maxlength="2000" required placeholder="例如：客户总问价格贵在哪里，我想把真实的服务过程讲清楚"></textarea>'+
      '<div class="home-trunk-actions"><span class="small muted">生成后保存到「我的作品」，也能在下方生成历史里找回。</span><button class="primary" type="submit" '+(llmReady?'':'disabled')+'>生成初稿 <span aria-hidden="true">→</span></button></div>'+
    '</form>'+
    (llmReady?'':'<p class="small error-text">写作模型未连接，先到「个人档案 → 服务连接」检查后再试。</p>')+
    '<p class="home-trunk-aux"><a class="link small" href="#/bench">还没有主题？去找选题 →</a></p>'+
  '</section>';
  const mainEntries='<section class="home-section home-main"><div class="home-sec-head"><h2>从哪儿开始</h2><span class="home-sec-note">六个主干入口 · 点一下就开始</span></div><div class="home-main-entries">'+
    '<button type="button" data-action="home-direct"><strong>文字剪视频</strong><small>上传视频 → 自动转写 → 对着文字点词剪</small></button>'+
    '<button type="button" data-action="create"><strong>我想写稿</strong><small>已有主题，从讲稿开始</small></button>'+
    '<a href="#/bench"><strong>找选区写稿</strong><small>看同类账号的作品和热评，挑一个值得讲的点</small></a>'+
    '<button type="button" data-action="home-reference"><strong>从视频参考经验</strong><small>粘贴链接或上传，先转写再拆解</small></button>'+
    '<a href="#/feedback"><strong>复盘</strong><small>看已发内容的真实反馈</small></a>'+
    '<a href="#/settings"><strong>个人档案</strong><small>说话风格、红线与做内容习惯</small></a>'+
    '</div></section>';
  const moreTools='<details class="home-section home-more" id="home-more"><summary><span aria-hidden="true">▦</span>更多工具<small>全部入口 · 搜索 · 参考视频解析</small></summary><div class="home-more-body">'+
    '<div class="home-search"><svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true"><circle cx="9" cy="9" r="6"/><path d="M13.4 13.4 17 17" stroke-linecap="round"/></svg><input id="home-search" type="search" placeholder="搜作品标题、搜我的经验库…" autocomplete="off" aria-label="搜索作品与认知"></div>'+
    chips+
    '<section class="home-subsection"><div class="home-sec-head"><h2>全部入口</h2><span class="home-sec-note">都收在这里 · 可拖动排序</span></div><div class="home-rail">'+tiles+'</div></section>'+
    reference+
  '</div></details>';
  return '<div class="home'+(location.hash.startsWith('#/create')?' home-create':'')+'">'+   /* t93：仅 #/create 落地页加作用域类；首页等不加，布局不变 */
    trunk+
    directPanel+
    mainEntries+
    '<div id="home-results" hidden></div>'+
    '<div id="home-body">'+
      '<div id="wash-history"></div>'+
      history+
      '<section class="home-section"><div class="home-sec-head"><h2>我的作品<span class="home-sec-note">'+projects.length+' 条</span></h2><span class="home-sec-note">最近更新优先</span></div>'+works+'</section>'+
      '<div class="factory-footer-links"><a class="link small" href="#/brain">◎ 我的经验库 · 找内容依据</a><a class="link small" href="#/feedback">↗ 发布与复盘 · 看内容反馈</a></div>'+
    '</div>'+
    moreTools+
  '</div>';
}

function factoryHistorySection(){
  const jobs=Array.isArray(creation.history)?creation.history:[];
  const topics=Array.isArray(state.bench&&state.bench.topics)?state.bench.topics:[];
  if(!jobs.length&&!topics.length)return '';
  const topicRows=topics.slice(0,6).map(t=>{
    const pid=t.project_id||(t.data||{}).project_id;
    return '<div class="rule"><div class="row spread"><span class="small">'+esc(t.title)+'</span><button class="ghost small-button" data-action="bench-use-topic" data-id="'+esc(t.id)+'">'+(pid?'继续这条作品':'去写稿')+' →</button></div></div>';
  }).join('');
  const blocks=[];
  if(topicRows)blocks.push('<div class="space"><h3>最近生成的选题</h3>'+topicRows+'</div>');
  if(jobs.length)blocks.push('<div class="space"><h3>最近生成任务</h3>'+creationHistoryItems(8)+'</div>');
  return '<section class="home-section" id="home-history"><div class="home-sec-head"><h2>生成历史<span class="home-sec-note">已落盘 · 退出重进仍可回看</span></h2><a class="link small" href="#/bench">全部记录 →</a></div><div class="panel home-history-panel">'+blocks.join('')+'</div></section>';
}

function metricNumber(value){return value===null||value===undefined?'—':typeof value==='number'?new Intl.NumberFormat('zh-CN').format(value):String(value);}
function timecode(ms){const seconds=Math.floor(ms/1000);return `${Math.floor(seconds/60)}:${String(seconds%60).padStart(2,'0')}.${String(Math.floor(ms%1000/100))}`;}
function renderSource(p){
  const s=p.source,r=s.resolution,m=r?.metadata,a=r?.analysis;
  const running=p.jobs.some(j=>j.kind==='resolve'&&['queued','running'].includes(j.status));
  const ready=Boolean(s.text.trim());
  if(running)return `<section class="panel intake-pending"><span class="launch-symbol" aria-hidden="true">◌</span><h2>正在读取这个视频</h2><p class="muted">链接已经保存。取得资料后，再显示字幕、互动数据和内容拆解。</p><div class="workflow-path"><span>视频资料</span><i>→</i><span>可用字幕</span><i>→</i><span>内容拆解</span></div><a class="link small" href="#/create">先回作品列表，后台继续处理 →</a></section>`;
  if(r?.status==='blocked'&&!ready)return `<section class="panel intake-pending"><span class="launch-symbol" aria-hidden="true">↗</span><h2>链接已收好，视频内容还没拿到。</h2><p class="muted">当前读取遇到平台校验。链接和处理记录已保存，可以稍后继续。</p><div class="badge-row">${pill('正文待取得')}${pill('字幕待取得')}${pill('互动数据待取得')}</div><div class="actions space"><a class="button-link primary" href="${purl(p.id,'script')}">没有视频？直接写稿 →</a>${s.url?`<a class="link small" href="${esc(s.url)}" target="_blank" rel="noopener noreferrer">打开原视频</a>`:''}<a class="link small" href="#/create">先保留，回到作品列表</a></div><details><summary>原因与继续方式</summary><p class="small muted space">${esc(r.message)}</p><p class="small muted space">自动提取失败时的替代办法：打开这条视频，把文案或字幕原文复制后，粘贴到下方「我已有这个视频的字幕」里；推荐：把抖音链接发给豆包，说「转文字」，把结果粘贴进来。也可以边看视频边用微信「语音转文字」把内容说出来，再把识别到的文字粘贴进来。粘贴乱一点没关系，AI 会先帮你整理成通顺的讲稿，再进入拆解与写稿。</p><div class="actions"><button data-action="resolve">重新尝试读取</button><a class="link small" href="#/settings">查看服务连接</a></div></details><details><summary>我已有这个视频的字幕</summary><form id="source-form" class="space"><label class="field"><span>粘贴真实字幕（可选）</span><textarea name="text" rows="8"></textarea></label><button type="submit">保存已有字幕</button></form></details></section>`;
  if(!r&&!ready&&s.url)return `<section class="panel intake-pending"><span class="launch-symbol" aria-hidden="true">↗</span><h2>先读取这个视频。</h2><p class="muted">取得资料后，再核对讲了什么、字幕时间轴和开场细节。</p><div class="actions"><button class="primary" data-action="resolve">开始解析视频 →</button><a class="link small" href="${purl(p.id,'script')}">没有视频？直接写稿 →</a></div></section>`;
  return `<div class="workspace intake-workspace"><div class="stack">
  ${running?'<div class="next-action-card"><span class="processing-mark" aria-hidden="true">◌</span><div><strong>正在读取这个视频</strong><p>视频资料 → 可用字幕 → 内容拆解。你可以离开页面，任务继续处理。</p></div></div>':r?.status==='blocked'?`<div class="next-action-card attention"><span aria-hidden="true">!</span><div><strong>链接已保留，视频资料还没拿到</strong><p>${esc(r.message)}</p><details><summary>没有 API，可以怎么继续</summary><p class="small muted space">自动提取失败时可改用文字版：把视频的文案/字幕原文，或把链接发给豆包说「转文字」，把结果粘贴到下方「手动补充已有字幕」；也可以用微信「语音转文字」。，AI 会先帮你整理成通顺的讲稿，再进入拆解。</p><a class="link small" href="#/settings">查看服务连接</a></details></div></div>`:''}
  <section class="panel"><div class="row spread"><h2>这个视频的资料</h2>${pill(r?({ready:'正文已取得',partial:'部分资料',blocked:'需要补充'}[r.status]||r.status):'等待读取',r?.status==='ready'?'ok':'warn')}</div>${m?`<h3>${esc(m.title||'视频标题暂未取得')}</h3><p class="small muted">${esc(m.author||'作者未提供')}${m.published_at?' · '+stamp(m.published_at):''}${m.duration_ms?' · '+fmt(m.duration_ms):''}</p>`:''}
  ${m?.platform==='uploaded_file'?notice('本机上传的视频：互动数和平台作者尚未取得，来源文件与原链接分别保留。'):`<div class="metric-grid">${[['likes','点赞','♡'],['favorites','收藏','☆'],['shares','转发','↗'],['comments','评论','◌'],['views','播放','▷']].map(([k,l,icon])=>`<div class="metric-tile"><span class="metric-icon" aria-hidden="true">${icon}</span><strong>${esc(metricNumber(m?.metrics?.[k]))}</strong><span>${l}${m?.metrics?.[k]===null||m?.metrics?.[k]===undefined?' · 未取得':''}</span></div>`).join('')}</div>`}
  <div class="row spread small muted space"><span>${m?.collected_at?'采集于 '+stamp(m.collected_at):'尚无平台数据快照'}</span>${s.url?`<a class="link small" href="${esc(s.url)}" target="_blank" rel="noopener noreferrer">查看原视频 ↗</a>`:''}</div></section>
  <section class="panel opening-panel"><div class="row spread"><h2>前 2 秒，怎么让人停下来</h2><span class="time-window">00:00 — 00:02</span></div>${r?.opening?.segments?.length?`<blockquote>${esc(r.opening.text)}</blockquote><p class="small muted">${esc(r.opening.note)}</p>${a?.hook?`<div class="rule"><strong>${esc(a.hook.type||'开场方式分析')}</strong><p class="small muted">${esc(a.hook.reason)}</p><span class="pill">模型解读，待本人核对</span></div>`:''}`:empty('先取得带时间轴的字幕','没有真实时间戳，就不能把第一句话标成“前 2 秒”。')}${a?.note?`<p class="small muted space">${esc(a.note)}</p>`:''}</section>
  ${a?.status==='ready'?`<section class="panel"><h2>讲了什么</h2><p>${esc(a.summary)}</p>${a.key_points?.length?`<ul>${a.key_points.map(t=>`<li>${esc(t)}</li>`).join('')}</ul>`:''}${a.structure?.length?`<div class="source-structure">${a.structure.map((b,i)=>`<div><span>${i+1}</span><section><strong>${esc(b.label)}</strong><blockquote>${esc(b.quote)}</blockquote><p>${esc(b.note)}</p></section></div>`).join('')}</div>`:''}</section>`:''}
  <section class="panel"><div class="row spread"><h2>字幕与原文</h2>${pill(r?.cues?.length?r.cues.length+' 段字幕':ready?'已保存正文':'尚未取得')}</div>${r?.cues?.length?`<details><summary>展开字幕时间轴</summary><div class="cue-list">${r.cues.map(c=>`<div><span>${timecode(c.start_ms)}—${timecode(c.end_ms)}</span><p>${esc(c.text)}</p></div>`).join('')}</div></details>`:''}<details open><summary>${ready?'查看或补充原文':'手动补充已有字幕（可选）'}</summary><form id="source-form" class="space"><label class="field"><span>真实字幕、参考文字或本人资料</span><textarea name="text" rows="10">${esc(s.text)}</textarea></label><button type="submit">保存补充资料</button></form></details>
  ${ready?`<div class="actions space"><a class="button-link primary" href="${purl(p.id,'script')}">下一步：整理自己的稿 →</a><button data-action="extract-knowledge" ${!state.boot.capabilities.llm?'disabled':''}>存入我的经验库</button></div>`:''}</section>
  </div><aside><section class="panel"><h3>下一步</h3><p class="small muted">${ready?'核对来源，再结合自己的经历组织表达。':'先拿到原文，才能做有依据的拆解。'}</p><button class="${r?'':'primary'}" data-action="resolve" ${running||!s.url?'disabled':''}>${running?'读取中…':r?'重新读取资料':'开始解析视频'}</button><details><summary>链接与高级选项</summary><p class="small muted space raw-markdown">${esc(s.share_text||s.url||'没有链接')}</p><button data-action="new-knowledge" data-source="${esc(s.id)}">手动记一条认知</button></details></section><div class="space">${jobs(p)}</div></aside></div>`;
}

// ---- 对标选题（#/bench）：账号库 → 作品榜/热评 → 选题池 ----
const benchCollectLabels={queued:'排队中',running:'处理中',succeeded:'已采集',failed:'未完成',needs_action:'需要处理'};
const benchTopicStatusLabels={pool:'点菜池',selected:'已选中',scripted:'已出稿',produced:'已出片',published:'已发布',closed:'已关闭'};
const benchOriginLabels={bench:'账号采集',comment_feedback:'评论回流',review_insight:'复盘',manual:'手动'};
function benchName(a){return a.display_name||a.nickname||a.handle||a.account_label||'名称未取得';}
function benchMetric(v){return v===null||v===undefined?'—':(typeof v==='number'?new Intl.NumberFormat('zh-CN').format(v):String(v));}
function benchWorkStat(w,key){const flatKey={like:'like_count',comment:'comment_count',share:'share_count',favorite:'collect_count',view:'play_count'}[key];if(w[flatKey]!==undefined&&w[flatKey]!==null)return w[flatKey];if(w.stats&&w.stats[key]!==undefined)return w.stats[key];return null;}
// 转发率 = 转发 ÷ 点赞。缺转发或点赞 → null（显示"—"，不补 0、不参与排序）；点赞为 0 → 'zero'（算不出）。
function benchShareRate(w){const like=benchWorkStat(w,'like'),share=benchWorkStat(w,'share');if(like===null||share===null)return null;if(like===0)return 'zero';return share/like;}
function benchRateLabel(w){const r=benchShareRate(w);if(r===null)return '—';if(r==='zero')return '算不出';return (r*100).toFixed(1).replace(/\.0$/,'')+'%';}
function benchWorkUrl(w){return w.source_url||w.canonical_url||'';}
function benchCollectPill(st){const cls=st==='succeeded'?'ok':st==='failed'?'error':st==='needs_action'?'warn':(st==='queued'||st==='running')?'gold':'';return pill(benchCollectLabels[st]||'未采集',cls);}
function benchCommentsHtml(d){
  const items=d.items||d.comments||[];
  if(!items.length)return notice('该作品评论尚未采集，或本次未取得。请先对该账号执行“采集”，稍后再查看；不会显示模拟数据。');
  const total=d.total!==undefined?d.total:items.length;
  return `<p class="small muted">共 ${esc(benchMetric(total))} 条 · 按点赞排序，仅显示真实采集到的内容，未取得标“—”。</p>`+items.map(c=>`<div class="bench-comment"><div class="row spread"><strong class="small">${esc(c.author_name||c.author||'—')}</strong><span class="small muted">♡ ${esc(benchMetric(c.like_count))} · ${stamp(c.collected_at||c.published_at)||'时间未取得'}</span></div><p class="small bench-comment-note">${esc(c.content||'')}</p></div>`).join('');
}
// ---------- 找选题：从「手上有什么」出发的抽屉式引导 ----------
// 六个抽屉对应六种真实素材来源；没有素材的人走「什么都没有」，只给方向不给标题。
const BENCH_ICONS={
  judge:'<path d="M9.5 18h5M10.5 21h3M12 3a6 6 0 0 0-3.4 10.9V16h6.8v-2.1A6 6 0 0 0 12 3z"/>',
  voices:'<path d="M4 5.5h16v11h-10l-6 4.5z"/><path d="M8 9.5h8M8 12.5h5"/>',
  works:'<rect x="3.5" y="5" width="17" height="12" rx="2.5"/><path d="M10.5 8.8v4.4l4-2.2z"/><path d="M8.5 20.5h7"/>',
  memories:'<path d="M3 17.5l5-6 4 3 5-7.5 4 4.5"/><circle cx="8" cy="11.5" r="1.1" fill="currentColor" stroke="none"/>',
  anchors:'<circle cx="12" cy="12" r="8"/><path d="M12 8v4.2l3 1.8"/>',
  none:'<rect x="4" y="4" width="16" height="16" rx="4" stroke-dasharray="3.5 3"/><path d="M12 9.5v5M9.5 12h5"/>'
};
const BENCH_DRAWER_LIST=[
  {id:'judge',label:'我的观点',note:'你核实过、敢直接写进稿子的说法'},
  {id:'voices',label:'观众的评论',note:'从别人视频下采集来的真实评论，想转给谁看的排前面'},
  {id:'works',label:'别人的作品',note:'看别人讲过什么题，换成你的讲法再讲一遍'},
  {id:'memories',label:'我讲过的',note:'旧作品换个角度再讲'},
  {id:'anchors',label:'挑个时间点',note:'挑一个大家都熟的日子，比如开学、年底、换季'},
  {id:'none',label:'什么都没有',note:'没素材也能开工：挑个问题回答，不给你瞎编标题',empty:true}
];
// 「什么都没有」= 手上没素材的人。所以这里不给术语选项，给十个第一人称的问句——
// 他不用懂什么叫「热点嫁接」「替人说话」，只要挑一个问题回答，稿子就开了头。
const BENCH_DIRECTIONS=[
  ['inverse','唱个反调','大家都这么说，我到底不同意哪一句？'],
  ['speak-for','替观众说','他们心里那句不好意思说出口的话是什么？'],
  ['cost','讲清后果','走错这一步，最坏会坏到什么程度？'],
  ['naming','起个名','这件事，我给它起过什么名字？'],
  ['next','往后会怎样','再过一两年，这件事会变成什么样？'],
  ['wrong','我改过主意','我以前认定、后来改过来的是哪一件？'],
  ['either','两个选择','我现在正纠结的这两个选择，各是什么？'],
  ['three','给三条做法','这件事能照着做的三步，是哪三步？'],
  ['hot','蹭个热点','最近哪件热闹事，能接到我这件事上？'],
  ['regret','我后悔过','我后悔的那件事，是哪一步没做对？']
];
// 时间点必须是「人人都有」的：开学、年底、换季、过年、升学、整岁。
// 不放个人周期框架（八年周期/2032/丙午年）——客户没有这套，写出来他接不住。
const BENCH_ANCHORS=[
  ['开学季','孩子一开学，家里的节奏全变了'],
  ['年底','一年到头，该算总账的时候'],
  ['换季','天气一变，身体和情绪都跟着变'],
  ['过年前','回家、走亲、被问东问西'],
  ['孩子升学','小升初、中考、高考，一家人在熬'],
  ['三十、四十','一过这个岁数，很多想法就变了']
];
// 类型专属时间锚：按第 4 条选的类型优先展示，没选/其它类型仍看通用锚。
const BENCH_TYPE_ANCHORS={
  '怎么赚钱':[['发薪日','工资到账那几天，人对钱最有感觉'],['年初立flag','新一年说要好好搞钱的自己'],['旺季','生意最忙、进账最密的那阵子'],['年终奖','一笔大钱到手，怎么安排']],
  '商家带货':[['换季','换季了，正是该买新的'],['年货','过年囤年货的那几天'],['开学','开学采购，家长最舍得出钱'],['大促','618、双11，所有人都在买']],
  '情感':[['过年回家','回家那几天，所有关系都被放大'],['七夕','一年里最在意关系的时候'],['婚礼季','扎堆喝喜酒的日子'],['开学分离','把孩子送出门，心里空了一下']],
  // 认知思维/营销方法/商家门店：只放这些类型的人真实过得着的通用节令，不凑数、不引入个人周期。
  '认知思维':[['长假结束','假期最后一天，人最容易想明白一些事'],['同学聚会','同学会后回家的路上，脑子里全是比较']],
  '营销方法':[['淡季','生意淡下来，才有空把打法想清楚'],['年后开工','年后开工第一周定的打法，管一年']],
  '商家门店':[['周年庆','店开满几年的那天，老顾客都会来'],['隔壁换铺','隔壁铺子又换了招牌，你会想点什么']]
};
function benchAnchorList(){
  const extra=BENCH_TYPE_ANCHORS[creation.contentType]||[],seen=new Set();
  return [...extra,...BENCH_ANCHORS].filter(a=>!seen.has(a[0])&&seen.add(a[0]));
}
// 内容类型第一问：不选也能继续；默认自动推断沿用封面推荐的 SIGNALS 特征。
const BENCH_CONTENT_TYPES=['情感','认知思维','怎么赚钱','营销方法','商家带货','商家门店'];
// 类型 → 更顺手的讲法（只影响结构候选的先后，不强制）
const BENCH_TYPE_STRUCT={'情感':['story','opinion'],'认知思维':['knowledge','opinion'],'怎么赚钱':['knowledge','opinion'],'营销方法':['knowledge','process'],'商家带货':['process','knowledge'],'商家门店':['process','opinion']};
function benchStructureOrder(list){
  const pref=BENCH_TYPE_STRUCT[creation.contentType];
  if(!pref)return list;
  return [...list].sort((a,b)=>{const ia=pref.indexOf(a.id),ib=pref.indexOf(b.id);return (ia<0?99:ia)-(ib<0?99:ib);});
}
function benchIconHtml(id,size){const s=size||19;return '<svg viewBox="0 0 24 24" width="'+s+'" height="'+s+'" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'+(BENCH_ICONS[id]||BENCH_ICONS.none)+'</svg>';}
function benchProjects(){return (state.boot&&state.boot.projects?state.boot.projects:[]).filter(p=>!p.is_test);}
function benchDrawerCount(){
  const voices=creation.voices?(creation.voices.length||0):'·';
  const works=creation.works?(creation.works.length||0):'·';
  return {judge:(creation.knowledge||[]).length,voices:voices,works:works,memories:benchProjects().length,anchors:benchAnchorList().length,none:BENCH_DIRECTIONS.length};
}
function benchTypePanel(){
  const cur=creation.contentType||'';
  return '<section class="panel bench-type-panel"><div class="bench-type-head"><h2>这条讲什么类型？</h2><span class="small muted">不选也行，会按你写的内容自动判断</span></div>'+
    '<div class="bench-types" role="group" aria-label="内容类型">'+BENCH_CONTENT_TYPES.map(t=>'<button type="button" class="bench-type-chip'+(cur===t?' is-on':'')+'" data-action="bench-type" data-type="'+t+'" aria-pressed="'+(cur===t)+'">'+t+'</button>').join('')+'</div>'+
    (cur?'<p class="small muted bench-type-note">已按「'+esc(cur)+'」把讲法和时间点排在前面，后面生成选题也会贴着这个类型。</p>':'')+'</section>';
}
function benchDrawerShell(){
  const c=benchDrawerCount(),cur=creation.drawer||'';
  return '<section class="panel bench-hero"><h1>现在手上有什么？</h1><p class="bench-lead">不用先想清楚讲什么。挑一件你已经有的，工作台把它变成一个能开的题。</p>'+
    '<div class="bench-drawers">'+BENCH_DRAWER_LIST.map(d=>{
      const n=c[d.id];
      return '<button class="bench-drawer'+(cur===d.id?' is-open':'')+(d.empty?' is-empty':'')+'" type="button" data-action="drawer-'+d.id+'" data-drawer="'+d.id+'" aria-pressed="'+(cur===d.id)+'">'+
        '<span class="bench-ico">'+benchIconHtml(d.id)+'</span>'+
        '<span class="bench-label">'+d.label+'</span>'+
        '<span class="bench-num'+(n==='待建'?' is-todo':'')+'">'+n+'</span></button>';
    }).join('')+'</div></section>';
}
function benchJudgeBody(){
  const list=creation.knowledge||[];
  if(!list.length)return '<div class="bench-none"><strong>我的经验库里还没有你认可的观点</strong><p>在我的经验库里把一条说法核实、并标成「可用于写稿」之后，会出现在这里。</p><a class="link small" href="#/brain">去我的经验库 →</a></div>';
  return '<div class="bench-list bench-scroll">'+list.map(k=>{
    const claim=String(k.claim||'').replace(/\s+/g,' ').trim();
    return '<label class="bench-check"><input type="checkbox" name="knowledge_ids" value="'+esc(k.id)+'" '+(creation.knowledge_ids.includes(k.id)?'checked':'')+'><div><strong>'+esc(k.title)+'</strong><span>'+esc(claim.length>60?claim.slice(0,60)+'…':claim||'主张待补')+'</span></div></label>';
  }).join('')+'</div><p class="small muted space">'+list.length+' 条认可的观点 · 列表可上下滚动 · 勾 1–3 条作为写稿依据（最多 10 条） · <a class="link" href="#/brain?scope=example">看看别人的做法（示例库）</a></p>';
}
// 转发放大型句式：命中说明读者想把它转给某个具体的人——这是「嘴替型」评论的代理信号。
// 评论没有转发数，不得用转发率套评论，只能用句式代理。
const BENCH_SHARE_PHRASE=/转给|发给|@[^\s]{1,20}|这不就是我吗|说的就是我|我闺蜜|我老公|我老婆|我同事|我妈就这样|我爸就这样|本人|你也是|建议发给/;
function benchVoiceScore(v){
  const t=String(v.content||'');
  const ask=/[？?]|吗|怎么|为什么|是不是|能不能|该不该|多少/;
  return (BENCH_SHARE_PHRASE.test(t)?2:0)+(ask.test(t)?1:0);
}
/* t133：加载失败与「真空」分开——失败显示可见错误条＋重试，真真空才给空态引导 */
function loadFailBar(what, message, action, extra){
  return '<div class="bench-none is-fail" role="alert">'
    + '<strong>× ' + esc(what) + '没读出来</strong>'
    + '<p>' + esc(message || '网络或服务暂时不可用') + '——这不是「没有数据」，已保留可重试入口。</p>'
    + (extra ? '<p class="small muted">' + esc(extra) + '</p>' : '')
    + '<div class="row space"><button class="small-button" type="button" data-action="' + esc(action) + '">重试</button></div></div>';
}

function benchVoicesBody(){
  if(creation.voicesError&&!creation.voicesLoading){
    return loadFailBar('已采集的评论',creation.voicesError,'retry-voices',creation.voicesPartial?('另有 '+creation.voicesPartial+' 个作品的评论没读到'):'');
  }
  if(creation.voicesPartial&&(!creation.voices||!creation.voices.length)){
    return loadFailBar('已采集的评论',('有 '+creation.voicesPartial+' 个作品的评论没读到'),'retry-voices');
  }
  if(creation.voicesPartial&&creation.voices&&creation.voices.length){
    /* 部分失败：已读到的照常展示，但明确标出缺口与重试入口 */
    return partialFailNote('有 '+creation.voicesPartial+' 个作品的评论没读到','retry-voices')+benchVoicesList();
  }
  if(!creation.voices)return '<div class="bench-none"><strong>'+(creation.voicesLoading?'正在读已采集的评论…':'还没有读过评论')+'</strong><p>从别人视频下采集到的真实评论里挑一句，直接变成起题依据。</p></div>';
  return benchVoicesList();
}
function partialFailNote(message,action){
  return '<div class="bench-none is-fail" role="alert"><strong>× '+esc(message)+'</strong>'
    +'<p>下面只显示已经读到的部分，不是全部；可点重试补读。</p>'
    +'<div class="row space"><button class="small-button" type="button" data-action="'+esc(action)+'">重试</button></div></div>';
}
function benchVoicesList(){
  if(!creation.voices.length)return '<div class="bench-none"><strong>库里还没有采集到评论</strong><p>先在页面底部「从别人的作品找依据」里保存账号、执行采集，评论才会进来；没有真实评论时不做模拟。</p></div>';
  const sorted=[...creation.voices].sort((a,b)=>benchVoiceScore(b)-benchVoiceScore(a)||(Number(b.like_count)||0)-(Number(a.like_count)||0));
  const shown=sorted.slice(0,6);
  const dir=creation.picked&&creation.picked.kind==='voice'?creation.picked.id:'';
  return '<div class="bench-list">'+shown.map(v=>{
    const text=String(v.content||'').replace(/\s+/g,' ').trim();
    const short=text.length>74?text.slice(0,74)+'…':text;
    const who=v.author_name||'匿名';
    const shareHit=BENCH_SHARE_PHRASE.test(text);
    return '<div class="bench-item'+(dir===v.id?' is-on':'')+'"><p>「'+esc(short)+'」</p>'+
      '<div class="bench-item-foot"><span class="bench-dot">'+esc(who)+'</span>'+(shareHit?'<span class="bench-share-mark">想转给某人</span>':'')+'<span>♡ '+esc(benchMetric(v.like_count))+'</span>'+(v.workTitle?'<span>'+esc(String(v.workTitle).replace(/\s+/g,' ').slice(0,16))+'…</span>':'')+'</div>'+
      '<button class="bench-go" type="button" data-action="pick-voice" data-id="'+esc(v.id)+'" data-label="'+esc(who)+'" data-text="'+esc('回答这个问题：'+text.slice(0,120))+'" aria-label="用这句起题">→</button></div>';
  }).join('')+'</div><p class="small muted space">共 '+creation.voices.length+' 条真实评论，想转给谁看的排最前，然后是提问，再按点赞排；点箭头会把这句话放进起题句。</p>';
}
function benchWorksBody(){
  if(creation.worksError&&!creation.worksLoading){
    return loadFailBar('已采的作品',creation.worksError,'retry-works');
  }
  if(!creation.works)return '<div class="bench-none"><strong>'+(creation.worksLoading?'正在读已采的作品…':'还没有读过别人的作品')+'</strong><p>点开这个抽屉会自动读库里已采的作品；也可以先在页面底部「从别人的作品找依据」里保存账号并采集。</p></div>';
  if(!creation.works.length)return '<div class="bench-none"><strong>库里还没有别人的作品</strong><p>先在页面底部「从别人的作品找依据」里保存账号、点采集；没有真实数据时不做模拟。</p></div>';
  const on=creation.picked&&creation.picked.kind==='work'?creation.picked.id:'';
  const sortMode=creation.worksSort||'share_rate';
  const shown=creation.works.slice(0,6);
  return '<div class="bench-list bench-scroll">'+shown.map(w=>{
    const title=String(w.title||'标题未取得').replace(/\s+/g,' ').trim();
    const short=title.length>42?title.slice(0,42)+'…':title;
    return '<div class="bench-item'+(on===w.id?' is-on':'')+'"><p>'+esc(short)+'</p>'+
      '<div class="bench-item-foot"><span>转发率 '+esc(benchRateLabel(w))+'</span><span>♡ '+esc(benchMetric(benchWorkStat(w,'like')))+'</span><span>↗ '+esc(benchMetric(benchWorkStat(w,'share')))+'</span></div>'+
      '<button class="bench-go" type="button" data-action="pick-work" data-id="'+esc(w.id)+'" data-label="'+esc(title.slice(0,24))+'" data-text="'+esc('别人讲过「'+title.slice(0,40)+'」；换个角度讲你自己的版本，别照抄')+'" aria-label="用这条作品起题">→</button></div>';
  }).join('')+'</div>'+
  '<div class="bench-sort space" role="group" aria-label="作品排序方式">'+
    '<button class="small-button'+(sortMode==='share_rate'?' primary':'')+'" type="button" data-action="works-sort" data-sort="share_rate"'+(sortMode==='share_rate'?' disabled aria-pressed="true"':'')+'>按转发率</button>'+
    '<button class="small-button'+(sortMode==='like'?' primary':'')+'" type="button" data-action="works-sort" data-sort="like"'+(sortMode==='like'?' disabled aria-pressed="true"':'')+'>按点赞</button>'+
    '<span class="small muted">转发率高的排前面 · 基于当前 '+creation.works.length+' 条样本</span></div>';
}
function benchMemoriesBody(){
  const list=benchProjects();
  if(!list.length)return '<div class="bench-none"><strong>还没有已建档的作品</strong><p>把以前发过的内容建一条作品，就能在这里换个角度重讲。</p><a class="link small" href="#/create">回开始创作 →</a></div>';
  const on=creation.picked&&creation.picked.kind==='memory'?creation.picked.id:'';
  return '<div class="bench-list">'+[...list].sort((a,b)=>String(b.updated_at||'').localeCompare(String(a.updated_at||''))).slice(0,8).map(p=>{
    const hook=(Array.isArray(p.draft)?p.draft.find(x=>x.role==='hook'&&String(x.text||'').trim()):null)||{};
    const line=String(hook.text||'').replace(/\s+/g,' ').trim();
    return '<div class="bench-item'+(on===p.id?' is-on':'')+'"><p>'+esc(p.title||'标题待补')+'</p>'+
      '<div class="bench-item-foot"><span>'+esc(stages[p.stage]||p.stage||'草稿')+'</span>'+(line?'<span>'+esc(line.length>30?line.slice(0,30)+'…':line)+'</span>':'')+'</div>'+
      '<button class="bench-go" type="button" data-action="pick-memory" data-id="'+esc(p.id)+'" data-label="'+esc(p.title||'')+'" data-text="'+esc('把「'+(p.title||'')+'」换个角度再讲一遍，不重复上次的讲法')+'" aria-label="换个角度再讲">→</button></div>';
  }).join('')+'</div><p class="small muted space">只能换个切角，不能把同一套话再说一遍。</p>';
}
function benchAnchorsBody(){
  const on=creation.picked&&creation.picked.kind==='anchor'?creation.picked.id:'';
  const list=benchAnchorList(),extra=BENCH_TYPE_ANCHORS[creation.contentType];
  return (extra?'<p class="small muted space">已把「'+esc(creation.contentType)+'」过得着的日子排在前面。</p>':'')+
    '<div class="bench-anchors">'+list.map(([id,note])=>{
    return '<button class="bench-anchor'+(on===id?' is-on':'')+'" type="button" data-action="pick-anchor" data-id="'+esc(id)+'" data-label="'+esc(id)+'" data-text="'+esc('挑「'+id+'」这个时间点当由头，讲一件正在发生的事：'+note)+'"><strong>'+esc(id)+'</strong><span>'+esc(note)+'</span></button>';
  }).join('')+'</div><p class="small muted space">这些都是人人过得着的日子，不用建档；点一个就把起题句填好，后面的字你可以随便改。</p>';
}
function benchNoneBody(){
  const on=creation.direction||'';
  return '<div class="bench-dirs">'+BENCH_DIRECTIONS.map(([id,label],i)=>{
    return '<button class="bench-dir'+(on===id?' is-on':'')+'" type="button" data-action="pick-direction" data-id="'+esc(id)+'" data-label="'+esc(label)+'" data-text="'+esc('回答这个问题：'+BENCH_DIRECTIONS[i][2])+'"><i>'+String(i+1).padStart(2,'0')+'</i><strong>'+esc(label)+'</strong><small>'+esc(BENCH_DIRECTIONS[i][2])+'</small></button>';
  }).join('')+'</div><p class="small muted space">挑一个你答得上来的问题就行，不用懂任何说法。点下去起题句会填好，口气和字数再随你改。</p>';
}
function benchMoreBody(){
  const cat=creation.catalog;if(!cat)return '';
  const goal=cat.goals.find(g=>g[0]===creation.goal),structure=cat.structures.find(x=>x.id===creation.structure);
  const key=creation.goal==='attention'?'elements':'reasons';
  return '<div class="creation-goals" role="group" aria-label="创作目标">'+cat.goals.map(([id,label,hint],i)=>{
      return '<label class="creation-goal '+(id===creation.goal?'selected':'')+'"><input type="radio" name="goal" value="'+id+'" '+(id===creation.goal?'checked':'')+'><span class="goal-num">0'+(i+1)+'</span><strong>'+({attention:'关注选题',conversion:'成交选题',pain:'痛点选题'}[id]||label)+'</strong><small>'+hint+'</small></label>';
    }).join('')+'</div>'+
    '<div class="field"><span>这条内容怎么讲'+(creation.contentType?'<span class="small muted"> · 已按「'+esc(creation.contentType)+'」把顺手的排前面</span>':'')+'</span><div class="creation-structures" role="group" aria-label="脚本结构">'+benchStructureOrder(cat.structures).map(x=>'<label class="creation-structure '+(x.id===creation.structure?'selected':'')+'"><input type="radio" name="structure" value="'+x.id+'" '+(x.id===creation.structure?'checked':'')+'><b aria-hidden="true">'+x.icon+'</b><strong>'+x.label+'</strong><small>'+x.hint+'</small></label>').join('')+'</div></div>'+
    (creation.goal!=='pain'?'<div class="field"><span>'+(creation.goal==='attention'?'关注元素（最多3项）':'选择你的理由（最多5项）')+'</span><div class="creation-chips">'+(creation.goal==='attention'?cat.elements:cat.reasons).map(([id,label,hint])=>{const k=creation.goal==='attention'?'elements':'reasons';return '<label title="'+esc(hint)+'"><input type="checkbox" name="'+k+'" value="'+id+'" '+(creation[k].includes(id)?'checked':'')+'><span>'+label+'</span></label>';}).join('')+'</div></div>':'')+
    '<p class="small muted">当前：'+esc(goal?goal[1]:'关注选题')+' · '+esc(structure?structure.label:'结构自动匹配')+' · 依据 '+(creation.knowledge_ids.length+Object.keys(state.bench.sel||{}).length)+' 条</p>';
}
function benchStage(){
  const id=creation.drawer||'voices';
  const meta=BENCH_DRAWER_LIST.find(x=>x.id===id)||BENCH_DRAWER_LIST[1];
  const body={judge:benchJudgeBody,voices:benchVoicesBody,works:benchWorksBody,memories:benchMemoriesBody,anchors:benchAnchorsBody,none:benchNoneBody}[id]();
  const goal=creation.catalog?creation.catalog.goals.find(g=>g[0]===creation.goal):null;
  const picked=creation.picked&&creation.picked.label?'<div class="bench-picked">已选：<b>'+esc(creation.picked.label)+'</b> · 起题句已填好，可以直接改</div>':'';
  return '<section class="panel creation-focus bench-stage" id="bench-stage"><form id="creation-topic-form">'+
    '<div class="bench-sec"><span class="bench-sec-ico">'+benchIconHtml(id,16)+'</span><strong>'+esc(meta.label)+'</strong><span class="bench-sec-note">'+esc(meta.note)+'</span></div>'+
    body+picked+
    '<label class="field bench-say space"><span>这次想讲什么</span><textarea name="context" rows="2" maxlength="12000" placeholder="挑一个素材会自动填进来；也可以直接写一句你自己的话。">'+esc(creation.context||'')+'</textarea>'+
    '<p class="bench-say-hint">'+(creation.picked&&creation.picked.label?'↑ 这句是按你选的素材套的模板，改成你自己的说法会更准。':'没有素材也没关系：点「什么都没有」，选一个方向就能起题。')+'</p></label>'+
    '<div class="bench-share-row space"><span class="small muted">这句话想让人转给谁</span><select name="share_to" aria-label="这句话想让人转给谁，选填"><option value="">不指定</option>'+['闺蜜','老公老婆','同事','老板','爸妈','自己收藏'].map(o=>'<option value="'+o+'" '+(creation.shareTo===o?'selected':'')+'>'+o+'</option>').join('')+'</select></div>'+
    '<details class="creation-methods bench-more"><summary>更多设定 <span class="small muted">'+esc(goal?({attention:'关注',conversion:'成交',pain:'痛点'}[creation.goal]||goal[1]):'关注')+' · 结构 / 元素 / 目标</span></summary><div class="creation-method-body">'+benchMoreBody()+'</div></details>'+
    '<div class="bench-run"><span class="bench-run-note">'+(creation.goal==='pain'?'先给 3 个待验证假设':'生成 3 条候选，选中再写稿')+'</span><button class="primary" type="submit" '+(!state.boot.capabilities.llm?'disabled':'')+'>'+(creation.goal==='pain'?'探索用户痛点':'生成 3 条选题')+' <span aria-hidden="true">→</span></button></div>'+
    (!state.boot.capabilities.llm?'<p class="small muted space">生成模型待连接。<a class="link" href="#/settings">查看服务连接</a>；已有作品仍可继续编辑。</p>':'')+
    '</form><div id="creation-status" role="status">'+creationStatus()+'</div></section>';
}
function benchRefreshCounts(){
  const c=benchDrawerCount();
  document.querySelectorAll('.bench-drawer').forEach(el=>{
    const n=el.querySelector('.bench-num'),v=c[el.dataset.drawer];
    if(n&&v!==undefined){n.textContent=v;n.classList.toggle('is-todo',v==='待建');}
  });
}
function benchRerenderStage(){
  const t=document.getElementById('bench-stage');
  if(t)t.outerHTML=benchStage();
}
async function benchEnsureVoices(){
  if(creation.voices||creation.voicesLoading)return;
  creation.voicesLoading=true;benchRefreshCounts();
  if((creation.drawer||'voices')==='voices')benchRerenderStage();
  try{
    let works=state.bench.works;
    if(!works||!works.length){const d=await api('/bench/works?'+new URLSearchParams({page:'1',page_size:'100'}));works=d.items||[];}
    let failed=0;
    const pages=await Promise.all(works.map(w=>api('/bench/comments?'+new URLSearchParams({work_id:w.id,page:'1',page_size:'50'})).then(d=>(d.items||[]).map(c=>Object.assign({},c,{workTitle:w.title||''}))).catch(()=>{failed++;return [];})));
    creation.voices=[].concat.apply([],pages);
    creation.voicesError='';
    creation.voicesPartial=failed;
  }catch(e){creation.voicesError=String(e&&e.message||e||'读取失败');creation.voicesPartial=0;}
  creation.voicesLoading=false;benchRefreshCounts();
  if((creation.drawer||'voices')==='voices')benchRerenderStage();
}
async function benchEnsureWorks(){
  if(creation.works||creation.worksLoading)return;
  creation.worksLoading=true;benchRefreshCounts();
  if(creation.drawer==='works')benchRerenderStage();
  try{
    const d=await api('/bench/works?'+new URLSearchParams({page:'1',page_size:'100',sort:creation.worksSort||'share_rate'}));
    creation.works=d.items||[];
    creation.worksError='';
  }catch(e){creation.worksError=String(e&&e.message||e||'读取失败');}
  creation.worksLoading=false;benchRefreshCounts();
  if(creation.drawer==='works')benchRerenderStage();
}
async function benchAction(el){
  const a=el.dataset.action||'';
  if(a.startsWith('drawer-')){
    creation.drawer=a.slice(7);
    // 换抽屉时，如果起题句还是上一条素材套的模板，就一起清掉；用户改过的文字不动。
    if(creation.picked&&creation.context===creation.picked.text)creation.context='';
    creation.picked=null;creation.direction='';
    document.querySelectorAll('.bench-drawer').forEach(b=>{const on=b.dataset.drawer===creation.drawer;b.classList.toggle('is-open',on);b.setAttribute('aria-pressed',String(on));});
    benchRerenderStage();
    if(creation.drawer==='voices')benchEnsureVoices();
    if(creation.drawer==='works')benchEnsureWorks();
    return;
  }
  if(a==='bench-type'){
    const t=el.dataset.type;
    creation.contentType=(creation.contentType===t)?'':t;
    document.querySelectorAll('.bench-type-chip').forEach(c=>{const on=c.dataset.type===creation.contentType;c.classList.toggle('is-on',on);c.setAttribute('aria-pressed',String(on));});
    const note=document.querySelector('.bench-type-note');
    if(note)note.remove();
    if(creation.contentType){
      const panel=document.querySelector('.bench-type-panel');
      if(panel){const p=document.createElement('p');p.className='small muted bench-type-note';p.textContent='已按「'+creation.contentType+'」把讲法和时间点排在前面，后面生成选题也会贴着这个类型。';panel.appendChild(p);}
    }
    const m=document.querySelector('.creation-methods'),wasOpen=m?m.open:false;
    benchRerenderStage();
    benchRefreshCounts(); // 类型变了，时间锚数量跟着变，抽屉上的计数要同步刷新
    const m2=document.querySelector('.creation-methods');if(m2&&wasOpen)m2.open=true;
    return;
  }
  if(a==='retry-voices'){
    creation.voices=null;creation.voicesError='';creation.voicesPartial=0;
    benchRerenderStage();benchEnsureVoices();return;
  }
  if(a==='retry-works'){
    creation.works=null;creation.worksError='';
    benchRerenderStage();benchEnsureWorks();return;
  }
  if(a==='retry-home-search'||a==='retry-knowledge'){
    /* t135：首页搜索重试——用记住的关键词重拉，不依赖输入框当前值 */
    const input=document.getElementById('home-search');
    const q=(input&&input.value.trim())||lastHomeQuery||'';
    if(q)homeSearch(q);else toast('先输入要搜的词再重试。');
    return;
  }
  if(a==='works-sort'){
    creation.worksSort=el.dataset.sort==='like'?'like':'share_rate';
    try{
      const d=await api('/bench/works?'+new URLSearchParams({page:'1',page_size:'100',sort:creation.worksSort}));
      creation.works=d.items||[];
      creation.worksError='';
    }catch(e){creation.worksError=String(e&&e.message||e||'排序读取失败');}
    benchRerenderStage();
    return;
  }
  const kind=a.slice(5);
  creation.picked={kind:kind,id:el.dataset.id||'',label:el.dataset.label||'',text:el.dataset.text||''};
  creation.context=el.dataset.text||'';
  if(kind==='direction')creation.direction=el.dataset.id||'';else creation.direction='';
  benchRerenderStage();
  const t=document.querySelector('#creation-topic-form textarea[name="context"]');
  if(t){t.focus();t.setSelectionRange(t.value.length,t.value.length);}
}
// ---------- 刷到一条视频 → 贴链接 → 拆 5 个你能讲的角度 ----------
// 和上面五个抽屉的区别：抽屉是「我手上有什么」，这里是我什么都没有、
// 但刷到别人一条不错的。产出是 5 个切角，不是一个答案。
function benchLink(){return (state.bench&&state.bench.link)||{};}
function benchLinkStatusHtml(){
  const l=benchLink();
  if(l.error){
    const needLogin=/登录|need_login/.test(l.error||'');
    return '<div class="bench-link-note is-error">× '+esc(l.error)+
      (needLogin?'<div class="row space"><button type="button" class="primary" id="bench-douyin-login">去登录抖音</button><span class="small muted">扫完自动重试同一条链接</span></div>':'')+
      '<p class="small muted space">推荐：把抖音链接发给豆包，说「转文字」，把结果粘贴到下方资料框；微信语音转文字也可以。</p></div>';
  }
  if(l.busy)return '<div class="bench-link-note"><span class="bench-spin"></span>'+esc(l.stage||'正在读取…')+'</div>';
  const w=l.work;
  if(w){
    return '<div class="bench-link-note is-ok">已读入 <b>'+esc(w.author||'作者未取得')+'</b> · ♡ '+esc(benchMetric(w.like_count))+
      ' · 评论 '+esc(benchMetric(w.comment_count))+'（本次抓到 '+esc(w.captured_comments||0)+' 条）'+
      (w.source_url?' · <a class="link" href="'+esc(w.source_url)+'" target="_blank" rel="noopener noreferrer">打开原视频 ↗</a>':'')+'</div>';
  }
  return '';
}
function benchInteractions(w){
  // 2026-11-17 t109：只展示真抓到的互动数；缺失一律「—」，不估算、不补零。
  const items=[['赞',benchWorkStat(w,'like')],['评',benchWorkStat(w,'comment')],
    ['藏',benchWorkStat(w,'favorite')],['转',benchWorkStat(w,'share')],['播放',benchWorkStat(w,'view')]];
  return '<p class="small muted bench-interactions" aria-label="这条视频的互动数据">'+items.map(([label,value])=>
    '<span><b>'+esc(label)+'</b> '+esc(benchMetric(value))+'</span>').join('')+'</p>';
}
function benchAnglesInner(){
  const l=benchLink();
  const list=l.angles||[];
  if(!list.length)return '';
  const w=l.work||{};
  const cards=list.map((a,i)=>'<article class="bench-angle">'+
      '<div class="bench-angle-head"><span class="bench-angle-tag">'+esc(a.label||'角度')+'</span>'+(a.basis?pill(a.basis):'')+'<span class="bench-angle-no">0'+(i+1)+'</span></div>'+
      '<h3>'+esc(a.title)+'</h3>'+
      '<p class="bench-angle-why">'+esc(a.why)+'</p>'+
      '<p class="bench-angle-hook">'+esc(a.hook)+'</p>'+
      '<button class="bench-angle-use" type="button" data-action="use-angle" data-id="'+i+'">用这个角度 →</button>'+
    '</article>').join('');
  return '<section class="panel"><div class="row spread"><h2>这条视频能拆出的 '+list.length+' 个角度</h2>'+
    '<div class="actions"><span class="small muted">点任意一条，起题句自动填进下面的输入框</span>'+
    '<button class="small-button" type="button" data-action="redo-angles">重新拆一次</button></div></div>'+
    (w&&!l.error&&!l.need_login?benchInteractions(w):'')+
    '<p class="small muted">角度是从这条视频的文案和 '+(w.captured_comments||0)+' 条真实评论里推出来的'+
    '（评论按提问、反驳、亲身经历各挑一些，不只看点赞），'+
    '依据写在每张卡里，不是抄它的结论。</p>'+
    '<div class="bench-angles">'+cards+'</div>'+
    '<p class="bench-angles-foot">五个角度分别站在：翻面 / 换人 / 换时间 / 推一层 / 落到动作。'+
    '哪条顺，就拿去生成；都不顺就点「重新拆一次」，同一个链接不会重复采集。</p></section>';
}
function benchLinkPanel(){
  const l=benchLink();
  return '<section class="panel bench-link"><div class="bench-link-head">'+
    '<span class="bench-link-ico"><svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M10 13.5a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7L11.5 6.3"/><path d="M14 10.5a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1.5-1.5"/></svg></span>'+
    '<div><strong>刷到一条，觉得自己也能讲</strong>'+
    '<p>贴链接，读它的文案和真实评论，拆成 5 个你能讲的角度——不是复述它。</p></div></div>'+
    '<form id="bench-link-form" class="bench-link-form">'+
    '<input name="url" type="text" autocomplete="off" spellcheck="false" placeholder="粘贴抖音视频链接，或「分享 → 复制链接」的整段文字" aria-label="抖音视频链接">'+
    '<button type="submit" class="primary" '+(l.busy?'disabled':'')+'>'+(l.busy?'处理中…':'拆 5 个角度')+'</button>'+
    '</form><div id="bench-link-status">'+benchLinkStatusHtml()+'</div></section>';
}
function benchAnglesSection(){
  return '<div id="bench-angles" class="bench-angles-wrap">'+benchAnglesInner()+'</div>';
}
function renderBench(b){
  const accounts=b.accounts||[];
  const selAccount=accounts.find(x=>x.id===b.accountId);
  const selectedCount=Object.keys(b.sel||{}).length;
  const sortMode=b.worksSort||'share_rate';
  const pageSize=10;
  const pageCount=Math.max(1,Math.ceil((b.worksTotal||0)/pageSize));
  const accountRows=accounts.map(a=>{
    const st=a.collect_status,busy=st==='queued'||st==='running',selected=a.id===b.accountId;
    return `<div class="bench-row ${selected?'current':''}"><div class="bench-main"><strong>${esc(benchName(a))}</strong> ${pill(a.platform||'—')}<div class="bench-sub"><a class="link" href="${esc(a.home_url)}" target="_blank" rel="noopener noreferrer">主页 ↗</a> · 粉丝 ${esc(benchMetric(a.follower_count))} · 作品 ${esc(benchMetric(a.works_count!==undefined?a.works_count:a.total_works))}</div><div class="bench-sub">${benchCollectPill(st)}${a.collect_error?`<span class="error-text">　${esc(a.collect_error)}</span>`:''}</div></div><div class="bench-actions"><button class="small-button" data-action="bench-select" data-id="${esc(a.id)}" ${selected?'disabled':''}>作品榜</button><button class="small-button" data-action="bench-collect" data-id="${esc(a.id)}" ${busy?'disabled':''}>${busy?'采集中…':(st==='succeeded'?'重新采集':'采集')}</button></div></div>`;
  }).join('');
  const works=b.works||[];
  let worksHtml;
  if(!b.accountId)worksHtml=notice('先在上方账号库点“作品榜”，选择一个账号。');
  else if(!works.length)worksHtml=`<div class="empty"><h3>这个账号还没有作品</h3><p>点“采集”获取作品与热评；未取得的数据会标“—”。</p></div>`;
  else worksHtml=`<div class="bench-head"><span></span><span>标题 · 发布时间 · ♡ 赞 / ◌ 评 / ↗ 转 / ☆ 藏 / 转发率 · 来源</span><span>热评</span></div>${works.map(w=>{const url=benchWorkUrl(w);const rate=benchShareRate(w);const rateMiss=sortMode==='share_rate'&&(rate===null||rate==='zero');return `<div class="bench-work"><label class="check"><input type="checkbox" name="bench-work" value="${esc(w.id)}" ${b.sel&&b.sel[w.id]?'checked':''} aria-label="勾选作品作为选题证据"></label><div class="bench-main"><strong>${esc(w.title||'标题未取得')}</strong><div class="bench-sub">${w.published_at?esc(stamp(w.published_at)):'发布时间未取得'}</div><div class="bench-stats"><span>♡ ${esc(benchMetric(benchWorkStat(w,'like')))}</span><span>◌ ${esc(benchMetric(benchWorkStat(w,'comment')))}</span><span>↗ ${esc(benchMetric(benchWorkStat(w,'share')))}</span><span>☆ ${esc(benchMetric(benchWorkStat(w,'favorite')))}</span><span>转发率 ${esc(benchRateLabel(w))}</span>${rateMiss?'<span class="bench-rate-miss">未参与转发率排序</span>':''}${url?`<a class="link" href="${esc(url)}" target="_blank" rel="noopener noreferrer">来源 ↗</a>`:'<span>来源未取得</span>'}</div></div><button class="small-button" data-action="bench-comments" data-id="${esc(w.id)}">热评</button></div>`;}).join('')}<div class="bench-foot"><span class="small muted">第 ${b.worksPage} / ${pageCount} 页 · 共 ${esc(benchMetric(b.worksTotal||0))} 条</span><div class="actions"><button class="small-button" data-action="bench-page" data-page="${b.worksPage-1}" ${b.worksPage<=1?'disabled':''}>上一页</button><button class="small-button" data-action="bench-page" data-page="${b.worksPage+1}" ${b.worksPage>=pageCount?'disabled':''}>下一页</button></div></div>`;
  const topics=b.topics||[];
  const sortBar=b.accountId
    ?'<div class="bench-sort" role="group" aria-label="作品排序方式">'+
      '<button class="small-button'+(sortMode==='share_rate'?' primary':'')+'" data-action="bench-sort" data-sort="share_rate"'+(sortMode==='share_rate'?' disabled aria-pressed="true"':'')+'>按转发率</button>'+
      '<button class="small-button'+(sortMode==='like'?' primary':'')+'" data-action="bench-sort" data-sort="like"'+(sortMode==='like'?' disabled aria-pressed="true"':'')+'>按点赞</button>'+
      '<span class="small muted">'+(sortMode==='share_rate'?'转发率 = 转发 ÷ 点赞，算不出的排在后面 · ':'')+((b.worksTotal||0)<30?'样本还少（不足 30 条），排序仅供参考 · ':'')+'基于当前 '+esc(benchMetric(b.worksTotal||0))+' 条样本</span></div>'
    :'<span class="small muted">勾选作品作为选题证据</span>';
  const reference='<details class="panel creation-reference bench-ref" '+(b.accountId?'open':'')+'><summary>从别人的作品找依据 <span class="small muted">账号 '+accounts.length+' · 已选 '+selectedCount+' 条作品</span></summary><div class="space">'+
    '<section><h3>同类账号库</h3><form id="bench-account-form" class="bench-add"><input name="url" type="text" inputmode="url" autocomplete="off" spellcheck="false" required placeholder="粘贴抖音账号主页链接，或整段分享文字" aria-label="账号主页链接"><button type="submit">保存账号</button></form>'+(accounts.length?accountRows:'<p class="small muted">保存同类账号后，采集作品与真实热评。</p>')+'</section>'+
    '<section class="space"><div class="row spread"><h3>作品榜'+(selAccount?' · '+esc(benchName(selAccount)):'')+'</h3><button class="ghost small-button" data-action="refresh">刷新</button></div><div class="bench-foot">'+sortBar+'<button class="small-button" id="bench-gen" data-action="bench-topics" '+(selectedCount?'':'disabled')+'>生成选题（'+selectedCount+'）</button></div>'+worksHtml+'</section></div></details>';
  return benchTypePanel()+benchDrawerShell()+benchLinkPanel()+benchAnglesSection()+
    '<div class="bench-split">'+benchStage()+'<aside id="creation-results">'+creationPool(topics)+'</aside></div>'+
    reference+creationHistory();

}


// ---- 多平台矩阵（#/matrix）：平台能力 → 账号 → 从已审片项目发起任务 → 任务列表 ----
const matrixStatusLabels={draft:'草稿',planned:'演练已生成',queued:'排队中',running:'发布中',submitting:'提交中',awaiting_confirmation:'待本人确认',published:'已发布',failed:'发布失败',needs_action:'需要处理',rejected:'未通过校验',cancelled:'已取消'};
const matrixAccountStatusLabels={connected:'已连接',pending:'待验收',needs_action:'需要处理',disconnected:'未连接',unimplemented:'未接入',active:'已连接',needs_refresh:'需重新登录',expired:'会话已过期',disabled:'已停用',none:'未登录',qr_pending:'等待扫码'};
const matrixPlatformNames={douyin:'抖音',kuaishou:'快手',bilibili:'B站',xiaohongshu:'小红书',wechat_channels:'视频号'};
function matrixPlatformName(p){return matrixPlatformNames[p]||p||'平台未知';}
function matrixCaps(d){return d&&Array.isArray(d.items)?d.items:[];}
function matrixAccounts(d){return d&&Array.isArray(d.items)?d.items:[];}
function matrixTasks(d){return d&&Array.isArray(d.items)?d.items:[];}
function matrixCapState(c){const v=String(c?.status??'').toLowerCase();if(v==='connected')return['已接入','ok'];if(v==='pending')return['待验收','gold'];return['未接入','warn'];}
function matrixTaskPill(st){const cls=st==='published'?'ok':['failed','rejected','cancelled'].includes(st)?'error':st==='needs_action'?'warn':['queued','running','planned','submitting','awaiting_confirmation'].includes(st)?'gold':'';return pill(matrixStatusLabels[st]||st||'状态未知',cls);}
function renderMatrix(m,boot){
  const caps=m.caps||[];
  const accounts=m.accounts||[];
  const capRows=caps.length?`<div class="caps">${caps.map(c=>{const [label,cls]=matrixCapState(c);return `<div class="cap"><span>${esc(matrixPlatformName(c.platform))}${c.name&&c.name!==c.platform?' · '+esc(c.name):''}</span>${pill(label,cls)}${c.message?`<span class="small muted">　${esc(c.message)}</span>`:''}</div>`;}).join('')}</div>`:notice('未取得平台能力状态：/api/matrix/capabilities 没有返回数据。接入状态一律以后端真实返回为准，不做推测。')+(m.capsError?`<p class="small muted">${esc(m.capsError)}</p>`:'');
  const capLabelOf=platform=>{const c=caps.find(x=>x.platform===platform);return c?matrixCapState(c)[0]:'未接入';};
  const addable=caps.filter(c=>matrixCapState(c)[0]!=='未接入');
  const accountRows=accounts.length?accounts.map(a=>{
    const st=a.status||a.login_state||'';
    const cls=st==='connected'||st==='active'?'ok':st==='pending'?'gold':['needs_action','disconnected','unimplemented','expired','disabled','needs_refresh'].includes(st)?'warn':'';
    const connected=capLabelOf(a.platform)!=='未接入';
    return `<div class="bench-row"><div class="bench-main"><strong>${esc(a.display_name||a.account_label||'名称未取得')}</strong> ${pill(matrixPlatformName(a.platform))} ${pill(matrixAccountStatusLabels[st]||st||'状态未知',cls)}<div class="bench-sub">${a.last_verified_at?'最近实测 '+esc(stamp(a.last_verified_at)):'尚未实测'}</div></div><div class="bench-actions">${connected?`<button class="small-button" data-action="matrix-login" data-id="${esc(a.id)}">登录 / 重新登录</button>`:'<span class="small muted">平台未接入，暂不能登录</span>'}</div></div>`;
  }).join(''):empty('还没有平台发布账号','用下方表单添加；未接入的平台只显示状态，不会提供假登录入口。');
  const addForm=`<select name="platform" aria-label="平台" ${addable.length?'':'disabled'}>${addable.map(c=>`<option value="${esc(c.platform)}">${esc(matrixPlatformName(c.platform))}（${esc(matrixCapState(c)[0])}）</option>`).join('')}</select><input name="display_name" type="text" required placeholder="账号显示名（本人命名）" aria-label="账号显示名"><button class="primary" type="submit" ${addable.length?'':'disabled'}>添加账号</button>${addable.length?'':'<span class="small muted">暂无可接入的平台，请等待后端接入适配器。</span>'}`;
  const candidates=(boot.projects||[]).filter(p=>p.reviewed_artifact_id);
  const p=m.project;
  const pack=p?.publication_pack;
  let launchHtml;
  if(!candidates.length)launchHtml=notice('还没有已审片的项目。先在开始创作完成审片，再发起矩阵发布。');
  else{
    const targets=accounts.map(a=>{
      const connected=capLabelOf(a.platform)!=='未接入';
      return `<div class="matrix-target"><label class="check"><input type="checkbox" name="matrix-account" value="${esc(a.id)}" ${connected?'':'disabled'}> ${esc(matrixPlatformName(a.platform))} · ${esc(a.display_name||a.account_label||'名称未取得')} ${pill(matrixAccountStatusLabels[a.status||a.login_state||'']||a.status||a.login_state||'状态未知',a.status==='active'?'ok':'')}</label>${connected?'':`<p class="small muted">该平台尚未接入，不能作为投放目标。</p>`}<div class="matrix-payload"><label class="field"><span>平台专用标题（必填）</span><input name="title-${esc(a.id)}" type="text" maxlength="160" value="${esc(pack?.selected_title||'')}" ${connected?'':'disabled'}></label><label class="field"><span>话题，逗号分隔</span><input name="tags-${esc(a.id)}" type="text" value="${esc((pack?.tags||[]).join('，'))}" ${connected?'':'disabled'}></label><label class="field span-all"><span>平台文案</span><textarea name="caption-${esc(a.id)}" rows="4" ${connected?'':'disabled'}>${esc(pack?.caption||'')}</textarea></label><label class="field"><span>封面说明</span><input name="cover-${esc(a.id)}" type="text" ${connected?'':'disabled'}></label><label class="field"><span>定时发布（可选，本机时间）</span><input name="scheduled-${esc(a.id)}" type="datetime-local" ${connected?'':'disabled'}></label></div></div>`;
    }).join('');
    launchHtml=`<form id="matrix-task-form"><label class="field"><span>已审片项目</span><select name="matrix-project">${candidates.map(x=>`<option value="${esc(x.id)}" ${m.projectId===x.id?'selected':''}>${esc(x.title)}</option>`).join('')}</select></label>
    ${m.projectError?notice('项目详情读取失败：'+m.projectError):p?`<div class="rule"><span class="fact-label">发布对象</span>成片 ${esc(p.reviewed_artifact_id||'未取得')} · 定稿版本 ${esc(p.confirmed_version_id||'未取得')}${p.draft_changed?' · 有新草稿未确认（任务仍绑定已确认版本）':''}</div>`:'<p class="small muted">正在读取项目详情…</p>'}
    ${accounts.length?targets:notice('还没有平台账号，先在上方添加。')}
    <label class="check space"><input type="checkbox" name="plan_only" checked> 只创建演练任务，不真实发布</label>
    <p class="small muted">演练（plan）只上传草稿并核对发布表单，不点击发布；即使取消勾选，任务也必须演练通过并由本人勾选确认才会真实提交。创建任务不会直接发布。</p>
    <button class="primary" type="submit" ${p&&!m.projectError&&accounts.length?'':'disabled'}>创建矩阵任务</button></form>`;
  }
  const taskRows=m.tasks.length?m.tasks.map(t=>{
    const acc=accounts.find(x=>x.id===t.platform_account_id);
    const name=acc?`${matrixPlatformName(acc.platform)} · ${acc.display_name||acc.account_label||'账号'}`:(t.platform?matrixPlatformName(t.platform):'账号未取得');
    const msg=t.result_message||t.last_error||t.error||(t.status==='rejected'?'平台未接入，未创建发布':'');
    const when=t.created_at?stamp(t.created_at):'';
    const sched=t.scheduled_for?('定时 '+stamp(t.scheduled_for)):'';
    let actions='';
    if(t.status==='draft')actions=`<button class="small-button" data-action="matrix-plan" data-id="${esc(t.id)}">生成发布演练（不发布）</button>`;
    else if(['planned','awaiting_confirmation'].includes(t.status))actions=`<label class="check"><input type="checkbox" name="matrix-confirm" value="${esc(t.id)}" id="matrix-confirm-${esc(t.id)}">本人确认演练无误，同意发布</label><button class="small-button primary" id="matrix-submit-${esc(t.id)}" data-action="matrix-submit" data-id="${esc(t.id)}" disabled>确认并发布</button>`;
    else if(t.status==='needs_action')actions='<span class="small">需要处理：请在平台侧完成验证后重新发起，系统不会自动重试。</span>';
    const url=t.platform_url?`<a class="link" href="${esc(t.platform_url)}" target="_blank" rel="noopener noreferrer">平台作品 ↗</a>`:'';
    return `<div class="bench-row"><div class="bench-main"><strong>${esc(name)}</strong> ${matrixTaskPill(t.status)}<div class="bench-sub">任务 ${esc(t.id)}${when?' · '+esc(when):''}${sched?' · '+esc(sched):''}</div>${msg?`<div class="bench-sub error-text">${esc(msg)}</div>`:''}${url?`<div class="bench-sub">${url}</div>`:''}</div><div class="bench-actions">${actions}</div></div>`;
  }).join(''):'<p class="small muted">该项目还没有矩阵任务。勾选目标账号并创建。</p>';
  return `<div class="eyebrow">MULTI-PLATFORM MATRIX</div><h1>多平台矩阵发布</h1><p class="lead">一个成片，多个平台；每个平台独立演练、独立确认、独立验收，任一平台失败不影响其他平台。只显示后端真实返回的状态，不做模拟。</p>
  <div class="workspace"><div class="stack">
  <section class="panel"><h2>平台能力状态</h2>${capRows}<p class="small muted space">“已接入”表示适配器存在且经真实验收；“待验收”表示接口存在但未经真实账号验收；其余为未接入。</p></section>
  <section class="panel"><div class="row spread"><h2>平台账号 <span class="small muted">${accounts.length}</span></h2><button class="ghost small-button" data-action="refresh">刷新</button></div>${m.accountsError?notice('账号列表读取失败：'+m.accountsError):accountRows}<form id="matrix-account-form" class="bench-add space">${addForm}</form></section>
  <section class="panel"><h2>从已审片项目发起矩阵任务</h2>${launchHtml}</section>
  <section class="panel"><h2>任务列表${p?' · '+esc(p.title||''):''}</h2>${m.tasksError?notice('任务列表读取失败：'+m.tasksError):taskRows}</section>
  </div><aside><section class="panel"><h3>发布闸门</h3><div class="rule"><span class="fact-label">演练不发布</span>plan 只上传草稿并核对表单，不会点击发布。</div><div class="rule"><span class="fact-label">本人确认</span>真实提交必须勾选确认并带 confirm=true；未确认不发送。</div><div class="rule"><span class="fact-label">成功判据</span>只有平台返回作品 ID 与链接才算成功；只显示真实返回的链接。</div><div class="rule"><span class="fact-label">需要处理</span>needs_action 只提示本人在平台侧处理，不自动重试。</div><a class="link small" href="#/create">← 返回开始创作</a></section></aside></div>`;
}

// ---- 发布与复盘（#/feedback）：发布数据与评论回流 → 复盘 ----
// 只显示后端真实返回：指标缺失一律 “—”，不估算；分类与洞察以后端返回为准。
const feedbackCollectLabels={queued:'排队中',running:'采集运行中',pending:'排队中',succeeded:'采集完成',failed:'采集失败',needs_action:'需要处理',cancelled:'已取消'};
function fbNum(v){return v===null||v===undefined?'—':(typeof v==='number'?new Intl.NumberFormat('zh-CN').format(v):String(v));}
function fbMetric(w,key){
  const flat={play:'play_count',like:'like_count',comment:'comment_count',favorite:'collect_count',share:'share_count'}[key];
  if(w&&w[flat]!==undefined&&w[flat]!==null)return w[flat];
  if(w&&w.stats&&w.stats[key]!==undefined&&w.stats[key]!==null)return w.stats[key];
  return null;
}
function fbWorkUrl(w){return (w&&(w.source_url||w.canonical_url))||'';}
function fbLatestSnapshotAt(workId,snapshots){const list=(snapshots&&snapshots[workId])||[];let latest='';for(const s of list){const t=s.collected_at||s.created_at||'';if(t&&t>latest)latest=t;}return latest;}
function fbEvidenceCount(i){if(typeof i.evidence_count==='number')return i.evidence_count;if(Array.isArray(i.evidence_ids))return i.evidence_ids.length;if(Array.isArray(i.findings))return i.findings.reduce((n,x)=>n+(Array.isArray(x.evidence_ids)?x.evidence_ids.length:0),0);return null;}
function fbInsightLinks(i,works){
  const urls=Array.isArray(i.platform_work_urls)?i.platform_work_urls.filter(Boolean):[];
  if(urls.length)return urls;
  const ids=Array.isArray(i.platform_work_ids)?i.platform_work_ids:[];
  return ids.map(id=>{const w=(works||[]).find(x=>x.id===id);return w?fbWorkUrl(w):'';}).filter(Boolean);
}
// “生成下一条选题并立项”：仅 candidate/confirmed 且带 project_id 的洞察可立项；文案与跳转集中在此，便于桩件断言。
function fbStartTopicVisible(i){return Boolean(i&&['candidate','confirmed'].includes(i.status)&&i.project_id);}
function fbStartTopicToast(r){return r&&r.reused?'已复用已有项目，选题已挂接到该项目。':'新选题已立项，进入这条作品继续写稿。';}
function fbProjectUrl(pid){return '#/project/'+encodeURIComponent(pid)+'/script';}
function fbCollectPill(collect){if(!collect||!collect.status)return pill('未采集');const st=collect.status,cls=st==='succeeded'?'ok':st==='failed'?'error':st==='needs_action'?'warn':['queued','running','pending'].includes(st)?'gold':'';return pill(feedbackCollectLabels[st]||st,cls);}
function fbWorkCard(w,f){
  const url=fbWorkUrl(w);
  const latest=fbLatestSnapshotAt(w.id,f.snapshots);
  const metrics=[['play','播放','▷'],['like','点赞','♡'],['comment','评论','◌'],['favorite','收藏','☆'],['share','转发','↗']].map(([k,l,icon])=>{const v=fbMetric(w,k);return `<div class="metric-tile"><span class="metric-icon" aria-hidden="true">${icon}</span><strong>${esc(fbNum(v))}</strong><span>${l}${v===null?' · 未取得':''}</span></div>`;}).join('');
  return `<article class="fb-card"><div class="row spread"><strong>${esc(w.title||'标题未取得')}</strong>${pill(matrixPlatformName(w.platform))}</div>
  <div class="bench-sub">${url?`<a class="link" href="${esc(url)}" target="_blank" rel="noopener noreferrer">平台作品 ↗</a>`:'<span>链接未取得</span>'} · ${w.published_at?esc(stamp(w.published_at)):'发布时间未取得'}</div>
  <div class="metric-grid fb-metrics">${metrics}</div>
  <div class="bench-sub">${latest?'最近快照 '+esc(stamp(latest)):'尚无指标快照；执行“采集最新数据”后更新。'}</div>
  <div class="actions"><button class="small-button" data-action="feedback-comments" data-id="${esc(w.id)}">评论</button></div>
  </article>`;
}
function fbInsightCard(i,works,f){
  const links=fbInsightLinks(i,works);
  const ev=fbEvidenceCount(i);
  const confirmed=i.status==='confirmed'||i.verified===true||i.confirmed===true;
  const canStart=fbStartTopicVisible(i);
  const pending=Boolean(f&&Array.isArray(f.startTopicPending)&&f.startTopicPending.includes(i.id));
  const evItems=Array.isArray(i.evidence_items)?i.evidence_items:[];
  const evidenceHtml=evItems.length?`<details class="fb-evidence"><summary>查看 ${evItems.length} 条证据明细（可点回原评论）</summary>${evItems.map(e=>e.kind==='comment_feedback'
    ?`<div class="fb-evidence-item"><div class="row spread"><strong class="small">${esc(e.author_name||'—')}</strong><span class="small muted">${esc(e.category||'未分类')}</span></div><p class="small">${esc(e.content||'')}</p><div class="bench-sub">评论ID ${esc(e.platform_comment_id||'—')}${e.source_url?` · <a class="link" href="${esc(e.source_url)}" target="_blank" rel="noopener noreferrer">查看原评论 ↗</a>`:''}</div></div>`
    :`<div class="fb-evidence-item"><div class="row spread"><strong class="small">指标快照 ${esc(e.metric_key||'')}</strong><span class="small">${esc(fbNum(e.value))}</span></div>${e.collected_at?`<div class="bench-sub">${esc(stamp(e.collected_at))}</div>`:''}</div>`).join('')}</details>`:'';
  const candId=i.candidate_knowledge_id||'';
  const propId=i.thesis_proposal_id||'';
  const candInfo=i.candidate_card||null;
  const reflowHtml=(candId||propId)?`<div class="fb-reflow">
    ${candId?`<div class="fb-reflow-row"><span class="small muted">这条复盘沉淀成一个新认知</span>${candInfo&&candInfo.endorsement==='accepted'?`<a class="link" href="#/knowledge/${encodeURIComponent(candId)}">${candInfo.auto?'已自动沉淀进积累（可撤销）→':'已沉淀进积累（下轮写稿会带上）→'}</a>`:`<a class="link" href="#/knowledge/${encodeURIComponent(candId)}">去背书（认同后自动进积累）→</a>`}</div>`:''}
    ${propId?`<div class="fb-reflow-row"><span class="small muted">命题可能要改</span><a class="link" href="#/restart?view=overview">去对比命题修订候选 →</a></div>`:''}
  </div>`:'';
  return `<article class="fb-insight"><div class="row spread"><h3>${esc(i.title||'洞察标题未取得')}</h3>${pill(confirmed?'本人已确认':'模型推断 · 待验证',confirmed?'ok':'warn')}</div>
  <p class="small">${esc(i.body||'')}</p>${reflowHtml}
  <div class="bench-sub">证据 ${ev===null?'—':esc(fbNum(ev))} 条${links.length?' · '+links.map(u=>`<a class="link" href="${esc(u)}" target="_blank" rel="noopener noreferrer">平台作品 ↗</a>`).join(' '):''}${i.created_at?' · '+esc(stamp(i.created_at)):''}</div>${evidenceHtml}${canStart?`<div class="actions"><button class="small-button" data-action="feedback-start-topic" data-id="${esc(i.id)}" ${pending?'disabled':''}>${pending?'立项中…':'生成下一条选题并立项'}</button></div>`:''}</article>`;
}
function fbCommentsHtml(workId,f){
  const data=(f.comments||{})[workId];
  const cls=(f.classify||{})[workId];
  const headBtn=`<div class="row spread"><span class="small muted">只显示真实回流评论，未取得的数据标“—”。</span><button class="small-button" data-action="feedback-classify" data-id="${esc(workId)}">分类评论需求</button></div>`;
  let clsHtml='';
  if(cls){
    if(cls.error)clsHtml=notice('分类未完成：'+cls.error);
    else{
      const counts=cls.counts||cls.categories||cls.summary;
      clsHtml=`<div class="badge-row">${pill('分类结果（以后端真实分类为准）')}${counts&&typeof counts==='object'?Object.entries(counts).map(([k,v])=>pill(String(k)+' '+String(v))).join(''):''}</div>${cls.message?`<p class="small muted">${esc(cls.message)}</p>`:''}${(!counts&&!cls.message)?'<p class="small muted">分类已提交；详细标签以评论条目上的分类标记为准。</p>':''}`;
    }
  }
  if(!data)return headBtn+clsHtml+notice('评论尚未加载；点作品卡片上的“评论”读取真实回流评论。');
  const items=data.items||[];
  const total=data.total!==undefined?data.total:items.length;
  const body=items.length?items.map(c=>{
    const cat=c.category||c.label||c.classification||'';
    return `<div class="bench-comment"><div class="row spread"><strong class="small">${esc(c.author_name||c.author||'—')}</strong><span class="small muted">♡ ${esc(fbNum(c.like_count))} · ${esc(stamp(c.collected_at||c.created_at)||'时间未取得')}</span></div><p class="small bench-comment-note">${esc(c.content||'')}</p>${cat?`<div class="badge-row">${pill(cat,'gold')}</div>`:''}</div>`;
  }).join(''):notice('该作品还没有回流评论；执行“采集最新数据”后刷新查看。');
  const page=data.page||1;
  const pager=data.page_size?`<div class="bench-foot"><span class="small muted">第 ${page} 页 · 共 ${esc(fbNum(total))} 条</span><div class="actions"><button class="small-button" data-action="feedback-comment-page" data-id="${esc(workId)}" data-page="${String(page-1)}" ${page<=1?'disabled':''}>上一页</button><button class="small-button" data-action="feedback-comment-page" data-id="${esc(workId)}" data-page="${String(page+1)}" ${items.length<(data.page_size||20)?'disabled':''}>下一页</button></div></div>`:'';
  return headBtn+clsHtml+`<p class="small muted space">共 ${esc(fbNum(total))} 条 · 按平台返回顺序显示。</p>`+body+pager;
}
function renderFeedback(f,boot){
  if(new URLSearchParams(location.hash.split('?')[1]||'').get('view')!=='review')return renderReleaseHub(f,boot);
  const projects=boot.projects||[];
  const ov=f.overview||{};
  const works=f.works||[];
  const collect=f.collect;
  const busy=collect&&['queued','running','pending'].includes(collect.status);
  const latestSnap=(ov.latest_snapshots||[]).map(s=>s.collected_at||s.created_at||'').sort().pop()||'';
  const reg=f.register||{};
  const regNotice=reg.message?`<div class="rule space"><span class="fact-label">${reg.needs_action?'需要本人处理':'登记结果'}</span>${esc(reg.message)}${reg.platform_work&&fbWorkUrl(reg.platform_work)?` <a class="link" href="${esc(fbWorkUrl(reg.platform_work))}" target="_blank" rel="noopener noreferrer">平台作品 ↗</a>`:''}</div>`:'';
  const toolbar=`<section class="panel"><div class="row spread"><div class="fb-toolbar"><label class="field"><span>复盘项目</span><select name="feedback-project" aria-label="复盘项目" ${projects.length?'':'disabled'}>${projects.map(p=>`<option value="${esc(p.id)}" ${f.projectId===p.id?'selected':''}>${esc(p.title)}</option>`).join('')}</select></label><button class="primary" data-action="feedback-collect" ${f.projectId&&!busy?'':'disabled'}>${busy?'采集中…':'采集最新数据'}</button>${fbCollectPill(collect)}</div></div>${collect&&collect.stage?`<p class="small muted space">任务阶段：${esc(collect.stage)}</p>`:''}${collect&&collect.error?`<p class="small error-text space">${esc(collect.error)}</p>`:''}${!projects.length?notice('还没有作品记录。登记已发布作品要先关联一条作品：到开始创作新建一条（直接写稿或导入已有视频都行），再回这里粘贴抖音链接登记。'):''}<div class="fb-register"><label class="field"><span>登记本人已发布作品（工作台不代发）</span><input name="feedback-register-url" type="text" inputmode="url" autocomplete="off" spellcheck="false" placeholder="粘贴抖音作品链接（支持 /video/… 、?modal_id=… 或分享文字）" ${f.projectId?'':'disabled'}></label><button class="small-button primary" data-action="feedback-register" ${f.projectId?'':'disabled'}>登记并采集</button><p class="small muted space">登记后作品关联到上方所选项目，只采集公开数据；链接已关联别的作品时会提示核对，不会静默改动。播放量等未取得时标“—”，不估算。</p></div>${regNotice}</section>`;
  const summary=(works.length||ov.comments_count!==undefined)?`<div class="fb-summary"><div class="fb-tile"><strong>${esc(fbNum(Array.isArray(ov.platform_works)?ov.platform_works.length:ov.platform_works!==undefined?ov.platform_works:works.length))}</strong><span>平台作品</span></div><div class="fb-tile"><strong>${esc(fbNum(ov.comments_count))}</strong><span>回流评论</span></div><div class="fb-tile"><strong>${esc(fbNum(ov.insights_count!==undefined?ov.insights_count:(f.insights||[]).length))}</strong><span>复盘洞察</span></div><div class="fb-tile"><strong>${latestSnap?esc(stamp(latestSnap)):'—'}</strong><span>最近快照时间</span></div></div>`:'';
  const worksSection=`<section class="panel"><div class="row spread"><h2>平台作品 <span class="small muted">${works.length}</span></h2></div>${f.errors&&f.errors.works?notice('作品列表读取失败：'+f.errors.works):works.length?`<div class="fb-works">${works.map(w=>fbWorkCard(w,f)).join('')}</div>`:empty('还没有可复盘的作品','点上方“登记并采集”粘贴本人已在抖音发布的作品链接；工作台不代发，只登记并采集公开数据。未取得的数据显示“—”，不做估算。')}</section>`;
  const insights=f.insights||[];
  const insightSection=`<section class="panel"><div class="row spread"><h2>复盘洞察 <span class="small muted">${insights.length}</span></h2><button class="small-button primary" data-action="feedback-insight" ${f.projectId?'':'disabled'}>生成复盘洞察</button></div>${f.errors&&f.errors.insights?notice('洞察读取失败：'+f.errors.insights):insights.length?insights.map(i=>fbInsightCard(i,works,f)).join(''):'<p class="small muted">还没有复盘洞察。采集数据后点“生成复盘洞察”；洞察默认标记为“模型推断 · 待验证”，需本人核对。</p>'}</section>`;
  return `<div class="eyebrow">PUBLISH & REVIEW</div><h1>发布与复盘</h1><p class="lead">发布数据与评论回流的真实记录，只做有依据的复盘；单条作品数据不构成确定因果。</p>${releaseTabs()}${releaseReceipts(f.release)}${notice('指标只显示真实已取得值，未取得显示“—”，不做估算；评论与洞察均以来源记录为准。')}${toolbar}${f.errors&&f.errors.overview?notice('总览读取失败：'+f.errors.overview):summary}${worksSection}${insightSection}`;
}

// ---- 首页搜索：作品标题 + 第二大脑认知（真实数据，不造结果） ----
let homeSearchTimer=null;
document.addEventListener('input',event=>{
  if(event.target.id!=='home-search')return;
  const q=event.target.value.trim();
  clearTimeout(homeSearchTimer);
  homeSearchTimer=setTimeout(()=>homeSearch(q),240);
});
let lastHomeQuery='';
async function homeSearch(q){
  const box=document.getElementById('home-results'),body=document.getElementById('home-body');
  if(!box||!body)return;
  if(!q){box.hidden=true;box.innerHTML='';body.hidden=false;return;}
  body.hidden=true;box.hidden=false;box.innerHTML='<p class="small muted">正在搜索…</p>';
  lastHomeQuery=q;
  const key=q.toLowerCase();
  const projects=(state.boot.projects||[]).filter(p=>String(p.title||'').toLowerCase().includes(key)).slice(0,8);
  let knowledge=[];
  let knowledgeError='';
  try{const d=await api('/knowledge?'+new URLSearchParams({q}));knowledge=(d.items||[]).slice(0,6);}catch(e){knowledgeError=String(e&&e.message||e||'搜索失败');}
  if(document.getElementById('home-results')!==box)return;
  const hit=(kind,href,title,sub)=>'<a class="home-hit" href="'+href+'"><span class="home-hit-kind">'+kind+'</span><div><strong>'+esc(title)+'</strong><small>'+esc(sub)+'</small></div></a>';
  const projHits=projects.length?projects.map(p=>{
    const n=p.next_action||{};const step=['source','script','pack','shoot','edit','publish'].includes(n.step)?n.step:'script';
    return hit('作品',purl(p.id,step),p.title,(stages[p.stage]||p.stage||'')+' · '+stamp(p.updated_at));
  }).join(''):'<p class="small muted">没有匹配的作品。</p>';
  const knHits=knowledgeError
    ?'<p class="small error-text">我的经验库这次没读出来：'+esc(knowledgeError)+'（不是「没有匹配」，可重试）</p>'
      +'<p><button class="small-button" type="button" data-action="retry-home-search">重试</button></p>'
    :(knowledge.length?knowledge.map(x=>hit('认知','#/knowledge/'+encodeURIComponent(x.id),x.title,String(x.claim||'').replace(/\*\*/g,'').slice(0,48)+'…')).join(''):'<p class="small muted">我的经验库里没有匹配的内容。</p>');
  box.innerHTML='<section class="home-section"><div class="home-sec-head"><h2>作品</h2><span class="home-sec-note">'+projects.length+' 条</span></div><div class="home-hits">'+projHits+'</div></section>'+
    '<section class="home-section"><div class="home-sec-head"><h2>我的经验库</h2><span class="home-sec-note">'+knowledge.length+' 条</span></div><div class="home-hits">'+knHits+'</div></section>';
  box.scrollIntoView({behavior:'smooth',block:'start'});
}
document.addEventListener('click',event=>{
  const btn=event.target.closest('[data-action="retry-home-search"]');
  if(!btn)return;
  event.preventDefault();
  const input=document.getElementById('home-search');
  const q=(input&&input.value.trim())||lastHomeQuery||'';
  if(q)homeSearch(q);
});

// ---- 首页快速开始：拖动排序（自定义指针拖拽，顺序只记在本机浏览器）----
// 不用原生 HTML5 drag：Chromium 在圆角+渐变+overflow:hidden 的卡片上会残留半透明拖拽影像，清不掉。
// 这里自己实现：原位置留一个占位框，拖起的卡片变成 fixed 跟随指针，松手再插回去。
let homeDrag=null,homeSuppressClick=false;

function homeSaveOrder(){
  const rail=document.querySelector('.home-rail');if(!rail)return;
  const order=Array.from(rail.querySelectorAll('.home-tile:not(.home-tile-placeholder)')).map(x=>x.dataset.tile).filter(Boolean);
  try{localStorage.setItem(HOME_TILE_ORDER_KEY,JSON.stringify(order));}catch(e){}
}

// CSP style-src 'self' 禁止一切内联 style；拖拽的临时定位改走 CSSOM 规则（不触发 CSP）。
function dragCssSet(selector,props){
  let rule=null;
  const sheet=document.styleSheets[0];
  if(!sheet)return;
  for(let i=0;i<sheet.cssRules.length;i++){
    const r=sheet.cssRules[i];
    if(r.selectorText===selector){rule=r;break;}
  }
  if(!rule){const i=sheet.insertRule(selector+'{}',0);rule=sheet.cssRules[i];}
  for(const k in props)rule.style[k]=props[k];
}

function homeDragBegin(ev){
  const st=homeDrag;if(!st||st.live)return;
  const el=st.el,rect=st.rect;
  const ph=document.createElement('div');
  ph.className='home-tile home-tile-placeholder';
  ph.setAttribute('aria-hidden','true');
  dragCssSet('.home-tile-placeholder',{width:rect.width+'px',height:rect.height+'px'});
  el.parentNode.insertBefore(ph,el);

  el.classList.add('is-dragging');
  dragCssSet('.home-tile.is-dragging',{position:'fixed',left:rect.left+'px',top:rect.top+'px',
    width:rect.width+'px',height:rect.height+'px',margin:'0',transform:'translate(0px,0px)'});

  st.live=true;st.ph=ph;
  document.body.classList.add('home-dragging');
  if(navigator.vibrate)try{navigator.vibrate(8);}catch(e){}
}

function homeDragMove(ev){
  const st=homeDrag;if(!st)return;
  const x=ev.clientX,y=ev.clientY;
  if(!st.live){
    if(Math.abs(x-st.startX)<7&&Math.abs(y-st.startY)<7)return;
    if(ev.cancelable)ev.preventDefault();
    homeDragBegin(ev);
  }else if(ev.cancelable){
    ev.preventDefault();
  }
  if(!st.live||!st.ph)return;

  dragCssSet('.home-tile.is-dragging',{transform:'translate('+(x-st.dx-st.rect.left)+'px,'+(y-st.dy-st.rect.top)+'px)'});

  const under=document.elementFromPoint(x,y);
  const over=under&&under.closest?under.closest('.home-rail .home-tile'):null;
  if(!over||over===st.el||over===st.ph)return;
  const rail=st.ph.parentNode;
  if(!rail)return;
  const all=Array.from(rail.querySelectorAll(':scope > .home-tile'));
  const pi=all.indexOf(st.ph),oi=all.indexOf(over);
  if(pi<0||oi<0)return;
  const r=over.getBoundingClientRect();
  // 用「当前索引 vs 目标索引」判断该不该动，只在方向需要时才搬。
  // 若拿相邻兄弟做判断，指针停在卡片中线上时会被最后一帧挪回去（拖完回原位）。
  const mid=r.left+r.width/2;
  if(x<mid-2){
    if(pi>oi)rail.insertBefore(st.ph,over);
  }else if(x>mid+2){
    if(pi<oi)rail.insertBefore(st.ph,over.nextSibling);
  }
}

function homeDragFinish(){
  const st=homeDrag;if(!st)return;
  homeDrag=null;
  document.body.classList.remove('home-dragging');
  if(st.live){
    st.el.classList.remove('is-dragging');
    dragCssSet('.home-tile.is-dragging',{position:'',left:'',top:'',width:'',height:'',margin:'',transform:''});
    if(st.ph&&st.ph.parentNode)st.ph.replaceWith(st.el);
    homeSaveOrder();
    homeSuppressClick=true;
    setTimeout(()=>{homeSuppressClick=false;},300);
    toast('入口顺序已记到这台浏览器。');
  }else if(st.ph&&st.ph.parentNode){
    st.ph.remove();
  }
}

document.addEventListener('pointerdown',event=>{
  if(event.button!==undefined&&event.button!==0)return;
  const tile=event.target.closest&&event.target.closest('.home-rail .home-tile');
  if(!tile||tile.classList.contains('home-tile-placeholder'))return;
  homeDragFinish();
  homeDrag={el:tile,startX:event.clientX,startY:event.clientY,dx:event.clientX-tile.getBoundingClientRect().left,dy:event.clientY-tile.getBoundingClientRect().top,rect:tile.getBoundingClientRect(),live:false,pointerId:event.pointerId};
});
document.addEventListener('pointermove',homeDragMove,{passive:false});
document.addEventListener('pointerup',()=>homeDragFinish());
document.addEventListener('pointercancel',()=>homeDragFinish());
window.addEventListener('blur',()=>homeDragFinish());
document.addEventListener('click',event=>{
  if(!homeSuppressClick)return;
  event.preventDefault();event.stopPropagation();homeSuppressClick=false;
},true);

// t107：找选题拆角度的 need_login 入口；复用 app.js 的全局扫码弹窗，登录后重跑同一条链接。
document.addEventListener('click',event=>{
  const btn=event.target.closest('#bench-douyin-login');
  if(!btn)return;
  event.preventDefault();
  const form=document.getElementById('bench-link-form');
  if(!form)return;
  if(document.getElementById('douyin-login-open')){document.getElementById('douyin-login-open').click();return;}
  if(typeof showDouyinLogin==='function'&&typeof benchLinkSubmit==='function'){
    showDouyinLogin(()=>benchLinkSubmit(form));
    setTimeout(()=>{const open=document.getElementById('douyin-login-open');if(open)open.click();},80);
  }else{
    btn.insertAdjacentHTML('afterend','<span class="small muted">登录弹窗未就绪，请刷新后再试。</span>');
  }
});


