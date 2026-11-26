'use strict';
/* 文字剪片：在逐字稿上点词，视频里对应的字词一起剪掉。

   这个面板是「出片之后手动微调」的入口，所以它显示的必须是这一版真实被剪过的样子：
   一进来，已经被剪掉的词就是划掉状态；点一下把它捞回来，再点别的词加删，改完重剪一版。

   区间一律由后端 media.word_cut_ranges 计算，且以当前净剪方案为基底（base_removed），
   所以恢复一个词只捞回那一段，不会把语义净剪、停顿清理已经剪掉的内容一起还原。
   前端只负责“哪些词是删的”和即时视觉反馈，不自己算时间。

   预览照开源实现 Rescript（github.com/wassgha/rescript，components/MediaPreview.tsx）的做法：
   单个 <video> 直接播源素材，播放时用 requestAnimationFrame 每帧检查播放头，
   落进被剪掉的区间就跳到下一段保留内容的开头；暂停时不跳，方便回看被剪掉的原话。 */

const TC_EPSILON_S=0.005;   /* 跳转后多走 5ms，避免正好停在区间边界反复命中 */

/* TC_BASE[tid] = 这一版净剪原本删掉的词（恢复它就等于"改回原样"的基准） */
const TC_BASE={};
let TC_META={};

function textcutStore(id){
  state.textcut=state.textcut||{};
  if(!state.textcut[id])state.textcut[id]=new Set();
  return state.textcut[id];
}

function textcutKeepStore(id){
  state.textcutKeep=state.textcutKeep||{};
  return state.textcutKeep[id]||null;
}

function tcBase(id){return TC_BASE[id]||new Set();}

function textcutChanged(id){
  const cur=textcutStore(id),base=tcBase(id);
  let n=0;
  cur.forEach(k=>{if(!base.has(k))n++;});
  base.forEach(k=>{if(!cur.has(k))n++;});
  return n;
}

/* 只认与本项目当前定稿匹配的转写；旧稿素材的转写不参与，避免剪错版本。
   优先读项目负载里的 transcripts（转写不一定留下 transcribe job，例如一键出片内部转写）。 */
function textcutSources(p){
  const current=(p.assets||[]).filter(a=>a.script_version_id===p.confirmed_version_id);
  const seen=new Set(),out=[];
  const add=t=>{
    if(!t||seen.has(t.id)||!Array.isArray(t.words)||!t.words.length)return;
    const asset=current.find(a=>a.id===t.asset_id);
    if(!asset)return;
    seen.add(t.id);out.push({transcript:t,asset});
  };
  (p.transcripts||[]).forEach(add);
  (p.jobs||[]).forEach(j=>{
    if(j.kind!=='transcribe'||j.status!=='succeeded')return;
    add(j.result&&j.result.transcript);
  });
  return out;
}

/* 中文词直接相连；西文/数字词之间留一个空格，避免粘成一个词。 */
function textcutJoin(prev){
  /* 2026-11-17（t60 修复）：这里只返回「词间分隔符」。旧实现把 text 也返回，
     调用处 sep+w.text 再把词拼一遍，导致文字剪片每个词显示两遍。 */
  if(!prev)return '';
  const glue=/[\u4e00-\u9fa5，。！？、；：""''（）《》…—·]$/;
  return glue.test(prev)?'':' ';
}

function textcutBody(t){
  const del=textcutStore(t.id),base=tcBase(t.id);
  let prev='',html='',line=0;
  t.words.forEach((w,i)=>{
    /* 长停顿或句末断行；一行最多 18 个词，防止重复内容黏成一坨读不下去 */
    if(i&&(w.start_ms-t.words[i-1].end_ms>500||/[。！？]$/.test(t.words[i-1].text)||line>=18)){html+='<br>';line=0;}
    const sep=textcutJoin(prev);
    const gone=del.has(w.id),back=!gone&&base.has(w.id);
    const cls='tc-word'+(gone?' is-del':'')+(back?' is-restored':'');
    const hint=gone?'这一版已经剪掉了，点一下捞回来':(back?'原本被剪掉了，你已经把它改回保留':'点一下划掉，出片时这个词跟着剪掉');
    html+=`<button type="button" class="${cls}" data-tc-word="${esc(w.id)}" data-start="${w.start_ms}" title="${timecode(w.start_ms)}–${timecode(w.end_ms)}　${hint}">${esc(sep+w.text)}</button>`;
    prev=w.text;line++;
  });
  return html;
}

function renderTextCut(p){
  const blocked=p.mode==='direct'?!p.current_asset_id:(p.draft_changed||!p.confirmed_version_id);
  const sources=textcutSources(p);
  if(!sources.length){
    /* t54（D2）：无转写不再整块消失——占位引导说清出现条件与下一步（主理人反复找不到文字剪的根因）。 */
    const hasAsset=(p.assets||[]).some(a=>p.mode==='direct'?a.id===p.current_asset_id:a.script_version_id===p.confirmed_version_id);
    return `<div class="rule space tc-await"><h3>文字剪片：对着文字点删，按删法重剪</h3><p class="small muted">${hasAsset?'素材已在。转写完成后即可对着文字剪——这里会出现逐字稿，点哪个词，视频里那个词就跟着剪掉。':'先上传素材并完成转写，这里就会出现逐字稿——点哪个词，视频里那个词就跟着剪掉。'}</p><p class="small muted">${p.mode==='direct'?'点上方「重新自动处理」或重新导入视频，转写完成后本面板自动出现。':'点上方「一键转写、剪辑并包装」，转写完成后本面板自动出现。'}</p></div>`;
  }
  const first=sources[0];
  /* t98：文字剪重剪出的成片要能就地下载去剪映包装。指向当前最新有效版（与主面板同口径：
     直剪按当前素材锚、写稿链按定稿版本锚；撤销版不冒充当前结果）。 */
  const vc=(p.artifacts||[]).filter(a=>['clean_video','packaged_video','final_video'].includes(a.kind)&&!a.undone_at&&(p.mode==='direct'?(!a.asset_id||a.asset_id===p.current_asset_id):a.script_version_id===p.confirmed_version_id));
  const vlatest=vc.find(a=>a.id===p.latest_artifact_id)||vc[0]||null;
  return `<div class="rule space"><h3>文字剪片：对着文字点词，视频跟着剪</h3><p class="small muted">下面划掉的就是成片里已经没了的字词。听着不对，点一下那个词把它捞回来；想再删，点别的词。改完点下面的按钮重剪一版——这一步只改删哪些词，不动任何参数。上面「剪掉停顿和口误」是让模型先找重复改口，「成片重建」是按目标时长出片；这里是出片之后发现哪句不合意，回来手动修的地方。</p><video class="video tc-video" id="textcut-video" src="${media('/api/assets/'+first.asset.id)}" controls playsinline preload="metadata"></video><p class="small muted">播放时会自动跳过划掉的词，听到的就是剪完的样子；暂停后拖进度条可以回看被剪掉的原话。</p><label class="field"><span>源素材</span><select id="textcut-asset">${sources.map(s=>`<option value="${esc(s.transcript.id)}" data-asset="${esc(s.asset.id)}">${esc(s.asset.filename)} · ${fmt(s.asset.info.duration_ms)} · ${s.transcript.words.length} 词</option>`).join('')}</select></label><div class="row spread"><span class="small muted" id="textcut-stats">正在读取这一版的剪辑状态…</span><span class="small"><span id="textcut-clock" class="muted">0.0s</span> · <span id="textcut-count">已删 0 词</span></span></div><div class="tc-text space" id="textcut-text">${textcutBody(first.transcript)}</div><div class="actions space"><button type="button" data-tc="reset" ${blocked?'disabled':''} title="把面板恢复成这一版清理版的样子，撤销你刚才点的词，不碰视频">撤销本次改动</button><button type="button" class="primary" data-tc="apply" ${blocked?'disabled':''} title="按面板上划掉的全部词真实剪掉音视频，生成新一版清理版；原素材、转写和旧版本都保留">按现在的删法重剪一版</button></div>${vlatest?`<div class="row space"><a class="button-link" href="${media('/api/artifacts/'+vlatest.id+'?download=1')}" download="${esc(p.title)}-文字精剪.mp4">下载视频 · 去剪映包装</a><span class="small muted">下载的是当前最新一版；重剪完成后自动指向新版。</span></div>`:''}<p class="small muted space">这一步只多生成一版，原素材、转写和旧版本都不会被改动。剪完可以直接接着「成片重建」出片。</p></div>`;
}

function textcutPaint(r){
  const n=textcutStore(document.querySelector('#textcut-asset')?.value||'').size;
  const num=document.querySelector('#textcut-count');
  const stats=document.querySelector('#textcut-stats');
  const chg=textcutChanged(document.querySelector('#textcut-asset')?.value||'');
  if(num)num.textContent='已删 '+n+' 词'+(chg?'（改动 '+chg+' 处）':'');
  if(!stats)return;
  if(!r){stats.textContent='正在算…';return;}
  if(r.ok===false){stats.textContent='删得太多了：'+((r&&r.message)||'保留内容不足 0.3 秒。');return;}
  if(!r.removed_seconds){
    stats.textContent='原文 '+r.original_seconds+'s · 这一版还没剪过内容';
    return;
  }
  stats.textContent='原文 '+r.original_seconds+'s → 剪掉 '+r.removed_seconds+'s → 剪后 '+r.keep_seconds+'s（'+r.segments+' 段 / '+r.cuts+' 刀）';
}

/* 读后端状态：这一版净剪删了哪些词，保留区间是什么。 */
async function textcutLoad(p,tid){
  const st=await api('/projects/'+p.id+'/textcut-state','POST',{transcript_id:tid});
  TC_BASE[tid]=new Set(st.deleted_word_ids||[]);
  state.textcut=state.textcut||{};
  state.textcut[tid]=new Set(TC_BASE[tid]);
  state.textcutKeep=state.textcutKeep||{};
  state.textcutKeep[tid]=st.keep||null;
  TC_META[tid]=st;
  return st;
}

/* 面板重绘：先拿真实状态，再铺词、再算一遍时长。 */
async function textcutBoot(p,tid,box){
  const hit=textcutSources(p).find(s=>s.transcript.id===tid);
  if(!hit)return;
  const st=await textcutLoad(p,tid);
  state.textcut=state.textcut||{};
  box.innerHTML=textcutBody(hit.transcript);
  textcutPaint({ok:true,original_seconds:st.original_seconds,removed_seconds:st.removed_seconds,
    keep_seconds:st.keep_seconds,segments:st.segments,cuts:st.cuts});
  tcLastWord=null;tcLastClock=-1;
}

let textcutTimer=null;
async function textcutRefresh(p,transcriptId){
  const ids=Array.from(textcutStore(transcriptId));
  textcutPaint(null);
  try{
    const r=await api('/projects/'+p.id+'/textcut-estimate','POST',{transcript_id:transcriptId,word_ids:ids});
    state.textcutKeep=state.textcutKeep||{};
    state.textcutKeep[transcriptId]=(r.ok&&r.keep)?r.keep:null;
    textcutPaint(r);
  }catch(e){
    textcutPaint({ok:false,message:e.message});
  }
}

/* ---- 预览：播放时跳过已剪掉的区间 ---- */
let tcLastClock=-1,tcLastWord=null;

function textcutMark(ms){
  const words=document.querySelectorAll('#textcut-text .tc-word');
  let hit=null;
  for(let i=0;i<words.length;i++){
    const s=Number(words[i].dataset.start||0);
    const nxt=words[i+1];
    const e=nxt?Number(nxt.dataset.start||0):s+400;
    if(ms>=s&&ms<e){hit=words[i];break;}
  }
  if(hit===tcLastWord)return;
  const old=document.querySelector('#textcut-text .tc-word.is-now');
  if(old)old.classList.remove('is-now');
  tcLastWord=hit;
  if(hit){
    hit.classList.add('is-now');
    hit.scrollIntoView({block:'nearest',inline:'nearest'});
  }
}

function textcutTick(){
  const v=document.getElementById('textcut-video');
  if(v&&v.readyState>0){
    let t=v.currentTime;
    if(!v.paused){
      const sel=document.getElementById('textcut-asset');
      const ranges=sel?textcutKeepStore(sel.value):null;
      if(ranges&&ranges.length){
        const ms=t*1000;
        if(!ranges.some(r=>ms>=r.start_ms&&ms<r.end_ms)){
          const nx=ranges.find(r=>r.start_ms>ms);
          if(nx){v.currentTime=nx.start_ms/1000+TC_EPSILON_S;t=v.currentTime;}
          else{v.pause();}
        }
      }
    }
    const clock=document.getElementById('textcut-clock');
    if(clock&&Math.abs(t-tcLastClock)>=0.05){tcLastClock=t;clock.textContent=t.toFixed(1)+'s';}
    textcutMark(t*1000);
  }
  requestAnimationFrame(textcutTick);
}
requestAnimationFrame(textcutTick);

/* 面板一出现就拉真实状态；切换素材也重新拉。dataset.booted 防止同一份反复初始化。 */
new MutationObserver(()=>{
  const box=document.querySelector('#textcut-text');
  const sel=document.querySelector('#textcut-asset');
  const p=state.project;
  if(!box||!sel||!p)return;
  const key=sel.value+'@'+(location.hash||'');
  if(box.dataset.booted===key)return;
  box.dataset.booted=key;
  textcutBoot(p,sel.value,box).catch(e=>toast(e.message));
}).observe(document.body,{childList:true,subtree:true});

document.addEventListener('click',async event=>{
  const p=state.project;if(!p)return;
  const page=document.querySelector('#textcut-text');if(!page)return;
  const sel=document.querySelector('#textcut-asset');
  const id=sel?sel.value:'';
  const word=event.target.closest('[data-tc-word]');
  const bar=event.target.closest('[data-tc]');
  if(word){
    const del=textcutStore(id),key=word.dataset.tcWord;
    if(del.has(key))del.delete(key);else del.add(key);
    const gone=del.has(key),back=!gone&&tcBase(id).has(key);
    word.classList.toggle('is-del',gone);
    word.classList.toggle('is-restored',back);
    word.title=gone?'这一版已经剪掉了，点一下捞回来':(back?'原本被剪掉了，你已经把它改回保留':'点一下划掉，出片时这个词跟着剪掉');
    clearTimeout(textcutTimer);
    textcutTimer=setTimeout(()=>textcutRefresh(p,id).catch(e=>toast(e.message)),220);
    return;
  }
  if(!bar)return;
  if(bar.dataset.tc==='reset'){
    const st=TC_META[id];
    state.textcut=state.textcut||{};
    state.textcut[id]=new Set(TC_BASE[id]);
    if(st){
      state.textcutKeep=state.textcutKeep||{};
      state.textcutKeep[id]=st.keep||null;
    }
    page.innerHTML=textcutBody({id,words:textcutSources(p).find(s=>s.transcript.id===id).transcript.words});
    textcutPaint(st?{ok:true,original_seconds:st.original_seconds,removed_seconds:st.removed_seconds,
      keep_seconds:st.keep_seconds,segments:st.segments,cuts:st.cuts}:null);
    return;
  }
  if(bar.dataset.tc==='apply'){
    const ids=Array.from(textcutStore(id));
    bar.disabled=true;
    try{
      await api('/projects/'+p.id+'/remove-words','POST',{transcript_id:id,word_ids:ids});
      toast(ids.length
        ?'已提交：按现在划掉的 '+ids.length+' 个词重剪一版，完成后在页面顶部播放。'
        :'已提交：这一版不再删任何词，按原片重剪。');
      delete TC_BASE[id];
      state.textcut[id]=new Set();
      if(state.textcutKeep)delete state.textcutKeep[id];
      await route();
    }catch(e){toast(e.message);}
    finally{bar.disabled=false;}
  }
});

document.addEventListener('change',async event=>{
  if(event.target.id!=='textcut-asset')return;
  const p=state.project;if(!p)return;
  const hit=textcutSources(p).find(s=>s.transcript.id===event.target.value);
  const box=document.querySelector('#textcut-text');
  if(!hit||!box)return;
  box.dataset.booted=event.target.value+'@'+(location.hash||'');
  box.innerHTML=textcutBody(hit.transcript);
  tcLastWord=null;tcLastClock=-1;
  const v=document.getElementById('textcut-video');
  if(v){v.src=media('/api/assets/'+hit.asset.id);v.load();}
  textcutBoot(p,event.target.value,box).catch(e=>toast(e.message));
});
