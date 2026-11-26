'use strict';
/* 封面工坊：审片页独立区块。不进包装风格列表——封面挂进风格列表会触发「换封面即重渲成片」。 */
function coverState(p){
  if(!state.cover||state.cover._pid!==p.id){
    state.cover={_pid:p.id,templateId:(p.cover&&p.cover.recommendation?p.cover.recommendation.template_id:''),fields:null,extras:null,person:null};
  }
  return state.cover;
}
function coverSync(){
  const f=document.getElementById('cover-form'),s=state.cover;
  if(!f||!s)return;
  s.fields={title:f.elements.title.value,subtitle:f.elements.subtitle.value,badge:f.elements.badge.value,account:f.elements.account.value};
  s.extras={};
  const tpl=((state.project&&state.project.cover&&state.project.cover.styles)||[]).find(x=>x.id===s.templateId);
  (tpl&&tpl.fields_extra||[]).forEach(x=>{
    const el=f.elements['extra-'+x.key];if(!el)return;
    s.extras[x.key]=x.type==='lines'?el.value.split('\n').map(v=>v.trim()).filter(Boolean).slice(0,3):el.value.trim();
  });
  s.person={on:f.elements['person-on'].checked,x:f.elements['person-x'].value,
    outline:f.elements['person-outline']?f.elements['person-outline'].value:'auto'};
}
function coverExtraFields(tpl,fields){
  return (tpl.fields_extra||[]).map(x=>{
    if(x.type==='lines'){
      const v=Array.isArray(fields[x.key])?fields[x.key].join('\n'):(fields[x.key]||'');
      return '<label class="field cover-wide"><span>'+esc(x.label)+'</span><textarea name="extra-'+esc(x.key)+'" rows="3">'+esc(v)+'</textarea></label>';
    }
    return '<label class="field cover-half"><span>'+esc(x.label)+'</span><input name="extra-'+esc(x.key)+'" maxlength="'+(x.max||30)+'" value="'+esc(fields[x.key]||'')+'"></label>';
  }).join('');
}
function coverVideoRef(p){
  const arts=(p.artifacts||[]).filter(a=>['clean_video','packaged_video'].includes(a.kind)&&a.script_version_id===p.confirmed_version_id);
  const latest=arts.find(a=>a.id===p.latest_artifact_id)||arts[0];
  if(latest)return {artifact_id:latest.id,label:latest.filename||'当前成片'};
  const assets=(p.assets||[]).filter(a=>a.info&&a.info.width);
  if(assets.length)return {asset_id:assets[assets.length-1].id,label:assets[assets.length-1].filename||'项目素材'};
  return null;
}
function coverMediaUrl(url){
  /* 取帧/形象图都要带手机访问码；frame 的旧 token 参数改名为 ftoken，避免和访问码同名。 */
  const u=String(url||'');
  if(!u||u.startsWith('data:'))return u;
  return media(u.replace(/([?&])token=/,'$1ftoken='));
}
function coverFramesBlock(fr){
  if(!fr)return '';
  if(fr.loading)return '<div class="cover-frame-pick"><p class="small">正在从视频抽帧、挑正脸清晰帧…约 10–30 秒</p></div>';
  if(fr.error)return '<div class="cover-frame-pick"><p class="small error-text">'+esc(fr.error)+'</p><button type="button" class="small-button" data-action="cover-avatar-video">重试</button></div>';
  const list=fr.candidates||[];
  if(!list.length)return '<div class="cover-frame-pick"><p class="small muted">'+esc(fr.message||'没挑到正脸清晰的帧，换一段视频或上传照片。')+'</p><div class="row"><button type="button" class="small-button" data-action="cover-avatar-video">重新挑一次</button><button type="button" class="small-button" data-action="cover-avatar-close">收起</button></div></div>';
  const i=Math.min(fr.index||0,list.length-1),c=list[i];
  return '<div class="cover-frame-pick">'+
    '<div class="row spread"><strong class="small">挑中的帧 · 第 '+(i+1)+'/'+list.length+' 个</strong><span class="small muted">'+esc(fr.video||'')+(c.time_ms!=null?' · '+fmt(c.time_ms):'')+'</span></div>'+
    '<span class="cover-frame-img"><img src="'+esc(coverMediaUrl(c.url||c.thumb))+'" alt="视频里挑中的真人帧"></span>'+
    '<p class="small muted">'+esc(c.reason||'')+'</p>'+
    '<div class="row">'+
      '<button type="button" data-action="cover-avatar-use" data-n="'+c.n+'" data-token="'+esc(fr.token||'')+'">用这一帧做形象</button>'+
      '<button type="button" class="small-button" data-action="cover-avatar-next">换下一帧</button>'+
      '<button type="button" class="small-button" data-action="cover-avatar-close">收起</button>'+
    '</div></div>';
}
function coverAvatarBlock(c,ref,localOnly=false){
  const a=c.avatar||{};
  const g=(a.probe&&a.probe.generation)||{available:false,detail:''};
  const fr=state.cover.frames||null;
  return '<div class="cover-avatar">'+
    '<div class="row spread"><strong class="small">我的形象</strong>'+
      (a.exists?'<span class="cover-avatar-pill">已保存 · 可复用</span>':'<span class="cover-avatar-pill warn">还没有形象</span>')+'</div>'+
    (a.exists?
      '<div class="cover-avatar-row"><span class="cover-avatar-thumb"><img src="'+esc(coverMediaUrl(a.url))+'" alt="我的形象"></span>'+
        '<div class="cover-avatar-meta"><p class="small">'+esc(a.message||'')+'</p>'+
        '<p class="small muted">来源：'+esc(a.source_label||'上传照片')+(a.source_note?' · '+esc(a.source_note):'')+' · '+esc(a.engine_label||'')+(a.elapsed_ms?' · 抠图 '+a.elapsed_ms+'ms':'')+(a.warning?' · 降级：'+esc(a.warning):'')+'</p></div></div>'
      :'<p class="small muted">默认从项目视频里取真人帧：封面里的人和视频里是同一个人、长得一致；选定一帧后按形象 ID 复用，不会每次随机挑。</p>')+
    '<div class="cover-avatar-sources">'+
      '<button type="button" class="small-button" data-action="cover-avatar-video" '+(ref?'':'disabled')+'>① 从视频取帧'+(ref?'':'（需先有成片/素材）')+'</button>'+
      '<label class="cover-upload"><input type="file" accept="image/jpeg,image/png,image/webp" data-avatar-input> ② 上传照片</label>'+
      (localOnly?'':'<button type="button" class="small-button" data-action="cover-avatar-ai">③ AI 生成虚构形象</button>')+
    '</div>'+
    '<p class="small muted">'+esc(state.cover.aiNote||g.detail||'')+'</p>'+
    '<span class="small muted" id="cover-avatar-status"></span>'+
    coverFramesBlock(fr)+
    '<p class="small muted">三个来源都按内容去重：同一张图或同一帧重复用不会重算；只有换一个新形象才需要再选一次。选定后，所有封面都复用这个形象。</p>'+
  '</div>';
}
function coverBillingLine(c){
  const b=c.billing||{};
  if(b.balance_credits==null)return '';
  return '<p class="small muted">额度：剩余 '+esc(String(b.balance_credits))+'（赠送 '+esc(String(b.granted_credits||'0'))+' / 购买 '+esc(String(b.purchased_credits||'0'))+'）'+(b.cover_price_credits?' · 解锁一套 '+esc(String(b.cover_price_credits))+' 额度':'')+' · '+esc(b.plan_label||'免费')+'</p>';
}
function renderCoverStudio(p){
  const c=p.cover||{};
  const styles=c.styles||[];
  const s=coverState(p);
  const direct=p.mode==='direct';
  const defaultOk=c.default_available!==false;
  const avail=!!c.available;
  const busy=(p.jobs||[]).find(j=>j.kind==='cover_render'&&['queued','running'].includes(j.status));
  const artifacts=(p.artifacts||[]).filter(a=>['clean_video','packaged_video'].includes(a.kind)
    && (direct||a.script_version_id===p.confirmed_version_id));
  const latest=artifacts.find(a=>a.id===p.latest_artifact_id)||artifacts[0]||null;
  const current=latest&&latest.cover_id?(p.artifacts||[]).find(a=>a.id===latest.cover_id&&a.kind==='cover'):null;
  const titleDefault=String((p.cover&&p.cover.defaults&&p.cover.defaults.title)||p.title||'').slice(0,40);
  const tpl=styles.find(x=>x.id===s.templateId)||styles[0]||null;
  if(tpl)s.templateId=tpl.id;
  const fields=tpl?Object.assign({},tpl.fields_default,c.defaults,s.fields||{},s.extras||{}):{};
  const person=tpl?Object.assign({},tpl.person_default,s.person||{}):{};
  const avatar=c.avatar||{};
  const needAvatar=!!(tpl&&tpl.requires_person&&!avatar.exists);
  const rec=c.recommendation||{};
  const recName=(styles.find(x=>x.id===rec.template_id)||{}).name||'';
  const locked=!!(tpl&&tpl.paid&&!tpl.unlocked);
  const tplBlocked=!tpl||busy||!avail||locked||needAvatar||(direct?false:(p.draft_changed||!p.confirmed_version_id||!latest));
  const tile=x=>'<button type="button" class="cover-tile'+(x.id===s.templateId?' cover-tile-active':'')+'" data-action="cover-pick" data-id="'+esc(x.id)+'" aria-pressed="'+(x.id===s.templateId?'true':'false')+'">'+
    '<span class="cover-thumb"><img src="'+esc(x.preview_url)+'" alt="'+esc(x.name)+'封面示意" loading="lazy"></span>'+
    '<span class="cover-tile-name">'+esc(x.name)+'</span>'+
    (x.id===rec.template_id?'<span class="cover-rec-pill">推荐</span>':'')+
    (x.paid?(x.unlocked?'<span class="cover-unlocked-pill">已解锁</span>':'<span class="cover-lock-pill">未解锁</span>'):'')+
    '</button>';
  const defaultBlock='<div class="cover-default">'+
    '<div class="row spread"><h3><span class="pack-symbol" aria-hidden="true">▣</span> 封面工坊 · 抽帧封面</h3><span class="small muted">1080×1440 · 抽帧+标题 · 只需 ffmpeg</span></div>'+
    (defaultOk?'':'<p class="small error-text space">默认封面需要 ffmpeg：当前环境没找到。可先安装 ffmpeg，或到下方高级模板封面（若模板环境可用）。</p>')+
    '<p class="small muted">从成片第 3 秒抽一帧（失败回退首帧），把你的标题压在画面上；直剪作品不需要先确认稿件。</p>'+
    '<label class="field"><span>封面标题（留空用作品标题）</span><input id="cover-default-title" maxlength="40" value="'+esc(titleDefault)+'"></label>'+
    '<div class="cover-actions"><button type="button" class="primary" data-action="cover-default" '+(defaultOk&&!busy?'':'disabled')+'>生成封面（可靠）</button>'+
    (busy?'<span class="small muted">'+esc(busy.stage||'封面任务进行中')+'</span>':'')+'</div>'+
    (current?'<div class="cover-current"><div class="row spread"><strong class="small">当前成片封面 · '+esc(current.filename||'封面')+'</strong><a class="link small" href="'+media('/api/artifacts/'+current.id+'?download=1')+'">下载封面</a></div><a href="'+media('/api/artifacts/'+current.id)+'" target="_blank" rel="noopener"><img class="cover-preview-img" src="'+media('/api/artifacts/'+current.id)+'" alt="当前封面预览"></a><p class="small muted">封面已挂到上方这版成片，确认审阅后交付四件套自动带上；重新生成会替换挂接。</p></div>':'')+
    '</div>';
  const advanced='<details class="cover-advanced" open><summary>选择封面模板 · 一次解锁，长期复用</summary>'+
    (!avail?'<p class="small muted space">模板环境缺少：'+esc((c.missing||[]).join('、')||'模板库')+'；不影响上面的默认封面。</p>':'')+
    (styles.length?'<div class="cover-grid">'+styles.map(tile).join('')+'</div>':'<p class="small muted">模板库为空；默认封面仍可用。</p>')+
    coverBillingLine(c)+
    coverAvatarBlock(c,coverVideoRef(p),!!(tpl&&tpl.shared))+
    (locked?'<button type="button" class="small-button" data-action="cover-unlock" data-id="'+esc(tpl.id)+'">解锁此模板 · 一次解锁，长期复用</button>':'')+
    (rec.reason?'<p class="small muted">推荐「'+esc(recName)+'」：'+esc(rec.reason)+'。主标题用【】包裹的词会高亮。</p>':'')+
    (tpl?('<form id="cover-form" class="cover-form">'+
      '<label class="field"><span>主标题（≤12字；10–12字自动缩字号并换行）</span><input name="title" maxlength="40" value="'+esc(fields.title)+'"></label>'+
      '<label class="field"><span>副标题（≤18字）</span><input name="subtitle" maxlength="40" value="'+esc(fields.subtitle)+'"></label>'+
      '<label class="field cover-half"><span>角标</span><input name="badge" maxlength="20" value="'+esc(fields.badge)+'"></label>'+
      '<label class="field cover-half"><span>账号名</span><input name="account" maxlength="30" value="'+esc(fields.account)+'"></label>'+
      coverExtraFields(tpl,fields)+
      '<label class="check cover-person"><input type="checkbox" name="person-on" '+(person.on?'checked':'')+(tpl.requires_person?' disabled':'')+'> 人物层'+(tpl.requires_person?'（这套模板必须用人像）':'（未上传形象时用剪影占位）')+'</label>'+
      '<label class="field cover-person-x"><span>人物位置</span><select name="person-x">'+[['left','偏左'],['center','居中'],['right','偏右']].map(([v,l])=>'<option value="'+v+'" '+(person.x===v?'selected':'')+'>'+l+'</option>').join('')+'</select></label>'+
      '<label class="field cover-person-x"><span>人像描边</span><select name="person-outline">'+[['auto','模板默认'],['yellow','黄色贴纸边'],['white','白色贴纸边'],['none','去掉描边']].map(([v,l])=>'<option value="'+v+'" '+(person.outline===v?'selected':'')+'>'+l+'</option>').join('')+'</select></label>'+
      (needAvatar?'<p class="small error-text cover-wide">这套人像模板需要先上传「我的形象」。</p>':'')+
      (locked?'<p class="small muted cover-wide">这套模板未解锁：解锁后可用；不解锁也能用上面的默认封面。</p>':'')+
      '<div class="cover-actions"><button type="submit" class="primary" '+(tplBlocked?'disabled':'')+'>生成模板封面</button></div>'+
      '</form>'):'')+
    (tplBlocked&&!avail?'<p class="small muted">模板封面暂不可用，先使用默认封面。</p>':'')+
    '</details>';
  return '<section class="panel space cover-studio" aria-label="封面工坊">'+advanced+defaultBlock+'</section>';
}
async function coverAction(el){
  const p=state.project;if(!p)return;
  if(el.dataset.action==='cover-focus'){
    const studio=document.querySelector('.cover-studio');
    if(!studio)throw Error('封面工坊还没准备好，请稍后重试。');
    const box=studio.closest('details');
    if(box)box.open=true;
    studio.scrollIntoView({block:'center'});
    return;
  }
  if(el.dataset.action==='cover-default'){
    const input=document.getElementById('cover-default-title');
    const title=input?String(input.value||'').trim():'';
    const direct=p.mode==='direct';
    const arts=(p.artifacts||[]).filter(a=>['clean_video','packaged_video'].includes(a.kind)
      && (direct||a.script_version_id===p.confirmed_version_id));
    const latest=arts.find(a=>a.id===p.latest_artifact_id)||arts[0]||null;
    el.disabled=true;
    try{
      await api('/projects/'+p.id+'/cover-generate','POST',{cover_kind:'default',
        artifact_id:latest?latest.id:'',title});
      toast('默认封面任务已提交，完成后在这里预览与下载。');
      return route();
    }catch(e){el.disabled=false;toast('封面没生成：'+e.message);return;}
  }
  coverSync();
  if(el.dataset.action==='cover-pick'){state.cover.templateId=el.dataset.id;return route();}
  if(el.dataset.action==='cover-avatar-video'){return coverLoadFrames();}
  if(el.dataset.action==='cover-avatar-next'){
    const fr=state.cover.frames;if(!fr||!fr.candidates||!fr.candidates.length)return;
    fr.index=((fr.index||0)+1)%fr.candidates.length;return route();
  }
  if(el.dataset.action==='cover-avatar-close'){state.cover.frames=null;return route();}
  if(el.dataset.action==='cover-avatar-use'){return coverUseFrame(el);}
  if(el.dataset.action==='cover-avatar-ai'){return coverAiAvatar();}
  if(el.dataset.action==='cover-avatar-pick'){
    const input=document.querySelector('[data-avatar-input]');
    if(input)input.click();
    return;
  }
  if(el.dataset.action==='cover-unlock'){
    const r=await api('/projects/'+p.id+'/cover-unlock','POST',{template_id:el.dataset.id});
    toast(r.message||'已解锁。');
    if(typeof refreshBilling==='function')await refreshBilling();
    return route();
  }
}
async function coverUploadAvatar(file){
  const statusEl=document.getElementById('cover-avatar-status');
  if(file.size>12*1024*1024){toast('照片太大（上限 12MB），请压缩后再传。');return;}
  const dataUrl=await new Promise((resolve,reject)=>{
    const fr=new FileReader();fr.onload=()=>resolve(fr.result);fr.onerror=()=>reject(Error('读不出这张图。'));fr.readAsDataURL(file);
  });
  if(statusEl)statusEl.textContent='正在抠图…首次可能需要下载模型，请稍候（不要关闭页面）';
  try{
    const r=await api('/avatar','POST',{image:dataUrl,filename:file.name});
    toast(r.message||'形象已保存，可复用。');
    return route();
  }catch(e){
    if(statusEl)statusEl.textContent='抠图失败：'+e.message;
    toast('形象没有保存：'+e.message);
  }
}
async function coverLoadFrames(){
  const p=state.project;if(!p)return;
  const ref=coverVideoRef(p);
  if(!ref){toast('还没有可用的项目视频：先完成剪辑，或上传一段素材。');return;}
  state.cover.frames={loading:true,index:0,candidates:[]};
  await route();
  try{
    const r=await api('/avatar/frames','POST',ref);
    state.cover.frames={loading:false,index:0,candidates:r.candidates||[],token:r.token||'',
      message:r.message||'',video:r.video||'',duration_ms:r.duration_ms,error:''};
  }catch(e){
    state.cover.frames={loading:false,index:0,candidates:[],error:e.message};
  }
  return route();
}
async function coverUseFrame(el){
  const statusEl=document.getElementById('cover-avatar-status');
  if(statusEl)statusEl.textContent='正在抠图并保存这一帧…';
  try{
    const r=await api('/avatar','POST',{frame:{token:el.dataset.token,n:Number(el.dataset.n)}});
    toast(r.message||'这一帧已存为形象，可复用。');
    state.cover.frames=null;
    return route();
  }catch(e){
    if(statusEl)statusEl.textContent='这一帧没有保存：'+e.message;
    toast('没有保存：'+e.message);
  }
}
async function coverAiAvatar(){
  const statusEl=document.getElementById('cover-avatar-status');
  if(statusEl)statusEl.textContent='正在探测图像生成能力…';
  try{
    const r=await api('/avatar/generate','POST',{});
    state.cover.aiNote=r.message||'';
    if(!r.available||!r.generated){
      if(statusEl)statusEl.textContent=r.message||'没有可用生成能力。';
      toast(r.message||'没有可用生成能力。');
      return route();
    }
    toast('AI 虚构形象已生成并保存，可复用。');
    return route();
  }catch(e){
    if(statusEl)statusEl.textContent='生成失败：'+e.message;
    toast('生成失败：'+e.message);
  }
}
async function coverGenerate(form){
  const p=state.project;if(!p)throw Error('请先选择作品。');
  const direct=p.mode==='direct';
  const artifacts=(p.artifacts||[]).filter(a=>['clean_video','packaged_video'].includes(a.kind)&&(direct||a.script_version_id===p.confirmed_version_id));
  const latest=artifacts.find(a=>a.id===p.latest_artifact_id)||artifacts[0];
  if(!latest)throw Error('还没有当前定稿的成片，先完成剪辑或包装。');
  const s=state.cover||{};
  if(!s.templateId)throw Error('请先选择一套封面模板。');
  const tpl=((p.cover&&p.cover.styles)||[]).find(x=>x.id===s.templateId)||{};
  if(tpl.requires_person&&!(p.cover&&p.cover.avatar&&p.cover.avatar.exists))throw Error('这套模板需要先上传「我的形象」。');
  const titlePlain=String(form.elements.title.value||'').replace(/[【】]/g,'').trim();
  const titleLength=Array.from(titlePlain).length;
  if(titleLength>12)throw Error('主标题 '+titleLength+' 字，缩到最小字号并换行也放不下；请改写到 12 字以内（可用【】标重点）。');
  const extra={};
  (tpl.fields_extra||[]).forEach(x=>{
    const el=form.elements['extra-'+x.key];if(!el)return;
    extra[x.key]=x.type==='lines'?el.value.split('\n').map(v=>v.trim()).filter(Boolean).slice(0,3):el.value.trim();
  });
  await api('/projects/'+p.id+'/cover-generate','POST',{artifact_id:latest.id,template_id:s.templateId,
    fields:Object.assign({title:form.elements.title.value,subtitle:form.elements.subtitle.value,badge:form.elements.badge.value,account:form.elements.account.value},extra),
    person:{on:form.elements['person-on'].checked,x:form.elements['person-x'].value,
      outline:form.elements['person-outline']?form.elements['person-outline'].value:'auto'}});
  toast('封面生成任务已提交，完成后在这里预览与下载。');
  return route();
}
document.addEventListener('change',function(e){
  if(e.target&&e.target.matches&&e.target.matches('[data-avatar-input]')&&e.target.files&&e.target.files[0]){
    coverUploadAvatar(e.target.files[0]);
  }
});
