'use strict';
// Secondary controls remember the current local form while results update independently.
const creation={catalog:null,history:[],pains:[],knowledge:[],topics:[],goal:'attention',context:'',structure:'',subtype:'',shareTo:'',contentType:'',elements:[],reasons:[],knowledge_ids:[],expanded:false,poll:null,loading:false,historyKind:'all',drawer:'',picked:null,direction:'',voices:null,voicesLoading:false,works:null,worksLoading:false,worksSort:'share_rate',axis:null,growthStats:null,knowledgeTouched:false,profileEditing:false,cssDone:false};
const creationLabels={creation_topics:'选题生成',creation_pain:'痛点探索',creation_draft:'选题成稿',creation_recreate:'参考二创',creation_polish:'口语化润色',rewrite:'定制改写',prepare_content:'视频拆解',prepare_reference:'参考整理'};
async function creationLoad(){
  const results=await Promise.all([creation.catalog?Promise.resolve(creation.catalog):api('/creation/catalog'),api('/creation/pain-points'),api('/creation/history'),api('/knowledge'),api('/growth/overview').catch(()=>null)]);
  creation.catalog=results[0];creation.pains=results[1].items;creation.history=results[2].items;
  creation.knowledge=results[3].items.filter(x=>x.content_use==='available'&&x.lifecycle==='active'&&!['rejected','reference_only'].includes(x.endorsement));
  /* 2026-11-17（t29 P7/P8/P13）：读成长主线（growth_goal root）与统一内容池分布。
     后端选题/写稿已自动接 axis_context＋persona_block（施工八 L8），这里只负责把它显形：
     命题维度条、选题卡主线追问、空状态镜子句。读不到就按「无主线」处理，不报错不编造。 */
  const ov=results[4];
  if(ov){const root=(ov.goals||[]).find(g=>g.level==='root'&&!g.archived);
    creation.axis=root?{main_goal:{id:root.id,title:String(root.title||''),description:String(root.description||'')},thesis:String(root.thesis||'').trim()}:null;
    creation.growthStats=ov.stats||null;}
  /* P9：经验库默认带上相关认知——首次进创作页且用户没动过选择时，默认勾选最多3条
     （认同优先、亲身验证优先、新近优先），写稿默认从你的积累长出来（信念×口吻）。 */
  if(!creation.knowledgeTouched&&!creation.knowledge_ids.length&&creation.knowledge.length){
    const rank=x=>(x.endorsement==='accepted'?2:0)+(x.validation==='personal_case'?1:0);
    creation.knowledge_ids=creation.knowledge.slice().sort((a,b)=>rank(b)-rank(a)||String(b.updated_at||'').localeCompare(String(a.updated_at||''))).slice(0,3).map(x=>x.id);
  }
  if(creation.history.some(j=>['queued','running'].includes(j.status)))creationPoll();
}
function creationGoal(){return creation.catalog.goals.find(g=>g[0]===creation.goal);}
/* 2026-11-17（t29）：少量布局样式走 CSSOM（CSP style-src 'self'，不写内联 style）。 */
function creationEnsureCss(){if(creation.cssDone)return;creation.cssDone=true;ipAddCssRules([
  '.creation-axis{border:1px solid var(--border);border-left:3px solid var(--gold);border-radius:10px;padding:10px 14px;margin:0 0 14px;background:var(--tint)}',
  '.creation-axis p{margin:4px 0 0}',
  '.creation-axis-check{margin:6px 0 0;padding:6px 10px;border-left:2px solid var(--gold);background:var(--tint);border-radius:0 6px 6px 0}',
  '.creation-mirror{margin:10px auto 0;max-width:560px;text-align:left}',
  '.creation-mirror blockquote{margin:8px 0;padding:8px 12px;border-left:3px solid var(--gold);background:var(--tint);border-radius:0 8px 8px 0;font-size:13px;line-height:1.7}',
  '.creation-profile-fill{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-top:6px}',
  '.creation-profile-fill input{flex:1 1 140px;min-width:0;padding:7px 10px}'
]);}
/* P7/P8：命题/主线维度条。主线未建立时引导立轴，不编造命题。 */
function creationAxisStrip(){
  const a=creation.axis;
  if(!a||!a.main_goal||!a.main_goal.title)return '<div class="creation-axis small muted">还没有立主线：这次内容先从你想讲的事找角度。<a class="link" href="#/restart?view=graph">去立主线 ↗</a></div>';
  const thesis=a.thesis||a.main_goal.description||'';
  return '<div class="creation-axis"><div class="row spread"><strong>✦ 服务于我的命题 / 主线</strong><span class="small muted">默认维度 · 选题与写稿已接主线和经验库</span></div><p class="small"><strong>'+esc(a.main_goal.title)+'</strong></p>'+(thesis?'<p class="small muted">'+esc(thesis.length>140?thesis.slice(0,140)+'…':thesis)+'</p>':'<p class="small muted">命题待结晶：在立轴引导里写下你信的那一句。</p>')+'<p class="small muted">不纯追爆款：先问这条是否服务你的主线；主线外的热点也能写，但不再是默认。</p></div>';
}
/* P12：行业/受众缺失时就地引导补（走既有 PATCH /api/profile），不再只剩一个跳设置的链接。 */
function creationProfileRow(){
  const p=state.boot.profile,missing=!p.industry||!p.audience;
  if(!missing)return '<div class="creation-profile small muted">'+esc(p.industry)+'<span> / </span>'+esc(p.audience)+' <a class="link" href="#/settings">完善档案 ↗</a></div>';
  if(!creation.profileEditing)return '<div class="creation-profile small muted">'+esc(p.industry||'行业待补')+'<span> / </span>'+esc(p.audience||'受众待补')+' <button type="button" class="link small-button" data-action="creation-profile-edit">就地补上 →</button> <a class="link" href="#/settings">完善档案 ↗</a></div>';
  return '<div class="creation-profile small muted">补一句就好，这次创作立刻用上：<div class="creation-profile-fill"><input type="text" id="creation-profile-industry" maxlength="120" placeholder="你的行业，如：宠物上门喂养" value="'+esc(p.industry||'')+'" aria-label="行业"><input type="text" id="creation-profile-audience" maxlength="200" placeholder="说给谁听，如：第一次养猫的年轻家庭" value="'+esc(p.audience||'')+'" aria-label="受众"><button type="button" class="primary small-button" data-action="creation-profile-save">保存</button><button type="button" class="ghost small-button" data-action="creation-profile-cancel">收起</button></div></div>';
}
/* P13：空状态镜子句——与施工七 M1 同一套规则口径（content_index 分布＋独特主张＋缺口），
   数据来自 /growth/overview 的 stats（同源 content_index.summary）与本地认知卡，全部本地算、不耗额度。
   M1 的 HTTP 路由（/api/brain/mirror）尚未挂载（app.py 不在本任务写域）；挂载后可直接切换。 */
function creationMirrorText(){
  const s=creation.growthStats,cats=Object.entries((s&&s.by_category)||{}).sort((a,b)=>b[1]-a[1]);
  const total=(s&&s.fragments)||0;
  if(!total&&!creation.knowledge.length)return '你还什么都没记。先记一条你昨天说过的一句话（原话就行），我再把你照给你看。';
  const top=cats.filter(x=>x[1]>0).slice(0,2);
  const x=top.length?top.map(([n,c],i)=>i===0&&total?n+'（'+c+' 条，占 '+Math.round(100*c/total)+'%）':n+'（'+c+' 条）').join('、'):'还没有成型的分类';
  const adv=creation.knowledge.find(k=>k.endorsement==='accepted'&&k.validation==='personal_case')||creation.knowledge.find(k=>k.endorsement==='accepted')||creation.knowledge[0];
  const claim=String((adv&&(adv.claim||adv.body))||'').replace(/\s+/g,' ').trim();
  const y=claim?'你那句「'+(claim.length>46?claim.slice(0,46)+'…':claim)+'」是别人给不了的':'还没看到能撑住的那一句——先记一条只有你能说的话';
  const empty=cats.filter(x=>x[1]===0).map(x=>x[0]);
  const z=empty.length?'「'+empty.slice(0,2).join('」「')+'」你一条都没碰过':(top.length?'「'+top[top.length-1][0]+'」还可以再往深里走':'还看不出缺口');
  return '集中在：'+x+'。你最独特的是：'+y+'。一直没碰的是：'+z+'。';
}
/* P9：经验库面板（默认展开＋默认带上相关认知）。legacy 作曲器与 bench 工作台注入共用同一份 markup。 */
function creationEvidenceHTML(){
  const c=creation;
  return '<details class="creation-evidence" open><summary>结合我的经验库'+(c.knowledge_ids.length?' · 已带上 '+c.knowledge_ids.length+' 条':'')+' <span class="small muted">默认展开 · 内容从你的积累长出来</span></summary>'+(c.knowledge.length?'<p class="small muted space">默认带上最相关的 '+Math.min(3,c.knowledge.length)+' 条认知（认同/亲身验证优先），可增删，最多 10 条；写稿按「信念×口吻」融入你的积累。</p><div class="creation-knowledge">'+c.knowledge.slice().sort((a,b)=>(c.knowledge_ids.includes(b.id)?1:0)-(c.knowledge_ids.includes(a.id)?1:0)).map(x=>'<label class="check"><input type="checkbox" name="knowledge_ids" value="'+esc(x.id)+'" '+(c.knowledge_ids.includes(x.id)?'checked':'')+'><span>'+esc(x.title)+'</span></label>').join('')+'</div>':'<p class="small muted space">先在我的经验库核对认知状态，并允许用于写稿。</p>')+'</details>';
}
function creationComposer(){
  creationEnsureCss();
  const c=creation,p=state.boot.profile,cat=c.catalog,structure=cat.structures.find(x=>x.id===c.structure),goal=creationGoal();
  const hasAxis=Boolean(c.axis&&c.axis.main_goal&&c.axis.main_goal.title);
  return '<section class="panel creation-focus"><div class="creation-kicker"><span class="creation-orbit" aria-hidden="true">✦</span><span>把想法变成下一条内容</span></div><h2>这次，你想让内容做什么？</h2><form id="creation-topic-form">'+
    creationAxisStrip()+
    '<div class="creation-goals" role="group" aria-label="创作目标'+(hasAxis?'（都服务于你的主线）':'')+'">'+cat.goals.map(([id,label,hint],i)=>'<label class="creation-goal '+(id===c.goal?'selected':'')+'"><input type="radio" name="goal" value="'+id+'" '+(id===c.goal?'checked':'')+'><span class="goal-num">0'+(i+1)+'</span><strong>'+({attention:'关注角度',conversion:'成交选题',pain:'痛点选题'}[id]||label)+'</strong><small>'+hint+(hasAxis&&id==='attention'?' · 不纯追爆款，先服务主线':'')+'</small></label>').join('')+'</div>'+
    '<label class="field"><span>'+ (c.goal==='pain'?'想进一步了解什么？（选填）':'这次想讲的问题、经历或产品')+'</span><textarea name="context" rows="3" maxlength="12000" '+(c.goal==='pain'?'':'required')+' placeholder="'+(c.goal==='pain'?'例如：初次养宠的家庭，决定前最担心什么？':hasAxis?'围绕你的主线，这次想讲哪件事？例如：客户总问价格贵在哪里，我想把真实的服务过程讲清楚。':'例如：客户总问价格贵在哪里，我想把真实的服务过程讲清楚。')+'">'+esc(c.context)+'</textarea></label>'+
    creationProfileRow()+
    '<div class="creation-method-heading"><strong>这条内容怎么讲</strong><span class="small muted">可选 · 不选则自动匹配</span></div><div class="creation-structures" role="group" aria-label="脚本结构">'+
    cat.structures.map(x=>'<label class="creation-structure '+(x.id===c.structure?'selected':'')+'"><input type="radio" name="structure" value="'+x.id+'" '+(x.id===c.structure?'checked':'')+'><b aria-hidden="true">'+x.icon+'</b><strong>'+x.label+'</strong><small>'+x.hint+'</small></label>').join('')+'</div><details class="creation-methods"><summary>细分脚本与'+(c.goal==='conversion'?'成交理由':c.goal==='pain'?'参考依据':'关注元素')+' <span class="small muted">'+esc(structure?structure.label:'自动匹配')+'</span></summary><div class="creation-method-body"><button type="button" class="ghost small-button" data-action="creation-auto">使用自动匹配</button>'+
    (structure?'<label class="field space"><span>细分方法</span><select name="subtype"><option value="">不限定</option>'+structure.subtypes.map(([id,label,hint])=>'<option value="'+id+'" '+(id===c.subtype?'selected':'')+'>'+label+' · '+hint+'</option>').join('')+'</select></label>':'')+
    (c.goal!=='pain'?'<div class="field space"><span>'+(c.goal==='attention'?'关注元素（最多3项）':'选择你的理由（最多5项）')+'</span><div class="creation-chips">'+(c.goal==='attention'?cat.elements:cat.reasons).map(([id,label,hint])=>{const key=c.goal==='attention'?'elements':'reasons';return '<label title="'+esc(hint)+'"><input type="checkbox" name="'+key+'" value="'+id+'" '+(c[key].includes(id)?'checked':'')+'><span>'+label+'</span></label>';}).join('')+'</div></div>':'')+
    creationEvidenceHTML()+'</div></details>'+
    '<div class="creation-submit"><span class="small muted">'+(c.goal==='pain'?'先给 3 个待验证假设':'先给 3 条，选中再写')+'</span><button class="primary" type="submit" '+(!state.boot.capabilities.llm?'disabled':'')+'>'+(c.goal==='pain'?'探索用户痛点':'生成'+(c.goal==='conversion'?'成交':'关注')+'选题')+' <span aria-hidden="true">→</span></button></div>'+
    (!state.boot.capabilities.llm?'<p class="small muted space">生成模型待连接。<a class="link" href="#/settings">查看服务连接</a>；已有作品仍可继续编辑。</p>':'')+'</form><div id="creation-status" role="status">'+creationStatus()+'</div></section>';
}
function creationStatus(){const j=creation.history.find(x=>['queued','running'].includes(x.status));return j?'<div class="creation-progress"><span class="processing-mark" aria-hidden="true">◌</span><span>'+esc(creationLabels[j.kind]||'创作任务')+' · '+esc(j.stage)+'，可以离开页面</span></div>':'';}
function creationTopicCard(t,i){
  const c=creation.catalog,brief=t.brief_snapshot,goal=c.goals.find(x=>x[0]===brief?.goal),pid=t.project_id||t.data?.project_id;
  const shareTo=t.share_to||brief?.share_to||'';
  const axis=creation.axis&&creation.axis.main_goal&&creation.axis.main_goal.title?creation.axis:null;
  const axisCheck=axis?'<p class="small muted creation-axis-check">这条符合你的主线「'+esc(axis.main_goal.title)+'」吗？'+(axis.thesis?' 你的命题：'+esc(axis.thesis.length>60?axis.thesis.slice(0,60)+'…':axis.thesis):'')+'</p>':'';
  return '<article class="creation-topic"><span class="creation-topic-num">'+String(i+1).padStart(2,'0')+'</span><div><div class="badge-row">'+pill(goal?goal[1]:t.origin==='bench'?'作品选题':'选题')+pill('待验证','warn')+(t.basis?pill('角度：'+t.basis):'')+(shareTo?pill('想让人转给：'+shareTo):'')+'</div><h3>'+esc(t.title)+'</h3><p class="small muted">'+esc(t.angle||t.thesis||'补充自己的切入点，再整理成稿。')+'</p>'+axisCheck+'<details><summary>依据与论点</summary><p class="small muted space">'+esc(t.thesis||'论点待补')+'</p><p class="small muted">参考的作品 '+(t.bench_work_ids||[]).length+' 条 · 我的经验库 '+(t.knowledge_ids||[]).length+' 条'+(t.pain_point_id?' · 来自痛点假设':'')+'</p>'+(brief?'<p class="small muted">本次想法：'+esc(brief.context||'未补充')+'</p>':'')+'</details><div class="creation-topic-actions"><button class="primary creation-oneclick" data-action="bench-oneclick-draft" data-id="'+esc(t.id)+'" '+(!state.boot.capabilities.llm?'disabled title="写作模型未连接：先到个人档案与服务连接检查"':'')+'><span aria-hidden="true">✦</span> 一键出稿</button><button class="ghost small-button" data-action="bench-use-topic" data-id="'+esc(t.id)+'">'+(pid?'继续这条作品':'选这条，去写稿')+' →</button></div></div></article>';
}
function creationPool(topics){
  creation.topics=topics;
  const visible=creation.expanded?topics:topics.slice(0,3);
  return '<section class="panel creation-pool"><div class="row spread"><h2>待拍清单 <span class="small muted">'+topics.length+'</span></h2><span class="small muted">选择一个就出发</span></div>'+
    (visible.length?visible.map(creationTopicCard).join(''):creationEmptyMirror())+
    (topics.length>3?'<button class="ghost small-button space" data-action="creation-more">'+(creation.expanded?'收起':'查看其余 '+(topics.length-3)+' 条选题')+'</button>':'')+'</section>'+creationPains();
}
/* P13：空状态不再只有一句「先找到一个值得说的点」——给镜子句（M1 同口径本地规则）＋命题引导。
   2026-11-25：原「问问知识库」入口随对话页一起取消。 */
function creationEmptyMirror(){
  const axis=creation.axis&&creation.axis.main_goal&&creation.axis.main_goal.title?creation.axis:null;
  return '<div class="creation-empty"><span aria-hidden="true">◇</span><h3>先找到一个值得说的点</h3><p class="small muted">生成的选题和从别人作品来的选题都会留在这里。</p><div class="creation-mirror"><p class="small muted">镜子照一下你的积累：</p><blockquote>'+esc(creationMirrorText())+'</blockquote>'+(axis?'<p class="small muted">你的主线是「'+esc(axis.main_goal.title)+'」——从你自己的经历讲它，比追热点更像你。</p>':'<p class="small muted">还没有主线？<a class="link" href="#/restart?view=graph">去立主线 ↗</a> 立完再回来，选题会围着它长。</p>')+'</div></div>';
}
function creationPains(){const items=creation.pains.filter(x=>!x.topic_id);return items.length?'<details class="panel space creation-pains" '+(creation.goal==='pain'?'open':'')+'><summary>待验证痛点 <span class="small muted">'+items.length+'</span></summary><p class="small muted space">模型提出的假设，结合真实客户反馈再判断。</p>'+items.slice(0,creation.expanded?50:3).map(x=>'<article class="creation-pain"><h3>'+esc(x.title)+'</h3><p class="small muted">'+esc(x.angle)+'</p><button class="small-button" data-action="creation-adopt" data-id="'+esc(x.id)+'">加入待拍清单 →</button></article>').join('')+'</details>':'';}
function creationHistory(){return '<details class="panel creation-history"><summary>创作记录 <span id="creation-history-count" class="small muted">最近 '+creation.history.length+' 次</span></summary><label class="field space"><span class="sr-only">按创作类型筛选</span><select id="creation-history-filter"><option value="all">全部创作</option>'+Object.entries(creationLabels).map(([id,label])=>'<option value="'+id+'" '+(creation.historyKind===id?'selected':'')+'>'+label+'</option>').join('')+'</select></label><div id="creation-history-items">'+creationHistoryItems()+'</div></details>';}
function creationHistoryItems(limit){const items=creation.history.filter(j=>creation.historyKind==='all'||j.kind===creation.historyKind),shown=limit?items.slice(0,limit):items;return shown.length?shown.map(j=>'<div class="creation-history-row"><div><strong class="small">'+esc(creationLabels[j.kind]||j.kind)+'</strong><span class="small muted"> · '+stamp(j.created_at)+'</span><div class="small '+(j.status==='failed'?'error-text':'muted')+'">'+esc(j.status==='failed'?j.error:jobLabels[j.status]||j.status)+(j.count?' · '+j.count+' 条':'')+'</div></div><button class="ghost small-button" data-action="creation-history" data-id="'+esc(j.id)+'">查看</button></div>').join('')+(limit&&items.length>limit?'<p class="small muted">其余 '+(items.length-limit)+' 条在「找选题 → 创作记录」里查看。</p>':''):'<p class="small muted">还没有这类创作记录。</p>';}
function creationRemember(form){const v=new FormData(form);creation.context=v.get('context')||'';creation.subtype=v.get('subtype')||'';creation.shareTo=v.get('share_to')||'';for(const key of ['elements','reasons','knowledge_ids'])if(form.querySelector('[name="'+key+'"]'))creation[key]=v.getAll(key);}
function creationRerenderComposer(keepOpen){const target=document.querySelector('.creation-focus');if(!target)return;const bench=Boolean(document.getElementById('bench-stage'));const html=bench&&typeof benchStage==='function'?benchStage():creationComposer();target.outerHTML=html;const m=document.querySelector('.creation-methods');if(m&&keepOpen!==false)m.open=true;}
async function creationSubmit(form){
  creationRemember(form);
  if(String(creation.context||'').trim().length>3000)throw Error('内容偏长，建议精简后再试。');
  if(creation.goal!=='pain'&&!String(creation.context||'').trim())throw Error('先在左边挑一个素材，或者在输入框里写一句这次想讲的事。');
  const key=creation.goal==='attention'?'elements':'reasons';if(creation[key].length>(key==='elements'?3:5))throw Error('请精简方法选择，突出一个重点。');
  await api('/creation/topics','POST',{goal:creation.goal,context:creation.context,structure:creation.structure,subtype:creation.subtype,elements:creation.elements,reasons:creation.reasons,knowledge_ids:creation.knowledge_ids,work_ids:Object.keys(state.bench.sel).slice(0,10),count:3,...(creation.shareTo?{share_to:creation.shareTo}:{}),...(creation.contentType?{content_type:creation.contentType}:{})});
  toast('任务已提交，结果会保存在待拍清单或待验证痛点中。');await creationRefresh();creationPoll();
}
async function creationRefresh(){
  if(creation.loading)return;creation.loading=true;
  try{const [h,t,p]=await Promise.all([api('/creation/history'),api('/bench/topics'),api('/creation/pain-points')]);creation.history=h.items;creation.pains=p.items;state.bench.topics=t.items;
    const pool=document.getElementById('creation-results');if(pool)pool.innerHTML=creationPool(t.items);
    const history=document.getElementById('creation-history-items');if(history)history.innerHTML=creationHistoryItems();
    const count=document.getElementById('creation-history-count');if(count)count.textContent='最近 '+creation.history.length+' 次';
    const status=document.getElementById('creation-status');if(status)status.innerHTML=creationStatus();
  }finally{creation.loading=false;}
}
function creationPoll(){if(creation.poll)return;creation.poll=setTimeout(async()=>{creation.poll=null;try{await creationRefresh();if(creation.history.some(j=>['queued','running'].includes(j.status)))creationPoll();}catch(e){toast(e.message);}},2000);}
/* t140 主权可感知：取本项目最近一次成功创作建议＋三信号条——委托 app.js 全局助手
   （sovereigntyLatestSuggestion/sovereigntyStrip，恒加载；edit/publish 步不载 creation-ui，
   助手放 app.js 才能全路由共用）。逻辑单一来源，无重复实现。 */
function creationLatestSuggestion(p){
  return typeof sovereigntyLatestSuggestion==='function'?sovereigntyLatestSuggestion(p):null;
}
function creationSovereignty(sug,lintButton){
  return typeof sovereigntyStrip==='function'?sovereigntyStrip(sug,lintButton):'';
}
function creationWritingPanel(p){
  const hasDraft=p.draft.some(x=>x.text.trim()),busy=p.jobs.some(j=>j.kind.startsWith('creation_')&&['queued','running'].includes(j.status));
  return '<div class="creation-writing"><h3>写初稿</h3><div class="creation-write-actions"><button class="'+(!hasDraft?'primary':'')+'" data-action="creation-write" data-mode="draft" '+(!state.boot.capabilities.llm||busy?'disabled':'')+'><span aria-hidden="true">✦</span> 帮我搭一版初稿骨架</button><button data-action="creation-write-dialog" data-mode="recreate" '+(!state.boot.capabilities.llm||busy?'disabled':'')+'><span aria-hidden="true">⇄</span> 参考二创</button><button data-action="creation-write" data-mode="polish" '+(!hasDraft||!state.boot.capabilities.llm||busy?'disabled':'')+'><span aria-hidden="true">≈</span> 口语化润色</button></div>'+creationSovereignty(creationLatestSuggestion(p),true)+'<details class="creation-help"><summary>这版骨架怎么用？</summary><p class="small muted">'+(busy?'正在生成建议，原稿仍可编辑。':'骨架由模型搭，「你为什么信」留给你自己填：先生成一版，再逐段改成你自己的话；原文与历史版本都会保留。')+'</p></details>'+(state.boot.capabilities.llm?'':'<p class="small error-text">写作模型未连接，生成按钮暂不可用；请到「个人档案 → 服务连接」检查后再试。</p>')+'</div>';
}
async function creationAction(el){
  const action=el.dataset.action;
  if(action==='creation-continue'){document.getElementById('modal').close();location.hash=purl(el.dataset.project);return;}
  if(action==='creation-auto'){creation.structure='';creation.subtype='';return creationRerenderComposer();}
  /* P12：行业/受众就地补——走既有 PATCH /api/profile（creation_service.update_profile），
     保存后立刻刷新 boot.profile 与创作表单，这次创作就用新档案。 */
  if(action==='creation-profile-edit'){creation.profileEditing=true;return creationRerenderComposer();}
  if(action==='creation-profile-cancel'){creation.profileEditing=false;return creationRerenderComposer();}
  if(action==='creation-profile-save'){
    const industry=(document.getElementById('creation-profile-industry')?.value||'').trim();
    const audience=(document.getElementById('creation-profile-audience')?.value||'').trim();
    if(!industry&&!audience){toast('先填一句行业或受众，再保存。');return;}
    const body={};if(industry)body.industry=industry;if(audience)body.audience=audience;
    const r=await api('/profile','PATCH',body);
    const prof=r.profile||r;state.boot.profile={...state.boot.profile,...(prof.industry!==undefined?{industry:prof.industry}:{}),...(prof.audience!==undefined?{audience:prof.audience}:{})};
    if(industry)state.boot.profile.industry=industry;if(audience)state.boot.profile.audience=audience;
    creation.profileEditing=false;creationRerenderComposer();toast('档案已补上，这次创作就用新的行业/受众。');return;
  }
  if(action==='creation-more'){creation.expanded=!creation.expanded;document.getElementById('creation-results').innerHTML=creationPool(creation.topics);return;}
  if(action==='creation-adopt'){await api('/creation/pain-points/'+encodeURIComponent(el.dataset.id)+'/adopt','POST',{});toast('已加入待拍清单，仍标记为待验证。');return creationRefresh();}
  if(action==='creation-write-dialog')return modal('参考二创', '<form id="creation-write-form"><input type="hidden" name="mode" value="recreate"><label class="field"><span>参考原文</span><textarea name="reference" rows="8" maxlength="24000" required placeholder="粘贴想参考的原文；它会作为资料单独保留。"></textarea></label><label class="field"><span>这次想保留或改变什么？（选填）</span><textarea name="context" rows="3" maxlength="12000"></textarea></label><button class="primary" type="submit">生成改写建议</button></form>');
  if(action==='creation-write'){if(state.dirty)await saveDraft();await api('/projects/'+state.project.id+'/writing-suggestions','POST',{mode:el.dataset.mode,draft_revision:state.project.draft_revision});toast('正在生成建议，完成后可以比较。');return route();}
  if(action==='creation-history'){
    const j=await api('/jobs/'+encodeURIComponent(el.dataset.id)),s=j.result?.suggestion,items=j.result?.items||[],brief=j.inputs?.brief||j.inputs?.snapshot?.brief||j.inputs?.brief;
    return modal(creationLabels[j.kind]||'创作记录','<p class="small muted">'+stamp(j.created_at)+' · '+esc(jobLabels[j.status]||j.status)+'</p>'+(j.error?notice(j.error):'')+
      (items.length?items.map(x=>'<div class="rule"><h3>'+esc(x.title)+'</h3><p>'+esc(x.angle)+'</p><p class="small muted">待验证 · '+esc(x.thesis)+'</p></div>').join(''):'')+
      (s?'<p class="small muted">基于草稿第 '+s.base_draft_revision+' 版生成</p>'+creationSovereignty(s,Boolean(state.project&&s.project_id===state.project.id&&document.getElementById('script-full-text')))+s.parts.map(x=>'<div class="rule"><strong>'+esc(x.label)+'</strong><p class="raw-markdown">'+esc(x.text)+'</p></div>').join(''):'')+
      (s?.input_snapshot?'<details><summary>输入与参考原文</summary><p class="small muted space">本次想法：'+esc(s.input_snapshot.context||s.input_snapshot.brief?.context||'未补充')+'</p><p class="small muted">说话风格：'+esc(s.input_snapshot.profile?.style||'未填写')+'</p><blockquote class="raw-markdown">'+esc(s.input_snapshot.reference||s.input_snapshot.source?.text||'未添加参考原文')+'</blockquote></details>':brief?'<details><summary>本次创作简报</summary><p class="small muted">'+esc(brief.context||'未补充')+'</p><p class="small muted">'+esc(brief.profile?.industry||'')+' / '+esc(brief.profile?.audience||'')+'</p></details>':'')+
      (j.project_id&&j.project_id!=='owner'?'<button data-action="creation-continue" data-project="'+esc(j.project_id)+'">继续这条作品 →</button>':''));
  }
}
document.addEventListener('input',e=>{if(e.target.closest('#creation-topic-form'))creationRemember(e.target.form);});
document.addEventListener('change',e=>{
  const form=e.target.closest('#creation-topic-form');
  if(form){creationRemember(form);if(e.target.name==='goal'){creation.goal=e.target.value;creation.structure='';creation.subtype='';const m=document.querySelector('.creation-methods'),wasOpen=m?m.open:false;creationRerenderComposer(wasOpen);}
    if(e.target.name==='structure'){creation.structure=e.target.value;creation.subtype='';creationRerenderComposer();}
    if(['elements','reasons','knowledge_ids'].includes(e.target.name)){if(e.target.name==='knowledge_ids')creation.knowledgeTouched=true;const max={elements:3,reasons:5,knowledge_ids:10}[e.target.name];if(creation[e.target.name].length>max){e.target.checked=false;creationRemember(form);toast('最多选择 '+max+' 项，保留最相关的。');}}
  }
  if(e.target.id==='creation-history-filter'){creation.historyKind=e.target.value;document.getElementById('creation-history-items').innerHTML=creationHistoryItems();}
});
/* 2026-11-17（t29）： live 创作工作台（#/bench）的舞台由 workflow-ui.js 的 benchStage() 渲染，
   不在本任务写域。这里用 MutationObserver 在舞台出现/重渲染后注入三件东西，全部走既有
   data-action / name 约定与现有 class＋CSSOM，不改 workflow-ui.js/app.js：
   ① 命题/主线维度条（form 顶部）② profile 就地补（想讲什么之后）③ 经验库面板（默认展开、默认带上）。
   注入的 knowledge_ids 复选框在 #creation-topic-form 内，creationRemember/既有 change 监听原样生效。 */
function creationEnhanceBench(){
  const stage=document.getElementById('bench-stage');
  if(!stage||stage.dataset.t29Enhanced)return;
  const form=stage.querySelector('#creation-topic-form');
  if(!form)return;
  stage.dataset.t29Enhanced='1';
  creationEnsureCss();
  form.insertAdjacentHTML('afterbegin',creationAxisStrip());
  const shareRow=form.querySelector('.bench-share-row');
  if(shareRow)shareRow.insertAdjacentHTML('beforebegin',creationProfileRow());
  const run=form.querySelector('.bench-run');
  if(run)run.insertAdjacentHTML('beforebegin',creationEvidenceHTML());
}
if(typeof MutationObserver==='function'){
  const creationBenchObserver=new MutationObserver(()=>{try{creationEnhanceBench();}catch(e){}});
  creationBenchObserver.observe(document.documentElement,{childList:true,subtree:true});
}
creationEnhanceBench();
