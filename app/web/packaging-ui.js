'use strict';
function packagingBgmOn(style){const id=style?.id;return id==='ms:观点变形口播'?state.rsEvaBgm!==false:(id==='ms:jd-cinema-voice-h1'||id==='ms:jd-spatial-voice-v1')?state.jdMotionBgm!==false:!!state.packBgm;}
function packagingSwatch(style,text,small=false){
  if(style.spatial_template){ipAddCssRules(['.pack-swatch-spatial{background:linear-gradient(135deg,#111318,#34424a);color:#e4d3a8}']);
    return '<div class="pack-swatch pack-swatch-spatial"><span>空间讲述</span><strong>'+esc(text||'原句随镜头展开')+'</strong></div>';}
  if(style.reference_template)return packagingReferenceSwatch(style,text,small);
  const theme=style.eva_template?'pack-theme-ms':'pack-theme-'+String(style.id||'');
  // 2026-11-17 t131：本机模板预览图只对**确实有图**的模板输出 img。
  // app/web/eva-thumbs 当前只有 ms-JDXH-01.jpg；其余模板走下方 CSS 色板兜底，
  // 不再对 31 张卡逐个发 /eva-thumbs/*.jpg 造成 404 风暴。新增缩略图时把文件名加进这里。
  const EVA_THUMB_IDS=new Set(['ms-JDXH-01']);
  const evaThumb=style.eva_template&&EVA_THUMB_IDS.has(String(style.id||'').replace(/:/g,'-'))?'<img class="pack-swatch-img" alt="" loading="lazy" src="/eva-thumbs/'+esc(String(style.id||'').replace(/:/g,'-'))+'.jpg">':'';
  ipAddCssRules(['.pack-swatch{position:relative}.pack-swatch-img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover;border-radius:inherit}']);
  return '<div class="pack-swatch '+(small?'pack-swatch-small ':'')+esc(theme)+'"><span class="pack-swatch-label">'+(small?'':'版式示意')+'</span><span class="pack-swatch-mark" aria-hidden="true">“</span><strong>'+esc(text||'让表达被看见')+'</strong><i></i><span class="pack-swatch-line"></span>'+evaThumb+'</div>';
}
function renderSmartPackaging(p){
  const data=p.smart_packaging||{},plan=data.plan,cap=state.boot.capabilities;
  if(p&&p.jobs)schedulePackagingFallback(p);
  if(!plan)return '<div class="smart-packaging pack-await rule space"><span class="pack-symbol" aria-hidden="true">✦</span><div><h3>智能包装</h3><p class="small muted">'+esc(data.note||'清理版完成后，自动推荐适合这条内容的包装。')+'</p></div></div>';
  const selected=plan.selected,preview=p.artifacts.find(a=>a.kind==='packaging_preview'&&a.packaging_plan_id===plan.id&&a.packaging_fingerprint===plan.fingerprint),
    busy=p.jobs.find(j=>['packaging_preview','packaging_render'].includes(j.kind)&&['queued','running'].includes(j.status)),
    blocked=p.draft_changed||!!busy,
    renderBlocked=p.draft_changed||!!cap.smart_packaging===false||!!busy,
    selectedAvailable=selected.available!==false,
    cleanArt=p.artifacts.find(a=>a.id===plan.clean_artifact_id)||{},
    subtitleOk=!!(cleanArt.asset_id&&cap.asr&&!p.draft_changed),
    missingText=!cap.smart_packaging?('模板渲染组件缺少：'+((cap.packaging_missing||[]).join('、')||'未检测到')):(!selectedAvailable?('模板「'+esc(selected.name)+'」缺少：'+((selected.missing||[]).join('、')||'组件未安装')):''),
    failedRender=(p.jobs||[]).filter(j=>j.kind==='packaging_render'&&j.status==='failed').slice(-1)[0],
    sample=plan.cues[0]?.text.slice(0,18),
    bgmOn=packagingBgmOn(selected),isRsEva=selected.id==='ms:观点变形口播',isJdMotion=selected.id==='ms:jd-cinema-voice-h1'||selected.id==='ms:jd-spatial-voice-v1',musicTracks=isRsEva?(selected.local_bgm_available?1:0):Number(cap.packaging_music_tracks||0),
    allStyles=(()=>{const seen=new Set(),out=[];const rec=plan.recommended?[plan.recommended]:[];for(const st of rec.concat(plan.styles||[],plan.alternatives||[])){if(st&&st.id&&!seen.has(st.id)){seen.add(st.id);out.push(st);}}return out;})(),
    presets=cap.caption_presets||[],
    item=s=>'<button class="pack-choice '+(s.id===selected.id?'pack-choice-active':'')+'" aria-pressed="'+(s.id===selected.id?'true':'false')+'" data-action="packaging-style" data-style="'+esc(s.id)+'" data-id="'+esc(plan.clean_artifact_id)+'" title="'+(s.available===false?'需安装：'+esc((s.missing||[]).join('、')):'选用这个模板')+'" '+(blocked||s.available===false?'disabled':'')+'>'+packagingSwatch(s,sample,true)+'<span><strong>'+esc(s.name)+(s.id===plan.recommended?.id?' <span class="small muted">推荐</span>':'')+(s.available===false?' <span class="small muted">需安装</span>':'')+'</strong><small>'+(s.family==='jd'?'JD 竖屏 · ':'')+esc(s.caption)+'</small></span></button>';
  return '<section class="smart-packaging space" aria-label="智能包装推荐"><div class="row spread"><h3><span class="pack-symbol" aria-hidden="true">✦</span> 智能包装</h3><span class="small muted">'+(plan.selection==='manual'?'本条自选':'随内容匹配')+'</span></div><div class="pack-recommend">'+packagingSwatch(selected,sample)+'<div class="pack-recommend-copy"><span class="pack-eyebrow">'+(plan.selection==='manual'?'本条风格':'本次推荐')+'</span><h2>'+esc(selected.name)+'</h2><p class="small muted">'+esc(selected.reason)+'</p><div class="pack-signals">'+(plan.analysis.signals||[]).slice(0,2).map(s=>'<span>'+esc(s)+'</span>').join('')+'</div><div class="pack-actions"><button class="primary" data-action="packaging-subtitle" data-id="'+esc(plan.id)+'" data-base="'+esc(plan.clean_artifact_id)+'" data-asset="'+esc(cleanArt.asset_id||'')+'" title="只需 ffmpeg：用这版剪切加字幕出片（可靠）" '+(subtitleOk?'':'disabled')+'>添加字幕包装（可靠）</button><button data-action="packaging-preview" data-id="'+esc(plan.id)+'" '+(renderBlocked||!selectedAvailable?'disabled':'')+'>'+ (preview?'重新试看模板':'▶ 模板试看（高级）')+'</button><button data-action="packaging-render" data-id="'+esc(plan.id)+'" '+(renderBlocked||!selectedAvailable||!preview?'disabled':'')+'>高级：模板渲染成片 →</button><button class="small-button" data-action="packaging-bgm" aria-pressed="'+(bgmOn?'true':'false')+'">配乐 BGM：'+(bgmOn?'开':'关')+'</button><button class="small-button" data-action="package-lite" data-id="'+esc(plan.clean_artifact_id)+'" title="轻量包装：转发卡＋双语字幕＋章节＋BGM，不做模板渲染" '+(p.draft_changed||!cap.llm||busy?'disabled':'')+'>学习包装（轻）</button></div><p class="small muted pack-bgm-note">'+(isRsEva?(bgmOn?(musicTracks?'本机配乐《'+esc(selected.local_bgm_title||'舒缓BGM')+'》已启用；说话时自动压低音乐。':'本机配乐未找到：请检查模板的本机曲库配置，本次保留原声与提示音。'):'本次关闭背景音乐，保留原声与提示音。'):(bgmOn?(musicTracks?'配乐文件夹里有 '+musicTracks+' 首，成片会加入配乐并自动压低人声。':'配乐文件夹里没有可用音乐：本次只保留原声，不会静默加乐；可在「设置 → 配乐文件夹」里选一个本地目录。'):'默认不添加配乐，保留清理原声。'))+'</p>'+((missingText||failedRender)?'<p class="small '+(failedRender?'error-text':'muted')+' pack-fallback-note">'+(failedRender?'上次模板渲染失败：'+esc(failedRender.error||'环境或渲染错误')+'。已自动改用字幕包装出片；模板渲染是高级选项，可稍后重试。':missingText+'，主按钮已回退字幕包装（只需 ffmpeg）。')+'</p>':'<p class="small muted pack-fallback-note">字幕包装只需 ffmpeg，最稳；模板渲染（MotionGit/HyperFrames）是高级选项。</p>')+'</div></div>'+
    (busy?'<p class="pack-status small">◌ '+esc(busy.stage)+' · 后台继续处理</p>':'')+
    (!cap.smart_packaging?'<p class="small error-text">包装环境缺少：'+esc((cap.packaging_missing||[]).join('、'))+'</p>':'')+
    (preview?'<div class="pack-preview"><div class="row spread"><strong class="small">'+esc(selected.name)+' · 片段试看</strong><span class="small muted">'+fmt(preview.info.duration_ms)+' · '+(bgmOn&&musicTracks?'原声 + 配乐':'原声')+'</span></div><video class="video" controls playsinline preload="metadata" src="'+media('/api/artifacts/'+preview.id)+'"></video></div>':'<p class="small muted pack-footnote">先看包装效果，再生成整条；保留原声。</p>')+
    '<details class="pack-options" open><summary>换包装 <span class="small muted">模板、文字与取景</span></summary>'+
    (selected.spatial_template?'<p class="small muted space">同一空间、原句定时、人物与字幕独立。调整原句结构与重点后需重新试看。</p><button class="small-button" data-action="packaging-spatial-edit" '+(blocked?'disabled':'')+'>原句结构与重点</button>':'')+
    (selected.reference_template?'<p class="small muted space">'+(selected.id==='dark-matrix'?'展示本期内容卡片与讲稿字幕，保留原声，不显示人物。':(selected.id==='boundary'?'3:4 画幅，人物连续播放；可调整取景位置。':'16:9 画幅，中央人物连续播放，外围展示本期卡片。'))+'</p><button class="small-button" data-action="packaging-edit" '+(blocked?'disabled':'')+'>编辑本期文字与取景</button>':'')+


    '<div class="pack-featured"><h4>全部包装模板（'+allStyles.length+' 种，点击选用）</h4><div class="pack-choices pack-all-choices">'+allStyles.map(item).join('')+'</div></div>'+
    (presets.length?'<div class="pack-featured"><h4>字幕预设（'+presets.length+' 种）</h4><div class="pack-choices">'+presets.map(f=>'<button class="small-button" data-action="pack-caption-preset" data-preset="'+esc(f.key)+'" aria-pressed="'+(String(p.caption_style?.preset||state.packCaptionPreset||'')===f.key?'true':'false')+'">'+esc(f.label)+'</button>').join('')+'</div><p class="small muted space">选好预设后再点上方「试看/生成」；也可在字幕样式下拉里改。当前：'+esc(p.caption_style?.preset?captionPresetLabel(p.caption_style.preset):'未设（自定义）')+'</p></div>':'')+
    '<div class="pack-featured"><h4>封面模板</h4><p class="small muted space">封面工坊按内容推荐模板（需先有定稿/成片），本区不假列；去下方「封面与稿本锚定」选择。</p><button class="small-button" data-action="pack-cover-link">去封面工坊选封面</button></div>'+
    '</details>'+
    '<details class="pack-options"><summary class="small muted">匹配依据与原有包装</summary><p class="small muted space">'+esc(plan.analysis.method)+'。'+esc(plan.analysis.confidence)+'。本条换风格不会修改个人档案；'+((isRsEva||isJdMotion)?'此模板默认开启本机配乐，可手动关闭。':'配乐默认关闭，只使用你放进曲库的已授权音轨。')+'</p><button class="small-button" data-action="packaging-auto" data-id="'+esc(plan.clean_artifact_id)+'" '+(blocked?'disabled':'')+'>使用推荐风格</button></details></section>';
}
function schedulePackagingFallback(p){setTimeout(function(){packagingAutoFallback(p);},0);}

async function packagingSubtitle(el){
  const p=state.project;if(!p||!el.dataset.asset)return;
  try{
    await api('/projects/'+p.id+'/prepare-video','POST',{asset_id:el.dataset.asset,
      artifact_id:el.dataset.base||undefined,
      caption_style:{preset:state.packCaptionPreset||p.caption_style?.preset||''}});
    toast('已提交字幕包装任务，完成后在这里播放。');
    return route();
  }catch(e){toast(e.message||'字幕包装提交失败，请重试。');}
}
function packagingAutoFallback(p){
  try{
    const failed=(p.jobs||[]).filter(j=>j.kind==='packaging_render'&&j.status==='failed').slice(-1)[0];
    if(!failed)return;
    const plan=p.smart_packaging&&p.smart_packaging.plan;if(!plan)return;
    const clean=(p.artifacts||[]).find(a=>a.id===plan.clean_artifact_id);if(!clean||!clean.asset_id)return;
    if((p.jobs||[]).some(j=>j.kind==='prepare_video'&&['queued','running'].includes(j.status)))return;
    if((p.artifacts||[]).some(a=>a.kind==='packaged_video'&&a.created_at&&failed.created_at&&a.created_at>failed.created_at))return;
    const key='ip-pack-fallback:'+p.id+':'+failed.id;
    try{if(localStorage.getItem(key))return;localStorage.setItem(key,'1');}catch(e){return;}
    api('/projects/'+p.id+'/prepare-video','POST',{asset_id:clean.asset_id,artifact_id:clean.id,
      caption_style:{preset:state.packCaptionPreset||p.caption_style?.preset||''}})
      .then(()=>{toast('模板渲染未完成，已自动改用字幕包装出片。');return route();})
      .catch(()=>{});
  }catch(e){}
}
async function packagingAction(el){
  const p=state.project;if(!p)return;
  if(el.dataset.action==='packaging-subtitle')return packagingSubtitle(el);
  if(el.dataset.action==='packaging-spatial-edit')return packagingSpatialEdit(p);
  if(el.dataset.action==='packaging-edit')return packagingEditContent(p);
  if(el.dataset.action==='pack-caption-preset'){const key=el.dataset.preset||'';state.packCaptionPreset=key;const sel=document.getElementById('caption-preset');if(sel)sel.value=key;toast('已选字幕预设：'+(captionPresetLabel(key)||'自定义')+'（下次字幕包装生效）。');return route();}
  if(el.dataset.action==='pack-cover-link'){const all=[...document.querySelectorAll('details')];const target=all.find(d=>/封面与稿本锚定|封面工坊/.test((d.querySelector('summary')||{}).textContent||''));if(target){target.open=true;target.scrollIntoView({behavior:'smooth',block:'center'});toast('在「封面与稿本锚定」里选择封面模板。');}else toast('封面区还没准备好，请稍后再试。');return;}
  if(el.dataset.action==='packaging-bgm'){const sid=p.smart_packaging?.plan?.selected?.id;if(sid==='ms:观点变形口播')state.rsEvaBgm=!packagingBgmOn(p.smart_packaging.plan.selected);else if(sid==='ms:jd-cinema-voice-h1'||sid==='ms:jd-spatial-voice-v1')state.jdMotionBgm=!packagingBgmOn(p.smart_packaging.plan.selected);else state.packBgm=!state.packBgm;return route();}
  if(el.dataset.action==='packaging-style'||el.dataset.action==='packaging-auto'){
    const style=el.dataset.action==='packaging-auto'?p.smart_packaging.plan.recommended.id:el.dataset.style;
    await api('/projects/'+p.id+'/packaging-plan','POST',{artifact_id:el.dataset.id,style_id:style});
    toast('已更新本条包装风格，重新试看后可生成成片。');
  }else{
    await api('/projects/'+p.id+'/'+el.dataset.action,'POST',{plan_id:el.dataset.id,bgm:packagingBgmOn(p.smart_packaging.plan.selected)?1:0});
    toast(el.dataset.action==='packaging-preview'?'正在生成片段试看，完成后可在这里播放。':'完整包装任务已提交，完成后继续审片。');
  }
  return route();
}

function packagingReferenceSwatch(style,text,small){
  const layout=style.id==='boundary'?'<div class="pack-ref-paper"><div class="pack-ref-lines">观点<br>方法<br>行动</div><div class="pack-ref-person"></div></div>':style.id==='card-theatre'?'<div class="pack-ref-person"></div><div class="pack-ref-card pack-ref-card-a">观点</div><div class="pack-ref-card pack-ref-card-b">方法</div><div class="pack-ref-card pack-ref-card-c">行动</div>':'<div class="pack-ref-tiles">'+Array.from({length:24},()=>'<i></i>').join('')+'</div>';
  return '<div class="pack-swatch pack-reference pack-ref-'+esc(style.id)+(small?' pack-swatch-small':'')+'" aria-label="'+esc(style.name)+'版式示意">'+layout+'<span class="pack-ref-sample">'+esc(text||'本期内容')+'</span></div>';
}
function packagingEditContent(p){
  const plan=p.smart_packaging.plan,c=plan.template_content||{},sid=plan.selected.id;
  const field=(key,label,max,rows)=>'<label class="field"><span>'+label+'</span>'+(rows?'<textarea name="'+key+'" maxlength="'+max+'" rows="'+rows+'">'+esc(c[key]||'')+'</textarea>':'<input name="'+key+'" maxlength="'+max+'" value="'+esc(c[key]||'')+'" '+(key==='title'?'required':'')+'>')+'</label>';
  const body=field('title','本期主题',40)+field('brand','署名／品牌',30)+(sid==='boundary'?field('headline','核心观点（最多36字）',36,3)+field('note','观点说明（最多60字）',60,3)+field('steps','行动步骤（最多3行，每行14字）',50,3)+field('closing','带走一句话（最多36字）',36,3):field('cards','卡片内容（每行一项，每项12字，最多'+(sid==='card-theatre'?'6':'40')+'项）',600,sid==='card-theatre'?6:10));
  const position=sid==='dark-matrix'?'<input type="hidden" name="position" value="center">':'<label class="field"><span>人物取景位置</span><select name="position">'+[['center','居中'],['top','偏上'],['bottom','偏下']].map(([v,l])=>'<option value="'+v+'" '+((c.position||'center')===v?'selected':'')+'>'+l+'</option>').join('')+'</select></label>';
  return modal('编辑 '+plan.selected.name,'<form id="packaging-content-form" data-plan="'+esc(plan.id)+'" data-style="'+esc(sid)+'" data-clean="'+esc(plan.clean_artifact_id)+'"><p class="small muted">初始文字摘自本条讲稿，请核对摘要与人物取景。保存后需重新试看；字幕仍在成片下方校对。</p>'+body+position+'<button class="primary" type="submit">保存本期内容</button></form>');
}
async function packagingSaveContent(form,content){
  if(form.dataset.spatial){
    const fd=new FormData(form),kind=fd.get('spatial-kind'),focus=fd.get('spatial-emphasis');
    content={version:1,groups:kind==='quote'?[]:[{kind,cue_ids:fd.getAll('spatial-cue')}],emphasis_cue_ids:focus?[focus]:[],theme:fd.get('spatial-theme'),preset:fd.get('spatial-preset'),object_position:fd.get('spatial-position')};
  }
  await api('/projects/'+state.project.id+'/packaging-plan','POST',{artifact_id:form.dataset.clean,style_id:form.dataset.style,expected_plan_id:form.dataset.plan,content});
  document.getElementById('modal').close();toast('本期内容已保存，请重新试看。');await route();
}

function packagingSpatialEdit(p){
 const plan=p.smart_packaging.plan,o=plan.spatial_options||{},g=(o.groups||[])[0]||{},selected=new Set(g.cue_ids||[]);
 const options=(list,value)=>list.map(([v,t])=>'<option value="'+esc(v)+'" '+(v===value?'selected':'')+'>'+esc(t)+'</option>').join('');
 return modal('空间口播 · 原句结构与重点','<form id="packaging-content-form" data-spatial="1" data-plan="'+esc(plan.id)+'" data-style="'+esc(plan.selected.id)+'" data-clean="'+esc(plan.clean_artifact_id)+'"><p class="small muted">只选择这版底片里已经保留的原句；保存后重新试看。字幕文字仍在字幕校对处修改。</p><label class="field">结构<select name="spatial-kind">'+options([['quote','逐句讲述'],['process','流程 · 选2至6句'],['points','要点 · 选2至5句'],['contrast','对照 · 选左、右、结论3句']],g.kind||'quote')+'</select></label>'+plan.cues.map(c=>'<label class="field"><span><input type="checkbox" name="spatial-cue" value="'+esc(c.id)+'" '+(selected.has(c.id)?'checked':'')+'> '+esc(c.text)+'</span></label>').join('')+'<label class="field">重点原句<select name="spatial-emphasis">'+options([['','不指定重点'],...plan.cues.map(c=>[c.id,c.text])],(o.emphasis_cue_ids||[])[0]||'')+'</select></label><label class="field">配色<select name="spatial-theme">'+options([['warm','暖金'],['cool','冷蓝']],o.theme||'warm')+'</select></label><label class="field">镜头策略<select name="spatial-preset">'+options([['auto','自动组合'],['04','斜向运镜'],['05','缓慢推进'],['06','走进流程'],['08','原位强调']],o.preset||'auto')+'</select></label><label class="field">人物取景<select name="spatial-position">'+options([['center','居中'],['top','偏上'],['bottom','偏下']],o.object_position||'center')+'</select></label><button class="primary" type="submit">保存并重新试看</button></form>');
}
