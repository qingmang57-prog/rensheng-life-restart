'use strict';
/* 稿本锚定：对定稿逐段选 take。机器只提候选与推荐，本人单选确认后才真实剪切。 */
function renderAnchorCut(p){
  const blocked=p.draft_changed||!p.confirmed_version_id;
  const current=p.assets.filter(a=>a.script_version_id===p.confirmed_version_id);
  const plan=(p.anchor_plans||[]).find(x=>x.script_version_id===p.confirmed_version_id);
  const cap=state.boot.capabilities;
  if(!current.length||blocked)return '';
  const timecode=window.timecode||fmt;
  let html=`<div class="rule space"><h3>稿本锚定：对着定稿选 take</h3>
    <p class="small muted">本地转写 → 逐段定位候选 take（含口误密度与表演评分）→ 你每段单选确认 → 实际剪切。原始素材、转写和旧版本都保留；稿本外的即兴内容默认全部保留，不想留的逐条取消勾选。</p>
    <div class="row"><label class="field ui-grow"><span>源素材</span><select id="anchor-asset">${current.map(a=>`<option value="${a.id}" ${plan&&plan.asset_id===a.id?'selected':''}>${esc(a.filename)} · ${fmt(a.info.duration_ms)}</option>`).join('')}</select></label>
    <button data-action="anchor-align" ${!cap.asr?'disabled':''}>按定稿分析 take</button></div>`;
  if(plan){
    html+=`<div class="space" id="anchor-plan" data-id="${esc(plan.id)}">`;
    if(plan.issues&&plan.issues.length)html+=plan.issues.map(i=>notice('覆盖问题：'+i)).join('');
    html+=`<p class="small muted">${plan.method==='anchor_llm_judged'?'模型已判定 take 可用性与表演分；':'生成模型不可用，仅本地模糊匹配评分；'}共 ${plan.parts.reduce((n,x)=>n+x.takes.length,0)} 个候选 take。推荐项已预选，可逐段换选。</p>`;
    html+=plan.parts.map(part=>{
      if(!part.takes.length)return `<div class="director-item"><strong>${esc(part.label)}</strong>${notice('没有找到对应内容：需要补拍，或改用下方「剪掉停顿和口误」。')}</div>`;
      return `<div class="director-item"><strong>${esc(part.label)}</strong><p class="small muted">稿本：${esc(part.text.slice(0,60))}${part.text.length>60?'…':''}</p>
        ${part.takes.map(t=>`<label class="word-chip ui-block">
          <input type="radio" name="anchor-take" value="${esc(part.part_id)}:${t.take_index}" ${part.recommended===t.take_index?'checked':''}>
          <small>${timecode(t.start_ms)}–${timecode(t.end_ms)}</small> 覆盖${Math.round(t.completeness*100)}% · 口误信号${t.signals} · ${t.performance?`表演${t.performance}/5 · `:''}总分${t.score}
          <br><span class="small">${esc(t.text.slice(0,80))}${t.text.length>80?'…':''}</span>
          ${t.reason?`<br><span class="small muted">理由：${esc(t.reason)}</span>`:''}
        </label>`).join('')}</div>`;
    }).join('');
    if(plan.improvised&&plan.improvised.length){
      // 2026-11-10（试用反馈 P0-2）：未匹配口播默认全部保留，要看全量、不再只列前 30 段；
      // 不想留的逐条取消勾选即可。
      ipAddCssRules(['.anchor-improv-list{max-height:320px;overflow:auto}']);
      html+=`<details class="space" open><summary class="small muted">稿本外内容 ${plan.improvised.length} 段（默认全部保留；不想留的取消勾选）</summary>
        <div class="anchor-improv-list">
        ${plan.improvised.map(c=>`<label class="word-chip ui-block"><input type="checkbox" name="anchor-improv" value="${esc(c.cue_id)}" checked>
          <small>${timecode(c.start_ms)}–${timecode(c.end_ms)}</small> <span class="small">${esc(c.text.slice(0,70))}</span></label>`).join('')}
        </div></details>`;
    }
    html+=`<div class="row"><label class="field ui-grow"><span>停顿清理</span><select id="anchor-silence"><option value="conservative">保守 · 只压1.2秒以上长停顿</option><option value="natural">自然 · 压0.8秒以上</option><option value="tight">紧凑 · 压0.55秒以上</option></select></label></div>
      <button class="primary space" data-action="anchor-apply">按所选 take 真实剪切</button>
      <p class="small muted">剪后自动机检：稿本覆盖、成片时长、开头结尾完整句（铁律：不能止于半句）。</p></div>`;
  }
  return html+'</div>';
}

/* 导演三件套：定稿资料页展示与按需重生成。 */
function renderDirectorBrief(p){
  const b=p.director_brief;
  if(!p.confirmed_version_id||p.draft_changed)return '';
  if(!b)return `<div class="panel space"><h3>导演三件套</h3><p class="small muted">确认稿件后可生成：情绪弧线（决定 BGM 选曲与分段）、节奏设计（哪里收紧哪里留白）、视觉预埋（哪句上值得转发的卡、哪个词做大字过场）。</p><button data-action="director-brief" ${state.boot.capabilities.llm?'':'disabled'}>生成导演三件套</button></div>`;
  const moodName={mood_arc:'情绪弧线',pacing:'节奏设计',visual_plan:'视觉预埋'};
  const partLabel=id=>{const r=(p.versions.find(v=>v.id===b.script_version_id)?.parts||[]).find(x=>x.id===id);return r?r.label:id;};
  let body='';
  const arc=b.mood_arc||{};
  if(arc.segments)body+=`<div class="rule"><span class="fact-label">情绪</span>${esc(arc.overall||'')} — ${(arc.segments||[]).map(s=>`${esc(partLabel(s.part_id))} ${esc(s.mood)}${s.intensity||''}`).join(' / ')}</div>`;
  if(b.pacing)body+=`<div class="rule"><span class="fact-label">节奏</span>${b.pacing.map(r=>`${esc(partLabel(r.part_id))} ${esc(r.pace)}${r.pause_before_ms?` 前留${r.pause_before_ms}ms`:''}`).join('；')}</div>`;
  const vp=b.visual_plan||{};
  if(vp.quote_cards)body+=`<div class="rule"><span class="fact-label">值得转发的卡</span>${vp.quote_cards.map(c=>`「${esc(c.quote)}」`).join(' ')}</div>`;
  if(vp.keyword_moments&&vp.keyword_moments.length)body+=`<div class="rule"><span class="fact-label">大字过场</span>${vp.keyword_moments.map(c=>`「${esc(c.keyword)}」`).join(' ')}</div>`;
  return `<div class="panel space"><div class="row spread"><h3>导演三件套</h3><span class="small muted">${b.mood_arc&&b.mood_arc.method==='rule_fallback'?'规则兜底版':'模型生成'}</span></div>
    ${body||'<p class="small muted">三件套为空。</p>'}
    <div class="actions space">${Object.entries(moodName).map(([k,v])=>`<button class="small-button" data-action="director-brief" data-item="${k}" ${state.boot.capabilities.llm?'':'disabled'}>重生成${v}</button>`).join('')}</div>
    <p class="small muted">转发卡与过场词必须逐字引用已确认稿；重生成只替换对应一件，旧值随稿本版本保留在记录里。</p></div>`;
}
