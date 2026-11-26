'use strict';
const $=s=>document.querySelector(s);
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
/* 2026-11-26 H8：后端 error_kind（app.py 登记表 CUSTOMER_ERRORS/CUSTOMER_BY_CODE 类别 key）
   → 定位码 E-XXX + 一句人话。键与后端类别一一对应；后端给了 title 时人话仍以后端为准。 */
const ERROR_KIND_VIEW={
  unsupported:{code:'E-UNSUPPORTED',text:'这个链接或文件类型目前还处理不了，可以直接粘贴文字。'},
  ffmpeg_missing:{code:'E-FFMPEG-MISSING',text:'处理这个音视频要用的组件不在软件包里，这一步做不了。'},
  cloud_login:{code:'E-CLOUD-LOGIN',text:'这项功能要先登录账号才能用，已经保存的内容不受影响。'},
  memory:{code:'E-MEMORY',text:'电脑内存不够，这一步没做完。关掉一些占内存的程序再试。'},
  model_missing:{code:'E-MODEL-MISSING',text:'自动写作还没连上，这次没法帮你生成；你写的内容都还在。'},
  asr_failed:{code:'E-ASR-FAILED',text:'把声音转成文字这一步没成功，可以再试一次。'},
  asr_missing:{code:'E-ASR-MISSING',text:'「听声音转文字」的功能暂时用不了，到设置页查看。'},
  timeout:{code:'E-TIMEOUT',text:'等了太久没有回应，已先停下。你发的内容还在，可以再试一次。'},
  file_missing:{code:'E-FILE-MISSING',text:'要用到的文件找不到了，可能被移动、改名或删除。'},
  module_missing:{code:'E-MODULE-MISSING',text:'缺少程序文件（可能被杀毒软件隔离）。恢复并加信任，或重新解压完整包。'},
  too_large:{code:'E-TOO-LARGE',text:'这个文件或内容太大了，一次处理不了。'},
  download_failed:{code:'E-DOWNLOAD-FAILED',text:'视频没取下来，可能链接失效、需要登录或网络不稳。'},
  cloud_unreachable:{code:'E-CLOUD-UNREACHABLE',text:'连不上账号服务器。检查是否断网或开了加速/代理；本地记录照常可用。'},
  provider_unreachable:{code:'E-PROVIDER-UNREACHABLE',text:'模型或识别服务暂时连不上。请检查网络后重试；持续失败请联系客服。'},
  network:{code:'E-NETWORK',text:'连不上网络或服务，这一步没做完，你发的内容还在。'},
  record_missing:{code:'E-RECORD-MISSING',text:'这条记录已经不在了，重新来一次。'},
  trial_readonly:{code:'E-TRIAL-READONLY',text:'免费体验已经结束，新增内容暂时做不了；已保存的内容还能查看和导出。'},
  login_required:{code:'E-LOGIN-REQUIRED',text:'这条内容要先登录抖音才能读取。'},
  bad_response:{code:'E-BAD-RESPONSE',text:'服务没有正常应答。请确认程序已启动，请稍后重试。'},
  bad_draft:{code:'E-BAD-DRAFT',text:'这次自动生成的稿子不完整，没有采用。你原来的稿子没动。'},
  unknown:{code:'E-GENERIC',text:'这一步没做完，是我们这边出了问题。你的内容还在。'}
};
/* 2026-11-16（CSP）：style-src 'self' 会拦掉 HTML 里的内联 style。
   需要少量一次性布局样式时，往同源样式表里插规则，避免再加 <style> 或 style 属性。 */
function ipAddCssRules(rules){
  try{
    const sheet=document.styleSheets[0];if(!sheet)return;
    (rules||[]).forEach(r=>{try{sheet.insertRule(r,sheet.cssRules.length);}catch(e){}});
  }catch(e){}
}
ipAddCssRules(['.workspace.no-aside{grid-template-columns:minmax(0,1fr)}',
  /* 2026-11-25：讲稿页整篇编辑框 */
  '#script-whole{width:100%;min-height:320px;resize:vertical;line-height:1.8;font-size:16px}']);

/* 2026-11-16（异常态）：请求挂起要看得见。
   1.2 秒还没响应就显示「处理中」；累计到 10 秒改成「还在处理，可稍后重试」，
   别再让用户盯着一个禁用按钮猜是不是没点上。
   网络断开 / 服务端 5xx 失败时，状态条给一个「重试」按钮，而不是只留一句错误。 */
let ipBusyCount=0,ipBusyTimer=null,ipSlowTimer=null;
function ipBusyEl(){
  let el=document.getElementById('net-status');
  if(!el){
    el=document.createElement('div');el.id='net-status';el.className='net-status';el.setAttribute('role','status');
    el.setAttribute('aria-live','polite');el.hidden=true;document.body.appendChild(el);
    ipAddCssRules([
      '.net-status{position:fixed;left:50%;bottom:24px;transform:translateX(-50%);z-index:9999;display:flex;align-items:center;gap:10px;max-width:min(560px,92vw);padding:10px 14px;border-radius:12px;background:#1c1c22;color:#f2f2f5;box-shadow:0 8px 28px rgba(0,0,0,.35);font-size:14px;line-height:1.4}',
      '.net-status[hidden]{display:none}',
      '.net-status-spin{flex:none;width:12px;height:12px;border-radius:50%;border:2px solid currentColor;border-right-color:transparent;animation:ip-net-spin .8s linear infinite}',
      '.net-status-retry{flex:none;border:0;border-radius:8px;padding:5px 12px;background:#e6c56a;color:#1c1c22;font-size:13px;cursor:pointer}',
      '.net-status.in-dialog{position:static;left:auto;bottom:auto;transform:none;max-width:none;margin-top:12px;background:#26262e;border:1px solid #3a3a46}',
      '@keyframes ip-net-spin{to{transform:rotate(360deg)}}'
    ]);
  }
  // 弹窗（<dialog>）在顶层图层，body 里的固定条会被遮罩压住看不见；
  // 有打开的弹窗时把状态条挂进弹窗，保证「处理中 / 重试」真的可见。
  const dlg=document.getElementById('modal');
  const host=(dlg&&dlg.open)?dlg:document.body;
  if(el.parentNode!==host)host.appendChild(el);
  el.classList.toggle('in-dialog',host!==document.body);
  return el;
}
function ipBusyStart(){
  ipBusyCount+=1;if(ipBusyCount>1)return;
  clearTimeout(ipBusyTimer);clearTimeout(ipSlowTimer);
  ipBusyTimer=setTimeout(()=>{
    const el=ipBusyEl();delete el.dataset.retry;
    el.innerHTML='<span class="net-status-spin" aria-hidden="true"></span><span>处理中…正在等工作台返回。</span>';
    el.hidden=false;
    // 再等 8.8 秒（合计约 10 秒）仍没结果，才提示可以稍后重试。
    ipSlowTimer=setTimeout(()=>{
      const slow=document.getElementById('net-status');
      if(slow&&!slow.dataset.retry)slow.innerHTML='<span class="net-status-spin" aria-hidden="true"></span><span>还在处理，可以继续等，也可以稍后重试。</span>';
    },8800);
  },1200);
}
function ipBusyEnd(){
  ipBusyCount=Math.max(0,ipBusyCount-1);if(ipBusyCount)return;
  clearTimeout(ipBusyTimer);clearTimeout(ipSlowTimer);
  const el=document.getElementById('net-status');
  // 失败重试条不跟着别的请求结束一起消失。
  if(el&&!el.dataset.retry){el.hidden=true;el.textContent='';}
}
function ipRetryable(e){if(e&&typeof e.retry==='boolean')return e.retry;return !!e&&(e.code==='network'||e.code==='bad_response'||(Number(e.status)||0)>=500);}
/* 2026-11-25（开始创作嫁接 A）：失败卡 = 一句人话 + 一个能点的「下一步」+ 原话收进「查看详情」。
   人话 / 下一步 / 能否重试来自后端登记表（app.py customer_error）：api() 把它们挂在错误对象上
   （e.message 人话、e.raw 原话、e.next、e.retry）。后端没给时退回原来的提示。 */
function errView(e){const d=(e&&e.data)||{};const broken=/is not defined|Cannot read|is not a function/.test((e&&e.message)||'');
  /* 2026-11-26 H8：定位码。优先级：本错误对象自带 locator > 后端 error_kind 映射 > 后端 error_code 已是 E- 开头。 */
  const kv=ERROR_KIND_VIEW[(d&&d.error_kind)||(e&&e.errorKind)]||null;
  const locator=(e&&e.locator)||(kv&&kv.code)||((d&&d.error_code&&/^E-/.test(String(d.error_code)))?d.error_code:'')||'';
  return {title:d.title||(broken?'页面有一块没加载出来，刷新一下通常就好。':(e&&e.message)||(kv&&kv.text)||'操作失败。'),detail:(e&&e.raw)||(e&&e.message)||'',next:(e&&e.next)||d.next||null,retry:e&&typeof e.retry==='boolean'?e.retry:ipRetryable(e),locator};}
function nextButtonHtml(next){if(!next)return '';const a=next.action,label=esc(next.label||'下一步');if(a==='settings')return `<a class="small-button" href="#/settings">${label}</a>`;return `<button type="button" class="small-button" data-fail-next="${esc(a)}">${label}</button>`;}
function failCard(e){const v=errView(e);return `<div class="error-box" role="alert"><strong>${esc(v.title)}</strong>${v.locator?`<div class="small muted">错误码：${esc(v.locator)}</div>`:''}<div class="row space">${nextButtonHtml(v.next)}${v.retry?`<button type="button" class="small-button" data-fail-next="retry">重试</button>`:''}<details><summary class="small muted">查看详情</summary><pre class="raw-markdown">${esc(v.detail)}</pre></details></div><a class="link small" href="#/create">返回工作台</a></div>`;}
/* 2026-11-25：诊断包一键导出（含启动器日志尾/端口判定/核心文件在不在）。
   失败卡「下一步」与设置页共用这一个出口：生成 zip 并报完整路径，客户原样发客服即可定位。 */
async function exportDiagnostics(){
  toast('正在生成诊断包…');
  try{
    const r=await api('/diagnostics/export?format=json');
    toast('诊断包已生成：'+(r.path||r.filename)+'。把这个 zip 原样发给客服即可。');
  }catch(e){toast(errView(e).title);}
}
document.addEventListener('click',event=>{
  const b=event.target.closest('[data-fail-next]');if(!b)return;const a=b.dataset.failNext;
  if(a==='retry'){if(typeof state.failRetry==='function')state.failRetry();}
  else if(a==='detail'){const d=b.closest('.error-box')?.querySelector('details');if(d)d.open=true;}
  else if(a==='login'){if(typeof flowOpenLogin==='function')flowOpenLogin();else location.hash='#/settings';}
  else if(a==='douyin_login')showDouyinLogin(()=>{if(typeof state.failRetry==='function')state.failRetry();});
  else if(a==='upgrade'){const m=document.createElement('button');m.dataset.action='member-renew';m.hidden=true;document.body.appendChild(m);m.click();m.remove();}
  else if(a==='diagnostics')exportDiagnostics();
  else if(a!=='settings')location.hash='#/create';
});
function ipFail(e,retry){
  const v=errView(e);
  /* 2026-11-26 H8：toast 也带定位码，客户截图就能给客服定位。 */
  const message=((e&&e.message)||'操作失败。')+(v&&v.locator?'（'+v.locator+'）':'');
  if(!ipRetryable(e)||typeof retry!=='function'){toast(message);return;}
  const el=ipBusyEl();el.dataset.retry='1';el.hidden=false;
  el.innerHTML='<span>'+esc(message)+'</span><button type="button" class="net-status-retry">重试</button>';
  el.querySelector('.net-status-retry').addEventListener('click',function(){
    delete el.dataset.retry;el.hidden=true;el.textContent='';
    Promise.resolve().then(retry).catch(err=>ipFail(err,retry));
  },{once:true});
}
const state={boot:null,project:null,knowledgeItem:null,dirty:false,saveChain:Promise.resolve(),routeGen:0,step:'source',answer:null,jobsOpen:false};
const roles=[['hook','开头'],['content','主体'],['anchor','让人记住的那一句'],['quote','值得被转发的一句话'],['ending','结尾']];
const steps=[['source','链接与资料'],['script','讲稿'],['pack','发布文案'],['shoot','拍摄上传'],['edit','自动剪停顿+加字幕'],['publish','发布交付']];
const stages={draft:'讲稿待确认',shoot:'待拍摄上传',review:'待审片',ready_to_export:'成片已确认'};
const endorsements={legacy:'历史导入',accepted:'本人认可',reference_only:'仅作参考',rejected:'不认同'};
const lifecycles={active:'当前使用',archived:'归档',superseded:'已被更新认知替代'};
const validations={unverified:'验证待补',personal_case:'有本人案例',external_evidence:'有外部证据',mixed:'混合依据'};
let saveTimer,toastTimer,promptTimer,publishPollTimer,polling=false,accessGranted=false;
/* 2026-11-25：视图缓存（内存变量级）。命中立即画最新一次的 UI，后台静默刷新后
   只有 html 真变了才写 DOM；相同则零 DOM 动作，不整页重加载、不闪旧 UI。 */
const viewCache=new Map();
let payPollTimer=null,payPollOrder='',payPaidFlag=false,payPlanCode='year';
// 2026-11-16：付款前协议确认。未勾选时不下单、不出码，重试也沿用这次勾选结果。
let payAgreed=false;
const LEGAL_VERSION='v1.0 · 2026-11-16';
let promptSpeed=Math.min(480,Math.max(120,+localStorage.getItem('ip-prompt-speed')||300)),promptCarry=0;
function accessCodeDialog(tryCode){
  /* t54（E1）：手机访问码不再用原生 prompt 黑框——页内 modal 表单，密码掩码＋错误内联提示＋可重试。
     表单不带 id，避开全局 submit 分发器（app.js 的 submit 监听按 form.id 分支）。 */
  return new Promise(resolve=>{
    modal('手机访问 · 输入访问码','<form data-access-form><p class="small muted">这台工作台开启了局域网访问，输入访问码继续。访问码在启动工作台时显示，或见 .runtime/lan-token.txt。</p><label class="field"><span>访问码</span><input data-access-code type="password" autocomplete="off" required placeholder="输入访问码"></label><p class="small error-text" data-access-error role="alert" hidden></p><div class="actions"><button class="primary" type="submit" data-access-submit>进入工作台</button><button type="button" data-access-cancel>取消</button></div></form>');
    const dlg=$('#modal'),form=dlg.querySelector('[data-access-form]'),input=dlg.querySelector('[data-access-code]'),err=dlg.querySelector('[data-access-error]'),busy=dlg.querySelector('[data-access-submit]');
    const onClose=()=>{
      resolve(false);
      /* t54（E1b）：取消也要有页内落点，不能留白屏——给一句说明＋可重开的入口。 */
      const main=document.getElementById('main');
      if(main){main.innerHTML='<div class="panel"><h2>需要访问码</h2><p class="muted">未输入访问码，暂时看不到这台工作台的内容。</p><button type="button" class="primary" data-action="access-code-again">输入访问码</button></div>';}
    };
    dlg.addEventListener('close',onClose,{once:true});
    dlg.querySelector('[data-access-cancel]').addEventListener('click',()=>dlg.close(),{once:true});
    form.addEventListener('submit',async ev=>{
      ev.preventDefault();
      const value=input.value.trim();if(!value)return;
      busy.disabled=true;err.hidden=true;
      let ok=false;try{ok=await tryCode(value);}catch{ok=false;}
      busy.disabled=false;
      if(ok){dlg.removeEventListener('close',onClose);dlg.close();resolve(true);return;}
      err.hidden=false;err.textContent='访问码不正确，请重试。';input.value='';input.focus();
    });
    input.focus();
  });
}
async function ensureAccess(){const token=localStorage.getItem('ip-access-token')||'';if(token){try{const r=await fetch('/api/access',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token})});if(r.ok){accessGranted=true;return;}}catch{}}const ok=await accessCodeDialog(async value=>{const r=await fetch('/api/access',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:value})});if(!r.ok)return false;accessGranted=true;localStorage.setItem('ip-access-token',value);return true;});if(!ok)throw new Error('未输入访问码，无法访问工作台。');}
function lanHost(){const h=location.hostname;return h!=='127.0.0.1'&&h!=='localhost'&&h!=='::1'&&h!=='[::1]';}
async function readJson(r){try{return await r.json();}catch{
  /* 2026-11-26 H8：非 JSON 应答按 HTTP 状态码分三类说人话（5xx 服务内部错误 / 未正常应答请确认程序已启动 / 其它）。
     原兜底语义保留（r.ok 仍是「无法解析」一句），只加不改。 */
  const st=Number(r.status)||0;let text;
  if(st>=500)text='服务内部错误（HTTP '+st+'），请稍后重试；反复出现请导出诊断包。';
  else if(!r.ok)text='服务没有正常应答（HTTP '+st+'）。请确认工作台程序已启动，请稍后重试。';
  else text='服务返回了无法解析的内容，请重试。';
  const e=new Error(text);e.code='bad_response';e.status=r.status;e.locator=st>=500?'E-INTERNAL-ERROR':'E-BAD-RESPONSE';throw e;}}
async function api(path,method='GET',body,opts){
  const options={method,headers:{}};
  if(body!==undefined){options.headers['Content-Type']='application/json';options.body=JSON.stringify(body);}
  if(method==='GET'&&window.__apiPrefetch){const pre=window.__apiPrefetch.take('/api'+path);if(pre){const hit=await pre;if(hit)return hit;}}
  /* 2026-11-26 H8：客户端超时，默认 60 秒，调用方可用 opts.timeout 覆盖（长任务接口自行放宽）。
     超时不再无限转圈：AbortController 中断请求，报「等太久」并带定位码 E-TIMEOUT。 */
  const timeoutMs=opts&&Number(opts.timeout)>0?Number(opts.timeout):60000;
  const abort=new AbortController();
  const abortTimer=setTimeout(()=>abort.abort(),timeoutMs);
  options.signal=abort.signal;
  // 2026-11-25：后台往返静默。GET（导航/轮询/缓存刷新）不弹挂起条，只有用户主动的写操作才显示；
  // 静默不等于吞错误：失败仍走 ipFail/failCard。确需可见等待的 GET 传 {busy:true}。
  const silent=opts&&typeof opts.silent==='boolean'?opts.silent:(method==='GET'&&!(opts&&opts.busy));
  if(!silent)ipBusyStart();
  try{
    let r;
    try{r=await fetch('/api'+path,options);}
    catch(err){if(abort&&abort.signal.aborted){const e=new Error('等了太久没有回应，已先停下。你发的内容还在，可以再试一次。');e.code='timeout';e.errorKind='timeout';e.locator='E-TIMEOUT';throw e;}const e=new Error('连不上工作台服务，请确认程序还在运行。');e.code='network';throw e;}
    let d=await readJson(r);
    if(!r.ok&&!accessGranted&&(d.code==='token_required'||(d.code==='forbidden'&&lanHost()))){
      await ensureAccess();
      try{r=await fetch('/api'+path,options);}
      catch(err){if(abort&&abort.signal.aborted){const e=new Error('等了太久没有回应，已先停下。你发的内容还在，可以再试一次。');e.code='timeout';e.errorKind='timeout';e.locator='E-TIMEOUT';throw e;}const e=new Error('连不上工作台服务，请确认程序还在运行。');e.code='network';throw e;}
      d=await readJson(r);
    }
    if(!r.ok){const e=new Error(d.error||'操作失败。');e.code=d.code;e.status=r.status;e.data=d;e.raw=d.detail||'';if(typeof d.retry==='boolean')e.retry=d.retry;e.next=d.next||null;throw e;}
    return d;
  }finally{clearTimeout(abortTimer);if(!silent)ipBusyEnd();}
}
function media(path){const t=state.lanToken||'';if(!t)return path;return path+(path.includes('?')?'&':'?')+'token='+encodeURIComponent(t);}
function toast(text){clearTimeout(toastTimer);$('#toast').textContent=text;$('#toast').hidden=false;toastTimer=setTimeout(()=>$('#toast').hidden=true,6500);}
function modal(title,html){stopPayPoll();$('#modal-title').textContent=title;$('#modal-body').innerHTML=html;$('#modal').showModal();}
/* ===== t177（2026-11-24，需求单 B 前端半 + G 提示层）：导入/导出一律作业化 =====
   契约（t176）：POST 拿 {job_id} → 轮询 GET /api/jobs/{job_id} 1 秒一次 →
   {status, stage, done, total, message, result_path}。进度全部来自服务端真实计数，
   不用定时器编百分比；关闭弹窗不取消作业，后台继续跑。 */
function jobPercent(job){
  const done=Number(job&&job.done),total=Number(job&&job.total);
  if(!Number.isFinite(done)||!Number.isFinite(total)||total<=0)return null;
  return Math.max(0,Math.min(100,Math.round(done/total*100)));
}
function jobWorkingHtml(job,title){
  const pct=jobPercent(job),done=Number(job&&job.done)||0,total=Number(job&&job.total)||0;
  const stage=esc((job&&job.stage)||'处理中');
  const bar=pct===null
    ? '<div class="job-bar is-indeterminate"><span></span></div>'
    : '<div class="job-bar"><span data-job-fill class="job-fill"></span></div>';
  const nums=pct===null?'<span class="job-nums" data-job-nums>已处理 '+done+' 项</span>'
    :'<span class="job-nums" data-job-nums>'+done+' / '+total+' · '+pct+'%</span>';
  return '<div class="job-wait" role="status" aria-live="polite">'
    +'<p class="job-title">'+esc(title||'正在处理')+'</p>'
    +'<p class="job-stage" data-job-stage>'+stage+'</p>'+bar
    +'<div class="row spread job-meta">'+nums+'<span class="small muted">关闭窗口不影响，作业在后台继续</span></div>'
    +'<p class="small muted job-hint">进度来自服务端真实计数（文件数/条目数），完成后会告诉你要等多久、文件在哪。</p></div>';
}
function jobModalShow(title,subtitle){
  modal(title, jobWorkingHtml(null,subtitle||title));
  const dlg=$('#modal');
  try{dlg.dataset.jobPersist='1';}catch(e){}
  return dlg;
}
function jobModalUpdate(job){
  const stage=document.querySelector('[data-job-stage]');
  const nums=document.querySelector('[data-job-nums]');
  const fill=document.querySelector('[data-job-fill]');
  const pct=jobPercent(job);
  if(stage)stage.textContent=(job&&job.stage)||'处理中';
  if(nums){
    const done=Number(job&&job.done)||0,total=Number(job&&job.total)||0;
    nums.textContent=pct===null?('已处理 '+done+' 项'):(done+' / '+total+' · '+pct+'%');
  }
  if(fill&&pct!==null)fill.style.width=pct+'%';
}
async function pollJob(jobId,onUpdate){
  let job=null;
  for(;;){
    job=await api('/jobs/'+encodeURIComponent(jobId));   /* api() 自带 /api 前缀 */
    if(onUpdate)onUpdate(job);
    if(!job||['succeeded','failed','needs_action'].includes(job.status))return job;
    await new Promise(r=>setTimeout(r,1000));
  }
}
/* 统一入口：start() 负责 POST 拿 job_id；本函数负责弹窗+轮询+收尾。
   onSucceeded(job) 返回要展示的 HTML（通常带 result_path）。 */
async function runJobWithProgress(title,start,opts={}){
  // 2026-11-26：模态先于 POST 出现。服务端闸慢（云端复核/试用判定）时，
  // 旧顺序会让用户点完几分钟看不到任何反馈——同类「等待没有进度」缺陷。
  jobModalShow(opts.modalTitle||'正在处理',title);
  let started;
  try{started=await start();}
  catch(e){
    modal(opts.failTitle||'没有完成',`<div class="notice">${esc(e.message||'请求没有成功')}</div>${opts.failHint?'<p class="small muted">'+esc(opts.failHint)+'</p>':''}<button class="primary" type="button" data-action="close-modal">知道了</button>`);
    throw e;
  }
  const jobId=(started&&(started.job_id||started.jobId))||'';
  if(!jobId){
    modal(opts.failTitle||'没有完成','<div class="notice">服务端没有返回作业编号（job_id），无法跟踪进度。</div><button class="primary" type="button" data-action="close-modal">知道了</button>');
    throw Error('服务端没有返回作业编号（job_id），无法跟踪进度。');
  }
  let job;
  try{job=await pollJob(jobId,jobModalUpdate);}
  catch(e){
    modal('进度查询中断',`<div class="notice">${esc(e.message||'查询进度失败')}</div><p class="small muted">作业可能仍在后台跑；稍后可在同一入口重试，或刷新页面后重做。</p><button class="primary" type="button" data-action="close-modal">知道了</button>`);
    throw e;
  }
  if(job.status==='failed'){
    const hint=opts.failHint?('<p class="small muted">'+esc(opts.failHint)+'</p>'):'';
    modal(opts.failTitle||'没有完成',`<div class="notice">${esc(job.message||'作业失败，服务端没有给出原因。')}</div>${job.stage?'<p class="small muted">阶段：'+esc(job.stage)+'</p>':''}${hint}<button class="primary" type="button" data-action="close-modal">知道了</button>`);
    return job;
  }
  if(job.status==='needs_action'){
    modal(opts.failTitle||'需要你处理',`<div class="notice">${esc(job.message||'这个作业需要你先完成一步操作。')}</div><button class="primary" type="button" data-action="close-modal">知道了</button>`);
    return job;
  }
  const html=opts.onSucceeded?opts.onSucceeded(job):'';
  modal(opts.doneTitle||'已完成',html||'<div class="notice">作业已完成。</div><button class="primary" type="button" data-action="close-modal">知道了</button>');
  return job;
}
/* 2026-11-26 同类排查：非项目页（bench/feedback/matrix）的后台作业也要有收尾告知。
   静默轮询到终态报一次人话；onDone 用于就地刷新（扫码窗/评论窗）。 */
const _bgWatchSeen=new Set();
function watchJobBackground(jobId,label,onDone){
  const id=jobId&&typeof jobId==='object'?(jobId.job_id||jobId.id):jobId;
  if(!id||_bgWatchSeen.has(id))return;_bgWatchSeen.add(id);
  pollJob(id).then(j=>{
    const name=label||(JOB_KIND_LABELS[j.kind]||j.kind);
    if(j.status==='succeeded'){toast(name+' 已完成。');if(onDone)onDone(j);}
    else if(j.status==='failed'||j.status==='needs_action'){const f=jobFail(j);toast(name+' 没有完成：'+f.title);if(onDone)onDone(j);}
  }).catch(()=>{});
}
function dataHomeHtml(dh){
  // 2026-11-25 资料外置：如实告诉客户资料存在哪。还在软件文件夹里时不谎称已外置，并把原因说出来。
  if(!dh||!dh.path)return'';
  const reason=String(dh.note||'').split('（这个说明')[0].trim().slice(0,320);
  const state=dh.in_package
    ?`<p class="small" data-home-state="in-package">资料目前还在软件文件夹里面——删除软件文件夹会连资料一起删掉。</p>${reason?`<p class="small muted">${esc(reason)}</p>`:''}`
    :`<p class="small muted" data-home-state="outside">在软件文件夹外面：删除或重装软件不影响这里。换电脑或重装系统时，把整个「${esc(dh.name||'人生重启系统数据')}」文件夹拷走、放回「文档」，资料就回来了。</p>`;
  return `<div class="data-home" id="data-home-box" style="margin-bottom:14px"><p class="small"><strong>你的资料保存在：</strong><span class="data-home-path">${esc(dh.path)}</span> <button type="button" class="small-button" data-action="data-home-reveal">打开文件夹</button> <span class="small muted" data-home-hint></span></p>${state}</div>`;
}
function exportResultHtml(job,label){
  const path=String(job&&job.result_path||'');
  if(!path)return '<div class="notice">'+esc(label||'导出完成')+'，但服务端没有返回文件路径。</div><button class="primary" type="button" data-action="close-modal">知道了</button>';
  const size=job.bytes?(' · '+backupSize(job.bytes)):'';
  return '<div class="notice">'+esc(label||'导出完成')+size+'。</div>'
    +'<p class="small muted">文件已存到：</p>'
    +'<p class="job-path" data-job-path>'+esc(path)+'</p>'
    +'<div class="row"><button class="primary small-button" type="button" data-action="reveal-job" data-job-id="'+esc(job&&job.job_id||job&&job.id||'')+'">打开所在文件夹</button><button class="ghost small-button" type="button" data-action="copy-text" data-text="'+esc(path)+'">复制完整路径</button>'
    +'<span class="small muted" data-reveal-hint>点按钮直接定位到文件；也可以复制路径，粘贴到资源管理器（访达）地址栏、回车。</span></div>'
    +'<button class="primary space" type="button" data-action="close-modal">知道了</button>';
}

/* t54（E3）：原生 confirm → 页内确认条。插在触发点所在容器里，不打断上下文（恢复弹窗里的选择器、
   认知表单里已填的内容都原样保留）；破坏性操作要求输入确认词。样式走 CSSOM（CSP）。 */
function confirmBar(host,message,opts={}){
  const okText=opts.okText||'确定',cancelText=opts.cancelText||'取消',confirmWord=opts.confirmWord||'';
  if(!host||host.querySelector('.confirm-bar'))return Promise.resolve(false);
  if(!confirmBar.css){confirmBar.css=true;ipAddCssRules(['.confirm-bar{margin-top:14px;padding:13px 15px;border:1px solid var(--border-strong);border-left:3px solid var(--gold);border-radius:8px;background:var(--tint)}','.confirm-bar p{margin:0 0 10px}','.confirm-bar-danger{border-left-color:var(--error)}','.confirm-bar .confirm-danger{background:var(--error);border-color:var(--error);color:#fff}']);}
  return new Promise(resolve=>{
    const bar=document.createElement('div');
    bar.className='confirm-bar'+(opts.danger?' confirm-bar-danger':'');
    bar.innerHTML='<p class="small">'+esc(message)+'</p>'+(confirmWord?'<label class="field"><span>输入「'+esc(confirmWord)+'」以确认</span><input type="text" data-confirm-word autocomplete="off"></label>':'')+'<div class="actions"><button type="button" class="primary'+(opts.danger?' confirm-danger':'')+'" data-confirm-ok'+(confirmWord?' disabled':'')+'>'+esc(okText)+'</button><button type="button" data-confirm-cancel>'+esc(cancelText)+'</button></div>';
    host.appendChild(bar);
    const ok=bar.querySelector('[data-confirm-ok]'),word=bar.querySelector('[data-confirm-word]');
    const done=v=>{bar.remove();resolve(v);};
    ok.addEventListener('click',()=>done(true),{once:true});
    bar.querySelector('[data-confirm-cancel]').addEventListener('click',()=>done(false),{once:true});
    if(word)word.addEventListener('input',()=>{ok.disabled=word.value.trim()!==confirmWord;});
    bar.scrollIntoView({block:'nearest'});
  });
}
// 2026-11-10（试用反馈 P1-6）：所有复制入口共用——剪贴板被拒先回退 execCommand，
// 仍失败就明说并给全文手动复制，不能一律 toast「已复制」。
async function copyWithFeedback(text,successMsg){
  let ok=false;
  try{await navigator.clipboard.writeText(text);ok=true;}
  catch(e){
    const box=document.createElement('textarea');box.value=text;box.style.position='fixed';box.style.opacity='0';document.body.appendChild(box);box.select();
    try{ok=document.execCommand('copy');}catch(e2){ok=false;}
    box.remove();
  }
  if(!ok){ipAddCssRules(['.copy-fallback-text{width:100%;height:260px}']);
    modal('复制失败，请手动复制',
    '<div class="rule"><p class="small">浏览器拒绝了剪贴板权限（常见于 http 局域网访问或旧浏览器）。下面是全文，请全选后手动复制。</p>'+
    '<textarea class="input copy-fallback-text" readonly>'+esc(text)+'</textarea></div>');return false;}
  toast(successMsg);return true;
}
function pill(t,c=''){return `<span class="pill ${c}">${esc(t)}</span>`;}
function complianceBasis(h){const b=h.basis;if(b&&typeof b==='object')return [b.title,b.issuer,b.detail].filter(Boolean).join(' · ');return String(b||'');}
function complianceBody(r){
  if(!r)return `<div class="row spread"><strong>合规检查</strong>${pill('保存后显示','')}</div><p class="small muted">保存草稿后显示必删词、风险表达与替换建议；确认与导出时服务端会再查一次。</p>`;
  const hits=(r.hits||[]).filter(h=>h.level==='block'||h.level==='warn');
  const blocks=hits.filter(h=>h.level==='block'),warns=hits.filter(h=>h.level==='warn');
  const waived=r.waivers||[];
  // 试用反馈 1C：必删类默认硬拦；只有本人在确认前逐项勾选并写明依据，才本次放行并留记录。
  const canWaive=!!(state.project&&(!state.project.confirmed_version_id||state.project.draft_changed));
  const items=hits.slice(0,12).map(h=>{
    const isBlock=h.level==='block';
    const rec=waived.find(w=>String(w.id)===String(h.id));
    const done=!!h.waived||!!rec;
    const waive=isBlock&&canWaive
      ?`<div class="space"><label class="check small"><input type="checkbox" name="compliance-waiver" value="${esc(h.id)}" ${done?'checked':''}> 本人确认是误判，本次放行</label><input class="input" name="compliance-waiver-reason" data-waiver-for="${esc(h.id)}" maxlength="200" placeholder="写明依据（必填），例如：引用古语，不是效果承诺" value="${esc(rec?rec.reason:'')}"></div>`
      :(done?`<p class="small">已由本人确认放行${rec&&rec.reason?'：'+esc(rec.reason):''}</p>`:'');
    return `<div class="compliance-hit ${esc(h.level)}"><div class="row spread"><span><strong>${esc(h.term)}</strong> ${pill(h.level==='block'?'必删':'提醒',h.level==='block'?'error':'warn')}</span><span class="small muted">${esc(complianceBasis(h))}</span></div>${h.reason?`<p class="small">${esc(h.reason)}</p>`:''}<p class="small muted">命中：${esc(h.snippet||'')}</p>${h.suggestions?.length?`<p class="small">建议：${esc(h.suggestions.join(' / '))}</p>`:''}${waive}${isBlock?`<div class="space"><button class="small-button" data-action="report-false-positive" data-term="${esc(h.term)}" data-level="${esc(h.level)}" type="button">认为误拦？提交这个词</button></div>`:''}</div>`;
  }).join('');
  const head=r.ok?(waived.length?`<strong>合规检查通过</strong>${pill(waived.length+' 处已确认放行','warn')}`:`<strong>合规检查通过</strong>${pill('无必删表达','ok')}`):`<strong>有 ${blocks.length} 处必删表达</strong>${pill('确认 / 导出会被拦截','error')}`;
  return `<div class="row spread">${head}</div>${r.message?`<p class="small muted">${esc(r.message)}</p>`:''}${items?`<div class="compliance-hits">${items}</div>`:'<p class="small muted">未发现必删词或风险表达；定稿确认与导出时仍会由服务端复查。</p>'}<p class="small muted space">规则库 ${esc(r.lexicon_version||'')} · 必删 ${blocks.length} · 已放行 ${waived.length} · 提醒 ${warns.length} · 仅记录命中，不改写正文。</p>`;
}
const COMPLIANCE_BLOCK_NAMES={hook:'第 1 段',content:'第 2 段',ending:'第 3 段',anchor:'可选段',quote:'可选段'};
function complianceBlocks(p){
  const parts=(p&&p.draft)||[]; const spans=[]; let off=0;
  parts.forEach(part=>{const text=String(part.text||'');spans.push({role:part.role,start:off,end:off+text.length});off+=text.length+2;});
  const out={};
  ((p&&p.compliance&&p.compliance.hits)||[]).forEach(h=>{
    const at=Number(h.start)||0;
    const span=spans.find(x=>at>=x.start&&at<=x.end)||spans[spans.length-1];
    if(!span)return;
    (out[span.role]=out[span.role]||[]).push(h);
  });
  return out;
}
function complianceInline(role,byBlock){
  const hits=(byBlock||{})[role]||[];
  if(!hits.length)return '';
  const blocks=hits.filter(h=>h.level==='block').length, warns=hits.length-blocks;
  const rows=hits.map(h=>`<div class="block-compliance-hit ${esc(h.level)}"><div class="row spread"><span><strong>${esc(h.term)}</strong> ${pill(h.level==='block'?'必删':'提醒',h.level==='block'?'error':'warn')}</span><span class="small muted">${esc(COMPLIANCE_BLOCK_NAMES[role]||role)}</span></div><p class="bc-sentence">${esc(h.sentence||h.snippet||'')}</p>${h.reason?`<p class="small muted">${esc(h.reason)}</p>`:''}${h.suggestions&&h.suggestions.length?`<p class="small">替换建议：${esc(h.suggestions.join(' / '))}</p>`:''}${h.level==='block'?'<p class="small muted">必删：改掉这一句，或在顶部合规框逐条放行（要写依据）。</p>':''}</div>`).join('');
  return `<div class="block-compliance" data-compliance-block="${esc(role)}"><p class="block-compliance-title">这段命中 ${blocks} 处必删、${warns} 处提醒 · 整句已就地贴出</p>${rows}</div>`;
}
function refreshBlockCompliance(){
  const p=state.project; if(!p||!p.compliance)return;
  const by=complianceBlocks(p);
  document.querySelectorAll('[data-compliance-block]').forEach(host=>{
    const role=host.dataset.complianceBlock;
    const html=complianceInline(role,by);
    if(!html){host.remove();return;}
    const tmp=document.createElement('div');tmp.innerHTML=html;
    if(tmp.firstElementChild)host.replaceWith(tmp.firstElementChild);
  });
  if(!document.querySelector('.block-compliance')&&ipAddCssRules){
    ipAddCssRules(['.block-compliance{margin:-4px 0 14px;padding:10px 12px;border:1px solid var(--border);border-left:3px solid var(--error);border-radius:9px;background:var(--surface)}','.block-compliance-title{margin:0 0 8px;font-size:12px;color:var(--muted)}','.block-compliance-hit{padding:8px 0;border-top:1px solid var(--border)}','.block-compliance-hit:first-of-type{border-top:0;padding-top:0}','.block-compliance-hit.warn{border-left:2px solid var(--warn);padding-left:9px;margin-left:-11px;opacity:.9}','.block-compliance-hit.hint{opacity:.75}','.bc-sentence{margin:5px 0;font-size:13px;line-height:1.75;color:var(--text)}']);
  }
}
function complianceHtml(r){return `<div class="compliance-box${r&&r.ok?'':' has-block'}" id="compliance-box">${complianceBody(r)}</div>`;}
function paintCompliance(r){const box=$('#compliance-box');if(box){box.className='compliance-box'+(r&&r.ok?'':' has-block');box.innerHTML=complianceBody(r);}if(state.project){state.project.compliance=r;refreshBlockCompliance();}}
// 收集本人逐项确认放行的必删命中；没写依据就不提交，避免无记录放行。
function collectComplianceWaivers(){
  const out=[],missing=[];
  document.querySelectorAll('input[name="compliance-waiver"]:checked').forEach(box=>{
    const el=Array.from(document.querySelectorAll('[data-waiver-for]')).find(x=>x.dataset.waiverFor===box.value);
    const reason=(el&&el.value||'').trim();
    if(!reason){const hit=box.closest('.compliance-hit');missing.push(hit&&hit.querySelector('strong')?hit.querySelector('strong').textContent.trim():box.value);}
    out.push({id:box.value,reason});
  });
  if(missing.length)throw Error('请先给「'+missing.join('」「')+'」写明放行依据（必填）。');
  return out;
}
function notice(t){return `<div class="notice">${esc(t)}</div>`;}
function empty(t,s){return `<div class="empty"><h3>${esc(t)}</h3><p>${esc(s)}</p></div>`;}
function fmt(ms){const n=Math.round(ms/1000);return `${Math.floor(n/60)}:${String(n%60).padStart(2,'0')}`;}
// 2026-11-25：讲稿页只有一个整篇编辑框，客户不用再分开写「开头 / 主体 / 结尾」。
// 数据结构不变（仍是 parts 五段）。保存时整篇自动归段：第一行 → 开头，最后一行 → 结尾，中间 → 主体；
// 一行都没有分段就整篇进主体。这样发布文案（开头＋结尾）、标题候选、导演计划这些下游照常有字段可用。
function draftWholeText(p){return (p.draft||[]).filter(x=>String(x.text||'').trim()).map(x=>String(x.text).trim()).join('\n\n');}
function wholeToParts(text){
  const whole=String(text||'').trim(),lines=whole?whole.split('\n'):[],n=lines.length;
  const hook=n>=2?String(lines[0]).trim():'';
  const ending=n>=3?String(lines[n-1]).trim():'';
  const content=n>=3?lines.slice(1,-1).join('\n').trim():(n===2?String(lines[1]).trim():whole);
  return [{role:'hook',label:roles[0][1],text:hook},{role:'content',label:roles[1][1],text:content},{role:'ending',label:roles[4][1],text:ending}];
}
const squashText=s=>String(s||'').replace(/\s+/g,'');
function missingCore(form){return String(form?.elements?.whole?.value||'').trim()?[]:['whole'];}
function scriptMissingText(missing){return missing.length?'还没写内容，写几句才能确认。':'内容写好了，读一遍，确认后进入拍摄。';}
function missingDraftLabels(draft){return squashText(draftWholeText({draft}))?[]:['稿件内容'];}
function updateScriptGate(form){
  const missing=missingCore(form),btn=form?.querySelector('[data-action="confirm"]'),tip=document.getElementById('script-missing');
  if(btn)btn.disabled=missing.length>0;
  if(tip)tip.textContent=scriptMissingText(missing);
}
function stamp(t){const d=new Date(t);return t&&Number.isFinite(d.getTime())?new Intl.DateTimeFormat('zh-CN',{month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit',timeZone:'Asia/Shanghai'}).format(d):'';}
function purl(id,step='script'){return `#/project/${id}/${step}`;}
function currentBrand(){return (state.boot&&(state.boot.brand_name||state.boot.profile?.name))||'人生重启系统';}
function applyBrand(value){
  const next=String(value||'').trim()||'人生重启系统',prev=state.brandShown||'';
  state.brandShown=next;
  const initial=Array.from(next)[0]||'人';
  const mark=$('#brand-mark'),name=$('#brand-name'),mini=$('#brand-name-mini'),avatar=$('#brand-avatar');
  if(mark&&!mark.querySelector('img'))mark.textContent=initial;if(name)name.textContent=next;if(mini)mini.textContent=next;if(avatar)avatar.textContent=initial;
  document.title=next;
  const account=document.querySelector('#cover-form input[name="account"]');
  if(account&&(!account.value.trim()||account.value==='@'+prev||account.value==='@'+next))account.value='@'+next;
  const profileName=document.querySelector('#profile-form input[name="name"]');
  if(profileName&&!document.activeElement?.matches('#profile-form input,#profile-form textarea'))profileName.value=next;
}
function editNameDialog(){
  const current=currentBrand(),fallback=state.boot?.brand_default||'';
  modal('我的名字',`<form id="brand-name-form"><label class="field"><span>名字 / 账号名</span><input name="name" type="text" maxlength="30" value="${esc(current===fallback?'':current)}" placeholder="例如：小王，或你的账号名" autocomplete="off"></label><p class="small muted">保存后立即出现在侧栏 logo、浏览器标签、封面默认账号和导出文件名上。${fallback?'留空则显示「'+esc(fallback)+'」。':'留空则恢复默认占位。'}</p><button class="primary" type="submit">保存名字</button></form>`);
  setTimeout(()=>{const input=$('#brand-name-form input[name="name"]');if(input){input.focus();input.select();}},60);
}
// t111：抖音登录“锁”状态徽标。cookie 持久化在 app/.runtime/douyin_cookie.json；
// 徽标只读 /douyin/login，过期/未登录时可点此走 t107 扫码入口，不暴露 cookie 值。
function ensureDouyinBadge(){
  const actions=document.querySelector('.top-actions');
  if(!actions||document.getElementById('douyin-login-badge'))return document.getElementById('douyin-login-badge');
  actions.insertAdjacentHTML('afterbegin','<button id="douyin-login-badge" class="small-button" type="button" title="抖音登录状态">抖音登录 · 检查中…</button>');
  return document.getElementById('douyin-login-badge');
}
function paintDouyinBadge(st){
  const el=ensureDouyinBadge();if(!el)return;
  const s=st||state.douyinLogin||{};
  el.dataset.configured=s.configured?'1':'0';
  if(s.configured){
    let when='';if(s.last_ok_at){const d=new Date(s.last_ok_at);if(!isNaN(d))when=d.toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'});}
    // 2026-11-25：标签上只写状态，不写时间（Fluent 2 Badge：文字一两个词，详情放悬停提示）
    el.textContent='抖音已登录';
    el.title='登录态已持久化'+(when?'，最近登录 '+when:'')+'；点此确认状态';
  }else{
    const expired=s.state==='expired'||/过期/.test(s.reason||'');
    el.textContent='抖音'+(expired?'登录已过期':'未登录')+' · 点此扫码';
    el.title=(s.reason||'还没有登录态')+'；点此打开扫码登录';
  }
}
async function refreshDouyinBadge(){
  ensureDouyinBadge();
  try{const s=await api('/douyin/login');state.douyinLogin=s;paintDouyinBadge(s);}
  catch(e){const el=document.getElementById('douyin-login-badge');if(el){el.dataset.configured='0';el.textContent='抖音状态未知 · 点此检查';el.title=e.message||'';}}
}
// t128：第二大脑导入（JSON/ZIP）与升级提示。数据在本机，导入按 id/标题去重。
async function importBrainFile(file, replace=false){
  if(!file)return null;
  // t177：上传段用 XHR 的 upload.onprogress 报真字节，处理段轮询作业进度（1 秒级）。
  modal('正在导入知识库', jobWorkingHtml(null,'上传文件 '+(file.name||'')+' · '+backupSize(file.size)));
  const uploadPct=p=>{
    const stage=document.querySelector('[data-job-stage]');
    const nums=document.querySelector('[data-job-nums]');
    const fill=document.querySelector('[data-job-fill]');
    if(stage)stage.textContent='上传文件（'+backupSize(file.size)+'）';
    if(nums)nums.textContent='上传 '+p+'%';
    if(fill)fill.style.width=p+'%';
  };
  const started=await new Promise((resolve,reject)=>{
    const xhr=new XMLHttpRequest();
    xhr.open('POST','/api/brain/import?replace='+(replace?'1':'0'));
    xhr.setRequestHeader('Content-Type',file.type||'application/octet-stream');
    xhr.upload.onprogress=e=>{if(e.lengthComputable)uploadPct(Math.round(e.loaded/e.total*100));};
    xhr.onload=()=>{
      let data={};try{data=JSON.parse(xhr.responseText||'{}');}catch(err){}
      if(xhr.status<200||xhr.status>=300||data.ok===false){
        const msg=(data&&(data.error||data.message))||('导入失败（HTTP '+xhr.status+'）');
        reject(Error(msg));return;
      }
      resolve(data);
    };
    xhr.onerror=()=>reject(Error('上传失败：网络中断或服务没响应。'));
    xhr.send(file);
  });
  const data=await runJobWithProgress('导入知识库',
    ()=>Promise.resolve(started),
    {modalTitle:'正在导入知识库',doneTitle:'导入完成',
     onSucceeded:job=>{
       const r=job.result||{};
       const imported=r.imported!=null?r.imported:'-',skipped=r.skipped!=null?r.skipped:'-',replaced=r.replaced||0;
       return '<div class="notice">导入完成：新增 '+esc(String(imported))+' 条，跳过 '+esc(String(skipped))+' 条'+(replaced?'，覆盖 '+esc(String(replaced))+' 条':'')+'。</div>'
         +'<button class="primary" type="button" data-action="close-modal">知道了</button>';
     }});
  toast('导入完成，列表已刷新。');
  await route();
  return data;
}
function ensureUpgradeBanner(){
  const main=document.getElementById('main');if(!main)return null;
  if(document.getElementById('upgrade-banner'))return document.getElementById('upgrade-banner');
  main.insertAdjacentHTML('afterbegin','<section class="panel space" id="upgrade-banner"><div class="row spread"><div><strong>新版本已就绪</strong><p class="small muted">你的知识库数据在本机，升级不丢。建议先导出备份；换机或重装可用导入恢复。</p></div><div class="row"><a class="button-link primary" id="upgrade-export" href="/api/brain/export" download="重启系统-整体导出.zip">导出备份</a><label class="button-link">导入备份<input id="upgrade-import-file" type="file" accept=".json,.zip" hidden></label><button type="button" class="small-button" id="upgrade-dismiss">稍后</button></div></div><p class="small muted">不着急也可以继续用；升级不会清空本机数据。</p></section>');
  const exportLink=document.getElementById('upgrade-export');
  if(exportLink){
    // t177：不再是裸下载链接——点一下走作业：进度弹窗 → 完成后告诉你文件存到哪。
    exportLink.removeAttribute('href');exportLink.removeAttribute('download');
    exportLink.setAttribute('role','button');exportLink.setAttribute('tabindex','0');
    const runExport=async ev=>{
      if(ev)ev.preventDefault();
      await runJobWithProgress('导出资产（重启系统整体导出）',
        ()=>api('/brain/export','POST',{format:'zip'}),
        {modalTitle:'正在导出资产',doneTitle:'导出完成',onSucceeded:job=>exportResultHtml(job,'资产导出完成')});
    };
    exportLink.addEventListener('click',runExport);
    exportLink.addEventListener('keydown',ev=>{if(ev.key==='Enter'||ev.key===' '){ev.preventDefault();runExport();}});
  }
  document.getElementById('upgrade-dismiss').addEventListener('click',async()=>{
    const b=document.getElementById('upgrade-banner');
    try{sessionStorage.setItem('ip-upgrade-dismissed:'+(b&&b.dataset.build||''),'1');}catch(e){}
    if(b)b.remove();
    try{await api('/version/seen','POST',{});}catch(e){}
  });
  return document.getElementById('upgrade-banner');
}
async function maybeShowUpgradeBanner(){
  try{
    if(!location.hash.startsWith('#/brain')&&!location.hash.startsWith('#/settings'))return;
    const r=await api('/version/status');
    if(!r||(!r.first_run&&!r.upgraded))return;
    const key='ip-upgrade-dismissed:'+r.build;
    if(sessionStorage.getItem(key))return;
    const b=ensureUpgradeBanner();if(b)b.dataset.build=r.build||'';
  }catch(e){}
}
// t139：资产备份提醒——每周或新增 10 条认知后温和提示；可稍后/关闭，不每次弹。
function assetBackupRecord(){
  try{return JSON.parse(localStorage.getItem('ip-asset-backup-v1')||'{}')||{};}catch(e){return {};}
}
function assetBackupSave(patch){
  try{localStorage.setItem('ip-asset-backup-v1',JSON.stringify({...assetBackupRecord(),...patch}));}catch(e){}
}
function assetKnowledgeCount(){
  try{const n=Number((state.boot&&state.boot.knowledge_count)||($('#brain-count')&&$('#brain-count').textContent)||0);return Number.isFinite(n)?n:0;}catch(e){return 0;}
}
function assetBackupDue(){
  const r=assetBackupRecord();if(r.disabled)return false;
  const now=Date.now(),count=assetKnowledgeCount();
  if(!count)return false;
  if(r.snoozeUntil&&now<r.snoozeUntil)return false;
  if(r.lastAt&&now-r.lastAt>=7*86400000)return true;
  if(r.lastPromptAt&&now-r.lastPromptAt>=7*86400000)return true;
  if(!r.lastAt&&!r.lastPromptAt)return true;
  return count-(Number(r.baseline)||0)>=10;
}
function showAssetBackupReminder(){
  const main=document.getElementById('main');if(!main||document.getElementById('asset-backup-banner'))return;
  if(!assetBackupDue())return;
  const count=assetKnowledgeCount();
  const html='<section class="panel space" id="asset-backup-banner"><div class="row spread"><div><strong>给资产做个备份</strong><p class="small muted">你的重启系统资产（认知和作品）可以整体导出带走。现在是 '+count+' 条认知；建议每周或新增 10 条后备份一次，不着急。</p></div><div class="row"><a class="button-link primary" id="asset-backup-banner-export" href="'+media('/api/brain/export')+'" download="重启系统-整体导出.zip">导出资产</a><button type="button" class="small-button" id="asset-backup-snooze">稍后</button><button type="button" class="small-button" id="asset-backup-disable">关闭提醒</button></div></div></section>';
  const up=document.getElementById('upgrade-banner');
  if(up)up.insertAdjacentHTML('afterend',html);else main.insertAdjacentHTML('afterbegin',html);
  const now=Date.now();
  assetBackupSave({lastPromptAt:now,baseline:count});
  const b=document.getElementById('asset-backup-banner');
  const exp=document.getElementById('asset-backup-banner-export');
  if(exp)exp.addEventListener('click',()=>{assetBackupSave({lastAt:Date.now(),baseline:assetKnowledgeCount()});if(b)b.remove();});
  const sn=document.getElementById('asset-backup-snooze');
  if(sn)sn.addEventListener('click',()=>{assetBackupSave({snoozeUntil:Date.now()+3*86400000});if(b)b.remove();});
  const dis=document.getElementById('asset-backup-disable');
  if(dis)dis.addEventListener('click',()=>{assetBackupSave({disabled:true});if(b)b.remove();});
}
/* t175：第二大脑导航入口已取消（需求单 D 方案A）。计数节点可能不存在，
   这里统一空安全写入，避免 boot() 因 null.textContent 抛错导致全站白屏。 */
function setNavCount(value){
  const el=document.getElementById('brain-count');
  if(el)el.textContent=value;
}
async function boot(force){const _now=Date.now();if(!force&&state.boot&&state._bootAt&&_now-state._bootAt<5000){state.lanToken=state.boot.lan_token||'';setNavCount(state.boot.knowledge_count);   /* t175：导航入口已删，写入改走空安全函数 */applyBrand(state.boot.brand_name);paintTrial(state.boot.trial);paintCredits(state.boot.billing);refreshDouyinBadge();startShareReflux();return state.boot;}let _slot=window.__bootPrefetch;if(!_slot||_now-_slot.at>5000){_slot=window.__bootPrefetch={at:_now,p:api('/bootstrap').catch(()=>null)};}const _preData=await _slot.p;state.boot=_preData||await api('/bootstrap');state._bootAt=Date.now();state.lanToken=state.boot.lan_token||'';setNavCount(state.boot.knowledge_count);   /* t175：导航入口已删，写入改走空安全函数 */applyBrand(state.boot.brand_name);paintTrial(state.boot.trial);paintCredits(state.boot.billing);refreshDouyinBadge();startShareReflux();}
// 2026-11-16（性能 E）：作品列表已从 bootstrap 移出，只在需要它的页面前按需取一次。
async function ensureBootProjects(){if(!state.boot)return;state.bootProjectsError='';try{state.boot.projects=(window.__bootProjectsPromise&&await window.__bootProjectsPromise)||await api('/projects');}catch(e){state.bootProjectsError=e.message||'作品列表读取失败';state.boot.projects=[];}}
function billingPlanLabel(b,plan){
  const plans=(b&&b.plans)||{};
  return (plans[plan]&&plans[plan].label)||'免费';
}
function paintCredits(b){
  // D12：单一会员订阅制。顶栏保留「会员中心」入口；
  // 2026-11-18 云端积分真源：登录账号另显示「知识库积分（云端）」，
  // 数据只来自 summary().cloud_wallet（云端 auth_points），绝不拿本地账本余额冒充。
  state.memberBilling=b||null;
  const old=document.getElementById('credit-pill');
  if(old)old.remove();
  paintCloudWallet(b&&b.cloud_wallet);
  paintMemberEntry();
}
function paintCloudWallet(w){
  const old=document.getElementById('cloud-wallet-pill');
  if(old)old.remove();
  if(!w||w.balance==null)return; // 未登录/从未读到云端余额：不显示，避免误导
  const bar=document.querySelector('.top-actions');if(!bar)return;
  const el=document.createElement('button');
  el.id='cloud-wallet-pill';el.type='button';
  el.className='member-entry '+(w.stale?'is-expired':'is-free');
  const avail=(w.available==null?w.balance:w.available);
  el.textContent='积分 '+avail+(w.stale?'（离线）':'');
  el.title=w.stale?'离线，显示最近一次云端余额；联网后自动刷新'
    :('知识库积分（云端）：可用 '+avail+' / 账面 '+w.balance+'；点击打开会员中心');
  el.setAttribute('aria-label','知识库积分（云端）');
  el.addEventListener('click',()=>openBilling());
  const anchor=document.getElementById('member-entry');
  if(anchor&&anchor.parentNode===bar)bar.insertBefore(el,anchor.nextSibling);
  else bar.insertBefore(el,bar.firstChild);
}
// 会员中心里的云端积分区块：真源余额 + 本地商品额度分行标注，两个账本不混淆。
function memberFreeNow(s){
  // 2026-11-19 t152：会员期内免费。后端字段优先，memberState 兜底，避免缓存/旧字段漏判。
  return !!(s&&(s.member_free||memberState(s).key==='active'));
}
function cloudWalletHtml(s){
  const w=s&&s.cloud_wallet;
  if(!w)return '';
  if(memberFreeNow(s)){
    const expiry=s.member_expire_text?(' · 有效期至 '+esc(s.member_expire_text)):'';
    return '<section class="member-section"><div class="member-section-head"><h3>知识库额度</h3><span>会员尊享 · 额度已包含</span></div>'+
      '<p><strong>会员尊享 · 额度已包含</strong>'+expiry+'</p>'+
      '<p class="small muted">会员有效期内，知识库问答、封面、导出等权益均可使用，不另扣积分；到期后按会员价格续费。</p></section>';
  }
  const head='<section class="member-section"><div class="member-section-head"><h3>积分与账本说明</h3><span>高级</span></div>';
  if(w.balance==null){
    const msg=(w.error==='UNAUTHORIZED'||w.error==='NOT_LOGGED_IN')
      ?'请登录云端账号后查看知识库积分。'
      :'云端暂时连不上，余额稍后自动刷新；问自己的记录（本地直答）不受影响。';
    return head+'</div><p class="small muted">'+esc(msg)+'</p></section>';
  }
  const avail=(w.available==null?w.balance:w.available);
  const price=(w.brain_ask_points==null?'':' · 每次问答 '+esc(String(w.brain_ask_points))+' 积分');
  const local=(s.balance&&s.balance.balance_credits!=null)
    ?('<p class="small muted">本地商品额度（封面/形象等，本机账本专用）：剩余 '
      +esc(String(s.balance.balance_credits))+'，与云端积分互不相通。</p>'):'';
  const body='<p><strong>'+esc(String(avail))+'</strong> 积分可用 · 账面 '+esc(String(w.balance))
    +(w.held?(' · 冻结 '+esc(String(w.held))):'')+price+'</p>'
    +'<p class="small muted">模型问答按次扣云端积分；充值请联系管理员在后台加分。每日 token 上限是另一道成本闸，两者互不替代。</p>'
    +local;
  return '<details class="member-advanced"><summary>积分与账本说明（高级）</summary>'+body+'</details>';
}
function memberSeatLine(s){
  if(memberFreeNow(s))return '';
  return '创始会员 · 限量席位';
}
function memberPolicyHtml(s){
  const n=Number(s&&s.free_seats);
  const seat=Number.isFinite(n)&&n>0?('当前还有 '+n+' 个创始会员席位（名额由云端控制，是否在席位内以账号实际权益为准）。'):'本轮创始会员席位已满，可开通尊享会员继续。';
  return '<details class="member-advanced"><summary>查看会员说明</summary><p class="small muted">'+esc(seat)+'</p><p class="small muted">会员权益按账号实际下发展示；到期不自动扣费，续费由你主动下单。</p></details>';
}
async function refreshBilling(force){
  // force=true：?refresh=1 跳过云端余额 ≤15s 缓存直读真源（问答结束后调用）。
  try{const b=await api('/billing/summary'+(force?'?refresh=1':''));paintCredits(b);return b;}catch(e){return null;}
}
function billingKindLabel(k){
  return {grant:'赠送',purchase:'兑换',adjust:'调整',hold:'冻结',settle:'结算',release:'退回',void:'过期作废'}[k]||k||'';
}
function entitlementDate(v){
  // 云端 expires_at 是 epoch 秒（数字），后台也可能下发 YYYY-MM-DD 字符串。
  if(v==null||v==='')return '';
  if(typeof v==='number'||/^\d+(\.\d+)?$/.test(String(v))){
    const d=new Date(Number(v)*1000);
    if(!isNaN(d.getTime()))return d.toISOString().slice(0,10);
  }
  return String(v).slice(0,10);
}
// ---- 续费流程（2026-11-17）：倒计时与「会失去什么」都只用云端下发的到期时间/权益，不猜、不造 ----
// expires_at 可能是 epoch 秒、epoch 毫秒或 ISO 串；认不出返回 0，调用方按「没有到期日」处理。
function expiryMs(v){
  if(v==null||v==='')return 0;
  if(typeof v==='number'||/^\d+(\.\d+)?$/.test(String(v))){
    const n=Number(v);
    if(!isFinite(n)||n<=0)return 0;
    return n>1e12?n:n*1000;
  }
  const t=new Date(String(v)).getTime();
  return isNaN(t)?0:t;
}
function daysToExpiry(v){
  const t=expiryMs(v);
  if(!t)return null;
  return Math.ceil((t-Date.now())/86400000);
}
// 「试用/到期后会失去」的清单：只列云端真下发的权益，没下发的项一律不写。
function lossItems(ent){
  const e=ent||{},out=[];
  const has=(o,k)=>Object.prototype.hasOwnProperty.call(o,k);
  if(has(e,'exports_per_month'))
    out.push(e.exports_per_month==null?'不限次导出':(e.exports_per_month+' 次/月导出'));
  if(has(e,'max_height'))
    out.push(e.max_height==null?'全清晰度导出':('导出上限 '+e.max_height+'p'));
  if(has(e,'allow_cloud_asr'))out.push('云端转写');
  if(e.all_style_packs)out.push('全部包装风格');
  if(e.vip_service)out.push('专属客服');
  return out;
}
function memberExpiry(s){
  const t=(s&&s.trial)||{},m=(s&&s.membership)||{};
  return t.expires_at||m.expire_at||m.expireAt||'';
}
// ---- 会员中心（2026-11-15，按用户定义 D12/D13 重做）------------------------
// 产品定义：单一会员订阅制 —— **没有月卡、没有多档套餐、没有积分**；
// 新用户注册即享 1 天免费试用（license.py TRIAL_DAYS=1，注册起算、云端落库）。
// 页面只呈现：会员身份标识 / 试用阶梯 / 会员权益 / 开通方式 / 兑换记录。
// 不呈现：积分余额、赠送·兑换·冻结分桶、积分流水、按量扣、三档档位卡、价格数字。
// 视觉：克制材质 + 信息秩序（腾讯米大师的卡片式分区与四级权重），不是黑金满底。
// 开通链路（2026-11-16 起）：微信 Native 动态码 —— 点开通 → 出专属二维码 → 扫码付款
// → 回调自动发权益。页面不再展示任何静态收款图；卡密兑换保留作为备用通道。
// 专属冠徽：纯 SVG 图形，只做身份标识，不新增/不改动任何文案与信息结构。
// 颜色全部走 --m-* 变量，深浅主题自动适配；不写内联 style（CSP style-src 'self'）。
function memberTierName(s){
  // t152 的 free_seats 行为/数值完全不动，只用于呈现名称。
  const n=Number(s&&s.free_seats);
  return Number.isFinite(n)&&n>0?'创始会员':'尊享会员';
}
function memberPremiumStyles(){
  if(window.__memberPremiumStyles)return;window.__memberPremiumStyles=true;
  ipAddCssRules([
    '.member-gem{position:relative}',
    '.member-halo{position:absolute;inset:-9px;border:1px solid var(--m-gold-soft);border-radius:50%;opacity:.32;box-shadow:0 0 18px rgba(214,178,106,.25);pointer-events:none}',
    '.member-hero-premium .member-halo{animation:member-halo 4s ease-in-out infinite}',
    '@keyframes member-halo{0%,100%{transform:scale(.94);opacity:.22}50%{transform:scale(1.06);opacity:.5}}',
    '.member-premium-badges{display:flex;flex-wrap:wrap;gap:8px;margin-top:12px}',
    '.member-premium-badge{font-size:11px;line-height:1;padding:7px 11px;border:1px solid var(--m-gold-dim);border-radius:999px;color:var(--m-gold-soft);background:rgba(214,178,106,.08);letter-spacing:.6px}',
    '.member-premium-badge.is-primary{border-color:var(--m-gold-soft);background:rgba(214,178,106,.16);color:var(--m-on)}',
    '.member-founder{display:inline-flex;align-items:center;gap:6px;margin:8px 0 0;color:var(--m-gold-soft);font-size:12px;letter-spacing:1px}',
    '.member-founder::before{content:"◇";font-size:13px}',
    '@media(max-width:720px){.member-premium-badges{gap:6px}.member-premium-badge{font-size:10px;padding:6px 9px}}'
  ]);
}
function memberCrestHtml(){
  return '<span class="member-gem" aria-hidden="true"><span class="member-halo"></span>'+
    '<svg class="member-crest" viewBox="0 0 64 64" focusable="false" aria-hidden="true">'+
      '<circle class="mc-field" cx="32" cy="32" r="23.5"/>'+
      '<circle class="mc-ring-outer" cx="32" cy="32" r="29"/>'+
      '<circle class="mc-ring-inner" cx="32" cy="32" r="23.5"/>'+
      '<path class="mc-crown" d="M15 41 L17.5 24 L25 31 L32 18 L39 31 L46.5 24 L49 41 Z"/>'+
      '<path class="mc-crown-base" d="M15 43 H49 V46.5 H15 Z"/>'+
      '<circle class="mc-star" cx="17.5" cy="21" r="1.5"/>'+
      '<circle class="mc-star" cx="32" cy="15" r="1.7"/>'+
      '<circle class="mc-star" cx="46.5" cy="21" r="1.5"/>'+
    '</svg></span>';
}
function memberState(s){
  const m=s.membership||{},t=s.trial||{};
  // license.state() 的 mode 只有 trial|active|expired|tampered；read_only 是布尔。
  if(t.mode==='trial')return {key:'trial',label:'体验中'};
  if(t.mode==='expired')return {key:'expired',label:'体验已结束'};
  if(t.mode==='tampered'||t.read_only)return {key:'readonly',label:'只读'};
  if(t.mode==='active'){
    // 2026-11-16：不再用 vip_level 判会员。该列只有年度会员路径会写，
    // 兑换 extend_days / 白名单等路径会漏，出现「权益生效但显示已解锁」。
    // 云端下发的 source:'cloud'（来自 /api/auth/me 的 membership_state）才是权威；
    // 本地 entitlement 兜底仍是「已解锁」，不是会员。
    if(t.source==='cloud'||m.active)return {key:'active',label:memberTierName(s)};
    return {key:'unlocked',label:'已解锁'};
  }
  return {key:'free',label:'尚未开通'};
}
function memberIdentityHtml(s){
  memberPremiumStyles();
  const m=s.membership||{},t=s.trial||{};
  const st=memberState(s);
  // 会员到期：云端 license.state() 的 expires_at 是 ISO 串；本地 membership 兜底用 epoch。
  const expiry=entitlementDate(t.expires_at||m.expire_at||m.expireAt||'');
  const hours=Number(t.hours_left||0);
  let headline,sub;
  if(st.key==='active'){
    headline='会员尊享 · 全功能畅享';
    sub='你的会员权益已激活'+(expiry?' · 有效期至 '+esc(expiry):'')+' · 到期不自动扣费';
  }else if(st.key==='trial'){
    headline=Number.isFinite(hours)&&hours>0?('体验剩余 '+hours+' 小时'):'1 天体验已开启';
    sub='注册即享 1 天体验 · 体验期内每月 3 次成片导出、最高 720p，云端转写不可用';
  }else if(st.key==='expired'){
    headline='体验已结束';
    sub='已有内容可查看 · 素材与剪映草稿仍可导出；成片导出与新增生成需开通会员';
  }else if(st.key==='readonly'){
    headline='当前为只读状态';
    sub=esc(t.message||'仅可查看，成片导出与新增生成需开通会员');
  }else if(st.key==='unlocked'){
    headline='当前设备已解锁';
    sub=esc(t.message||'本地授权已生效，可正常使用');
  }else{
    headline='尚未开通会员';
    sub='注册即享 1 天体验 · 之后可开通尊享会员';
  }
  return '<section class="member-hero member-hero-'+st.key+(st.key==='active'?' member-hero-premium':'')+'">'+
    '<div class="member-hero-top">'+
      memberCrestHtml()+
      '<div class="member-wordmark"><strong>会员中心</strong><span>MEMBERSHIP</span></div>'+
      '<span class="member-state">'+esc(st.label)+'</span>'+
    '</div>'+
    '<div class="member-hero-title">'+esc(headline)+'</div>'+
    '<div class="member-hero-sub">'+sub+'</div>'+
    (st.key==='active'?'<div class="member-premium-badges"><span class="member-premium-badge is-primary">'+esc(memberTierName(s))+' · 限量席位</span><span class="member-premium-badge">全功能畅享</span><span class="member-premium-badge">专属身份</span></div>':'')+
  '</section>';
}
// 身份阶梯：注册 → 1 天试用 → 会员。这是真实产品路径，不是等级进度条，
// 所以只做三段静态说明 + 当前所处位置，不画成长值/升级进度。
// 2026-11-17（续费流程自然化）：已经在会员/到期/只读的用户不该再看「注册→试用→开通」入门阶梯
// ——那正是「不自然」的主因。这类用户换成一行续期说明，阶梯只留给 free / trial。
function memberLadderHtml(s){
  const st=memberState(s);
  if(st.key==='active'||st.key==='expired'||st.key==='readonly'){
    const raw=memberExpiry(s);
    const expiry=entitlementDate(raw);
    const d=daysToExpiry(raw);
    let tail;
    if(st.key==='active'){
      tail=(expiry?('会员有效期至 '+esc(expiry)):'会员权益生效中')+
        (d!=null&&d>0?('（还有 '+d+' 天）'):'');
    }else{
      tail='会员权益已暂停，续费后当场恢复';
    }
    return '<section class="member-section"><div class="member-honest">'+
      '<b>续费</b>：'+tail+'。续费由你主动下单，<b>到期不会自动扣费</b>。</div></section>';
  }
  const here=st.key==='trial'?2:1;
  const steps=[['01','注册账号','新用户自动获得 1 天体验'],
               ['02','1 天体验','1 天内每月 3 次成片导出、最高 720p；云端转写不可用'],
               ['03','开通尊享会员','体验到期后在线开通，扫码付款即刻生效']];
  return '<section class="member-section"><div class="member-section-head">'+
    '<h3>成为会员</h3><span>注册 → 试用 1 天 → 开通会员</span></div>'+
    '<div class="member-ladder">'+steps.map((x,i)=>
      '<div class="member-ladder-step'+(i+1===here?' is-here':'')+'">'+
        '<span class="member-ladder-n">'+x[0]+'</span>'+
        '<div><strong>'+x[1]+(i+1===here?'<em>当前</em>':'')+'</strong><p>'+x[2]+'</p></div>'+
      '</div>').join('')+'</div></section>';
}
// 会员中心顶部条（2026-11-17）：active 临期（≤30 天）出倒计时条，到期/只读出损失清单。
// 文案只用「续费 / 继续」，不出现「开通」；天数从 expires_at 原始值算。
function memberRenewBarHtml(s, ent){
  const st=memberState(s);
  const items=lossItems(ent);
  const lossLine=items.length?('到期后你将失去：'+esc(items.join(' · '))):'';
  if(st.key==='active'){
    const d=daysToExpiry(memberExpiry(s));
    if(d==null||d>30)return '';
    const days=d>0?d:1;
    return '<div class="member-flash member-renew-bar">'+
      '<strong>会员将于 '+days+' 天后到期</strong>'+
      '<span> · 别让创作断档，还差这一步就接着用</span>'+
      '<button type="button" class="small-button" data-action="member-renew">继续享有全部权益（续费）</button>'+
    '</div>';
  }
  if(st.key==='expired'){
    return '<div class="member-flash member-renew-bar">'+
      '<strong>会员已到期</strong>'+
      (lossLine?('<span> · '+lossLine+'</span>'):'<span> · 成片导出与新增生成已暂停，已有内容仍可查看</span>')+
      '<button type="button" class="small-button" data-action="member-renew">续费恢复</button>'+
    '</div>';
  }
  return '';
}
function memberFlowHtml(){
  const steps=[['01','微信扫码付款','点「微信扫码开通」生成这一单专属的付款二维码'],
               ['02','付款自动确认','微信支付成功后直接通知服务端，不用发截图、不用找客服'],
               ['03','权益自动到账','会员到期时间自动延长，本页几秒内自动刷新']];
  return '<div class="member-flow">'+steps.map(x=>
    '<div class="member-flow-step"><span class="member-flow-n">'+x[0]+'</span>'+
    '<div><strong>'+x[1]+'</strong><p>'+x[2]+'</p></div></div>').join('')+'</div>'+
    '<p class="member-honest"><b>自动到账</b>：付款成功后由微信回调直接开通；'+
    '万一网络波动没及时刷新，重开会员中心即可看到最新到期时间。</p>';
}
// ---- 在线开通：微信 Native 动态码（2026-11-16）-----------------------------
// 每单单独向云端下单拿 code_url，本地渲染成 SVG 二维码；金额与套餐只认云端配置，
// 这里只传套餐代号。不展示任何静态收款图，也不再引导「转账后找客服发卡密」。
function payCents(fen){return '¥'+(Number(fen||0)/100).toFixed(2);}
function payFlags(cfg){
  const p=(cfg&&cfg.config&&cfg.config.pay)||{};
  // 2026-11-16：一分钱测试单已停用，前端不再读取云端 pay.test_sku（云端值待后台关闭）。
  return {enabled:!!p.enabled,test_sku:false};
}
function payIdem(){
  if(window.crypto&&typeof crypto.randomUUID==='function')return crypto.randomUUID().replace(/-/g,'');
  return 'i'+Date.now().toString(36)+Math.random().toString(36).slice(2,10);
}
function stopPayPoll(){if(payPollTimer){clearTimeout(payPollTimer);payPollTimer=null;}}
function memberPayHtml(cfg,s){
  const f=payFlags(cfg);
  if(!f.enabled)return '<div class="member-pay" id="member-pay">'+
    '<p class="small muted">在线开通暂时不可用，可先用下方兑换码开通，或联系管理员。</p></div>';
  const active=memberState(s).key==='active';
  const agreeHtml=
    '<div class="pay-agree-row">'+
      '<label class="pay-agree"><input type="checkbox" id="pay-agree"'+(payAgreed?' checked':'')+'>'+
        '<span>我已阅读并同意</span></label>'+
      legalAnchorHtml('service','《服务协议》')+
      '<span>与</span>'+
      legalAnchorHtml('privacy','《隐私政策》')+
      '<span class="small muted">（'+LEGAL_VERSION+'）</span>'+
    '</div>'+
    '<p class="small muted pay-agree-hint" id="pay-agree-hint"'+(payAgreed?' hidden':'')+'>'+
      '请先勾选同意，再开通会员。</p>';
  let html='<div class="member-pay" id="member-pay">'+
    agreeHtml+
    '<button class="primary'+(payAgreed?'':' is-locked')+'" type="button" data-action="pay-start" '+
      'data-plan="year" aria-disabled="'+(payAgreed?'false':'true')+'">'+
      (active?'续费会员（微信扫码）':'微信扫码开通')+'</button>'+
    '<p class="small muted">用微信「扫一扫」付款，成功后权益自动到账，无需联系客服。</p>';
  return html+'</div>';
}
// 2026-11-17（支付流修复 ①）：协议不再用 iframe 内嵌，改成新标签直开。
// 原实现 '<iframe class="legal-frame" src="/legal/….html">' 必被拦成空白页：app.py 给
// 静态文件统一下发 CSP `frame-ancestors 'none'`（防点击劫持，安全头不许削弱，也不许改）。
// 副本实测（8931）控制台原话：Framing 'http://127.0.0.1:8931/' violates the following
// Content Security Policy directive: "frame-ancestors 'none'". The request has been blocked.
// 所以只改前端出口：链接直开新标签，客户能真正读到协议全文；弹窗被拦时兜底给可点直链。
function legalUrl(doc){return '/legal/'+(doc==='privacy'?'privacy':'service')+'.html';}
function legalAnchorHtml(doc,title){
  return '<a class="link-button" href="'+legalUrl(doc)+'" target="_blank" rel="noopener">'+title+'</a>';
}
function openLegal(doc){
  // 注意：window.open 传第三参数 'noopener' 时返回值恒为 null（Chrome 行为，副本实测），
  // 会被误判成「弹窗被拦」。所以这里不带特性串，取到窗口后手动断开 opener。
  const w=window.open(legalUrl(doc),'_blank');
  if(w){try{w.opener=null;}catch(e){}return;}
  const isPrivacy=doc==='privacy';
  modal(isPrivacy?'隐私政策':'服务协议',
    '<p class="small">浏览器拦住了自动打开的新标签。点下面的链接即可直接阅读'+
      (isPrivacy?'《隐私政策》':'《服务协议》')+'全文（仍是新标签，不影响本页）。</p>'+
    '<p class="row">'+legalAnchorHtml(doc,'在新标签打开全文')+'</p>');
}
function payPanelHtml(order,svg){
  return '<div class="member-pay-panel" id="member-pay-panel" data-order="'+esc(order.order_no)+'">'+
    '<div class="row spread"><strong>微信扫码支付</strong>'+
      '<span class="member-pay-amount">'+esc(payCents(order.amount_fen))+'</span></div>'+
    '<div class="pay-qr" id="pay-qr" data-action="pay-qr-zoom" role="button" tabindex="0" '+
      'aria-label="点击放大二维码">'+svg+'</div>'+
    '<p class="small muted">请用微信「扫一扫」扫码付款；付款成功后本页自动更新，不用刷新。</p>'+
    '<div class="member-pay-meta"><span>订单号 '+esc(order.order_no)+'</span>'+
      '<span id="pay-status">'+esc(order.status_text||'等待支付')+'</span></div>'+
    '<p class="small muted" id="pay-countdown"></p>'+
    '<div class="row"><button class="small-button ghost" type="button" data-action="pay-close">关闭</button></div>'+
  '</div>';
}
function payRetryHtml(plan,text){
  return '<p class="small member-pay-err">'+esc(text)+'</p>'+
    '<button class="small-button" type="button" data-action="pay-start" data-plan="'+esc(plan||'year')+'">重试</button>';
}
async function startCheckout(plan){
  // 扫码前的同一道闸：没勾选就不下单、不出码（重试入口也走这里）。
  if(!payAgreed){
    toast('请先勾选同意《服务协议》与《隐私政策》，再开通会员。');
    const ag=$('#pay-agree');if(ag)ag.focus();
    return;
  }
  stopPayPoll();payPaidFlag=false;
  plan=plan||'year';payPlanCode=plan;
  const box=$('#member-pay');
  if(box)box.innerHTML='<p class="small muted">正在生成付款二维码…</p>';
  let r;
  // agreed 一并送服务端：本地接口在 agreed 不是 true 时直接拒单，不只靠按钮置灰。
  try{r=await api('/member/checkout','POST',{plan:plan,idem_key:payIdem(),agreed:payAgreed===true});}
  catch(e){
    const b=$('#member-pay');if(b)b.innerHTML=payRetryHtml(plan,e.message||'下单失败，请稍后重试。');
    return;
  }
  const order=(r&&r.order)||{};
  if(!order.order_no){toast('下单返回异常，请稍后重试。');return;}
  let svg='';
  try{const q=await api('/member/order/'+encodeURIComponent(order.order_no)+'/qr');svg=String(q.svg||'');}
  catch(e){
    const b=$('#member-pay');if(b)b.innerHTML=payRetryHtml(plan,e.message||'二维码获取失败。');
    return;
  }
  if(!svg){const b=$('#member-pay');if(b)b.innerHTML=payRetryHtml(plan,'二维码为空，请重试。');return;}
  const b=$('#member-pay');if(b)b.outerHTML=payPanelHtml(order,svg);
  payPollOrder=order.order_no;
  payPollTimer=setTimeout(()=>pollPayOrder(order.order_no),2500);
}
async function pollPayOrder(no){
  if(!$('#member-pay-panel')){stopPayPoll();return;}
  let r;
  try{r=await api('/member/order/'+encodeURIComponent(no));}
  catch(e){
    if(String((e&&e.code)||'')==='ORDER_NOT_FOUND'){payFailed('订单不存在，请重新下单。');return;}
    payPollTimer=setTimeout(()=>pollPayOrder(no),5000);return;
  }
  const o=(r&&r.order)||{};
  if(Number(o.status)===1){payDone(o);return;}
  if(Number(o.status)===2){payFailed('订单已关闭（超时未支付），请重新下单。');return;}
  const st=$('#pay-status');if(st)st.textContent=o.status_text||'等待支付';
  const expires=Number(o.expires_at||0),left=Math.max(0,Math.floor(expires-Date.now()/1000));
  const cd=$('#pay-countdown');
  if(cd)cd.textContent=left>0?('二维码有效期剩余 '+Math.floor(left/60)+' 分 '+
    String(left%60).padStart(2,'0')+' 秒'):'二维码已过期，请重新下单。';
  if(expires>0&&left<=0){payFailed('二维码已过期，请重新下单。');return;}
  payPollTimer=setTimeout(()=>pollPayOrder(no),3000);
}
function payFailed(text){
  stopPayPoll();
  const box=$('#member-pay-panel');
  if(!box)return;
  box.innerHTML=payRetryHtml(payPlanCode,text);
}
async function paySyncAfterPaid(){
  const errs=[];
  try{const b=await api('/billing/summary');state.memberBilling=b;paintCredits(b);}catch(e){errs.push('权益摘要：'+(e.message||'读取失败'));}
  try{const me=await api('/auth/me');const box=$('#billing-rights');if(box)box.outerHTML=rightsHtml((me&&me.entitlements)||{});paintRightsCopy();}catch(e){errs.push('会员权益：'+(e.message||'读取失败'));}
  return errs;
}
async function payDone(o){
  state.lastPaidOrder=o;
  stopPayPoll();payPaidFlag=true;
  const box=$('#member-pay-panel');
  if(box)box.innerHTML='<div class="member-pay-ok"><strong>支付成功，会员权益已开通</strong>'+
    '<p class="small muted">订单号 '+esc(o.order_no)+'</p><p class="small muted" id="pay-sync-status">正在同步会员权益…</p></div>'+
    '<div class="row"><button class="primary" type="button" data-action="pay-close">完成</button><button class="small-button" type="button" data-action="pay-refresh">重新同步</button></div>';
  toast('支付成功，会员权益已到账。');
  const status=$('#member-pay-panel')?$('#member-pay-panel').querySelector('#pay-sync-status'):null;
  const errs=await paySyncAfterPaid();
  if(status){
    if(errs.length){status.className='small error-text';status.innerHTML='会员已开通，但权益显示同步失败：'+esc(errs.join('；'))+'。可点「重新同步」重试。';}
    else{status.className='small muted';status.textContent='会员权益已同步。';}
  }
}
// 只留卡密兑换记录，不显示积分流水；记录里也不带任何积分数字。
function memberRecordsHtml(redeemed){
  return '<details class="member-records"><summary>卡密兑换记录</summary>'+
    (redeemed?'<div class="space">'+redeemed+'</div>':'<p class="small muted">还没有卡密兑换记录。</p>')+
  '</details>';
}
function rightsHtml(ent){
  // 必须返回单个带 id="billing-rights" 的根元素：refreshRights() 用 outerHTML 整体替换。
  // 云端没下发 entitlements（{} 或全缺键）时不能显示「不限」——那是虚假权益，
  // 免费档实际有限额（free_limits）。缺键一律显示「未下发」，不猜数值。
  // 注意：null 是「不限」的有效语义（见 billing_service._features / _quota_overrides），
  // 只有「键根本不存在」才算未下发，不能按值判空。
  const hasOwn=(o,k)=>Object.prototype.hasOwnProperty.call(o,k);
  const keys=['exports_per_month','max_height','expires_at','whitelist','exports_per_day','watermark','allow_cloud_asr','asr_unlimited','all_style_packs','vip_service'];
  if(!ent)return '<div class="member-rights" id="billing-rights"><p class="small muted">云端未下发权益，以服务端实际权益为准</p></div>';
  if(!keys.some(k=>hasOwn(ent,k)))return '<div class="member-rights" id="billing-rights">'+
    '<p class="small muted">当前账号未下发具体额度，以服务端实际权益为准；不显示未核实的「不限」。</p></div>';
  // 六条权益（2026-11-16 主理人批准）：标题说价值（虚一点），副文案一句感受，
  // 值仍由云端下发的 entitlements 逐项决定——门禁判断一个字不动，不猜、不造假。
  const cell=(k,sub,v)=>'<div class="member-right"><span>'+k+'</span>'+
    (sub?'<span class="member-right-sub">'+sub+'</span>':'')+
    '<strong>'+esc(v)+'</strong></div>';
  const parts=[];
  if(hasOwn(ent,'exports_per_month'))
    parts.push(cell('创作不设限','灵感来了，随时成片',ent.exports_per_month==null?'不限':(ent.exports_per_month+' 次/月')));
  if(hasOwn(ent,'max_height'))
    parts.push(cell('每一帧都拿得出手','你的作品，值得被认真呈现',ent.max_height==null?'不限':(ent.max_height+'p')));
  if(hasOwn(ent,'allow_cloud_asr'))
    parts.push(cell('开口即成稿','你只管说，云端帮你落成文字',ent.asr_unlimited?'会员包含 · 受公平使用约束':(Number(ent.allow_cloud_asr)>0?'已开通':'未开通')));
  // 会员专属权益：只按云端下发的布尔展示，没下发就不显示，不猜。
  if(ent.all_style_packs)parts.push(cell('风格随心换','全部包装风格，为你敞开','全部开放'));
  // 「专属服务」拆成两条（优先体验 / 专属客服），凑足六条；两条同源 vip_service。
  if(ent.vip_service)parts.push(cell('上新先一步','新功能，会员先体验','会员优先体验'));
  if(ent.vip_service)parts.push(cell('创作不孤单','专属客服，一路有人同行','专属客服'));
  // 「权益到期」不再占权益格（2026-11-16）：memberIdentityHtml 已显示有效期，避免重复。
  if(!parts.length)parts.push('<p class="small muted">当前账号未下发具体额度，以服务端实际权益为准。</p>');
  return '<div class="member-rights" id="billing-rights">'+parts.join('')+'</div>';
}
// 副文案与标题都是 span，CSS 里 .member-right span 统一 11px 灰，会糊成一片；
// 这里用 CSSOM（不是内联 style 属性，CSP 允许）把标题提亮、副文案压暗，拉出层次。
function paintRightsCopy(){
  const root=document.getElementById('billing-rights');
  if(!root)return;
  root.querySelectorAll('.member-right').forEach(card=>{
    const title=card.querySelector('span:not(.member-right-sub)');
    const sub=card.querySelector('.member-right-sub');
    if(title){title.style.fontSize='12px';title.style.color='var(--text)';title.style.letterSpacing='.3px';}
    if(sub){sub.style.fontSize='11px';sub.style.opacity='.8';}
  });
}
async function refreshRights(){
  try{
    const me=await api('/auth/me');
    const box=$('#billing-rights');
    if(box)box.outerHTML=rightsHtml(me&&me.entitlements||{});
    paintRightsCopy();
    return (me&&me.entitlements)||null;
  }catch(e){
    // 断网或云 token 过期时，用本机会员快照兜底，会员中心不能只剩「读取中」。
    const ent=localRights();
    const box=$('#billing-rights');
    if(box&&ent)box.outerHTML=rightsHtml(ent);
    paintRightsCopy();
    return ent;
  }
}
function localRights(){
  const t=state.memberTrial||{};
  const ent=(t.entitlements&&typeof t.entitlements==='object')?Object.assign({},t.entitlements):{};
  if(!Object.prototype.hasOwnProperty.call(ent,'expires_at')&&t.vip_expires_at)ent.expires_at=t.vip_expires_at;
  if(Object.keys(ent).length)return ent;
  // 再退一层：本地计费摘要里的会员 features（离线时也能看到次数/清晰度/是否不限）。
  const m=(state.memberBilling&&state.memberBilling.membership)||{};
  const f=m.features||{};
  const out={};
  ['exports_per_month','max_height','allow_cloud_asr','watermark','asr_unlimited','all_style_packs','vip_service'].forEach(function(k){
    if(Object.prototype.hasOwnProperty.call(f,k))out[k]=f[k];
  });
  if(m.expire_at)out.expires_at=m.expire_at;
  return Object.keys(out).length?out:null;
}
// 兑换走云端核销：本机只转发，不再自己核销。
// 云端契约：POST /api/redeem {code}（大小写、带不带横线都接受）。
const CLOUD_REDEEM_ENABLED=true;
function cloudRedeemError(e){
  const c=String((e&&e.code)||'');
  const text={
    redeem_code_invalid:'兑换码不对，请核对后重试。',
    redeem_code_void:'这个兑换码已作废，请联系管理员换一个。',
    redeem_code_expired:'兑换码已过期，请联系管理员换一个。',
    redeem_code_used:'这个兑换码已经兑换过了。',
    redeem_rate_limited:'操作太频繁，请稍后再试。',
    unauthenticated:'登录状态已失效，请重新登录后再兑换。'
  }[c];
  return text||'兑换没有成功，请稍后再试，或联系管理员。';
}
async function redeemCode(code){
  if(CLOUD_REDEEM_ENABLED){
    try{return await api('/redeem','POST',{code});}
    catch(e){throw Error(cloudRedeemError(e));}
  }
  return api('/billing/redeem','POST',{code});
}
// 静态收款码（2026-11-15）：只展示「扫码付款 → 联系客服 → 凭卡密兑换」，
// 不做自动到账、不生成订单、不判定支付结果。配置入口只在运行工作台的本机出现，
// 手机端客户只读，避免任何人替换收款码。p 读不到时降级为一句提示，不拖垮弹窗。
function payeeHtml(p){
  if(!p)return '<div class="panel space payee-box"><strong class="small">充值方式</strong>'+
    '<p class="small muted">收款方式暂时读不到，可继续用兑换码；需要开通请联系管理员。</p></div>';
  const configured=!!p.configured;
  const contact=String(p.contact||'').trim();
  let html='<div class="panel space payee-box"><div class="row spread">'+
    '<strong class="small">充值方式</strong>'+pill(configured?'扫码付款':(p.paused?'已暂停':(contact?'联系客服购买':'未配置')))+'</div>'+
    '<p class="small member-policy"><b>虚拟商品，一经售出不予退款；如需发票，请在购买前联系客服确认。</b></p>';
  if(!configured){
    // 无收款图片时不渲染二维码框：有客服就展示客服与公告，没有客服也必须给一句话，不留空白。
    html+=contact
      ? (p.title?'<p class="small">'+esc(p.title)+'</p>':'')+
        '<p class="small">联系方式：<b>'+esc(contact)+'</b></p>'+
        (p.note?'<p class="small muted">'+esc(p.note)+'</p>':'')+
        '<p class="small muted payee-notice">'+esc(p.notice||'')+'</p>'
      : (p.paused
        ? '<p class="small muted">收款方式暂时停用，请联系管理员获取卡密；已购买的内容仍可查看，导出需开通会员。</p>'
        : '<p class="small muted">暂未配置在线收款方式，请联系管理员获取兑换码或付款方式，不要向任何私人账户转账。</p>');
  }else{
    html+='<div class="payee-qr-wrap"><img class="payee-qr" alt="收款二维码" src="'+esc(media(p.image_url))+'"></div>'+
      (p.title?'<p class="small">'+esc(p.title)+'</p>':'')+
      (p.contact?'<p class="small">联系方式：'+esc(p.contact)+'</p>':'')+
      (p.note?'<p class="small muted">'+esc(p.note)+'</p>':'')+
      '<p class="small muted payee-notice">'+esc(p.notice||'')+'</p>';
  }
  if(p.can_edit){
    html+='<details class="payee-admin"><summary class="small">管理收款方式（仅本机可见）</summary>'+
      '<form id="payee-info-form" class="space">'+
        '<label class="field"><span>标题</span><input name="title" maxlength="500" value="'+esc(p.title||'')+'" placeholder="例如：扫码付款后联系客服"></label>'+
        '<label class="field"><span>联系方式</span><input name="contact" maxlength="120" value="'+esc(p.contact||'')+'" placeholder="客服微信 / 手机号"></label>'+
        '<label class="field"><span>补充说明</span><input name="note" maxlength="500" value="'+esc(p.note||'')+'" placeholder="例如：付款后请发送截图"></label>'+
        '<button class="small-button" type="submit">保存文字</button></form>'+
      '<form id="payee-image-form" class="space">'+
        '<label class="field"><span>收款码图片（PNG / JPG / WEBP，≤4MB）</span>'+
        '<input type="file" name="image" accept="image/png,image/jpeg,image/webp" required></label>'+
        '<button class="small-button" type="submit">'+(configured?'替换收款码':'上传收款码')+'</button></form>'+
      (configured?'<button class="small-button ghost" type="button" data-action="payee-clear-image">清空收款码</button>':'')+
      '<p class="small muted">云端收款方式在 admin.jidixh.com「收款方式」页统一配置；'
      '这里只在云端连不上时作为本机备用。图片不进安装包、不上传日志。'+
      '静态收款码不会自动到账：客户付款后仍需你人工核对并给卡密。</p></details>';
  }
  return html+'</div>';
}
function payeeReadFile(file){
  return new Promise((resolve,reject)=>{
    const reader=new FileReader();
    reader.onload=()=>resolve(String(reader.result||''));
    reader.onerror=()=>reject(Error('图片读取失败，请换一张重试。'));
    reader.readAsDataURL(file);
  });
}
function contactHtml(p){
  const c=p&&p.contact?String(p.contact):'';
  return '<p class="member-contact small">联系管理员：'+
    (c?('<b>'+esc(c)+'</b>'):'请用你收到开通说明里的客服方式联系，拿到卡密后可直接在下面兑换。')+
    '</p>';
}
// 只重绘收款码区块：不重开已 open 的 dialog（showModal 二次调用会报错）。
async function refreshPayeeBlock(){
  const p=await api('/billing/payee');
  const box=$('#modal-body .payee-box');
  if(box)box.outerHTML=payeeHtml(p);
  return p;
}
async function openBilling(message,rights){
  if(typeof message!=='string')message='';
  try{
    // /billing/ledger 是积分流水，按 D12「没有积分」不再拉取、不再渲染。
    const [s,h,payee,cfg]=await Promise.all([
      api('/billing/summary'),api('/billing/redeem'),
      api('/billing/payee').catch(()=>null),api('/config').catch(()=>null)]);
    // 价格唯一来源：云端 /api/config 的 year 套餐；读不到才回落到 499（仅兜底，不再出现第二个数字）。
    const yearPlan=((cfg&&cfg.config&&cfg.config.plans)||[]).find(p=>p.plan==='year');
    // 云端字段异常（缺失/非数字/<=0）同样走兜底，避免出现 ¥NaN 或空白。
    const rawCents=yearPlan?Number(yearPlan.price_cents):NaN;
    const yearCents=Number.isFinite(rawCents)&&rawCents>0?rawCents:49900;
    const yearText='¥'+(yearCents/100);
    // 价格拆解（2026-11-17）：只用云端金额现算，不硬编码、不造划线价（没有官方建议价就不标原价）。
    const perDayText='≈¥'+((yearCents/100)/365).toFixed(2)+'/天';
    paintCredits(s);
    rights=rights||localRights();
    // 卡密兑换记录只显示时间/批次/尾号，不带任何积分数字。
    const redeemed=(h.items||[]).map(x=>
      '<div class="row spread small"><span>'+esc(x.used_text||'')+' · 批次 '+esc(x.batch||'')+
      ' · 尾号 '+esc(x.code_tail||'')+'</span><span class="muted">已兑换</span></div>').join('');
    // 2026-11-17 续费流程自然化：顺序改成「身份卡 → 临期/到期条 → 权益（先讲拥有）→ 入门阶梯（只给没进来的）→ 续费/开通（最后讲钱）」。
    const renewing=memberState(s).key==='active'||memberState(s).key==='expired';
    modal('会员中心',
      (message?'<p class="member-flash">'+esc(message)+'</p>':'')+
      memberIdentityHtml(s)+
      (memberSeatLine(s)?('<p class="member-founder">'+esc(memberSeatLine(s))+'</p>'):'')+memberPolicyHtml(s)+
      cloudWalletHtml(s)+
      memberRenewBarHtml(s,rights)+
      '<section class="member-section"><div class="member-section-head"><h3>会员权益</h3>'+
        '<span>会员，是把创作里那些『将就』，一件件拿掉</span></div>'+rightsHtml(rights)+
        '<p class="small muted">以上按账号实际下发的权益展示，未下发的项不显示。</p>'+
      '</section>'+
      memberLadderHtml(s)+
      '<section class="member-section member-recharge"><div class="member-section-head">'+
        '<h3>'+(renewing?'续费尊享会员':'开通尊享会员')+'</h3>'+
        '<span>'+esc(yearText)+' / 年 · '+esc(perDayText)+(renewing?' · 续费后有效期顺延':' · 导出不限')+'</span></div>'+
        '<div class="member-price"><div><strong>'+esc(yearText)+'</strong><span>元 / 年 · '+esc(perDayText)+'</span></div>'+
        '<p class="member-founder">创始会员 · 限量席位</p><p class="small muted">权益自动到账 · 到期不自动扣费 · 续费由你主动下单</p></div>'+
        memberFlowHtml()+
        memberPayHtml(cfg,s)+
        contactHtml(payee)+
        '<p class="small muted member-redeem-tip">已经拿到卡密？在下面兑换。</p>'+
        '<form id="redeem-form">'+
        '<label class="check small"><input type="checkbox" name="policy" required> 我已阅读并同意上述说明（兑换后不支持退款；如需发票请在购买前联系客服确认）</label>'+
        '<label class="field"><span>输入卡密</span><input name="code" placeholder="输入客服给你的卡密，可带横线" autocomplete="off" required></label><button class="primary" type="submit">兑换</button><p class="small muted">卡密一次性使用，兑换成功后权益立即生效；同一设备连续输错会被临时限制。</p></form>'+
        '<p class="small muted member-legal-note">虚拟商品，售出后不退款；如需发票请在购买前联系客服。退款与发票以 '+
          legalAnchorHtml('service','《服务协议》')+' 为准。</p>'+
      '</section>'+
      memberRecordsHtml(redeemed));
    if(s.trial)paintTrial(s.trial);
    paintRightsCopy();          // 会员中心首屏就带权益，标题/副文案层次要当场生效
    if(!rights)refreshRights();
  }catch(e){toast('会员信息读取失败：'+e.message);}
}
// 2026-11-17（支付流修复 ③）：兑换失败必须在会员中心对话框内说清楚。
// 会员中心是 <dialog>.showModal()，在浏览器顶层；body 级 #toast（fixed;z-index:20）会被
// 整个盖住——副本 8931 实测 elementFromPoint(提示条中心) 返回的是 modal 本体，客户看不见。
// 所以抛出型失败（500/断网/返回非 JSON）过去只走 toast，等于静默吞错；现在先写进对话框。
function memberSay(msg){
  const body=$('#modal-body');if(!body||!msg)return;
  let el=body.querySelector('.member-flash');
  if(!el){el=document.createElement('p');el.className='member-flash';body.insertBefore(el,body.firstChild);}
  el.setAttribute('role','alert');el.textContent=String(msg);
  if(el.scrollIntoView)el.scrollIntoView({block:'nearest'});
  const form=$('#redeem-form');const code=form&&form.querySelector('input[name="code"]');
  if(code)code.focus();
}
async function redeemSubmit(form,values){
  const code=String(values.code||'').trim().replace(/[^0-9A-Za-z]/g,'').toUpperCase();
  if(!code){memberSay('请输入兑换码。');return;}
  let r;
  try{r=await redeemCode(code);}
  catch(e){
    const msg=(e&&e.message)||'兑换没有成功，请稍后再试，或联系管理员。';
    memberSay(msg);toast(msg);return;
  }
  if(!r||!r.ok){
    const msg=(r&&r.message)||'兑换未完成，请核对兑换码。';
    try{await openBilling(msg);}catch(e2){memberSay(msg);}
    return;
  }
  // 兑换成功后立刻重拉余额与云端权益，页面当场变，不用重启软件。
  await refreshBilling();
  let ent=(r&&r.entitlements)||null;
  try{const me=await api('/auth/me');ent=(me&&me.entitlements)||ent;}catch(e){}
  const msg=r.message||'兑换成功，权益已更新。';
  await openBilling(msg,(ent&&Object.keys(ent).length)?ent:null);
  toast(msg);
}
// ---- 顶栏「会员中心」入口（2026-11-16）------------------------------------
// 原来右上角是一个显示「已解锁 / 免费试用中」的小胶囊，用户要求改成统一入口：
// 入口只写「会员中心」四个字，不显示到期日、试用剩余、续费提示等信息；
// 这些信息只在点开后的会员中心面板里出现。三态外观统一，不靠副行区分。
// 视觉走克制的细金线，跟着 --m-* 主题变量，浅色主题同样适用。
function memberSnapshot(){
  const t=state.memberTrial||{},b=state.memberBilling||{};
  return {trial:t,membership:(b&&b.membership)||b||{}};
}
function memberEntryState(s){
  const st=memberState(s);
  if(st.key==='active')return {cls:'is-vip'};
  if(st.key==='trial')return {cls:'is-trial'};
  if(st.key==='expired'||st.key==='readonly')return {cls:'is-expired'};
  return {cls:'is-free'};
}
function paintMemberEntry(){
  const bar=document.querySelector('.top-actions');if(!bar)return;
  let el=document.getElementById('member-entry');
  if(!el){
    el=document.createElement('button');el.id='member-entry';el.type='button';
    el.addEventListener('click',()=>openBilling());
    bar.insertBefore(el,bar.firstChild);
  }
  const st=memberEntryState(memberSnapshot());
  el.className='member-entry '+st.cls;
  el.title='打开会员中心：权益、到期日与兑换码';
  el.setAttribute('aria-label','打开会员中心');
  el.innerHTML='<span class="me-crest" aria-hidden="true"><svg viewBox="0 0 24 24" focusable="false">'+
      '<circle cx="12" cy="12" r="10.2"/>'+
      '<path d="M6.9 15.6 L7.7 9.4 L11.1 12.2 L12 8.1 L12.9 12.2 L16.3 9.4 L17.1 15.6 Z"/>'+
    '</svg></span>'+
    '<span class="me-title">会员中心</span>';
}
function paintTrial(t){
  const el=$('#trial-banner');
  state.memberTrial=t||null;
  paintMemberEntry();
  if(!el)return;
  if(!t||t.mode==='active'||t.mode==='licensed'||!t.mode){el.hidden=true;return;}
  if(t.mode==='trial'){
    el.className='trial-banner trial-active';el.hidden=false;
    const hours=Number(t.hours_left);
    const left=Number.isFinite(hours)&&hours>0?('剩余 '+hours+' 小时'):('剩余 '+(Number(t.days_left)||0)+' 天');
    // 2026-11-17（试用转化）：剩余时间 + 「结束后会停」清单 + 一键续费；清单只列云端真下发的权益。
    const ent=(t.entitlements&&typeof t.entitlements==='object')?t.entitlements:{};
    const items=lossItems(ent);
    const stop=items.length?items.join(' · '):'每月 3 次成片导出 · 导出上限 720p';
    el.innerHTML='<strong>免费体验 1 天 · '+esc(left)+'</strong>'+
      '<span>'+esc('试用结束后这些会停：'+stop+'。已有内容仍可查看。')+'</span>'+
      '<button type="button" class="small-button" data-action="member-renew">立即续费</button>';
  }else{
    el.className='trial-banner trial-expired';el.hidden=false;
    el.innerHTML='<strong>'+esc(t.message||'免费体验 1 天已结束')+'</strong>'+
      '<span>新增生成与成片导出已暂停；素材与剪映草稿仍可导出，已有内容可查看，续费后恢复。</span>'+
      '<button type="button" class="small-button" data-action="member-renew">查看续费</button>';
  }
}
let douyinLoginTimer=null;
function clearDouyinWatch(){clearInterval(douyinLoginTimer);douyinLoginTimer=null;}
function closeDouyinLogin(){clearDouyinWatch();if($('#modal').open)$('#modal').close();}
function showDouyinLogin(retry){
  clearDouyinWatch();
  modal('这条要登录抖音才能抓',`<p class="field-explain">扫码登录一次就行，登录信息只存在你自己电脑上。</p><p class="small muted" id="douyin-login-status"></p><div class="row space"><button class="primary" type="button" id="douyin-login-open">去登录</button><button type="button" id="douyin-login-skip">先不登</button></div>`);
  $('#douyin-login-skip').addEventListener('click',closeDouyinLogin);
  $('#douyin-login-open').addEventListener('click',()=>openDouyinLogin(retry));
}
async function openDouyinLogin(retry){
  const btn=$('#douyin-login-open'),status=$('#douyin-login-status');if(!btn||!status)return;
  clearDouyinWatch();btn.disabled=true;btn.textContent='等你扫码…';
  const started=Date.now();
  try{
    const r=await api('/douyin/login/open','POST',{});
    if(!r||r.ok===false||r.opened===false)throw Error((r&&r.message)||'登录窗口没打开');
  }catch(e){
    status.textContent=(e.message||'登录窗口没打开')+'。没等到登录，可以再点一次，或者改用「上传视频/录音」的方式拿文案。';
    btn.disabled=false;btn.textContent='去登录';return;
  }
  status.textContent='已打开登录窗口，等你扫码…';
  douyinLoginTimer=setInterval(async()=>{
    if(!$('#modal').open){clearDouyinWatch();return;}
    try{
      const s=await api('/douyin/login');
      if(s&&s.configured){
        clearDouyinWatch();if($('#modal').open)$('#modal').close();
        refreshDouyinBadge();
        toast('已登录，正在重新抓取这条链接。');
        retry().catch(e=>toast(e.message));
      }else if(Date.now()-started>90000){
        clearDouyinWatch();
        status.textContent='没等到登录，可以再点一次，或者改用「上传视频/录音」的方式拿文案。';
        btn.disabled=false;btn.textContent='去登录';
      }
    }catch(e){status.textContent='查不到登录状态：'+e.message;}
  },2500);
}
async function startProjectFromShare(share){
  let p;
  try{p=await api('/projects','POST',{url:share,auto_resolve:true});}
  catch(e){if(e.data&&e.data.need_login){showDouyinLogin(()=>startProjectFromShare(share));return;}throw e;}
  if(p&&p.need_login&&p.ok===false){showDouyinLogin(()=>startProjectFromShare(share));return;}
  if(!p||!p.id)throw Error((p&&p.message)||'没能读取这个链接。');
  location.hash=purl(p.id,'source');toast('链接已保存，开始读取视频资料。');
}
state.bench={accounts:[],accountId:null,works:[],worksTotal:0,worksPage:1,worksSort:'share_rate',sel:{},topics:[],link:{work:null,angles:null,jobId:null,stage:'',error:'',busy:false}};
state.matrix={caps:[],accounts:[],tasks:[],projectId:null,project:null};
state.feedback={projectId:null,overview:null,works:[],snapshots:{},comments:{},classify:{},insights:[],collect:null,errors:{},startTopicPending:[]};
let benchPollTimer=null;
let matrixPollTimer=null;
let feedbackPollTimer=null;
let sourcePollTimer=null;
const pollHealth={};
function pollWarn(text){
  let el=document.getElementById('poll-warning');
  if(!text){if(el)el.remove();return;}
  if(!el){el=document.createElement('p');el.id='poll-warning';el.className='small error-text';const main=document.getElementById('main');if(main)main.prepend(el);}
  el.textContent=text;
}
function pollOk(key){const s=pollHealth[key];if(s){s.fails=0;s.skip=0;}if(!Object.keys(pollHealth).some(k=>pollHealth[k].fails>0))pollWarn('');}
function pollSkip(key){const s=pollHealth[key];if(s&&s.skip>0){s.skip--;return true;}return false;}
function pollFail(key,e){
  const s=pollHealth[key]||(pollHealth[key]={fails:0,skip:0});
  s.fails++;s.skip=Math.min(s.skip?s.skip*2:1,8);
  if(s.fails>=2)pollWarn('后台状态刷新失败，正在自动重试…'+(e&&e.message?'（'+e.message+'）':''));
}

function startFeedbackPoll(){if(feedbackPollTimer)return;feedbackPollTimer=setInterval(async()=>{if(pollSkip('feedback'))return;try{const pending=state.feedback.collect&&['queued','running','pending'].includes(state.feedback.collect.status);if(location.hash.startsWith('#/feedback')&&!$('#modal').open&&!document.hidden)await route();pollOk('feedback');if(!pending)stopFeedbackPoll();}catch(e){pollFail('feedback',e);}},4000);}
function stopFeedbackPoll(){if(feedbackPollTimer){clearInterval(feedbackPollTimer);feedbackPollTimer=null;}}
function startMatrixPoll(){if(matrixPollTimer)return;matrixPollTimer=setInterval(async()=>{if(pollSkip('matrix'))return;try{const pending=state.matrix.tasks.some(t=>['queued','running','planned','submitting'].includes(t.status));if(location.hash.startsWith('#/matrix')&&!$('#modal').open&&!document.hidden)await route();pollOk('matrix');if(!pending)stopMatrixPoll();}catch(e){pollFail('matrix',e);}},4000);}
function stopMatrixPoll(){if(matrixPollTimer){clearInterval(matrixPollTimer);matrixPollTimer=null;}}
async function matrixIdemKey(parts){const s=parts.join('|');try{const buf=await crypto.subtle.digest('SHA-256',new TextEncoder().encode(s));return 'mtx_'+Array.from(new Uint8Array(buf)).slice(0,20).map(b=>b.toString(16).padStart(2,'0')).join('');}catch{return 'mtx_'+Date.now().toString(36)+Math.random().toString(36).slice(2,10);}}
function startBenchPoll(){if(benchPollTimer)return;benchPollTimer=setInterval(async()=>{if(pollSkip('bench'))return;try{const d=await api('/bench/accounts');state.bench.accounts=d.items||d||[];const pending=state.bench.accounts.some(x=>['queued','running'].includes(x.collect_status));if(location.hash.startsWith('#/bench')&&!$('#modal').open&&!document.hidden&&!state.dirty&&!document.activeElement?.closest('form'))await route();pollOk('bench');if(!pending)stopBenchPoll();}catch(e){pollFail('bench',e);}},4000);}
function stopBenchPoll(){if(benchPollTimer){clearInterval(benchPollTimer);benchPollTimer=null;}}

function startSourcePoll(p){
  if(!p||!p.id)return;
  if(sourcePollTimer){clearInterval(sourcePollTimer);sourcePollTimer=null;}
  const id=p.id;
  const tick=async()=>{
    if(pollSkip('source'))return;
    if(document.hidden||$('#modal').open)return;
    let np;
    try{np=await api('/projects/'+id);}catch(e){pollFail('source',e);return;}
    pollOk('source');
    const jobs=np.jobs||[];
    const active=jobs.find(j=>['resolve','prepare_reference','prepare_content'].includes(j.kind)&&['queued','running'].includes(j.status));
    const bad=jobs.find(j=>['resolve','prepare_reference','prepare_content'].includes(j.kind)&&['failed','needs_action'].includes(j.status));
    const ready=String((np.source||{}).text||'').trim();
    const stageEl=document.getElementById('source-progress-stage');
    if(active&&stageEl)stageEl.textContent='当前阶段：'+(active.stage||'处理中')+' · 正在工作…';
    if(!active){
      stopSourcePoll();
      if(ready||bad){state.project=np;await route();}
    }
  };
  sourcePollTimer=setInterval(tick,2000);
}
function stopSourcePoll(){if(sourcePollTimer){clearInterval(sourcePollTimer);sourcePollTimer=null;}}

// ---- 贴一条视频链接 → 采集 → 拆 5 个角度（只重绘本区块，不打乱已输入的内容）----
let benchLinkTimer=null;
const LINK_FALLBACK_HINT='提取失败可改用文字版：打开这条视频，把文案/字幕原文复制后粘贴到「链接与资料」的资料框；也可以边看视频边用微信「语音转文字」把内容说出来（对着微信说→长按语音→转文字→复制），再粘贴进来。为什么值得费这一步：你本人说话的口吻和节奏是去 AI 味的金矿，学到的才是你的声音，不是机器腔。说不好、说断了、没头绪都没关系，想到哪说到哪，AI 会先帮你整理成通顺讲稿，再进入拆解与写稿。';
function benchLinkPaint(){
  const l=state.bench.link||{};
  const st=document.getElementById('bench-link-status');
  if(st)st.innerHTML=benchLinkStatusHtml();
  const box=document.getElementById('bench-angles');
  if(box)box.innerHTML=benchAnglesInner();
  const btn=document.querySelector('#bench-link-form button[type="submit"]');
  if(btn){btn.disabled=!!l.busy;btn.textContent=l.busy?'处理中…':'拆 5 个角度';}
}
async function benchLinkAngles(workId){
  const l=state.bench.link;
  l.busy=true;l.error='';l.stage='正在拆 5 个角度（约 20–40 秒）…';benchLinkPaint();
  try{
    const r=await api('/bench/angles','POST',{work_id:workId});
    l.angles=r.angles||[];l.work=r.work||l.work;
    if(!l.angles.length)throw new Error('这次没拆出角度，请重试一次。');
  }catch(e){l.error=e.message;l.angles=null;}
  l.busy=false;l.stage='';benchLinkPaint();
}
function benchLinkWatch(jobId){
  clearInterval(benchLinkTimer);
  const l=state.bench.link;
  const tick=async()=>{
    try{
      const j=await api('/jobs/'+encodeURIComponent(jobId));
      l.stage=j.stage||'正在读取…';
      if(j.status==='succeeded'){
        clearInterval(benchLinkTimer);
        const w=(j.result||{}).work||null;
        l.work=w;l.stage='';
        if(w&&w.id)await benchLinkAngles(w.id);
        else{l.busy=false;l.error='采集完成但没有拿到作品记录；请重新贴一次链接。\n\n'+LINK_FALLBACK_HINT;benchLinkPaint();}
        return;
      }
      if(j.status==='failed'||j.status==='needs_action'){
        clearInterval(benchLinkTimer);
        l.busy=false;
        l.error=(j.error||(j.result&&j.result.message)||'这次没读到这条视频。')+'\n\n'+LINK_FALLBACK_HINT;
        benchLinkPaint();return;
      }
      benchLinkPaint();
    }catch(e){clearInterval(benchLinkTimer);l.busy=false;l.error=e.message;benchLinkPaint();}
  };
  benchLinkTimer=setInterval(tick,2500);
  tick();
}
async function benchLinkSubmit(form){
  const url=(form.querySelector('[name="url"]')?.value||'').trim();
  if(!url)throw Error('先粘贴一条抖音视频链接，或抖音「分享 → 复制链接」的整段文字。');
  const l=state.bench.link;
  Object.assign(l,{busy:true,error:'',stage:'正在打开视频页，读取文案与评论…',angles:null,work:null,jobId:null});
  benchLinkPaint();
  try{
    const r=await api('/bench/video','POST',{url});
    if(r.reused&&r.work){l.work=r.work;await benchLinkAngles(r.work.id);return;}
    if(!r.job||!r.job.id)throw Error('后端没有返回任务编号，无法跟踪进度。');
    l.jobId=r.job.id;benchLinkWatch(r.job.id);
  }catch(e){l.busy=false;l.error=e.message+'\n\n'+LINK_FALLBACK_HINT;benchLinkPaint();}
}
function benchUseAngle(el){
  const i=Number(el.dataset.id)||0;
  const a=((state.bench.link&&state.bench.link.angles)||[])[i];
  if(!a)throw Error('这条角度已经不在了，请重新贴一次链接。');
  creation.picked={kind:'angle',id:String(i),label:a.label||'角度',text:a.title};
  creation.context=a.title;
  benchRerenderStage();
  const t=document.querySelector('#creation-topic-form textarea[name="context"]');
  if(t){t.focus();t.setSelectionRange(t.value.length,t.value.length);}
  const stage=document.getElementById('bench-stage');
  if(stage)stage.scrollIntoView({behavior:'smooth',block:'center'});
}
async function bench(){const requestedGoal=new URLSearchParams(location.hash.split('?')[1]||'').get('goal');if(['attention','conversion','pain'].includes(requestedGoal)){creation.goal=requestedGoal;creation.structure='';creation.subtype='';history.replaceState(null,'','#/bench');}$('#breadcrumb').textContent='开始创作 / 找选题';const b=state.bench;const acc=await api('/bench/accounts');b.accounts=acc.items||acc||[];if(b.accounts.some(x=>['queued','running'].includes(x.collect_status)))startBenchPoll();if(b.accountId&&b.accounts.some(x=>x.id===b.accountId)){const w=await api('/bench/works?'+new URLSearchParams({account_id:b.accountId,page:String(b.worksPage||1),page_size:'10',sort:b.worksSort||'share_rate'}));b.works=w.items||w.works||[];b.worksTotal=w.total!==undefined?w.total:b.works.length;}else{b.accountId=null;b.works=[];b.worksTotal=0;b.worksPage=1;}const t=await api('/bench/topics');b.topics=t.items||t.topics||[];await creationLoad();const html=renderBench(b);setTimeout(()=>{if(location.hash.startsWith('#/bench'))benchEnsureVoices();},80);return html;}
async function matrix(){$('#breadcrumb').textContent='多平台矩阵';const m=state.matrix;
  /* 2026-11-25（性能）：四个请求改并行；进过一次的页面先用缓存立刻画，
     后台刷新完再重画——串行等待是「处理中」转圈和切页慢的主因（实测冷态串行 3s+）。 */
  const gen=state.routeGen;
  const candidates=(state.boot.projects||[]).filter(p=>p.reviewed_artifact_id);
  if(m.projectId&&!candidates.some(p=>p.id===m.projectId)){m.projectId=null;m.project=null;}
  if(!m.projectId&&candidates.length)m.projectId=candidates[0].id;
  const load=async()=>{
    const [c,a]=await Promise.all([
      api('/matrix/capabilities').catch(e=>{m.capsError=e.message;return null;}),
      api('/matrix/accounts').catch(e=>{m.accountsError=e.message;return null;})]);
    if(c){m.caps=matrixCaps(c);m.capsError=null;}
    if(a){m.accounts=matrixAccounts(a);m.accountsError=null;}
    m.projectError=null;m.tasksError=null;
    if(m.projectId){
      const [p,t]=await Promise.all([
        m.project?null:api('/projects/'+encodeURIComponent(m.projectId)).catch(e=>{m.projectError=e.message;return null;}),
        api('/matrix/tasks?'+new URLSearchParams({project_id:m.projectId})).catch(e=>{m.tasksError=e.message;return null;})]);
      if(p)m.project=p;
      if(t){m.tasks=matrixTasks(t);m.tasksError=null;}
    }else m.tasks=[];
    if(m.tasks.some(t=>['queued','running','planned','submitting'].includes(t.status)))startMatrixPoll();
  };
  if(m.loaded){
    load().then(()=>{if(gen===state.routeGen&&location.hash.startsWith('#/matrix'))$('#main').innerHTML=renderMatrixReserved(m,state.boot);});
    return renderMatrixReserved(m,state.boot);
  }
  await load();
  m.loaded=true;
  return renderMatrixReserved(m,state.boot);}
/* 2026-11-17（t31，主理人定）：多平台矩阵在起步阶段降级为「预留窗口」。
   页面收敛成一张接入路线图：当前聚焦抖音单平台把飞轮跑通，其余四个平台先留位，
   只显示后端真实状态、不做模拟发布。完整的矩阵执行台（平台能力、账号、任务、演练、闸门）
   仍留在 workflow-ui.js 的 renderMatrix()，把上面那行切回去即可恢复——降级是定位收敛，不砍代码。 */
const MATRIX_RESERVED_PLATFORMS=['kuaishou','bilibili','wechat_channels','xiaohongshu'];
function renderMatrixReserved(m,boot){
  const caps=m.caps||[],accounts=m.accounts||[];
  const capOf=p=>caps.find(c=>c.platform===p)||null;
  const dyCap=capOf('douyin');
  const dyState=dyCap?matrixCapState(dyCap):['未接入','warn'];
  const dyAccounts=accounts.filter(a=>a.platform==='douyin');
  const acctStatus=a=>matrixAccountStatusLabels[a.status||a.login_state||'']||a.status||a.login_state||'状态未知';
  const dyAccountsHtml=dyAccounts.length?dyAccounts.map(a=>
    `<div class="bench-row"><div class="bench-main"><strong>${esc(a.display_name||a.account_label||'名称未取得')}</strong> ${pill(acctStatus(a),a.status==='active'||a.status==='connected'?'ok':'warn')}<div class="bench-sub">${a.last_verified_at?'上次实测 '+esc(stamp(a.last_verified_at)):'尚未实测'}</div></div><div class="bench-actions"><button class="small-button" data-action="matrix-login" data-id="${esc(a.id)}">登录 / 重新登录</button></div></div>`).join('')
    :'<p class="small muted">还没有抖音发布账号。抖音账号在「发布与复盘」里扫码连接后，才会出现在这里。</p>';
  const reservedRows=MATRIX_RESERVED_PLATFORMS.map(p=>{
    const c=capOf(p);
    const st=c?matrixCapState(c)[0]:'未接入';
    return `<div class="cap"><span>${esc(matrixPlatformName(p))}</span>${pill('预留','warn')}${st!=='未接入'?pill(st,matrixCapState(c)[1]):''}<span class="small muted">　抖音跑通后接入</span></div>`;
  }).join('');
  const errLine=(m.capsError||m.accountsError)
    ?notice('平台状态读取失败：'+(m.capsError||m.accountsError)+'（按未接入显示，以服务端返回为准）'):'';
  const taskLine=m.tasks.length&&!m.tasksError
    ?`<p class="small muted space">已有 ${m.tasks.length} 条矩阵任务：仍按原规则在后台执行，恢复完整矩阵页后可查看明细。</p>`:'';
  return `<div class="eyebrow">MULTI-PLATFORM · 预留窗口</div><h1>多平台矩阵</h1>
  <p class="lead">现在只做一件事：把抖音单平台跑通。快手 / B站 / 视频号 / 小红书 先留位，抖音这条飞轮跑顺之后再逐个接入、各自验收。这里不摆未接入的假入口，也不做模拟发布。</p>
  <div class="workspace"><div class="stack">
  <section class="panel"><div class="row spread"><h2>平台接入路线图</h2><button class="ghost small-button" data-action="refresh">刷新</button></div>
    ${errLine}
    <div class="caps"><div class="cap"><span>抖音 · 当前聚焦</span>${pill(dyState[0],dyState[1])}${dyAccounts.length?`<span class="small muted">　${dyAccounts.length} 个账号</span>`:''}</div>${reservedRows}</div>
    ${dyCap&&dyCap.message?`<p class="small muted space">${esc(dyCap.message)}</p>`:''}
    ${dyAccountsHtml}
    ${taskLine}
    <p class="small muted space">预留不等于空壳：每个平台接入都要用本人真实账号跑通登录、发布与回收，缺一项都算未接入。</p>
  </section>
  <section class="panel"><h2>接入一个新平台要过什么</h2>
    <div class="rule"><span class="fact-label">真实账号</span>用本人真实账号实测，不拿接口文档和模拟数据充数。</div>
    <div class="rule"><span class="fact-label">真实返回</span>只有平台返回作品 ID 与链接才算成功，状态一律以服务端返回为准。</div>
    <div class="rule"><span class="fact-label">演练先行</span>演练只上传草稿不点发布；真实提交必须本人勾选确认。</div>
    <div class="rule"><span class="fact-label">互不影响</span>逐个接入、各自验收，任一平台失败不影响其他平台。</div>
  </section>
  </div><aside><section class="panel"><h3>为什么先单平台</h3>
    <p class="small muted">选题 → 出片 → 发布 → 复盘，这条飞轮在一个平台上跑顺，接到第二个平台才是复制；同时铺五个平台只会把注意力摊薄。</p>
    <div class="rule"><span class="fact-label">抖音链路</span>不变：发布与复盘里的抖音单账号流程照旧可用。</div>
    <a class="link small" href="#/feedback">→ 去发布与复盘</a>
    <a class="link small" href="#/create">→ 返回开始创作</a>
  </section></aside></div>`;
}
/* 2026-11-16（点检 S2）：发布与复盘改成「先出骨架/旧内容，再补真实数据」。
   原因：旧实现把 release/overview/works/insights 以及每个作品的 snapshots 全部 await 完才替换 #main，
   面包屑已经切换、正文却还挂着上一页；快照又是逐作品请求（N+1），作品越多越慢。
   现在：骨架立即返回；复盘数据并行拉取；每作品最近快照时间直接用 overview.latest_snapshots 派生，不再 N+1。 */
async function feedback(){
  $('#breadcrumb').textContent='发布与复盘';
  const f=state.feedback;
  const view=new URLSearchParams(location.hash.split('?')[1]||'').get('view')==='review'?'review':'hub';
  const projects=state.boot.projects||[];
  if(f.projectId&&!projects.some(p=>p.id===f.projectId)){f.projectId=null;f.collect=null;}
  if(!f.projectId&&projects.length)f.projectId=projects[0].id;
  f.errors={};f.overview=null;f.works=[];f.snapshots={};f.insights=[];
  f.release=null;f.releaseError=null;
  f.view=view;
  const gen=state.routeGen;
  loadFeedbackData(f,f.projectId,view,gen);
  const key=view+'|'+(f.projectId||'');
  // 同一项目/视图已经渲染过：先复用上一份 DOM，后台刷新，避免轮询时骨架闪一下。
  if(f.lastKey===key&&f.lastHtml)return f.lastHtml;
  return feedbackSkeleton(f);
}
function feedbackSkeleton(f){
  const review=f.view==='review';
  const tabs=typeof releaseTabs==='function'?releaseTabs():'';
  return `<div class="eyebrow">PUBLISH & REVIEW</div><h1>发布与复盘</h1><p class="lead">${review?'发布数据与评论回流的真实记录，只做有依据的复盘；单条作品数据不构成确定因果。':'整理发布资料，发到平台，再把反馈带回来。'}</p>${tabs}<section class="panel space" id="feedback-loading"><p class="small muted">正在加载${review?'复盘数据':'发布资料'}…</p></section>`;
}
function paintFeedback(f,gen){
  requestAnimationFrame(()=>{
    if(gen!==state.routeGen||!location.hash.startsWith('#/feedback'))return;
    const main=$('#main');
    if(!main)return;
    // 骨架还在（首次进入/刷新）；或页面仍是发布与复盘（轮询刷新旧内容）时才替换。
    if(!main.querySelector('#feedback-loading')&&!main.querySelector('.factory-navigation'))return;
    const html=renderFeedback(f,state.boot);
    f.lastHtml=html;f.lastKey=f.view+'|'+(f.projectId||'');
    main.innerHTML=html;
  });
}
async function loadFeedbackData(f,projectId,view,gen){
  const stale=()=>gen!==state.routeGen||!location.hash.startsWith('#/feedback');
  if(projectId){
    try{f.release=await api('/releases/kit?'+new URLSearchParams({project_id:projectId}));}catch(e){f.releaseError=e.message;}
  }
  if(stale())return;
  if(!projectId){paintFeedback(f,gen);return;}
  if(view==='review'){
    await Promise.all([
      (async()=>{try{f.overview=await api('/feedback/overview?'+new URLSearchParams({project_id:projectId}));}catch(e){f.errors.overview=e.message;}})(),
      (async()=>{try{const w=await api('/feedback/works?'+new URLSearchParams({project_id:projectId}));f.works=w.items||w.works||w.platform_works||[];}catch(e){f.errors.works=e.message;f.works=[];}})(),
      (async()=>{try{const i=await api('/feedback/insights?'+new URLSearchParams({project_id:projectId}));f.insights=i.items||i.insights||[];
        // t71：候选卡背书状态回显（只读 GET，失败不影响复盘页）
        for(const ins of f.insights){
          if(!ins.candidate_knowledge_id)continue;
          try{const c=await api('/knowledge/'+encodeURIComponent(ins.candidate_knowledge_id));ins.candidate_card={endorsement:c.endorsement,title:c.title,markers:c.markers||[],auto:!!c.auto_accepted_at};}catch(e){ins.candidate_card=null;}
        }}catch(e){f.errors.insights=e.message;f.insights=[];}})()
    ]);
    // 页面只用到「每个作品最近一次快照时间」，overview 已经带回 latest_snapshots，无需再逐作品请求。
    const byWork={};
    ((f.overview&&f.overview.latest_snapshots)||[]).forEach(s=>{
      const wid=s&&s.platform_work_id;if(!wid)return;
      (byWork[wid]=byWork[wid]||[]).push(s);
    });
    f.snapshots=byWork;
  }
  if(stale())return;
  if(f.collect&&['queued','running','pending'].includes(f.collect.status))startFeedbackPoll();
  paintFeedback(f,gen);
}
function createDialog(knowledgeId=''){
  modal('新建一条内容',`<div class="create-choice"><p class="lead">先写一句你想说的，就能开工。</p><div class="create-choice-actions"><button class="primary" data-action="create-mode" data-mode="script" data-knowledge="${esc(knowledgeId)}">我直接写稿</button><button data-action="create-mode" data-mode="video" data-knowledge="${esc(knowledgeId)}">我有参考视频</button></div><p class="small muted">不用准备素材，直接开始写讲稿；以后有参考视频，随时回来补，会先转写、拆解再帮你起草。</p></div>`);
}
function createForm(mode,knowledgeId=''){
  const video=mode==='video';
  modal(video?'新建 · 从参考视频开始':'新建 · 直接写稿',`<form id="create-project"><input type="hidden" name="knowledge_id" value="${esc(knowledgeId)}"><input type="hidden" name="create_mode" value="${video?'video':'script'}"><label class="field"><span>这条内容讲什么</span><input name="title" type="text" maxlength="160" required placeholder="给一个具体问题起名字"></label>${video?`<label class="field"><span>参考视频链接（可选）</span><input name="url" type="text" placeholder="粘贴链接或完整分享文字"></label><label class="field"><span>原字幕、参考资料或本人经历（可选）</span><textarea name="source_text" placeholder="暂未连接解析器时，可以先粘贴正文。只粘链接也会保存，不会制造解析结果。"></textarea></label>`:''}<p class="small muted">${video?'资料真实保存在本机。模型未配置时，也可以先手动写稿。':'先写你要说的话；以后想补参考视频，在「链接与资料」页随时能加。'}</p><button class="primary" type="submit">${video?'建立作品，去取视频资料':'建立作品，开始写讲稿'}</button></form>`);
}
async function factory(){$('#breadcrumb').textContent='创作中心 / 工作总览';try{const [h,t]=await Promise.all([api('/creation/history'),api('/bench/topics')]);creation.history=h.items||[];state.bench.topics=t.items||t.topics||[];}catch(e){toast(e.message);}const listError=state.bootProjectsError?`<div class="notice error">作品列表读取失败：${esc(state.bootProjectsError)} <button type="button" class="small-button" data-action="retry-projects">重试</button></div>`:'';return listError+renderFactory(state.boot);}
async function brain(){const query=new URLSearchParams(location.hash.split('?')[1]||''),data=await api('/knowledge?'+query);state.items=data.items;const newsData=await api('/brain/news');state.brainNews=(newsData.ok&&newsData.items)||[];$('#breadcrumb').textContent='知识库';
  // 页面加载时自动抓一次新闻（后台静默执行）
  if(!state.brainNewsFetched){
    state.brainNewsFetched=true;
    const canRefresh=!!(state.boot&&state.boot.capabilities&&state.boot.capabilities.resolver);
    if(canRefresh){
      api('/brain/news/refresh','POST',{keywords:['短视频','AI','自媒体','个人IP','带货']}).then(r=>{
        if(r&&r.ok&&r.items&&r.items.length){
          state.brainNews=r.items;
          route();
        }
      }).catch(()=>{state.brainNewsAutoFailed=true;});
    }
  }
  return renderBrain(data,query);}
async function knowledge(id){const x=await api('/knowledge/'+encodeURIComponent(id));state.knowledgeItem=x;$('#breadcrumb').textContent='知识库 / '+x.legacy_id;const lifeNotice=x.lifecycle==='deleted'?notice('这条认知在「已删除」里：默认不出现在认知列表和认知图谱。可以恢复，或彻底删除（不能找回）。')+`<div class="row space"><button type="button" data-action="knowledge-restore" data-id="${esc(x.id)}">恢复这条认知</button><button type="button" class="danger" data-action="knowledge-purge" data-id="${esc(x.id)}">彻底删除</button></div>`:x.lifecycle==='archived'?notice('这条认知已归档：默认不出现在认知列表和认知图谱里。要恢复，把右侧「生命周期」改回「当前使用」并保存。'):x.lifecycle==='superseded'?notice('这条认知已被更新版本替代：默认不出现在认知列表和认知图谱里。要恢复，把右侧「生命周期」改回「当前使用」并保存。'):'';return `<a class="link small" href="#/brain">← 返回认知库</a>${lifeNotice}<div class="eyebrow space">${esc(x.legacy_id)}</div><h1>${esc(x.title)}</h1><div class="workspace space"><article class="panel"><h3>核心主张</h3><p class="source-quote">${esc(x.claim.replace(/\*\*/g,''))}</p><h3 class="space">边界</h3><p>${esc(x.boundary.replace(/\*\*/g,'')||'原资料未单独提供，不能推定普遍适用。')}</p><details><summary>完整原文与历史标记</summary>${x.body_origin==='claim'?notice('这段原文不是单独记录的：保存时勾选了用「核心主张」填充，属于自动带入。'):(!x.body_origin&&x.body&&x.body===x.claim?notice('这段原文与核心主张完全相同，可能是早期保存时自动带入的，请自行核对。'):'')}${String(x.body||'').trim()?`<pre class="raw-markdown space">${esc(x.body)}</pre>`:'<p class="small muted">这条认知没有单独记录原文（不是自动生成）。</p>'}${x.context_notes?`<h3 class="space">相关修订与用途说明（历史原文）</h3><pre class="raw-markdown">${esc(x.context_notes)}</pre>`:''}</details></article><aside><div class="panel"><h3>我怎样使用这条认知</h3>${x.content_use==='review'?notice('这条有历史用途限制，写稿前请展开下方选项核对。'):''}<form id="knowledge-state"><label class="field"><span>本人认同</span><select name="endorsement">${Object.entries(endorsements).map(([v,l])=>`<option value="${v}" ${x.endorsement===v?'selected':''}>${l}</option>`).join('')}</select></label><details><summary>补充验证与使用状态（可稍后）</summary><label class="field space"><span>验证依据</span><select name="validation">${Object.entries(validations).map(([v,l])=>`<option value="${v}" ${x.validation===v?'selected':''}>${l}</option>`).join('')}</select></label>${x.content_use==='review'?notice('原文有用途限制，请核对后再决定是否用于写稿。')+'<label class="check"><input name="allow_content" type="checkbox">我已核对，允许用于写稿</label>':''}<label class="field"><span>生命周期</span><select name="lifecycle">${Object.entries(lifecycles).map(([v,l])=>`<option value="${v}" ${(x.lifecycle||'active')===v?'selected':''}>${l}</option>`).join('')}</select></label></details><button class="primary space" type="submit">保存我的判断</button></form><div class="divider"></div><button data-action="from-knowledge" data-id="${esc(x.id)}">从这条认知创建作品</button><div class="rule space"><button type="button" class="danger" data-action="knowledge-delete" data-id="${esc(x.id)}">删除这张认知卡</button><p class="small muted">删除后移到「已删除」，随时能恢复。</p><div id="kb-delete-bar"></div></div><div class="rule space"><span class="fact-label">来源定位</span>${x.source_locator?.line?'历史导出 · 第 '+x.source_locator.line+' 行':'手动记录 / 来源提炼'}${x.source_id?`<br><a class="link small" href="${media('/api/sources/'+x.source_id)}">下载完整来源与修订记录</a>`:''}</div><div class="rule"><span class="fact-label">关联条目</span>${esc(x.links?.join(' · ')||'未指定')}</div><div class="rule"><span class="fact-label">原始标记</span>${esc(x.markers?.join(' / ')||'无额外标记')}</div></div></aside></div>`;}
const JOB_RETRY={
  direct_pipeline:j=>({name:'direct-run',body:{asset_id:j.inputs&&j.inputs.asset_id,silence_mode:(j.inputs&&j.inputs.silence_mode)||'conservative'}}),
  resolve:()=>({name:'resolve',body:{}}),
  transcribe:j=>({name:'transcribe',body:{asset_id:j.inputs.asset_id}}),
  cut:j=>({name:'cut',body:{asset_id:j.inputs.asset_id,mode:j.inputs.mode||'natural',segments:j.inputs.segments}}),
  prepare_reference:j=>({name:'prepare-content',body:{asset_id:j.inputs.asset_id}}),
  prepare_content:j=>({name:'prepare-content',body:j.inputs.asset_id?{asset_id:j.inputs.asset_id}:{}}),
  semantic_propose:j=>({name:'semantic-cut',body:{asset_id:j.inputs.asset_id}}),
  semantic_cut:j=>({name:'apply-semantic-cut',body:{asset_id:(state.project.semantic_plans||[]).find(x=>x.id===j.inputs.plan_id)?.asset_id,plan_id:j.inputs.plan_id,deletion_ids:j.inputs.deletion_ids,auto_apply:!!j.inputs.auto_apply}}),
  semantic_auto:j=>({name:'semantic-auto-apply',body:{asset_id:j.inputs.asset_id,silence_mode:j.inputs.silence_mode||'conservative'}}),
  rebuild:j=>({name:'rebuild-render',body:{asset_id:j.inputs.asset_id,target_s:j.inputs.target_s,lufs:j.inputs.lufs}}),
  prepare_video:j=>({name:'prepare-video',body:{asset_id:j.inputs.asset_id,artifact_id:j.inputs.base_artifact_id}}),
  caption_revision:j=>({name:'revise-captions',body:{artifact_id:j.inputs.artifact_id,edits:j.inputs.edits}}),
  motion_render:j=>({name:'motion',body:{artifact_id:j.inputs.artifact_id,brief:j.inputs.brief,render:true}}),
  motion_preview:j=>({name:'motion',body:{artifact_id:j.inputs.artifact_id,brief:j.inputs.brief,render:false}}),
  packaging_render:j=>({name:'packaging-render',body:{plan_id:j.inputs.plan_id,bgm:!!j.inputs.bgm}}),
  packaging_preview:j=>({name:'packaging-preview',body:{plan_id:j.inputs.plan_id,bgm:!!j.inputs.bgm}}),
  package_lite:j=>({name:'package-lite',body:{artifact_id:j.inputs.artifact_id}}),
  cover_render:j=>({name:'cover-generate',body:Object.assign({},j.inputs)}),
  anchor_align:j=>({name:'anchor-align',body:{asset_id:j.inputs.asset_id}}),
  /* 2026-11-25：写作类任务失败可重试。writing-suggestions 就是讲稿页「帮我搭一版初稿骨架 / 参考二创 / 口语化润色」共用的接口
     （py/app.py → creation_service.writing；任务 inputs 是当时的快照，含 mode/reference/context）。 */
  creation_draft:j=>({name:'writing-suggestions',body:{mode:'draft',draft_revision:state.project.draft_revision,context:(j.inputs&&j.inputs.context)||''}}),
  creation_recreate:j=>({name:'writing-suggestions',body:{mode:'recreate',draft_revision:state.project.draft_revision,reference:(j.inputs&&j.inputs.reference)||'',context:(j.inputs&&j.inputs.context)||''}}),
  creation_polish:j=>({name:'writing-suggestions',body:{mode:'polish',draft_revision:state.project.draft_revision}}),
  rewrite:()=>({name:'generate',body:{}})
};
const JOB_NEXT_STEP={
  anchor_cut:'这类任务要逐段选择 take：回「精细剪辑」重新选择后再提交。',
  publish_plan:'回发布页重新点「发布演练」。',
  publish_submit:'回发布页核对成片与资料、重新勾选确认后再发布。',
  creation_topics:'回「找选题」重新点「生成选题」。',
  creation_pain:'回「找选题」重新点「生成选题」。',
  creation_series:'回「找选题」重新点「生成选题」。'
};
function jobItem(j){
  const retry=JOB_RETRY[j.kind],next=JOB_NEXT_STEP[j.kind],failed=j.status==='failed';
  return `<div class="job"><div class="row spread"><span class="small">${esc({growth_transcribe:'视频转文字稿',creation_draft:'选题成稿',creation_recreate:'参考二创',creation_polish:'口语化润色',cut:'剪掉停顿和口误',rewrite:'讲稿改写',resolve:'读取链接',extract_knowledge:'认知提炼',transcribe:'识别视频里的语音',motion_preview:'模板预览帧',motion_render:'模板成片',prepare_reference:'视频转文字并整理',prepare_content:'内容整理与初稿',prepare_video:'一键字幕出片',caption_revision:'字幕校对出片',semantic_propose:'剪掉停顿和口误分析',semantic_cut:'剪掉停顿和口误剪切',direct_pipeline:'直剪自动处理',rebuild:'成片重建',anchor_align:'稿本锚定分析',anchor_cut:'稿本锚定剪切',director_brief:'导演三件套',package_lite:'学习包装（轻）',packaging_preview:'包装片段试看',packaging_render:'智能包装成片',cover_render:'封面生成',publish_login:'抖音扫码登录',publish_plan:'发布演练',publish_submit:'正式发布'}[j.kind]||j.kind)}</span>${pill(jobLabels[j.status]||j.status,j.status==='succeeded'?'ok':j.status==='failed'?'error':j.status==='needs_action'?'warn':'gold')}</div><p id="job-stage-${esc(j.id)}">${esc(j.stage_text||j.stage)}</p>${j.error?jobFailHtml(j):''}${failed&&retry?`<button class="small-button space" data-action="job-retry" data-id="${esc(j.id)}">▶ 重新处理${/服务重启/.test(j.stage||'')?'（素材还在）':''}</button>`:''}${failed?jobNextHtml(j)||(failed&&!retry&&next?`<span class="jobs-next">${esc(next)}</span>`:''):''}</div>`;
}
// t97（修订 t95）：净剪成功不自动渲染包装；只自动展开/滚动到模板选择，由本人选定后手动出片。
function openPackageDetails(){
  const sec=document.querySelector('.smart-packaging');
  if(!sec)return false;
  const outer=sec.closest('details');if(outer)outer.open=true;
  sec.querySelectorAll('details').forEach(d=>{d.open=true;});
  sec.scrollIntoView({block:'center'});
  return true;
}
function maybeRevealPackaging(p){
  if(!p||p.mode==='direct')return;
  let manual=false;try{manual=!!localStorage.getItem('ip-pack-open:'+p.id);}catch(e){}
  if(manual){openPackageDetails();return;}
  const jobs=p.jobs||[];
  const j=jobs.find(x=>x.kind==='semantic_auto'&&x.status==='succeeded');
  if(!j)return;
  const cleanArtifacts=(p.artifacts||[]).filter(a=>a.kind==='clean_video'&&(!j.inputs?.asset_id||a.asset_id===j.inputs.asset_id));
  const art=(p.artifacts||[]).find(a=>a.id===p.latest_artifact_id&&a.kind==='clean_video')||cleanArtifacts.sort((a,b)=>String(b.created_at||'').localeCompare(String(a.created_at||'')))[0];
  if(!art)return;
  const key='ip-packselect:'+p.id+':'+art.id;
  try{if(localStorage.getItem(key))return;localStorage.setItem(key,'1');}catch(e){return;}
  setTimeout(openPackageDetails,60);
}
function jobs(p){const isDirect=p.mode==='direct';const onCurrent=j=>{const a=j.inputs&&j.inputs.asset_id;return !(isDirect&&a&&p.current_asset_id&&a!==p.current_asset_id);};const cur=p.jobs.filter(onCurrent),hist=p.jobs.filter(j=>!onCurrent(j));return `<div class="panel"><div class="row spread"><h3>后台任务</h3><button data-action="refresh" class="ghost small-button">刷新</button></div>${cur.length?cur.slice(0,7).map(jobItem).join(''):'<p class="small muted">后台任务开始后，即使关闭网页也会继续执行。</p>'}${hist.length?`<details class="space"><summary class="small muted">历史任务（其他素材，${hist.length} 条，不影响当前成片）</summary>${hist.slice(0,10).map(jobItem).join('')}</details>`:''}</div>`;}

const JOB_KIND_LABELS={growth_transcribe:'视频转文字稿',creation_draft:'选题成稿',creation_recreate:'参考二创',creation_polish:'口语化润色',cut:'剪掉停顿和口误',rewrite:'讲稿改写',resolve:'读取链接',extract_knowledge:'认知提炼',transcribe:'识别视频里的语音',motion_preview:'模板预览帧',motion_render:'模板成片',prepare_reference:'视频转文字并整理',prepare_content:'内容整理与初稿',prepare_video:'一键字幕出片',caption_revision:'字幕校对出片',semantic_propose:'剪掉停顿和口误分析',semantic_cut:'剪掉停顿和口误剪切',direct_pipeline:'直剪自动处理',rebuild:'成片重建',anchor_align:'稿本锚定分析',anchor_cut:'稿本锚定剪切',director_brief:'导演三件套',package_lite:'学习包装（轻）',packaging_preview:'包装片段试看',packaging_render:'智能包装成片',cover_render:'封面生成',publish_login:'抖音扫码登录',publish_plan:'发布演练',publish_submit:'正式发布'};
function jobStatusDot(j){return ['queued','running'].includes(j.status)?'run':j.status==='failed'?'err':j.status==='succeeded'?'ok':'idle';}
/* 2026-11-25（开始创作嫁接 B）：任务失败卡 = 人话原因 + 查看详情（原话）+ 一个能点的「下一步」。
   人话 / 下一步 / 能否重试来自后端（py/app.py customer_error）：任务对象带 error_view，或平铺的 error_title / error_detail / error_next / error_retry。 */
let _jobsCssDone=false;
function jobFail(j){
  const v=j.error_view||(j.error_title?{title:j.error_title,detail:j.error_detail,next:j.error_next,retry:j.error_retry}:{});
  if(!_jobsCssDone){_jobsCssDone=true;ipAddCssRules(['.jobs-detail{max-width:min(300px,52vw)}.jobs-detail pre{white-space:pre-wrap;word-break:break-word;max-height:200px;overflow:auto;font-size:11px;line-height:1.5}','.jobs-act{display:flex;flex-direction:column;align-items:flex-end;gap:6px}','.jobs-act:empty{display:none}']);}
  return {title:v.title||j.error||'这一步没有完成。',detail:v.detail||j.error||'',next:v.next||null,retry:typeof v.retry==='boolean'?v.retry:true};
}
function jobFailHtml(j){const f=jobFail(j);return `<span class="jobs-error" title="${esc(f.title)}">${esc(f.title)}</span>${f.detail&&f.detail!==f.title?`<details class="jobs-detail"><summary class="small muted">查看详情</summary><pre class="raw-markdown">${esc(f.detail)}</pre></details>`:''}`;}
/* 「下一步」里 retry / detail / back 三种交给旁边的重试按钮或原提示，不再画按钮；没有下一步时退回 JOB_NEXT_STEP 的文字提示。 */
function jobNextHtml(j){const f=jobFail(j),a=f.next&&f.next.action,retry=JOB_RETRY[j.kind],hint=JOB_NEXT_STEP[j.kind];
  if(!f.next)return retry?'':hint?`<span class="jobs-next">${esc(hint)}</span>`:`<button class="ghost small-button" data-action="refresh">刷新</button>`;
  if(a==='settings')return `<a class="small-button" href="#/settings">${esc(f.next.label)}</a>`;
  if(['retry','detail','back'].includes(a))return !retry&&hint?`<span class="jobs-next">${esc(hint)}</span>`:'';
  return `<button class="small-button" data-action="job-next" data-id="${esc(j.id)}">${esc(f.next.label)}</button>`;}
function jobRow(j){
  const label=JOB_KIND_LABELS[j.kind]||j.kind,retry=JOB_RETRY[j.kind],failed=j.status==='failed';
  const act=failed?(retry?`<button class="small-button" data-action="job-retry" data-id="${esc(j.id)}">重新处理</button>`:'')+jobNextHtml(j):'';
  return `<div class="jobs-row" data-status="${esc(j.status)}"><span class="jobs-dot ${jobStatusDot(j)}" aria-hidden="true"></span><div class="jobs-main"><strong>${esc(label)}</strong><span class="jobs-stage">${esc(j.stage_text||j.stage||'')}</span>${failed?jobFailHtml(j):''}</div>${act?`<span class="jobs-act">${act}</span>`:''}</div>`;
}
function jobsDrawer(p){
  const cur=p.jobs.filter(j=>['queued','running','failed','needs_action'].includes(j.status));
  const hist=p.jobs.filter(j=>!['queued','running','failed','needs_action'].includes(j.status));
  return `<div class="jobs-drawer" id="jobs-drawer" role="dialog" aria-label="后台任务" aria-modal="false"><div class="jobs-head"><strong>后台任务</strong><span class="small muted">${cur.length?cur.length+' 条待看':'暂无进行中'}</span><button class="ghost small-button" data-action="jobs-close">收起</button></div><div class="jobs-body">${cur.length?cur.map(jobRow).join(''):'<p class="small muted">没有进行中的任务。生成与转写都在后台继续，完成后这里会出现结果。</p>'}${hist.length?`<button type="button" class="jobs-hist-toggle small muted" data-action="jobs-hist" aria-expanded="false" aria-controls="jobs-hist">历史任务（${hist.length} 条） ▾</button><div class="jobs-hist" id="jobs-hist" hidden>${hist.slice(0,20).map(jobRow).join('')}</div>`:''}</div></div>`;
}
function syncJobsFab(){
  const p=state.project;
  let root=document.getElementById('jobs-root');
  if(!root){root=document.createElement('div');root.id='jobs-root';document.body.appendChild(root);}
  if(!p){root.innerHTML='';state.jobsOpen=false;return;}
  const active=p.jobs.filter(j=>['queued','running'].includes(j.status)).length;
  const failed=p.jobs.filter(j=>j.status==='failed').length;
  const busy=active>0;
  // 2026-11-26 同类排查：FAB 直接带当前阶段文字，不打开抽屉也看得见进度。
  const stageJob=p.jobs.find(j=>['queued','running'].includes(j.status));
  const stageTxt=stageJob?String(stageJob.stage_text||stageJob.stage||'').slice(0,14):'';
  const fabLabel=busy&&stageTxt?'后台任务 · '+stageTxt:'后台任务';
  root.innerHTML=`<button type="button" class="jobs-fab${busy?' is-busy':''}${!busy&&failed?' has-error':''}" data-action="jobs-toggle" aria-expanded="${state.jobsOpen?'true':'false'}" aria-controls="jobs-drawer"><span class="jobs-fab-icon" aria-hidden="true">${busy?'◌':'▤'}</span><span class="jobs-fab-label">${esc(fabLabel)}</span>${active?`<span class="jobs-badge">${active}</span>`:failed?`<span class="jobs-badge is-error">!</span>`:''}</button>`+(state.jobsOpen?jobsDrawer(p):'');
}
function toggleJobs(open){state.jobsOpen=open===undefined?!state.jobsOpen:!!open;syncJobsFab();}
function hideInlineJobs(){
  document.querySelectorAll('#main .panel').forEach(el=>{
    const h=el.querySelector('h3');
    if(h&&h.textContent.trim()==='后台任务'){el.hidden=true;const wrap=el.parentElement;if(wrap&&wrap.classList.contains('space'))wrap.hidden=true;}
  });
  // 2026-11-26：后台任务移去右下抽屉后，.workspace 右列只剩被隐藏的面板，
  // 成为一条永久空白带（用户红框反馈）。右列无可见内容时藏掉并收回列宽；
  // 有可见内容（如 brain-layout 侧栏）仍保持双列。
  document.querySelectorAll('#main .workspace').forEach(ws=>{
    const aside=ws.querySelector(':scope > aside');
    if(!aside)return;
    const visible=Array.from(aside.children).some(c=>!c.hidden);
    aside.hidden=!visible;
    ws.classList.toggle('workspace-solo',!visible);
  });
}

function sourceView(p){return renderSourceChain(p);}
/* t140 主权可感知：三信号条（全局助手，写稿/审片/交付共用；纯显示层，复用现有数据，不新建模型逻辑）。
   ① 用你的声音＝本次生成快照 style_card 非空（风格卡真注入了才标）；
   ② 去AI味＝复用既有 lint-script 入口（只提示不改稿），更显性；
   ③ 这条只有你能讲＝conviction_hook.source_id 溯源到本人真实积累（t30 hook_source，无源 null 不标）。
   空/无来源不假标。creation-ui.js 的写稿面板/创作记录弹窗委托本助手（app.js 恒加载，edit/publish 步不载 creation-ui）。 */
let _sovCssDone=false;
function sovereigntyLatestSuggestion(p){
  const jobs=(p.jobs||[]).filter(j=>j.kind&&j.kind.startsWith('creation_')&&j.status==='succeeded'&&j.result&&j.result.suggestion);
  if(!jobs.length)return null;
  jobs.sort((a,b)=>String(b.updated_at||b.created_at||'').localeCompare(String(a.updated_at||a.created_at||'')));
  return jobs[0].result.suggestion;
}
function sovereigntyStrip(sug,lintButton){
  if(!_sovCssDone){_sovCssDone=true;ipAddCssRules([
    '.sov-strip{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:10px 0 0;padding:8px 12px;border:1px solid var(--border);border-radius:10px;background:var(--tint)}',
    '.sov-chip{font-size:12px;line-height:1.6;padding:2px 10px;border-radius:999px;border:1px solid var(--gold);color:var(--text);background:var(--surface)}',
    '.sov-chip.sov-own{border-color:var(--ok);color:var(--ok)}'
  ]);}
  const chips=[];
  if(sug){
    const sc=sug.input_snapshot&&typeof sug.input_snapshot.style_card==='string'?sug.input_snapshot.style_card.trim():'';
    if(sc)chips.push('<span class="sov-chip" title="本次生成注入了你的风格卡（句长/称呼/节奏/口头禅），不是通用腔">◉ 用你的声音 · 风格卡已注入</span>');
    const hook=sug.conviction_hook;
    if(hook&&hook.line&&hook.source_id){
      const pool=(window.creation&&creation.knowledge)||state.items||[];
      const src=pool.find(k=>k.id===hook.source_id);
      chips.push('<span class="sov-chip sov-own" title="开头钩子经 hook_source 溯源到你本人的真实积累（原话公共子串≥4字），不是编的">✦ 这条只有你能讲 · 开头来自「'+esc(src?src.title:'你的真实积累')+'」</span>');
    }
  }
  const lint=lintButton
    ?'<button type="button" class="ghost small-button" data-action="lint-script">去AI味检查</button>'
    :'<span class="small muted">去AI味：到写稿页点「去AI味检查」（只提示不改稿）</span>';
  const note=chips.length?'信号来自本次生成的真实输入；无来源不标。':'本次未命中风格卡/钩子溯源，不假标；生成后可再查。';
  return '<div class="sov-strip">'+chips.join('')+lint+'<span class="small muted">'+note+'</span></div>';
}
/* t140：edit 步的审片主面板由 chain-ui 渲染（editView 被收进折叠 details，t104 IA），
   渲染后把信号条注入可见的审片面板（含 review 按钮的那个），不动 chain-ui 写域。 */
function syncSovereignty(){
  try{
    if(!state.project||state.step!=='edit')return;
    if(document.querySelector('.sov-strip[data-sov-edit]'))return;
    const reviewBtn=document.querySelector('[data-action="review"]');
    const panel=reviewBtn&&reviewBtn.closest('section.panel,.panel');
    if(!panel)return;
    const html=sovereigntyStrip(sovereigntyLatestSuggestion(state.project),false).replace('<div class="sov-strip">','<div class="sov-strip" data-sov-edit="1">');
    const head=panel.querySelector('.row.spread');
    if(head)head.insertAdjacentHTML('afterend',html);else panel.insertAdjacentHTML('afterbegin',html);
  }catch(e){}
}
/* 2026-11-25：只读的「口播逐字稿全文」面板换成整篇编辑框的小头部（标题 + 去AI味检查 + 复制全文）。
   creation-ui.js 的创作记录弹窗还靠页面上有没有 #script-full-text 判断「当前在讲稿页」，所以编辑框外面留这个 id。 */
function scriptEditorHead(p){return `<div class="row spread space"><h3>口播逐字稿全文<span class="small muted"> · ${draftWholeText(p).length} 字</span></h3><span class="row"><button type="button" class="ghost small-button" data-action="lint-script" title="只提示不改稿，不挡确认">去AI味检查</button><button type="button" class="ghost small-button" data-action="copy-script">复制全文</button><button type="button" class="ghost small-button" data-action="share-script" title="生成公开网页链接，微信里可直接打开">生成分享链接</button></span></div>`;}
/* 2026-11-25（分享一期）：公开分享链接。预览+本人确认才发；脱敏闸在服务端，
   这里只负责把「将要公开的全部内容」摆给人看。撤销后链接立即失效。 */
function shareBar(p){
  const s=state.share;
  if(!s||s.ref!==p.id||s.revoked)return '';
  return `<div class="panel space"><div class="row spread"><strong>已公开分享</strong><span class="small muted">被看 <span id="share-views">${Number(s.views)||0}</span> 次</span></div><p class="small" style="word-break:break-all">${esc(s.url)}</p><span class="row"><button type="button" class="ghost small-button" data-action="share-copy">复制链接</button><button type="button" class="ghost small-button" data-action="share-revoke">撤销分享</button></span></div>`;
}
function sharePreviewHtml(title,body){
  return `<label class="field"><span>分享页标题（可改）</span><input id="share-title" type="text" maxlength="80" value="${esc(title)}"></label>`+
    `<p class="small muted">下面是要公开的全部内容（${body.length} 字）。任何人打开链接都能看到，不需登录；页底带「体验人生重启系统」入口。</p>`+
    `<pre class="raw-markdown" style="max-height:40vh;overflow:auto">${esc(body)}</pre>`+
    `<label class="check"><input id="share-confirm" type="checkbox">我确认公开这份稿子，不含隐私与未授权信息</label>`+
    `<p id="share-error" class="small space" style="color:#c0392b"></p>`+
    `<div class="actions"><button type="button" class="primary" data-action="share-publish">确认并复制链接</button><button type="button" class="ghost" data-action="share-close">取消</button></div>`;
}
function shareDoneHtml(s){
  return `<div class="rule"><strong>分享链接</strong><p class="small" style="word-break:break-all">${esc(s.url)}</p></div>`+
    `<p class="small muted">链接已自动复制，去微信粘贴即可。被看 ${Number(s.views)||0} 次（按设备按天去重）；点撤销后链接立即失效。</p>`+
    `<div class="actions"><button type="button" class="primary" data-action="share-close">关闭</button><button type="button" class="ghost" data-action="share-copy">再复制一次</button><button type="button" class="ghost" data-action="share-revoke">撤销分享</button></div>`;
}
async function refreshShareViews(ref){
  const s=state.share;
  if(!s||s.ref!==ref||s.revoked)return;
  try{
    const r=await api('/share/mine');
    const hit=(r.items||[]).find(x=>x.token===s.token);
    if(hit){s.views=hit.views;s.revoked=hit.revoked;const el=$('#share-views');if(el)el.textContent=String(hit.views);}
  }catch(e){/* 计数刷新失败不打扰：链接本身仍可用 */}
}
/* 2026-11-26（分享二期）：阅读数回流。分享出去要有回报——mine 静默轮询，
   与 localStorage 基线比增量，有新阅读才 toast 一次；首见只记基线不打扰。
   弹窗开着/页面隐藏时跳过，不抢用户正在做的确认。 */
const SHARE_SEEN_KEY='rs_share_seen_views';
let shareRefluxTimer=null;
function shareSeenRead(){try{const m=JSON.parse(localStorage.getItem(SHARE_SEEN_KEY)||'{}');return m&&typeof m==='object'?m:{};}catch(e){return {};}}
function shareSeenWrite(m){try{localStorage.setItem(SHARE_SEEN_KEY,JSON.stringify(m));}catch(e){}}
async function refluxShareViews(){
  if(document.hidden||$('#modal').open)return;
  let r;try{r=await api('/share/mine');}catch(e){return;}
  const items=(r&&r.items)||[];if(!items.length)return;
  const seen=shareSeenRead();let changed=false;let added=0;let first=null;
  items.forEach(x=>{
    if(!x||x.revoked)return;
    const v=Number(x.views)||0;const old=seen[x.token];
    if(typeof old!=='number'){seen[x.token]=v;changed=true;return;}
    if(v>old){added+=v-old;if(!first)first={title:String(x.title||'口播稿'),delta:v-old,total:v};seen[x.token]=v;changed=true;}
    else if(v!==old){seen[x.token]=v;changed=true;}
  });
  if(changed)shareSeenWrite(seen);
  if(added>0)toast(first&&added===first.delta?`你的分享《${first.title}》新增 ${first.delta} 次阅读，共 ${first.total} 次。`:`你的分享共新增 ${added} 次阅读。`);
  const s=state.share;
  if(s){const hit=items.find(x=>x.token===s.token);if(hit){s.views=hit.views;s.revoked=hit.revoked;const el=$('#share-views');if(el)el.textContent=String(hit.views);}}
}
function startShareReflux(){if(shareRefluxTimer)return;shareRefluxTimer=setInterval(refluxShareViews,60000);refluxShareViews();}
function autoWashBanner(p){
  const w=p.auto_wash;
  if(!w||w.outcome==='noop')return '';
  if(w.outcome==='abandoned')return `<div class="panel auto-wash space"><div class="row spread"><strong>这次没敢改</strong><span class="row"><button type="button" class="ghost small-button" data-action="wash-again">再洗一遍</button></span></div><p class="small muted">原因：${esc(w.reason||'改动触发保护条件')}。原稿保持不变，没有留下半成品。</p></div>`;
  const buttons=`<button type="button" class="ghost small-button" data-action="wash-detail" data-id="${esc(w.id)}">看明细</button><button type="button" class="ghost small-button" data-action="wash-restore" data-id="${esc(w.id)}">还原原稿</button><button type="button" class="ghost small-button" data-action="wash-again">再洗一遍</button>`;
  return `<div class="panel auto-wash space"><div class="row spread"><strong>已自动洗 ${Number(w.change_count)||0} 处</strong><span class="row">${buttons}</span></div><p class="small muted">字数 ${Number(w.chars_before)||0} → ${Number(w.chars_after)||0}（${Number(w.delta_pct)>0?'+':''}${Number(w.delta_pct)||0}%），AI 腔命中 ${Number(w.hits_before)||0} → ${Number(w.hits_after)||0}。只删起跑铺垫、空动词和段末金句，观点、数字、口头禅没动。</p></div>`;
}
function todoBanner(p){
  const hits=[];
  (p.draft||[]).forEach(part=>{
    const text=String(part.text||'');const re=/【待补[^】]*】/g;let m;
    while((m=re.exec(text))){hits.push({ctx:text.slice(Math.max(0,m.index-24),m.index+m[0].length+24)});}
  });
  if(!hits.length)return '';
  return `<div class="panel todo-banner space"><strong>有 ${hits.length} 处要你补的真实经历</strong><p class="small muted">稿子里标了【待补】的地方，模型不会替你编。发出去之前换成你自己的事。</p>${hits.map((h,i)=>`<div class="rule small"><span class="muted">${i+1}.</span><p>…${esc(h.ctx).replace(/【待补[^】]*】/g,'<mark class="todo-mark">$&</mark>')}…</p></div>`).join('')}</div>`;
}
function scriptView(p){
  refreshShareViews(p.id);
  const s=p.jobs.find(j=>['rewrite','prepare_reference','prepare_content','resolve','creation_draft','creation_recreate','creation_polish'].includes(j.kind)&&j.status==='succeeded'&&j.result?.suggestion?.base_draft_revision===p.draft_revision)?.result.suggestion;
  // 2026-11-25：三个必填框 + 两个可选段 → 一个整篇编辑框（见 wholeToParts）。旧稿几段的文字打开时并成整篇，一字不丢。
  const missing=draftWholeText(p).trim()?[]:['whole'];
  return `<div class="workspace"><article class="panel"><div class="row spread"><h2>读一遍，把它说成自己的话。</h2>${pill(p.confirmed_version_id?'历史版本已保留':'当前为草稿')}</div>${sovereigntyStrip(s,true)}${p.selected_topic?`<div class="creation-topic-context"><span class="small muted">本次选题 · 待验证</span><strong>${esc(p.selected_topic.title)}</strong></div>`:''}${scriptEditorHead(p)}${shareBar(p)}${autoWashBanner(p)}${todoBanner(p)}<div class="script-writer">${creationWritingPanel(p)}</div>${complianceHtml(p.compliance)}<form id="script-form"><div id="script-full-text"><textarea id="script-whole" name="whole" data-draft rows="16" aria-label="口播逐字稿全文" placeholder="把你要说的话写在这里，一整篇就行。">${esc(draftWholeText(p))}</textarea></div><div id="lint-box"></div><div class="actions"><button type="submit">保存草稿</button><button type="button" data-action="confirm" class="primary" ${missing.length?'disabled':''}>确认稿件，建立发布文案</button></div><p id="script-missing" class="small muted">${scriptMissingText(missing)}</p></form></article><aside><div class="panel"><details><summary>我的表达档案</summary>${[['说话风格',state.boot.profile.style],['讲给谁听',state.boot.profile.audience],['红线',state.boot.profile.redlines]].map(([l,v])=>`<div class="rule"><span class="fact-label">${l}</span>${esc(v||'尚未填写')}</div>`).join('')}<a class="link small space" href="#/settings">去填写或修改 →</a><button data-action="generate" class="space" ${state.boot.capabilities.llm&&p.source.text.trim()?'':'disabled'}>用参考文字帮我改写</button></details>${!state.boot.capabilities.llm?'<p class="small muted space">生成模型待配置。当前可真实保存、修改和确认本人稿件。</p>':''}${s?`<div class="rule space"><button data-action="suggestion" data-id="${s.id}">比较新建议，再决定是否接受</button></div>`:''}</div><div class="panel space"><h3>定稿历史</h3>${p.versions.map(v=>`<div class="rule small">${stamp(v.confirmed_at)}<br><span class="muted">${esc(v.id)}</span></div>`).join('')||'<p class="small muted">确认稿件后产生不可覆盖的版本。</p>'}</div></aside></div>`;
}
function packView(p){const x=p.publication_pack;if(!x||p.draft_changed)return empty('先确认一版拍摄稿','定稿后才整理标题、词条、发布文案与内容导演计划。');return `${renderDirectorBrief(p)}<div class="grid2"><div class="panel"><h2>先用这版标题</h2><p class="small muted">${esc(x.method)}</p><p class="default-title">${esc(x.selected_title)}</p>${x.copy_check&&!x.copy_check.ok?`<div class="rule space"><strong>标题有套路词，建议换</strong>${x.copy_check.fields.map(f=>f.hits.map(h=>`<p class="small">“${esc(h.word)}” → ${esc(h.suggestion)}</p>`).join('')).join('')}</div>`:''}<details><summary>查看其他标题候选</summary>${x.titles.map(t=>`<label class="title-option"><input type="radio" name="title-choice" value="${esc(t)}" ${t===x.selected_title?'checked':''}><span>${esc(t)}</span></label>`).join('')}</details></div><div class="panel"><form id="metadata-form"><label class="field"><span>选中的封面标题（可改）</span><input name="selected_title" id="selected-title" type="text" value="${esc(x.selected_title)}" required></label><label class="field"><span>内容词条 / 话题，逗号分隔</span><input name="tags" type="text" value="${esc(x.tags.join('，'))}"></label><label class="field"><span>发布文案</span><textarea name="caption" rows="8">${esc(x.caption)}</textarea></label><button class="primary" type="submit">确认资料，去拍摄 →</button></form><div class="rule space"><a class="link" href="${purl(p.id,'shoot')}">进入拍摄与上传 →</a></div></div></div>`;}
function uploadView(p){return `<div class="panel"><h2>上传真实拍摄素材</h2>${p.draft_changed||!p.confirmed_version_id?notice('上传前先确认拍摄稿，避免素材关联错版本。'):''}<div class="file-zone dropzone"><span class="dropzone-icon" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 16V4"/><path d="m6 10 6-6 6 6"/><path d="M4 20h16"/></svg></span><span class="dropzone-title">拖拽视频到此处，或点击选择文件</span><input id="video-upload" type="file" accept=".mp4,.mov,.webm,.mkv,.m4v" aria-label="上传拍摄素材" ${p.draft_changed||!p.confirmed_version_id?'disabled':''}><p>单文件 4GB 以内 · 4MB 分片 · 中断后重新选择同一文件可恢复</p><progress id="upload-progress" value="0" max="100"></progress><div id="upload-info" class="upload-info">文件传到本机工作台，不上传外部服务。</div><span class="dropzone-formats" aria-hidden="true"><i>.MP4</i><i>.MOV</i><i>.WEBM</i><i>.MKV</i><i>.M4V</i></span></div>${p.assets.map(a=>`<div class="rule space"><div>${esc(a.filename)}</div><span class="small muted">${fmt(a.info.duration_ms)} · ${a.info.width}×${a.info.height} · ${a.script_version_id===p.confirmed_version_id?'当前稿件':'旧稿件素材'}</span></div>`).join('')}</div>`;}
function shootView(p){const version=p.versions.find(v=>v.id===p.confirmed_version_id);if(!version||p.draft_changed)return empty('拍摄需要固定稿件','回到讲稿，读稿、修改并确认。');return `<div class="grid2"><div class="panel"><h2>大字提词</h2><div class="row prompt-controls"><button data-action="prompt">开始 / 暂停滚动</button><button data-action="prompt-reset">回到开头</button><label>字号<input id="prompt-size" type="range" min="20" max="44" value="28"></label><label>语速<input id="prompt-speed" type="range" min="120" max="480" step="10" value="${promptSpeed}"><span id="prompt-speed-label">${promptSpeed} 字/分</span></label><a class="link" href="#/prompter/${p.id}">手机全屏提词 →</a></div><div id="teleprompt" class="teleprompt"><div id="prompt-text" class="prompt-text">${version.parts.filter(x=>String(x.text||'').trim()).map(x=>`<p>${esc(x.text)}</p>`).join('')}</div></div><p class="small muted space">手机可在同一局域网打开本工作台（需访问码）；也可用系统相机拍好后上传。</p></div>${uploadView(p)}</div>`;}
/* 2026-11-25：开头 / 结尾没归到段里时（整篇只有一两行），退回用整篇的开头 / 结尾，导演三件套不留空。 */
function brief(p){const b=Object.fromEntries(p.draft.map(x=>[x.role,x.text])),whole=draftWholeText(p).replace(/\s+/g,' ').trim(),tail=t=>{t=String(t||'').trim();return t.length>42?t.slice(-42):t;};return {title:p.title.slice(0,45),presenterLabel:state.boot.profile.name||'',language:'zh',presenterFace:{cx:null,cy:null,h:null},groups:[{id:'B1',title:'提出问题'},{id:'B2',title:'理解关键内容'},{id:'B4',title:'收束与行动'}],basic:{keyword:p.title.slice(0,8),explanation:String(b.hook||whole).slice(0,42),heading:'本次关键内容',points:[String(b.content||whole).slice(0,18)],summary:'记住这个判断',action:b.ending?String(b.ending).slice(0,42):tail(whole)}};}
function editView(p){const artifacts=p.artifacts.filter(a=>['clean_video','packaged_video','final_video'].includes(a.kind)&&a.script_version_id===p.confirmed_version_id),latest=artifacts.find(a=>a.id===p.latest_artifact_id)||artifacts[0],frame=p.artifacts.find(a=>a.kind==='template_frame'),transcript=p.jobs.find(j=>j.kind==='transcribe'&&j.status==='succeeded')?.result?.transcript;return `<div class="workspace"><div class="stack"><div class="panel"><div class="row spread"><h2>网页审片 · 后台处理</h2></div>${latest?`<video class="video" controls playsinline preload="metadata" src="${media('/api/artifacts/'+latest.id)}"></video><div class="row spread"><span class="small muted">${latest.info.width}×${latest.info.height} · ${fmt(latest.info.duration_ms)} · ${latest.kind==='clean_video'?'剪掉停顿和口误':latest.kind==='final_video'?'成片':'模板成片'}</span><button data-action="review" data-id="${latest.id}" class="primary">${p.reviewed_artifact_id===latest.id?'已确认这版成片':'确认已审阅这版成片'}</button><a class="button-link" href="${media('/api/artifacts/'+latest.id+'?download=1')}" download="${esc(p.title)}-精剪成片.mp4">下载视频 · 去剪映包装</a></div>`:empty('还没有可播放结果','上传素材后，提交自动静音清理或手动区间剪切。')}${notice('静音清理会实际剪掉音视频区间；有声的嗯啊需要原始转写与时间戳，不能只删字幕。')}</div><div class="panel"><h2>剪掉停顿和口误</h2>${p.assets.length?`<label class="field"><span>源素材</span><select id="active-asset">${p.assets.map(a=>`<option value="${a.id}">${esc(a.filename)} · ${fmt(a.info.duration_ms)}${a.script_version_id!==p.confirmed_version_id?' · 旧稿':''}</option>`).join('')}</select></label><div class="row"><label>节奏<select id="cut-mode"><option value="conservative">保守</option><option value="natural" selected>自然</option><option value="tight">紧凑</option></select></label><button data-action="auto-cut" class="primary">自动缩短长停顿</button><button data-action="transcribe" ${state.boot.capabilities.asr?'':'disabled'}>识别原始语音</button><button data-action="asr-warmup" class="ghost small-button" title="首次转写前先把本地语音模型加载进内存">预热语音模型</button></div><details><summary>按源时间手动保留片段</summary><p class="small muted space">每行“开始秒-结束秒”，如 0-5.2。保留顺序即输出顺序。</p><textarea id="manual-segments" placeholder="0-5.2&#10;7.4-18"></textarea><button data-action="manual-cut" class="space">按以上区间真实剪切</button></details>${transcript?`<div class="rule space"><h3>核对口误和口头禅，再删音视频</h3><p class="small muted">不默认勾选。逐词核对含义和源时间后提交；原素材保留。</p><div class="word-list">${transcript.words.map(w=>`<label class="word-chip"><input type="checkbox" name="remove-word" value="${esc(w.id)}">${esc(w.text)} <small>${(w.start_ms/1000).toFixed(2)}–${(w.end_ms/1000).toFixed(2)}s</small></label>`).join('')}</div><button class="space" data-action="remove-words" data-id="${esc(transcript.id)}">删除勾选词的实际音视频区间</button></div><details><summary>原始转写与时间戳</summary><pre class="raw-markdown space">${esc(JSON.stringify(transcript.words,null,2))}</pre><p class="small muted">核对后，可将保留范围填写到上方区间；不会直接按净文本猜时间。</p></details>`:''}`:notice('请先到“拍摄上传”完成素材上传。')}</div><div class="panel"><h2>内容导演 · 先决定讲清什么</h2><p class="small muted">规则分镜建议，待本人核对；不会增加未讲过的事实。后续接入模型扩散思考与更多视觉组件。</p>${p.director.map(d=>`<div class="director-item"><strong>${esc(roles.find(x=>x[0]===d.role)?.[1]||d.role)} · ${esc({talking_head:'保留真人',quote_emphasis:'值得转发的话强调',steps:'步骤卡',comparison:'对比卡',cause_chain:'因果关系'}[d.visual])}</strong><p>${esc(d.reason)}</p><div class="small">${esc(d.overlay)}</div></div>`).join('')||'<p class="muted small">确认稿件后建立分镜。</p>'}</div>${latest?`<div class="panel"><h2>MotionTemplates 本地适配</h2><p class="small muted">${esc(state.boot.capabilities.motion_format)}。下面是待核对的上屏摘要，不替代完整讲稿。请先按素材实测填写 presenterFace 的 cx/cy/h（0–1）；模板会校验字数、最小时长与总时长上限。</p><textarea id="motion-brief" class="json-editor">${esc(JSON.stringify(brief(p),null,2))}</textarea><div class="actions space"><button data-action="motion-preview" data-id="${latest.id}" ${state.boot.capabilities.motion_installed?'':'disabled'}>调用模板生成预览帧</button><button data-action="motion-render" data-id="${latest.id}" ${state.boot.capabilities.motion_installed?'':'disabled'}>正式渲染（横屏）</button></div>${frame?`<img class="frame-preview space" src="${media('/api/artifacts/'+frame.id)}" alt="本地模板生成的真实预览帧">`:''}</div>`:''}</div><aside>${uploadView(p)}</aside></div>`;}
function publishView(p){const x=p.publication_pack;return `<div class="grid2"><div class="panel"><h2>交付资料准备好，再接发布。</h2>${sovereigntyStrip(sovereigntyLatestSuggestion(p),false)}${!x||p.draft_changed?notice('先确认当前稿件并保存发布资料。'):`<div class="rule"><span class="fact-label">封面标题</span>${esc(x.selected_title)}</div><div class="rule"><span class="fact-label">发布文案</span><p class="raw-markdown">${esc(x.caption)}</p></div><div class="badge-row">${x.tags.map(t=>pill(t)).join('')}</div><a class="link" href="${media('/api/projects/'+p.id+'/export')}">下载发布文案 ZIP →</a>`}${p.reviewed_artifact_id&&!p.draft_changed?`<div class="space"><a class="button-link primary" href="${media('/api/projects/'+p.id+'/export?include_video=1')}">下载成片、封面、字幕与文案 →</a></div>`:''}${notice('真实发布只提交到本人已扫码连接的抖音账号；未勾选本人确认不会发送。')}</div><div class="panel"><h2>抖音发布</h2><div id="publish-panel"><p class="small muted">正在读取账号与执行器状态…</p></div><div class="space"><span class="factory-section-label">多平台发布</span><div id="publish-workbench"><p class="small muted">正在读取平台能力…</p></div></div><div class="rule space"><span class="fact-label">多平台</span>当前聚焦抖音单平台跑通飞轮。<a class="link" href="#/matrix">平台接入路线图（预留窗口）→</a><p class="small muted space">快手 / B站 / 视频号 / 小红书 先留位，抖音跑通后逐个接入、各自验收；未接入的平台只显示真实状态，不做模拟发布。抖音单账号链路保持不变。</p></div></div></div>`;}
function publishAccountPanel(st){if(!state.boot.capabilities.publishing)return notice('发布执行器未安装：需要浏览器运行环境与 patchright。')+(st.message?`<p class="small muted">${esc(st.message)}</p>`:'');const packReady=state.project?.publication_pack&&!state.project?.draft_changed&&state.project?.reviewed_artifact_id;const row=`<div class="cap"><span>抖音 · 本人账号</span>${st.logged_in?pill('已连接','ok'):pill(st.reason==='no_session'?'未登录':'未连接','warn')}</div>`;const login=st.logged_in?'':`<button data-action="publish-login" class="space">用抖音 APP 扫码登录</button><div class="space"><img id="publish-qr" hidden alt="登录二维码"><p id="publish-login-text" class="small muted"></p></div>`;const actions=st.logged_in?`<div class="actions space"><button data-action="publish-plan" ${packReady?'':'disabled'}>发布演练（上传草稿，不发布）</button><button data-action="publish-refresh" class="ghost small-button">刷新状态</button></div><label class="check"><input id="publish-confirm" type="checkbox">我已核对成片与发布资料，同意发布到我的抖音账号</label><button data-action="publish-submit" class="primary space" ${packReady?'':'disabled'}>正式发布</button>${packReady?'':'<p class="small muted space">完成审片并确认成片后，才能发布演练或正式发布。</p>'}`:`<div class="actions"><button data-action="publish-refresh" class="ghost small-button">刷新状态</button></div>`;return row+login+actions+(st.message?`<p class="small muted space">${esc(st.message)}</p>`:'');}
async function refreshPublishPanel(force=false){const panel=$('#publish-panel');if(!panel)return;if(!force&&state.publish)return;let st={logged_in:false,reason:'unavailable',message:'账号状态未取得。'};try{st=await api('/publish/status');}catch(e){st={logged_in:false,reason:'unavailable',message:e.message};}state.publish=st;panel.innerHTML=publishAccountPanel(st);}
async function pollPublishLogin(){if(publishPollTimer)return;publishPollTimer=setInterval(async()=>{try{const pr=await api('/publish/login-progress'),qr=$('#publish-qr'),txt=$('#publish-login-text');if(pr.phase==='waiting_scan'){if(pr.qrcode_data_url&&qr){qr.hidden=false;qr.src=pr.qrcode_data_url;}if(txt)txt.textContent=pr.message||'请用抖音 APP 扫码登录。';}else if(pr.phase==='logged_in'){clearInterval(publishPollTimer);publishPollTimer=null;toast('抖音账号已连接，可以进行发布演练。');await route();}else if(pr.phase==='failed'||pr.phase==='expired'||pr.phase==='timeout'){clearInterval(publishPollTimer);publishPollTimer=null;toast(pr.message||'登录未完成，可重新扫码。');await route();}else if(txt&&pr.message)txt.textContent=pr.message;pollOk('publish');}catch(e){pollFail('publish',e);const t=$('#publish-login-text');if(t)t.textContent='登录状态刷新失败，正在自动重试…';}},2000);}
async function project(id,step){const p=await api('/projects/'+id);state.project=p;const direct=p.mode==='direct';const navSteps=direct?[['edit','剪辑与审片']]:steps;state.step=navSteps.some(x=>x[0]===step)?step:(direct?'edit':'script');$('#breadcrumb').textContent='创作中心 / '+p.title;const views=direct?{edit:renderDirectChain}:{source:sourceView,script:scriptView,pack:packView,shoot:shootView,edit:renderEditChain,publish:publishView};return `<div class="row spread"><div><div class="eyebrow">CONTENT WORKSPACE</div><h1>${esc(p.title)}</h1><div class="badge-row">${direct?pill('直剪模式：以素材版本为准，不写稿','gold'):pill(p.next_action?.step==='source'?'视频资料待取得':stages[p.stage]||p.stage,'gold')}${!direct&&p.draft_changed&&p.confirmed_version_id?pill('新稿未确认，旧成片仍绑定历史版本','warn'):''}</div></div><a class="link small" href="#/create">作品列表 →</a></div><nav class="steps" aria-label="作品流程">${navSteps.map(([k,l],i)=>{const allowed=direct||k==='source'||k==='script'&&(p.source.text.trim()||!p.source.url)||['pack','shoot'].includes(k)&&p.confirmed_version_id&&!p.draft_changed||k==='edit'&&p.assets.length||k==='publish'&&p.reviewed_artifact_id;return allowed?`<a href="${purl(p.id,k)}" class="${state.step===k?'active':''}"><span>${i+1}</span>${l}</a>`:`<span class="step-locked" aria-disabled="true"><span>${i+1}</span>${l}</span>`;}).join('')}</nav>${p.is_test?notice('链路验收项目：定稿与审片记录仅用于功能测试，不代表本人确认或发布授权。'):''}${(views[state.step]||views[navSteps[0][0]])(p)}`;}
async function waitForDraft(id,tries=8){
  // 生成是后台任务：这里只轮询项目本身，不新增接口；用户已开始改稿就不打扰。
  for(let i=0;i<tries;i++){
    await new Promise(r=>setTimeout(r,2000));
    let p;
    try{p=await api('/projects/'+id);}catch(e){return;}
    if(Array.isArray(p.draft)&&p.draft.some(x=>String(x.text||'').trim())){
      if(state.project&&state.project.id===id&&!state.dirty&&location.hash.indexOf('/script')>0){state.project=p;await route();}
      return;
    }
    const bad=(p.jobs||[]).find(j=>j.status==='failed');
    if(bad){toast('生成未完成：'+(bad.error||'请稍后重试'));return;}
  }
  toast('生成还在后台进行，稍后点「刷新」即可看到逐字稿。');
}
function ask(){const r=state.answer;$('#breadcrumb').textContent='重启系统 / 问我的经验库';return `<div class="eyebrow">ASK YOUR KNOWLEDGE</div><h1>先找到依据，再作判断。</h1><p class="lead">${state.boot.capabilities.llm?'可结合引用进行模型分析，仍需本人判断。':'当前为本机资料检索，模型推理待配置；不会生成假决策结论。'}</p><div class="panel"><form id="ask-form"><label class="field"><span>问题、目标、选项和已有条件</span><textarea name="question" required placeholder="写下你现在拿不准的事…"></textarea></label><button class="primary" type="submit">查找相关认知</button></form></div>${r?`<div class="panel space"><h2>${r.analysis?'基于资料的分析':'相关认知检索结果'}</h2>${r.analysis?`<p class="raw-markdown">${esc(r.analysis.answer)}</p>${notice('待补信息：'+(r.analysis.missing_info||[]).join('；'))}`:''}${r.references.map(x=>`<div class="rule"><a class="link" href="#/knowledge/${x.id}">${esc(x.legacy_id)} · ${esc(x.title)}</a><p class="small muted">${esc(x.claim.replace(/\*\*/g,''))}</p><p class="small">边界：${esc(x.boundary.replace(/\*\*/g,'')||'待补')}</p></div>`).join('')||'<p class="muted">没有找到相关资料，请换个关键词。</p>'}</div>`:''}`;}
async function loadCatchphrases(){
  const box=$('#catchphrase-box'), list=$('#catchphrase-list'), status=$('#catchphrase-status'), save=$('#catchphrase-save');
  if(!box||!list)return;
  try{
    const r=await api('/style/catchphrases');
    const items=r.items||[];
    if(!items.length){
      status.textContent='语料不足，先补语音记录：转写本人真实口播后，这里才会出现可勾选的口癖候选（不会造假数据）。';
      list.innerHTML=''; if(save)save.hidden=true; return;
    }
    status.textContent=`从本人真实口播里挖到 ${items.length} 个候选（括号里是出现次数）。勾选你认的口癖，写稿时会带进【本人确认的口癖】。`;
    list.innerHTML=items.map(it=>`<label class="check small space"><input type="checkbox" name="catchphrase" value="${esc(it.phrase)}" ${it.confirmed?'checked':''}> ${esc(it.phrase)} <span class="muted">（${Number(it.count)||0} 次）</span></label>`).join('');
    if(save)save.hidden=false;
  }catch(e){
    status.textContent='口癖候选读取失败：'+(e.message||'网络或服务异常')+'；可稍后刷新重试。';
    list.innerHTML=''; if(save)save.hidden=true;
  }
}
async function saveCatchphrases(){
  const list=$('#catchphrase-list'); if(!list)return;
  const phrases=[...list.querySelectorAll('input[name=catchphrase]:checked')].map(x=>x.value);
  try{
    const r=await api('/style/catchphrases','POST',{phrases});
    toast('口癖已保存：'+(r.confirmed||[]).length+' 个，会带进写稿的本人口吻块。');
    await loadCatchphrases();
  }catch(e){toast(e.message||'保存失败，请重试。');}
}

function styleCardPanel(p){setTimeout(loadCatchphrases,0);
  const sc=p.style_card&&typeof p.style_card==='object'?p.style_card:null;
  const src=(sc&&sc.source)||{},sentence=(sc&&sc.sentence)||{},rhythm=(sc&&sc.rhythm)||{};
  const phrases=(sc&&Array.isArray(sc.catchphrases)?sc.catchphrases:[]).slice(0,4).map(x=>typeof x==='string'?x:(x&&(x.phrase||x.term||x.text||x.value))||'').filter(Boolean);
  const summary=sc?`<div class="rule"><span class="fact-label">样本</span>${esc(src.text_work_count||src.work_count||0)} 条真实作品 · ${esc(sc.method==='llm'?'模型归纳':'统计归纳')}</div><div class="rule"><span class="fact-label">声音样本 · 去AI味</span>真实口播语料，写稿时压成你的口吻、去掉机器腔；这是别人抄不走的部分。</div><div class="rule"><span class="fact-label">句长</span>平均 ${esc(sentence.avg_chars??'—')} 字/句</div><div class="rule"><span class="fact-label">节奏</span>${esc(rhythm.summary||'按作品节奏归纳')}</div><div class="rule"><span class="fact-label">口头禅</span>${esc(phrases.join(' / ')||'未识别')}</div><div class="rule"><span class="fact-label">避免水词</span>${esc((sc.avoid_words||[]).slice(0,6).join(' / ')||'未识别')}</div>`:'<p class="small muted space">还没有风格卡。写稿会沿用上方「说话风格」，不会报错，也不会编造样本。</p>';
  return `<div class="panel space" id="style-card-panel"><div class="row spread"><h2>账号风格卡</h2>${sc?pill('已生成','ok'):pill('未生成','warn')}</div><p class="small muted">从已采集的真实作品归纳句长、称呼、节奏、口头禅与避免水词，写稿时压成一段注入 prompt；引用句只作风格证据，不会整句照抄。</p>${summary}<button data-action="style-card-generate" class="space" ${state.boot.trial?.read_only?'disabled':''}>从已采集作品生成风格卡</button><p class="small muted space">至少 5 条有效作品；样本不足会如实提示，不会用假数据凑数。</p><div class="rule space" id="catchphrase-box"><h3>口癖勾选</h3><p class="small muted" id="catchphrase-status">正在读你的真实口播语料…</p><div id="catchphrase-list"></div><button type="button" class="small-button" data-action="catchphrase-save" id="catchphrase-save" hidden>保存勾选</button></div></div>`;
}
const profileTagSets={
  pain_points:['不知道拍什么','怕露脸','没时间','不会写','拍了没人看'],
  after_view:['关注','私信','下单','转发','只是看看'],
  monetization:['带货','课程','服务','广告','还没想好'],
  update_frequency:['每天','一周 3–4 条','一周 1–2 条','不稳定'],
  video_length:['30 秒内','1 分钟左右','3 分钟以上']
};
const profileHabits=[
  ['pain_points','观众痛点','他们最卡在哪？挑最像的，也可以自己补。'],
  ['after_view','看完做什么','你最想让他们看完做哪一步？'],
  ['monetization','变现方式','现在靠什么挣钱，或者还没想好。'],
  ['update_frequency','更新频率','你实际能做到的节奏，不是理想节奏。'],
  ['video_length','片长','你平时一条多长？']
];
function profileHabitFields(p){
  const rows=profileHabits.map(([k,label,tip])=>{
    const cur=String(p[k]||'');
    const tags=(profileTagSets[k]||[]).map(t=>`<button class="onboard-tag${cur.includes(t)?' is-on':''}" type="button" data-tag-field="${k}" data-tag-value="${esc(t)}">${esc(t)}</button>`).join('');
    return `<label class="field space"><span>${label}</span><small class="field-explain">${tip}</small><input name="${k}" type="text" maxlength="120" value="${esc(cur)}"><div class="profile-tags">${tags}</div></label>`;
  }).join('');
  return `<details class="creation-profile-fields" open><summary>做内容的习惯 <span class="small muted">不填也能用；填了写的稿更像你的账号</span></summary>${rows}<label class="field space"><span>禁忌</span><small class="field-explain">写了就不碰，比如不聊政治、不承诺收益。</small><textarea name="taboos" rows="3" maxlength="2000" placeholder="不聊政治、不承诺收益、不提家里人">${esc(p.taboos||'')}</textarea></label></details>`;
}
function backupSize(n){const v=Number(n)||0;return v>=1073741824?(v/1073741824).toFixed(2)+' GB':v>=1048576?(v/1048576).toFixed(1)+' MB':Math.max(1,Math.round(v/1024))+' KB';}
function backupKind(k){return k==='full'?'全量包':'数据库包';}
function backupKindPill(k){return pill(backupKind(k),k==='full'?'warn':'ok');}
function backupPanel(bk){
  const list=bk&&Array.isArray(bk.exports)?bk.exports:[];
  const last=list.length?list[list.length-1]:null;
  const when=last?stamp(new Date(last.modified_at).getTime()):'';
  ensureBackupCss();
  const rows=list.slice(-10).reverse().map(x=>`<div class="small backup-row">${backupKindPill(x.package_type)} ${esc(stamp(new Date(x.modified_at).getTime()))} · ${esc(backupSize(x.bytes))}<br><span class="small muted backup-name">${esc(x.name)}</span></div>`).join('');
  return `<div class="rule space" id="backup-panel"><h3>数据备份</h3><p class="small muted">数据库包能救回内容、设置和记录，但救不回视频素材；全量包连视频素材一起备份。</p>${last?`<p class="small">最近一次：${backupKindPill(last.package_type)} ${esc(when)} · ${esc(backupSize(last.bytes))} · 共 ${list.length} 份（保留最近 ${bk.keep||10} 份）</p><p class="small muted backup-name">${esc(last.path||'')}</p>`:'<p class="small muted">还没有备份。</p>'}${bk&&bk.last_auto_backup?`<p class="small muted">自动备份：${esc(bk.last_auto_backup)}（每天一份，保留最近 7 份）</p>`:''}<button data-action="backup-now" class="small-button" type="button">立即备份（数据库）</button> <button data-action="backup-full" class="small-button" type="button">全量备份（含素材）</button> <button data-action="backup-restore-open" class="small-button" type="button" ${list.length?'':'disabled'}>从备份恢复…</button>${rows?`<div class="space"><p class="small muted">备份列表</p>${rows}</div>`:''}</div>`;
}
function ensureBackupCss(){
  if(ensureBackupCss.done)return;ensureBackupCss.done=true;
  ipAddCssRules(['.backup-row{margin:6px 0}','.backup-name{word-break:break-all}']);
}
function asrDeviceLabel(dev){
  const d=dev||{};
  if(d.effective==='gpu')return 'GPU 加速中（CUDA），比 CPU 快很多';
  if(d.gpu_ready)return '已装 GPU 加速包，当前模型未加载；加载后自动用 GPU，失败会安静回落 CPU';
  if(d.gpu_pack)return 'CPU（加速包不是 CUDA 版，已安静回落，不报错）';
  return 'CPU 模式运行中（未安装 GPU 加速包）；装有 NVIDIA 显卡的用户可另装加速包提速';
}
function asrSettingsPanel(c,asr){
  const a=asr||{},dev=a.device||{},res=a.resident||{},cache=a.cache||{},fb=a.fallback_policy||{};
  const resident=res.running
    ?`常驻运行中：占用 ${res.memory_mb==null?'未知':res.memory_mb} MB，空闲 ${Math.round((res.idle_unload_sec||600)/60)} 分钟后自动卸载、内存归还系统`
    :'当前未常驻：模型内存已回收，下次转写会自动拉起并预热';
  const cond=`本地不可用、超时（启动 ${Math.round((fb.startup_timeout_seconds||600)/60)} 分钟 / 转写停滞 ${Math.round((fb.stall_timeout_seconds||900)/60)} 分钟）、或连续失败 ${fb.circuit_fails||2} 次（暂停 ${Math.round((fb.circuit_cooldown_seconds||600)/60)} 分钟），都会自动切云端，不再弹窗询问`;
  return `<div class="rule space"><h3>语音识别通道</h3>`
    +`<label class="field"><span>当前策略</span><select id="asr-mode-select">${[['auto','自动：本地优先（不联网），本地不可用时回落云端（需上传音频）'],['local','只用本地（不联网，较慢）'],['cloud','只用云端（秒级，需上传音频）']].map(([v,l])=>`<option value="${v}" ${(c.asr_mode||'auto')===v?'selected':''}>${l}</option>`).join('')}</select></label>`
    +`<p class="small muted">本地：${c.asr_ready_local?'已就绪':'不可用'} · 云端：${c.asr_ready_cloud?'可用':(c.asr_cloud_allowed===false?'账号未开通':'未配置')} · 云端需联网上传音频。</p>`
    +`<div class="rule small"><span class="fact-label">本地设备</span>${asrDeviceLabel(dev)}</div>`
    +`<div class="rule small"><span class="fact-label">常驻内存</span>${resident}</div>`
    +`<div class="rule small"><span class="fact-label">启动预热</span>${(a.prewarm&&a.prewarm.reason)||'按物理内存自动判断：≥16 GB 才在启动时预热，低于则首次转写时再加载'}</div>`
    +`<div class="rule small"><span class="fact-label">转写缓存</span>已缓存 ${cache.entries||0} 条结果；同一文件再转直接复用，不重复识别</div>`
    +`<div class="rule small"><span class="fact-label">自动兜底</span>${fb.circuit_open?'<strong>当前已暂停本地通道，正在走云端</strong>；':''}${cond}。走云端时会上传音频到云端服务器。</div>`
    +`<label class="check space"><input id="asr-diarize" type="checkbox" ${a.diarize?'checked':''}> 说话人分离（默认关闭；口播用不上，关着能省近一半时间，切换会重新加载模型）</label>`
    +`<div class="actions"><button data-action="asr-mode-save" class="small-button" type="button">保存通道</button><button data-action="asr-diarize-save" class="small-button" type="button">保存说话人开关</button><button data-action="asr-warmup" class="small-button" type="button">预热语音模型</button></div>`
    +`<p class="small muted">速度参考：本机 CPU 实测短片段约 15 倍实时，长片段会明显变慢（接近 1 倍），装 GPU 加速包更快；具体取决于机器负载。</p></div>`;
}
async function settings(){const p=state.boot.profile;let c=state.boot.capabilities;
  // 2026-11-16（性能 E-2）：能力探测过期时后端最多等 2 秒重探，设置页不显示旧状态。
  try{const fresh=await api('/capabilities');if(fresh&&fresh.capabilities){c=fresh.capabilities;state.boot.capabilities=c;}}catch{}$('#breadcrumb').textContent='个人档案与服务连接';let bk=null;try{bk=await api('/backup/status');}catch{}let dh=null;try{dh=await api('/data-home');}catch{}let asr=null;try{asr=await api('/asr/status');}catch{}return `<div class="eyebrow">YOUR IP PROFILE</div><h1>先认识你，再学会你的表达。</h1><p class="lead">云端能力统一由官方云端网关提供，本机无需填写任何 API Key；登录账号后按会员权益使用。</p><nav class="row space" aria-label="设置分区"><button type="button" class="small-button" data-action="settings-jump" data-target="settings-account">账号与档案</button><button type="button" class="small-button" data-action="settings-jump" data-target="asset-backup">资产与备份</button><button type="button" class="small-button" data-action="settings-jump" data-target="settings-legal">法律与协议</button><button type="button" class="small-button" data-action="settings-jump" data-target="about-product">关于</button><button type="button" class="small-button" data-action="settings-jump" data-target="settings-pref">偏好与连接</button></nav><div class="grid2"><div class="panel" id="settings-account"><form id="profile-form"><input type="hidden" name="revision" value="${p.revision||0}">${[['name','怎么称呼你'],['account','账号名称'],['audience','主要对谁说话']].map(([k,l])=>`<label class="field"><span>${l}</span><input name="${k}" type="text" ${k==='name'?'maxlength="30"':''} value="${esc(p[k])}"></label>`).join('')}<details class="creation-profile-fields"><summary>行业与业务资料 <span class="small muted">用于选题与痛点探索</span></summary>${[['industry','行业背景','例如：宠物医疗，经营一家宠物医院'],['experience','真实经历与专业积累','例如：从业7年；只填写能核实的经历'],['product','产品或服务','提供什么、适合谁、有什么实际区别']].map(([k,l,ph])=>`<label class="field space"><span>${l}</span><textarea name="${k}" rows="2" maxlength="5000" placeholder="${ph}">${esc(p[k]||'')}</textarea></label>`).join('')}</details><label class="field"><span>说话风格</span><textarea name="style">${esc(p.style)}</textarea></label><label class="field"><span>表达红线</span><textarea name="redlines">${esc(p.redlines)}</textarea></label>${profileHabitFields(p)}<button class="primary" type="submit">保存个人档案</button></form></div><div class="panel" id="settings-pref"><h2>执行能力</h2><div class="caps">${[['本机FFmpeg',c.ffmpeg],['生成模型 '+(c.llm_model||''),c.llm],['本地分段工坊转写',c.asr],['链接解析',c.resolver],['本地MotionGit',c.motion_installed],['平台发布',c.publishing]].map(([n,v])=>`<div class="cap"><span>${n}</span>${pill(v?'已配置 / 已找到':'待连接',v?'ok':'warn')}</div>`).join('')}</div>${c.resolver_note?`<p class="small muted space">链接解析${c.resolver?'已通过实测':'还没通过实测'}：${esc(c.resolver_note)}</p>`:''}<p class="small muted space">找到文件不等于完整能力实测。${esc(c.motion_format)}</p>${asrSettingsPanel(c,asr)}<div class="rule space"><h3>手机访问（同一局域网）</h3><label class="check"><input id="lan-enabled" type="checkbox" ${state.boot.lan_enabled?'checked':''} ${state.boot.lan_mode?'':'disabled'}> 允许手机访问（需要访问码）</label><p class="small muted">默认开启；访问码在启动窗口显示，也保存在 app\\.runtime\\lan-token.txt。关闭后手机立即连不上，电脑本机不受影响。${state.boot.lan_mode?'':'当前以仅本机方式启动，重启 start.bat 后开关才生效。'}</p>${state.boot.lan_url?`<p class="small">手机打开：<a class="link" href="${esc(state.boot.lan_url)}" target="_blank" rel="noopener">${esc(state.boot.lan_url)}</a><br>访问码：<strong>${esc(state.boot.lan_token||'')}</strong>（换码：改 app\\.runtime\\lan-token.txt 后重启）</p>`:''}<button data-action="lan-save" class="small-button" ${state.boot.lan_mode?'':'disabled'}>保存手机访问开关</button></div><div class="rule space"><h3>配乐文件夹</h3><p class="small muted">包装时从这里取曲，人声段自动压低；工作台不内置音乐，也不会上网抓曲。目录为空就不加配乐，只保留原声。</p><label class="field"><span>文件夹路径</span><input id="music-folder" type="text" value="${esc(c.packaging_music_dir||'')}" placeholder="例如：本机配乐文件夹的完整路径"></label><p class="small muted" id="music-status">当前 ${c.packaging_music_tracks||0} 首 · ${esc(c.packaging_music_hint||'')}</p><button data-action="music-pick" class="small-button" type="button">选文件夹…</button> <button data-action="music-save" class="small-button" type="button">保存路径</button></div>${backupPanel(bk)}<section class="panel space" id="data-sovereignty"><h2>知识库 · 数据主权</h2><p class="small">你的思考只属于你：知识库记录只存本机数据目录。调用模型时只把<strong>本次问题/检索片段</strong>作为提示词发出去，工作台不整库上传，也不用于训练；第三方模型服务的数据处理以其条款为准。</p><div class="row space"><a class="button-link primary" href="${media('/api/brain/export')}" download="重启系统-整体导出.zip">一键整体导出（JSON＋Markdown）</a><a class="link small" href="${media('/api/brain/export?format=json')}" download="重启系统-整体导出.json">仅 JSON</a><a class="link small" href="${media('/api/brain/export?format=markdown')}" download="重启系统-整体导出.md">仅 Markdown</a><label class="button-link">导入备份<input id="brain-import-file" type="file" accept=".json,.zip" hidden></label></div><p class="small muted">导入默认按 id/标题合并去重、不覆盖已有卡；可选 replace=1 覆盖同 id（接口参数）。</p><p class="small muted">导出含认知卡、碎片、会话、打卡档案与打卡记录；不包含视频素材和密钥。</p><div class="rule space"><h3>删除说明</h3><p class="small muted">认知卡「删除」先移到已删除，可恢复；「彻底删除」会从本机数据库移除、不能找回，并同步删掉其索引记录。备份包是历史副本，不会被自动清理——要删干净，请同时删除对应备份文件。碎片/会话/打卡目前没有界面批量删除入口，可先整体导出，再按数据目录中的记录/文件处理。</p></div></section><details><summary>开发配置</summary><pre class="code">IP_LLM_URL / IP_LLM_MODEL / IP_LLM_KEY<br>IP_ASR_URL / IP_ASR_KEY<br>IP_RESOLVER_URL / IP_RESOLVER_KEY<br>IP_FFMPEG / IP_FFPROBE<br>IP_MOTION_ADAPTER / IP_MOTION_PYTHON</pre><p class="small muted">Key 只填在包外 .secrets 目录（deepseek.key / volc_asr.key，默认与包同级，可用 IP_SECRETS_DIR 指定）；数据目录 app\\.runtime\\.env.local 仅作兜底。包根 .env.local 是空模板，程序不会写入。start.bat 以 --host 0.0.0.0 启动，局域网内手机访问 /api 需要访问码，可在上方「手机访问」开关里随时关闭。公开部署前仍需 HTTPS 与多用户隔离。接入契约见项目 README。</p></details></div><section class="panel space" id="asset-backup"><h2>资产备份</h2>${dataHomeHtml(dh)}<p class="small">资产带得走：认知、作品、会话和打卡记录可以整体导出成一个压缩包；换机、重装或下一版本都能再导入。建议每周或每新增 10 条认知后备份一次。</p><div class="row space"><a class="button-link primary" id="asset-backup-export" href="${media('/api/brain/export')}" download="重启系统-整体导出.zip">一键导出资产（JSON＋Markdown）</a><span class="small muted" id="asset-backup-note">软件只是容器，资产在你的本机。</span></div><p class="small muted">与升级提示里的「导出备份」是同一个导出端点；导入在下方「数据主权」区。</p></section><section class="panel space" id="settings-legal"><h2>法律与协议</h2><p class="small muted">使用本工作台前请阅读并同意以下文本；会员开通时还会再次确认勾选。</p><div class="row">${legalAnchorHtml('service','《服务协议》')}${legalAnchorHtml('privacy','《隐私政策》')}</div></section><section class="panel space" id="about-product"><h2>关于人生重启系统</h2><p class="small">在 AI 把所有人抹平的时代，让每个普通人把自己的真实经历，炼成不可复制的认知与作品，拥有自己的声音、资产和命运。</p><p class="small muted">软件只是容器，你在里面建起的认知与作品，才是那笔越用越值钱、带得走、抄不走的资产。</p><div class="row space"><button type="button" class="small-button" data-fail-next="diagnostics">导出诊断包（排障用）</button><span class="small muted">含启动器日志、端口判定与核心文件在不在；不含密钥与稿件正文。</span></div></section>${styleCardPanel(p)}</div>`;}
const ROUTE_ALIAS={growth:'restart',factory:'create',home:'restart',brain:'restart'}   /* t175（需求单 D 方案A）：旧 #/brain 直接改成 #/restart，历史收藏不白屏 */;
  // 2026-11-16 旧书签/收藏不再 404：旧 hash 静默改写成新路由（replaceState 不触发二次渲染）。
  /* t182（2026-11-24）：别名改为「参数感知」。
    裸 #/brain（无参数）→ #/restart（旧收藏不白屏）；
    #/brain?category=X / ?q=… 这类深链承载知识清单筛选与搜索，原样保留、正常渲染知识视图。
    2026-11-25：?mode=ask（问答页）已取消，带它的旧链接仍落到知识清单，由知识页忽略该参数。 */
function canonicalHash(){const raw=location.hash||'#/chat';   /* 2026-11-25（方案 A）：打开软件默认落到对话页 */const cut=raw.indexOf('?');const path=cut<0?raw:raw.slice(0,cut);const query=cut<0?'':raw.slice(cut);const segs=path.slice(2).split('/');const alias=segs[0]&&ROUTE_ALIAS[segs[0]]?ROUTE_ALIAS[segs[0]]:'';const keepDeep=!!(query&&segs[0]==='brain');if(alias&&!keepDeep){segs[0]=alias;history.replaceState(null,'','#/'+segs.join('/')+query);}return location.hash||'#/chat';}
let checkinHintTimer = 0;
// 2026-11-18 t138：打卡页只做一处增强——说明每条打卡都在给资产充值。用 MutationObserver 兜住异步渲染。
function decorateCheckin() {
  const insert = () => {
    const main = document.getElementById('main');
    if (!main || !location.hash.startsWith('#/checkin')) return true;
    if (main.querySelector('.ck-asset-hint')) return true;
    const tip = document.createElement('p');
    tip.className = 'small muted ck-asset-hint';
    tip.innerHTML = '每条打卡都在给资产充值：认知、作品、声音样本会累计到 <a class="link" href="#/restart?view=overview">成长总览 · 资产</a>。';
    main.prepend(tip);
    return true;
  };
  clearInterval(checkinHintTimer);
  insert();
  let tries = 0;
  checkinHintTimer = setInterval(() => {
    tries += 1;
    insert();
    if (tries > 12) clearInterval(checkinHintTimer);
  }, 400);
}
function viewSkeleton(){return '<div class="view-skeleton" aria-busy="true"><div class="view-skeleton-dot"></div><p class="small muted">正在打开…</p></div>';}
async function route(){const vpath0=canonicalHash().split('?')[0];if(state.routeBusy&&state.routeBusy.path===vpath0&&!state.navPending)return;state.navPending=false;const gen=++state.routeGen;state.routeBusy={path:vpath0,gen};window.ipGrowth?.unmount();if(window.ipChat)window.ipChat.unmount();clearInterval(promptTimer);promptTimer=null;clearInterval(publishPollTimer);publishPollTimer=null;clearTimeout(saveTimer);
  const vkey=canonicalHash();const vc=viewCache.get(vkey);
  /* 2026-11-26：高亮/面包屑/骨架在点击当下同步响应。原先它们排在 boot() 网络往返之后，
     那 1-2 秒里点击像没反应、正文还挂上一页。换正文仅在非编辑态：saveDraft 还要读旧 DOM 里的草稿输入框。 */
  const vpath=vkey.split('?')[0];const b0=vpath.slice(2).split('/').map(decodeURIComponent);
  const navKey=b0[0]==='chat'?'chat':b0[0]==='restart'?'restart':['brain','knowledge','ask'].includes(b0[0])?'brain':b0[0]==='bench'?'bench':(b0[0]==='feedback'||b0[0]==='matrix')?b0[0]:b0[0]==='settings'?'settings':b0[0]==='checkin'?'checkin':'create';
  document.querySelectorAll('[data-nav]').forEach(el=>el.classList.toggle('active',el.dataset.nav===navKey));
  const crumb={chat:'对话',restart:'个人成长',brain:'知识库',knowledge:'知识库',ask:'知识库',bench:'开始创作 / 找选题',create:'开始创作',feedback:'发布与复盘',matrix:'多平台矩阵',settings:'个人档案与服务连接',checkin:'60 天打卡',start:'开始 · 认识你'}[b0[0]];
  if(crumb&&$('#breadcrumb'))$('#breadcrumb').textContent=crumb;
  const ae0=document.activeElement;const focusEdit=!!(ae0&&(/^(INPUT|TEXTAREA|SELECT)$/.test(ae0.tagName)||ae0.isContentEditable));
  const crossView=(state.renderedHash||'').split('?')[0]!==vpath;
  if(vc&&!state.dirty&&(!focusEdit||crossView)&&$('#main'))$('#main').innerHTML=vc.html;
  else if(!state.dirty&&crossView&&$('#main'))$('#main').innerHTML=viewSkeleton();
  /* 2026-11-16（点检 S2）：切到「发布与复盘」时先立刻换成骨架，
     不再等 boot()/ensurePanelsFor() 把脚本和项目拉完才动 #main——否则面包屑已经变了、正文还挂着上一页。
     已在本页（有 factory-navigation）或流程还没完成时不动，避免轮询/首次引导被骨架打断。 */
  if(location.hash.startsWith('#/feedback')&&state.boot&&state.boot.flow&&(state.boot.flow.completed||state.boot.flow.skipped)&&!document.querySelector('#main .factory-navigation')){
    const f0=state.feedback;f0.view=new URLSearchParams(location.hash.split('?')[1]||'').get('view')==='review'?'review':'hub';
    $('#breadcrumb').textContent='发布与复盘';$('#main').innerHTML=feedbackSkeleton(f0);
  }
  try{if(state.dirty)await saveDraft();await boot();const b=canonicalHash().split('?')[0].slice(2).split('/').map(decodeURIComponent);document.body.classList.toggle('growth-open',b[0]==='restart');document.body.classList.toggle('chat-open',b[0]==='chat');document.querySelectorAll('[data-nav]').forEach(el=>el.classList.toggle('active',el.dataset.nav===(b[0]==='chat'?'chat':b[0]==='restart'?'restart':['brain','knowledge','ask'].includes(b[0])?'brain':b[0]==='bench'?'bench':(b[0]==='feedback'||b[0]==='matrix')?b[0]:b[0]==='settings'?'settings':b[0]==='checkin'?'checkin':'create')));if(!location.hash.startsWith('#/bench'))stopBenchPoll();if(!location.hash.startsWith('#/matrix'))stopMatrixPoll();if(!location.hash.startsWith('#/feedback'))stopFeedbackPoll();if(b[0]!=='restart'&&b[0]!=='start'){const needP=!['chat','brain','knowledge','project','checkin','settings','ask','prompter'].includes(b[0])&&!state.boot.projects;const panels=window.ensurePanelsFor?window.ensurePanelsFor(b[0],b[2]):(window.ensurePanels?window.ensurePanels():null);await Promise.all([needP?ensureBootProjects():null,panels]);}if(b[0]==='start')$('#breadcrumb').textContent='开始 · 认识你';if(b[0]==='chat')$('#breadcrumb').textContent='对话';let html;if(b[0]==='start')html=await flowView();else if(b[0]==='chat')html=await window.ipChat.render(state.boot);else if(b[0]==='restart')html=await window.ipGrowth.render();else if(b[0]==='brain')html=await brain();else if(b[0]==='knowledge')html=await knowledge(b[1]);else if(b[0]==='bench')html=await bench();else if(b[0]==='matrix')html=await matrix();else if(b[0]==='feedback')html=await feedback();else if(b[0]==='prompter')html=await prompterView(b[1]);else if(b[0]==='project')html=await project(b[1],b[2]);else if(b[0]==='settings')html=await settings();else if(b[0]==='ask')html=ask();else if(b[0]==='checkin')html=await window.ipCheckin.render(b[1]||'');else html=await factory();if(gen!==state.routeGen)return;if(b[0]!=='project')state.project=null;
  const ae=document.activeElement;const focusNow=!!(ae&&(/^(INPUT|TEXTAREA|SELECT)$/.test(ae.tagName)||ae.isContentEditable));
  /* 编辑守卫只护同视图刷新：跨视图导航必须落正文。焦点留在旧输入框时若也跳过写入，骨架屏就永不被替换（2026-11-26 卡死回归根因）。 */
  const sameView=(state.renderedHash||'').split('?')[0]===vpath;
  const editing=sameView&&(state.dirty||focusNow);
  const prev=viewCache.get(vkey);
  if(!editing&&(!prev||prev.html!==html))$('#main').innerHTML=html;
  viewCache.set(vkey,{html});if(b[0]==='checkin')decorateCheckin();hideInlineJobs();syncJobsFab();syncSovereignty();if(b[0]==='project')maybeRevealPackaging(state.project);maybeShowUpgradeBanner();showAssetBackupReminder();if(b[0]==='project'){if(state.step==='source')startSourcePoll(state.project);else stopSourcePoll();}else stopSourcePoll();if(b[0]==='project'&&state.step==='publish')refreshPublishPanel(true);if(b[0]==='project'&&state.step==='publish'&&window.ipPublish)window.ipPublish.mount(state.project);if(state.renderedHash!==location.hash){$('#main').scrollTop=0;state.renderedHash=location.hash;}if(typeof flowAfterRender==='function')flowAfterRender();if(b[0]==='restart')window.ipGrowth.mount();if(b[0]==='chat'&&window.ipChat)window.ipChat.mount();refreshDouyinBadge();}catch(e){state.failRetry=route;toast(errView(e).title);if(!state.dirty)$('#main').innerHTML=failCard(e);}finally{if(state.routeBusy&&state.routeBusy.gen===gen)state.routeBusy=null;}}
function promptTick(){const area=$('#teleprompt'),t=$('#prompt-text');if(!area||!t)return;const fs=parseFloat(getComputedStyle(t).fontSize)||28;const cpl=Math.max(8,Math.floor(t.getBoundingClientRect().width/fs));promptCarry+=(promptSpeed/60)*(fs*2.2)/cpl*0.05;const px=Math.floor(promptCarry);promptCarry-=px;if(px>0)area.scrollBy(0,px);}
function togglePrompt(btn){if(promptTimer){clearInterval(promptTimer);promptTimer=null;promptCarry=0;if(btn)btn.textContent='开始匀速滚动';}else{promptTimer=setInterval(promptTick,50);if(btn)btn.textContent='暂停滚动';}}
async function prompterView(id){const p=await api('/projects/'+id);const version=p.versions.find(v=>v.id===p.confirmed_version_id);if(!version||p.draft_changed)return empty('拍摄需要固定稿件','回到讲稿，读稿、修改并确认。');return `<div class="prompter-page"><div class="row spread"><a class="link" href="${purl(p.id,'shoot')}">← 返回拍摄</a><strong>${esc(p.title)}</strong><span class="small muted">点正文暂停/继续</span></div><div id="teleprompt" class="teleprompt prompter-full"><div id="prompt-text" class="prompt-text" data-size="32">${version.parts.filter(x=>String(x.text||'').trim()).map(x=>`<p>${esc(x.text)}</p>`).join('')}</div></div><div class="prompt-controls prompter-bar"><button data-action="prompt" id="prompt-toggle" class="primary">开始匀速滚动</button><button data-action="prompt-reset">回到开头</button><label>语速<input id="prompt-speed" type="range" min="120" max="480" step="10" value="${promptSpeed}"><span id="prompt-speed-label">${promptSpeed} 字/分</span></label><label>字号<input id="prompt-size" type="range" min="20" max="44" value="32"></label></div></div>`;}
async function saveDraft(){
  clearTimeout(saveTimer);
  if(state.saving){await state.saving;return saveDraft();}
  const form=$('#script-form');if(!state.dirty||!form||!state.project)return;
  const id=state.project.id,parts=wholeToParts($('#script-whole').value);
  state.saving=(async()=>{
    const saved=await api(`/projects/${id}/draft`,'PATCH',{parts,draft_revision:state.project.draft_revision},{silent:true});
    state.project={...state.project,...saved};state.dirty=!!$('#script-whole')&&squashText($('#script-whole').value)!==squashText(draftWholeText({draft:parts}));
    const fresh=await api(`/projects/${id}`,'GET',undefined,{silent:true});
    state.project={...state.project,...fresh};
    paintCompliance(fresh.compliance);
    $('#save-status').textContent=state.dirty?'有新修改':'已保存';
  })();
  try{await state.saving;}finally{state.saving=null;}
  if(state.dirty)return saveDraft();
}

async function upload(file,purpose='shooting',project=state.project){
  if(state.uploading)return toast('当前文件仍在上传，请等待。');
  if(file.size>4*1024*1024*1024)return toast('本地单文件上限为 4GB。');
  const p=project,key='ip-upload:'+purpose+':'+p.id+':'+(p.confirmed_version_id||p.material_version||0)+':'+file.name+':'+file.size+':'+file.lastModified;
  state.uploading=true;const input=$(purpose==='reference'?'#reference-upload':purpose==='direct'?'#direct-upload':'#video-upload'),info=$(purpose==='reference'?'#reference-info':purpose==='direct'?'#direct-info':'#upload-info'),progress=$(purpose==='reference'?'#reference-progress':purpose==='direct'?'#direct-progress':'#upload-progress');if(input)input.disabled=true;
  try{
    let session;const old=localStorage.getItem(key);
    if(old){try{session=await api('/uploads/'+old);if(session.status==='ready'){toast('这个文件已上传完成。');await route();return;}}catch{localStorage.removeItem(key);}}
    if(!session){session=await api('/uploads','POST',{project_id:p.id,filename:file.name,size:file.size,purpose});localStorage.setItem(key,session.id);}
    let offset=session.offset;
    while(offset<file.size){
      const response=await fetch(`/api/uploads/${session.id}?offset=${offset}`,{method:'PUT',body:file.slice(offset,offset+4*1024*1024)}),result=await response.json();
      if(!response.ok)throw Error(result.error||'上传中断，请重新选择同一文件恢复。');
      offset=result.offset;progress.value=100*offset/file.size;info.textContent=`已上传 ${Math.round(progress.value)}% · ${(offset/1024/1024).toFixed(1)}MB`;
    }
    info.textContent='文件已收到，正在校验视频…';await api('/uploads/'+session.id+'/complete','POST',{});localStorage.removeItem(key);toast(purpose==='reference'?'视频已保存，后台开始本地转写与初稿整理。':purpose==='direct'?'视频已保存，后台开始转写、净剪与字幕。':'拍摄素材已保存。下一步一键字幕出片。');if(purpose==='reference'&&location.hash!==purl(p.id,'source'))location.hash=purl(p.id,'source');else if(purpose==='shooting'||purpose==='direct')location.hash=purl(p.id,'edit');await route();
  }catch(e){toast(e.message);info.textContent='上传未完成；重新选择同一文件即可尝试恢复。';}finally{state.uploading=false;if(input)input.disabled=false;}
}

async function action(el){
  const a=el.dataset.action,p=state.project;
  if(a==='settings-jump'){
    const t=document.getElementById(el.dataset.target||'');if(t)t.scrollIntoView({block:'start'});return;
  }
  if(a==='data-home-reveal'){
    // 2026-11-25 资料外置：打开自己的资料文件夹。不传路径，服务端用它的资料目录；只在电脑本机可用。
    const hint=el.parentElement&&el.parentElement.querySelector('[data-home-hint]');
    try{
      const r=await api('/data-home/reveal','POST',{});
      if(hint)hint.textContent=(r&&r.ok)?'已打开资料文件夹。':((r&&r.error)||'没能打开文件夹。');
    }catch(e){
      if(hint)hint.textContent=((e&&e.message)||'没能打开文件夹。')+' 可以复制完整路径手动打开。';
    }
    return;
  }
  if(a==='pay-refresh'){
    const errs=await paySyncAfterPaid();
    const st=$('#pay-sync-status');
    if(st){if(errs.length){st.className='small error-text';st.innerHTML='权益显示仍失败：'+esc(errs.join('；'))+'。可稍后再点「重新同步」。';}else{st.className='small muted';st.textContent='会员权益已同步。';}}
    if(!errs.length)toast('会员权益已同步。');return;
  }
  if(a==='pay-start'){
    if(!payAgreed){toast('请先勾选同意《服务协议》与《隐私政策》，再开通会员。');
      const ag=$('#pay-agree');if(ag)ag.focus();return;}
    await startCheckout(el.dataset.plan||'year');return;
  }
  if(a==='legal-open'){openLegal(el.dataset.doc);return;}
  // 2026-11-17（续费流程）：顶部条/试用条的「立即续费 / 继续享有」统一走这里：
  // 没开会员中心就先开，再滚到「续费会员」区块并把焦点放到同意勾选上（不代替用户勾选、不代替下单）。
  if(a==='member-renew'){
    const dlg=$('#modal');
    if(!dlg||!dlg.open)await openBilling();
    // 等一帧再滚：刚 innerHTML 完，位置还没算好（375 下尤其明显）；不用 smooth，落地确定
    await new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)));
    const sec=document.querySelector('#modal-body .member-recharge');
    if(sec&&typeof sec.scrollIntoView==='function')sec.scrollIntoView({block:'start'});
    const ag=$('#pay-agree');
    if(ag&&typeof ag.focus==='function')ag.focus();
    return;
  }
  if(a==='pay-qr-zoom'){el.classList.toggle('is-zoom');return;}
  if(a==='pay-close'){
    const paid=payPaidFlag;payPaidFlag=false;stopPayPoll();
    const dlg=$('#modal');if(dlg&&dlg.open)dlg.close();
    if(paid){try{await openBilling('支付成功，会员权益已开通。');}catch(e){toast(e.message);}}
    return;
  }
  if(a==='payee-clear-image'){
    if(!window.confirm('确定清空当前收款码吗？清空后客户将看不到收款码，只能继续使用兑换码。'))return;
    await api('/billing/payee/image/clear','POST',{});
    await refreshPayeeBlock();toast('收款码已清空。');return;
  }
  if(a.startsWith('release-'))return releaseAction(el);
  if(a.startsWith('creation-'))return creationAction(el);
  if(a.startsWith('drawer-')||a.startsWith('pick-')||a.startsWith('retry-')||a==='works-sort'||a==='bench-type')return benchAction(el);
  if(a.startsWith('packaging-'))return packagingAction(el);
  if(a.startsWith('cover-'))return coverAction(el);
  if(a==='edit-name')return editNameDialog();
  if(a==='create')return createDialog();
  if(a==='create-mode')return createForm(el.dataset.mode==='video'?'video':'script',el.dataset.knowledge||'');
  if(a==='home-reference'){const d=document.getElementById('home-reference');if(d){d.open=true;d.scrollIntoView({behavior:'smooth',block:'center'});const t=d.querySelector('textarea');if(t)setTimeout(()=>t.focus(),320);}return;}
  if(a==='home-direct'){const d=document.getElementById('home-direct');if(d){d.open=true;d.scrollIntoView({behavior:'smooth',block:'center'});const t=d.querySelector('input[type=file]');if(t)setTimeout(()=>t.focus(),320);}return;}
  /* 2026-11-25：「问知识库」对话页已取消。brain-mode（切到问答）、brain-quick（快捷提问）、
     brain-save-knowledge / brain-to-topics（对回答再加工）四个动作一并摘除；知识库表单只剩搜索。 */
  if(a==='brain-view'){localStorage.setItem('ip-brain-density',el.dataset.mode);return route();}
  if(a==='retry-projects'){
    state.bootProjectsError='';
    window.__bootProjectsPromise=null;
    if(state.boot)state.boot.projects=null;
    await ensureBootProjects();
    await route();
    return;
  }
  if(a==='refresh-brain-news'){
    const btn=el;btn.disabled=true;btn.textContent='抓取中…';
    const kwInput=document.querySelector('input[name="news_keywords"]');
    const keywords=kwInput?kwInput.value.split(/[,，]/).map(k=>k.trim()).filter(k=>k):null;
    state.brainNewsKeywords=keywords; // 保存关键词，route()后恢复
    try{
      const r=await api('/brain/news/refresh','POST',{keywords:keywords});
      if(r.ok){
        state.brainNews=r.items||[];
        toast('抓到 '+r.saved+' 条新鲜事（共匹配 '+r.matched+' 条）');
      }else{
        toast(r.error||'刷新失败');
      }
    }catch(e){toast('刷新失败：'+e.message);}
    btn.disabled=false;btn.textContent='↻';
    return route();
  }
  if(a==='knowledge-delete'){
    const bar=document.getElementById('kb-delete-bar');
    if(!await confirmBar(bar||el.parentElement,'删除这张认知卡？会移到「已删除」，可以恢复。',{okText:'删除',cancelText:'不删',danger:true}))return;
    await api('/knowledge/'+encodeURIComponent(el.dataset.id),'DELETE');
    toast('已移到「已删除」，可在「显示已删除」里恢复。');
    location.hash='#/brain?include_deleted=1';
    return;
  }
  if(a==='knowledge-restore'){
    await api('/knowledge/'+encodeURIComponent(el.dataset.id),'PATCH',{lifecycle:'active'});
    toast('已恢复这张认知卡。');
    await route();
    return;
  }
  if(a==='knowledge-purge'){
    if(!await confirmBar(el.closest('.panel')||el.parentElement,'彻底删除后不能找回。确定要彻底删除这张认知卡吗？',{okText:'彻底删除',cancelText:'取消',danger:true,confirmWord:'彻底删除'}))return;
    await api('/knowledge/'+encodeURIComponent(el.dataset.id)+'?hard=1','DELETE');
    toast('已彻底删除。');
    await route();
    return;
  }
  if(a==='from-knowledge')return createDialog(el.dataset.id);
  if(a==='new-knowledge')return modal('记录一条认知',`<form id="new-knowledge-form"><input name="source_id" type="hidden" value="${esc(el.dataset.source||'')}"><label class="field"><span>标题</span><input name="title" required maxlength="160"></label><label class="field"><span>核心主张</span><textarea name="claim" required></textarea></label><label class="field"><span>适用条件与反例</span><textarea name="boundary"></textarea></label><label class="field"><span>补充记录 / 本人经历</span><textarea name="body"></textarea></label><p class="small muted">保存后默认为参考资料、验证待补；可在详情中分别记录你的判断。</p><button class="primary" type="submit">存入我的经验库</button></form>`);
  if(a==='refresh')return route();
  if(a==='close-modal')return $('#modal').close();
  if(a==='reveal-job'){
    // 2026-11-25（G.2.1）：只传作业号，路径由服务端从作业记录里取；失败就退回「复制路径」的办法。
    const hint=el.parentElement&&el.parentElement.querySelector('[data-reveal-hint]');
    try{
      const r=await api('/jobs/'+encodeURIComponent(el.dataset.jobId||'')+'/reveal','POST',{});
      if(hint&&r&&r.ok)hint.textContent='已在资源管理器里选中这个文件。';
    }catch(e){
      if(hint)hint.textContent=((e&&e.message)||'没能打开文件夹。')+' 可以复制完整路径手动打开。';
    }
    return;
  }
  if(a==='copy-text'){
    const text=el.dataset.text||'';
    if(!text)return toast('没有可复制的路径。');
    try{await navigator.clipboard.writeText(text);toast('路径已复制，粘贴到资源管理器地址栏即可打开。');}
    catch(e){toast('复制失败，请手动选中路径复制。');}
    return;
  }
  if(a==='backup-now'){
    // t177：作业化——弹窗轮询真实 done/total，完成后给出落盘路径。
    await runJobWithProgress('数据库备份',()=>api('/backup/export','POST',{kind:'database'}),
      {modalTitle:'正在备份数据库',doneTitle:'数据库备份完成',
       onSucceeded:job=>exportResultHtml(job,'数据库备份完成')});
    await route();
    return;
  }
  if(a==='backup-full'){
    if(!await confirmBar(el.closest('#backup-panel')||el.parentElement,'全量备份含视频素材，体积大、耗时长。确定现在备份？',{okText:'开始备份'}))return;
    await runJobWithProgress('全量备份（含视频素材）',()=>api('/backup/export','POST',{kind:'full'}),
      {modalTitle:'正在打包全量备份',doneTitle:'全量备份完成',
       onSucceeded:job=>exportResultHtml(job,'全量备份完成（含视频素材）')});
    await route();
    return;
  }
  if(a==='backup-restore-open'){
    const bk=await api('/backup/status');
    const list=(bk.exports||[]).slice().reverse();
    if(!list.length)throw Error('还没有可恢复的备份，请先点「立即备份」。');
    const options=list.map(x=>`<option value="${esc(x.name)}">${esc(backupKind(x.package_type))} · ${esc(backupSize(x.bytes))} · ${esc(stamp(new Date(x.modified_at).getTime()))} · ${esc(x.name)}</option>`).join('');
    return modal('从备份恢复',`<div class="notice">恢复将覆盖当前数据。数据库包只还原数据库，不改动视频素材；全量包会连素材一起还原。恢复前会自动把当前数据库再备份一份到 backups 文件夹；失败会自动回滚。</div><label class="field"><span>选择备份文件</span><select id="backup-file-select">${options}</select></label><p class="small muted backup-name">备份来自：${esc(bk.exports_dir||'数据目录/exports')}</p><button class="primary" data-action="backup-restore-do" type="button">开始恢复</button>`);
  }
  if(a==='backup-restore-do'){
    const sel=document.getElementById('backup-file-select');
    const name=sel?sel.value:'';
    if(!name)throw Error('请选择要恢复的备份文件。');
    if(!await confirmBar(document.getElementById('modal-body'),'确定恢复「'+name+'」？当前数据将被覆盖；恢复前会自动备份一份当前数据库。',{okText:'恢复并覆盖',danger:true,confirmWord:'恢复'}))return;
    // t177：恢复也作业化——恢复过程轮询 done/total；失败按服务端 message 说话（含「请先退出工作台再恢复」这类具体原因）。
    try{
      const job=await runJobWithProgress('从备份恢复',()=>api('/backup/restore-in-place','POST',{filename:name,confirm:true}),
        {modalTitle:'正在恢复备份',doneTitle:'恢复完成',failHint:'请先退出工作台后再恢复（数据库被占用时无法覆盖）。',
         onSucceeded:job=>{
           const r=job.result||{};
           const total=(r.tables&&r.tables.total)||0;
           const media=r.package_type==='full'?'视频素材已按全量包还原。':'视频素材未改动（uploads / artifacts / avatars / packages）。';
           return '<div class="notice">已用'+esc(backupKind(r.package_type))+'恢复 '+esc(String(total))+' 条记录、'+esc(String(r.files||0))+' 个文件。</div>'
             +'<p class="small muted">'+esc(media)+'</p>'
             +'<p class="small muted">恢复前已自动备份当前数据库到 backups 文件夹；如结果不对可在那里找回。</p>'
             +'<button class="primary" type="button" data-action="close-modal">知道了</button>';
         }});
      await route();
      return job;
    }catch(e){
      // runJobWithProgress 已在作业 failed 时弹窗说明；
      modal('恢复失败',`<div class="notice">${esc(e.message||'恢复没有完成。')}</div><p class="small muted">如果提示数据库被占用，请先完全退出工作台，再从备份恢复。</p><button class="primary" data-action="close-modal" type="button">知道了</button>`);
    }
    return;
  }
  if(a==='lan-save'){const box=document.getElementById('lan-enabled');if(!box)return;const enabled=!!box.checked;if(!enabled&&!await confirmBar(el.closest('.rule')||el.parentElement,'关闭后手机立即无法访问本工作台，确定吗？',{okText:'关闭访问'}))return;const r=await api('/lan','POST',{enabled});state.boot.lan_enabled=r.enabled;toast(r.message||'已保存。');return;}
  if(a==='music-save'){const box=document.getElementById('music-folder');const r=await api('/packaging/music','POST',{folder:box?box.value.trim():''});const st=document.getElementById('music-status');if(st)st.textContent=`当前 ${r.count||0} 首 · ${r.hint||''}`;toast(r.ok?'配乐文件夹已保存。':(r.error||'保存失败。'));return;}
  if(a==='music-pick'){toast('请在电脑弹出的窗口里选文件夹…');const r=await api('/packaging/music/pick','POST',{});const st=document.getElementById('music-status');if(st)st.textContent=`当前 ${r.count||0} 首 · ${r.hint||''}`;const box=document.getElementById('music-folder');if(box&&r.folder)box.value=r.folder;toast(r.ok?'配乐文件夹已保存。':(r.message||r.error||'没有选择文件夹。'));return;}
  if(a==='matrix-login'){const r=await api('/matrix/accounts/'+encodeURIComponent(el.dataset.id)+'/login','POST',{});if(r.qrcode_data_url){modal('抖音扫码登录','<img alt="登录二维码" src="'+r.qrcode_data_url+'" style="width:220px;height:220px;display:block;margin:0 auto 12px"><p class="small muted">用抖音 APP 扫码；登录成功会自动关窗并刷新账号状态，失败会给出原因。</p>');watchJobBackground(r.job||r,'矩阵登录',()=>{if($('#modal').open)$('#modal').close();route();});}else toast(r.message||'登录流程已发起，请按平台提示完成。');return route();}
  if(a==='matrix-plan'){const r=await api('/matrix/tasks/'+encodeURIComponent(el.dataset.id)+'/plan','POST',{});watchJobBackground(r.job||r,'发布演练');toast('发布演练已提交：只上传草稿并核对发布表单，不会点击发布；完成会提示。');return route();}
  if(a==='matrix-submit'){const id=el.dataset.id;if(!document.getElementById('matrix-confirm-'+id)?.checked)throw Error('请先勾选“本人确认演练无误，同意发布”，再提交真实发布。');const r=await api('/matrix/tasks/'+encodeURIComponent(id)+'/submit','POST',{confirm:true});watchJobBackground(r.job||r,'正式发布');toast('真实发布已提交；以平台侧返回的作品链接为成功凭据，未返回链接不算成功；完成会提示。');return route();}
  if(a==='bench-select'){state.bench.accountId=el.dataset.id;state.bench.worksPage=1;state.bench.sel={};if(location.hash==='#/bench')return route();location.hash='#/bench';return;}
  if(a==='bench-page'){state.bench.worksPage=Math.max(1,Number(el.dataset.page)||1);return route();}
  if(a==='bench-sort'){state.bench.worksSort=el.dataset.sort==='share_rate'?'share_rate':'like';state.bench.worksPage=1;if(location.hash==='#/bench')return route();location.hash='#/bench';return;}
  if(a==='bench-collect'){const r=await api('/bench/accounts/'+encodeURIComponent(el.dataset.id)+'/collect','POST',{});watchJobBackground(r.job||r,'采集');toast('采集任务已提交：作品与热评取得后，状态会自动更新，完成会提示。');startBenchPoll();return route();}
  if(a==='bench-topics'){const work_ids=Object.keys(state.bench.sel);if(!work_ids.length)throw Error('请先勾选至少一条作品作为选题依据。');const r=await api('/bench/topics/from-evidence','POST',{work_ids,max_topics:8});if(r.items||r.topics)state.bench.topics=r.items||r.topics;toast('选题已生成，默认带“待验证”标记。');return route();}
  if(a==='bench-comments'){const w=(state.bench.works||[]).find(x=>x.id===el.dataset.id);const d=await api('/bench/comments?'+new URLSearchParams({work_id:el.dataset.id,page:'1',page_size:'20'}));return modal('热评 · '+(w?(w.title||'标题未取得'):''),benchCommentsHtml(d));}
  if(a==='bench-use-topic'){const r=await api('/bench/topics/'+encodeURIComponent(el.dataset.id)+'/start-project','POST',{});const pid=r.project_id||r.project?.id||r.id;if(!pid)throw Error('后端未返回项目 ID，无法跳转；这条选题仍保留在待拍清单中。');toast('已基于这条选题建立作品，进入开始创作。');location.hash='#/project/'+encodeURIComponent(pid)+'/script';return;}
  /* 2026-11-26 一键出稿：立项→初稿作业→模态真实进度→完成/失败都有明确告知。
     复用 runJobWithProgress（t177 契约），不新造进度轮询。 */
  if(a==='bench-oneclick-draft'){
    const id=el.dataset.id;
    if(!state.boot.capabilities.llm){toast('写作模型未连接：先到「个人档案与服务连接」检查，再一键出稿。');return;}
    const topic=(state.bench.topics||[]).find(x=>x.id===id);
    el.disabled=true;
    try{
      const r=await api('/bench/topics/'+encodeURIComponent(id)+'/start-project','POST',{});
      const pid=r.project_id||r.project?.id||r.id;
      if(!pid)throw Error('后端未返回项目 ID，无法出稿；这条选题仍保留在待拍清单中。');
      const proj=await api('/projects/'+encodeURIComponent(pid));
      const job=await runJobWithProgress('一键出稿'+(topic?' · '+topic.title:''),
        ()=>api('/projects/'+encodeURIComponent(pid)+'/writing-suggestions','POST',{mode:'draft',draft_revision:proj.draft_revision})
          .then(r=>({job_id:r.job_id||r.id})),
        {modalTitle:'一键出稿',doneTitle:'稿子写好了',failTitle:'这次没有出稿',
         failHint:'可进入作品重试「帮我搭一版初稿骨架」；原文与历史版本都保留。',
         onSucceeded:()=>'<div class="notice">初稿骨架已生成。读一遍，把每段改成你自己的话；原文与历史版本都保留。</div><div class="row"><button class="primary" type="button" data-action="oneclick-open-script" data-project="'+esc(pid)+'">去读稿子 →</button><button class="ghost" type="button" data-action="close-modal">留在找选题</button></div>'});
      if(job&&job.status==='succeeded'){
        toast('稿子写好了：'+(topic?topic.title:'这条选题')+' 的初稿骨架已生成。');
        if(typeof creationRefresh==='function')creationRefresh();
      }
    }catch(e){toast(e.message||'一键出稿没有完成。');}
    finally{el.disabled=false;}
    return;
  }
  if(a==='oneclick-open-script'){document.getElementById('modal').close();location.hash='#/project/'+encodeURIComponent(el.dataset.project)+'/script';return;}
  if(a==='use-angle'){return benchUseAngle(el);}
  if(a==='redo-angles'){const w=state.bench.link.work;if(!w||!w.id)throw Error('没有可重拆的作品，请先贴一次链接。');return benchLinkAngles(w.id);}
  if(a==='feedback-collect'){const pid=state.feedback.projectId;if(!pid)throw Error('请先选择复盘项目。');const r=await api('/feedback/collect','POST',{project_id:pid});state.feedback.collect=r.job||r;watchJobBackground(state.feedback.collect,'评论采集');toast((r&&r.message)||'采集任务已提交；排队/运行/失败以真实任务状态为准，完成会提示。');startFeedbackPoll();return route();}
  if(a==='feedback-register'){const pid=state.feedback.projectId;if(!pid)throw Error('请先选择复盘项目。');const input=document.querySelector('input[name="feedback-register-url"]');const url=input?String(input.value||'').trim():'';if(!url)throw Error('请粘贴抖音作品链接。');toast('正在登记并真实采集评论，约需 1 分钟…');const r=await api('/feedback/register-work','POST',{project_id:pid,url});state.feedback.register=r;toast((r&&r.message)||'登记完成，结果以真实采集为准。');return route();}
  if(a==='feedback-insight'){const pid=state.feedback.projectId;if(!pid)throw Error('请先选择复盘项目。');toast('复盘洞察生成中（约半分钟），完成后这里会更新。');const r=await api('/feedback/insight','POST',{project_id:pid});toast((r&&r.message)||'复盘洞察已生成，结果已更新。');return route();}
  if(a==='feedback-comments'){const wid=el.dataset.id;const d=await api('/feedback/comments?'+new URLSearchParams({platform_work_id:wid,page:'1',page_size:'20'}));state.feedback.comments[wid]=d;return modal('回流评论',fbCommentsHtml(wid,state.feedback));}
  if(a==='feedback-comment-page'){const wid=el.dataset.id,page=String(Math.max(1,Number(el.dataset.page)||1));const d=await api('/feedback/comments?'+new URLSearchParams({platform_work_id:wid,page,page_size:'20'}));state.feedback.comments[wid]=d;$('#modal-body').innerHTML=fbCommentsHtml(wid,state.feedback);return;}
  if(a==='feedback-classify'){const wid=el.dataset.id;try{const r=await api('/feedback/classify-comments','POST',{platform_work_id:wid});state.feedback.classify[wid]=r&&typeof r==='object'?r:{message:'分类任务已提交。'};const job=r&&r.job;if(job&&['queued','running','pending'].includes(job.status)){watchJobBackground(job,'评论分类',()=>{if($('#modal').open&&state.feedback.comments[wid])$('#modal-body').innerHTML=fbCommentsHtml(wid,state.feedback);});toast('评论分类任务已提交，完成后自动刷新并提示。');}else toast('评论分类完成，结果已更新。');}catch(e){state.feedback.classify[wid]={error:e.message};toast(e.message);}$('#modal-body').innerHTML=fbCommentsHtml(wid,state.feedback);return;}
  if(a==='feedback-start-topic'){
    const iid=el.dataset.id,f=state.feedback;
    if(!Array.isArray(f.startTopicPending))f.startTopicPending=[];
    if(f.startTopicPending.includes(iid))return;
    f.startTopicPending.push(iid);
    try{
      const r=await api('/feedback/insights/'+encodeURIComponent(iid)+'/start-topic','POST',{});
      const pid=r.project_id||r.project?.id;
      if(!pid)throw Error('后端未返回 project_id，无法跳转；立项结果请稍后核对，不会视为成功。');
      f.startTopicPending=f.startTopicPending.filter(x=>x!==iid);
      toast(fbStartTopicToast(r));
      location.hash=fbProjectUrl(pid);
      return;
    }catch(e){
      f.startTopicPending=f.startTopicPending.filter(x=>x!==iid);
      toast(e.message);
      if(location.hash.startsWith('#/feedback'))await route();
      return;
    }
  }
  if(a==='import-brain')return modal('导入认知 Markdown',`<form id="import-brain-form"><label class="field"><span>选择导出的 Markdown</span><input name="file" type="file" accept=".md,.txt" required></label><p class="small muted">原文与来源保存在本机。相同内容按 SHA-256 去重；历史认同状态不会推定为本人认可。</p><button class="primary" type="submit">导入并保留原始资料</button></form>`);
  if(a==='prompt'){togglePrompt(el);return;}
  if(a==='prompt-reset'){$('#teleprompt').scrollTop=0;return;}
  if(a==='lint-script'){
    const box=$('#lint-box');
    const form=$('#script-form');
    const text=form?String(form.elements.whole?.value||'').trim():'';
    if(!text)return toast('当前还没有逐字稿可检查。');
    if(box)box.innerHTML='<p class="small muted">正在检查…</p>';
    const r=await api(`/projects/${p.id}/lint`,'POST',{text});
    if(!r.enabled){if(box)box.innerHTML='<p class="small muted">检查开关已关闭（rules_zh.json 里 enabled=false）。</p>';return;}
    const hits=r.hits||[];
    if(box)box.innerHTML=hits.length
      ?`<div class="rule"><strong>命中提示 ${hits.length} 条（只提示，不改稿）</strong><p class="small muted">逐条看，标“需你自己判断”的可能是误伤；关掉不看也行，不挡确认。</p></div>`+hits.map(h=>`<div class="rule"><strong>[${esc(h.severity)}] ${esc(h.rule)}</strong><p class="small">第 ${h.sentence_index+1} 句：${esc(h.sentence)}</p><p class="small muted">为什么：${esc(h.why||'')}<br>建议：${esc(h.suggestion||'')}${h.manual_judgement?'（需你自己判断，可能误伤）':''}</p></div>`).join('')
      :'<p class="small muted">这次没命中规则。注意：规则只覆盖少量已确认句式，不等于稿子没问题。</p>';
    return;
  }
  if(a==='copy-script'){
    const form=$('#script-form');
    const text=form?String(form.elements.whole?.value||'').trim():'';
    if(!text)return toast('当前还没有可复制的逐字稿。');
    return await copyWithFeedback(text,'逐字稿全文已复制。');
  }
  if(a==='share-script'){
    const form=$('#script-form');
    const body=form?String(form.elements.whole?.value||''):draftWholeText(p);
    if(!body.trim())return toast('当前还没有可分享的逐字稿。');
    state.shareDraft={ref:p.id,title:String(p.publication_pack?.selected_title||p.title||'').slice(0,80),body};
    return modal('生成公开分享链接',sharePreviewHtml(state.shareDraft.title,body));
  }
  if(a==='share-publish'){
    const d=state.shareDraft;if(!d)return;
    if(!$('#share-confirm')?.checked)return toast('先勾选本人确认，才会公开。');
    const title=String($('#share-title')?.value||d.title).trim()||d.title;
    el.disabled=true;
    try{
      const res=await api('/share/publish','POST',{kind:'script',title,body:d.body,author:state.boot.profile?.name||'',ref:d.ref});
      state.share=res;
      modal('分享链接已生成',shareDoneHtml(res));
      let copied=false;try{await navigator.clipboard.writeText(res.url);copied=true;}catch(e){}
      toast(copied?'链接已复制，去微信粘贴即可。':'链接已生成，点「复制链接」拿走。');
    }catch(e){
      const box=$('#share-error');if(box)box.textContent=e.message||'生成失败，请稍后再试。';
      el.disabled=false;
    }
    return;
  }
  if(a==='share-copy'){
    if(!state.share?.url)return toast('还没有分享链接。');
    return await copyWithFeedback(state.share.url,'分享链接已复制，去微信粘贴即可。');
  }
  if(a==='share-revoke'){
    if(!state.share?.token)return;
    try{await api('/share/revoke','POST',{token:state.share.token});}
    catch(e){return toast(e.message||'撤销失败，请稍后再试。');}
    state.share.revoked=true;
    $('#modal').close();
    toast('已撤销，链接打开会提示不存在。');
    return await route();
  }
  if(a==='share-close')return $('#modal').close();
  if(a==='wash-detail'){
    const r=await api(`/projects/${p.id}/auto-wash?wash=${encodeURIComponent(el.dataset.id||'')}`);
    const rows=(r.changes||[]).map((c,i)=>`<div class="rule"><strong>${i+1}. ${esc(c.rule)}</strong><p class="small">原句：${esc(c.before)}</p><p class="small">改后：${esc(c.after)||'（整句删除）'}</p><p class="small muted">规则 ${esc(c.rule_id)} · 位置：第 ${Number(c.position||0)+1} 字起</p></div>`).join('');
    const before=(r.parts_before||[]).map(x=>String(x.text||'')).join('\n\n');
    const after=(r.parts_after||[]).map(x=>String(x.text||'')).join('\n\n');
    return modal('这次自动洗了 '+((r.changes||[]).length)+' 处',
      `<div class="rule"><strong>结果</strong><p class="small muted">字数 ${Number(r.chars_before)||0} → ${Number(r.chars_after)||0}（${Number(r.delta_pct)>0?'+':''}${Number(r.delta_pct)||0}%）；AI 腔命中 ${Number(r.hits_before)||0} → ${Number(r.hits_after)||0}。</p></div>`+
      (rows||'<p class="small muted">这次没有改动。</p>')+
      `<details class="space"><summary>洗前全文</summary><pre class="raw-markdown">${esc(before)}</pre></details>`+
      `<details class="space"><summary>洗后全文</summary><pre class="raw-markdown">${esc(after)}</pre></details>`+
      '<p class="small muted">只删不写：观点、数字、口头禅没动；不放心就点“还原原稿”。</p>');
  }
  if(a==='wash-restore'){
    if(!window.confirm('还原会把当前草稿换回自动洗之前的原稿（你之后的手改也会被覆盖）。确定还原？'))return;
    await api(`/projects/${p.id}/auto-wash`,'POST',{action:'restore',auto_wash_id:el.dataset.id});
    toast('已还原到自动洗之前的原稿。');
    await route();return;
  }
  if(a==='wash-again'){
    if(state.dirty)await saveDraft();
    el.disabled=true;
    const r=await api(`/projects/${p.id}/auto-wash`,'POST',{action:'rewash'});
    const w=r.auto_wash||{};
    if(w.outcome==='applied')toast(`又洗了 ${w.change_count||0} 处；命中的是同一批安全改法。`);
    else toast(w.outcome==='abandoned'?('这次没敢改：'+(w.reason||'')):(w.reason||'没有可安全修改的地方。'));
    await route();return;
  }
  if(a==='suggestion'){
    const s=p.jobs.find(j=>j.result?.suggestion?.id===el.dataset.id)?.result.suggestion;
    return modal('比较建议与当前稿件',`<div class="grid2">${['当前稿件','模型建议'].map((title,i)=>`<section><h3>${title}</h3>${(i?s.parts:p.draft).map(x=>`<div class="rule"><strong>${esc(x.label)}</strong><p class="raw-markdown">${esc(x.text)}</p></div>`).join('')}</section>`).join('')}</div>${sovereigntyStrip(s,true)}<p class="small muted">核对观点归属、事实和个人经历。接受后会立即建立不可覆盖的定稿版本，进入拍摄。</p><button class="primary" data-action="accept" data-id="${s.id}">采纳并定稿</button>`);
  }
  if(state.dirty)await saveDraft();
  const post=(name,body={})=>api(`/projects/${p.id}/${name}`,'POST',body);
  if(a==='prepare-content'){await post('prepare-content');toast('正在拆解并生成待确认稿件。');}
  else if(a==='prepare-reference'){await post('prepare-content',{asset_id:el.dataset.id});toast('已提交本地转写。');}
  else if(a==='pack-select'){
    try{localStorage.setItem('ip-pack-open:'+p.id,'1');}catch(e){}
    if(!openPackageDetails())throw Error('包装区还没准备好，请稍后重试。');
    return;
  }
  else if(a==='prepare-video'){
    const body={asset_id:$('#chain-asset').value,artifact_id:el.dataset.asset===$('#chain-asset').value?el.dataset.base:undefined};
    const fontEl=$('#caption-font');
    if(fontEl)body.caption_style={font:fontEl.value,enhanced:!!$('#caption-enhanced')?.checked,preset:$('#caption-preset')?.value||''};
    await post('prepare-video',body);toast('已提交后台字幕出片任务。');
  }
  else if(a==='access-code-again'){
    location.reload();
  }
  else if(a==='jobs-toggle'){toggleJobs();}
  else if(a==='jobs-close'){toggleJobs(false);}
  else if(a==='jobs-hist'){
    const h=document.getElementById('jobs-hist');
    if(h){h.hidden=!h.hidden;el.setAttribute('aria-expanded',String(!h.hidden));}
  }
  else if(a==='job-next'){
    // 2026-11-25：失败卡「下一步」按钮——动作只有后端登记表里那几种，全部走现成落点。
    const j=(state.project.jobs||[]).find(x=>x.id===el.dataset.id),act=((jobFail(j||{}).next)||{}).action;
    if(act==='upgrade'){const m=document.createElement('button');m.dataset.action='member-renew';m.hidden=true;document.body.appendChild(m);m.click();m.remove();}
    else if(act==='login'){if(typeof flowOpenLogin==='function')flowOpenLogin();else location.hash='#/settings';}
    else if(act==='douyin_login')showDouyinLogin();
    else if(act==='rechoose'||act==='back')location.hash='#/create';
    else if(act==='diagnostics')exportDiagnostics();
    else location.hash='#/settings';
  }
  else if(a==='job-retry'){
    const j=(state.project.jobs||[]).find(x=>x.id===el.dataset.id);
    if(!j)throw Error('任务记录已刷新，请重试。');
    const m=JOB_RETRY[j.kind];
    if(!m)throw Error((JOB_NEXT_STEP[j.kind]||'这类任务需要回对应面板重新提交。'));
    const req=m(j);
    if(req.name==='apply-semantic-cut'&&!req.body.asset_id)throw Error('清理建议对应的素材已不在，回「精细剪辑」重新分析后再提交。');
    if(req.body.asset_id===undefined&&['transcribe','cut','semantic-cut','semantic-auto-apply','rebuild-render','prepare-video','anchor-align','direct-run'].includes(req.name))
      throw Error('这条任务缺原素材记录，请回对应面板重新提交。');
    await api('/projects/'+state.project.id+'/'+req.name,'POST',req.body);
    toast('已用原素材重新提交，进度会自动更新。');
    pollOk('jobs');await route();
  }
  else if(a==='douyin-login-retry'){
    showDouyinLogin(async()=>{await api('/projects/'+state.project.id+'/resolve','POST',{});toast('已重新提交读取，进度会自动更新。');await route();});
  }
  else if(a==='scroll-source-form'){
    const form=document.getElementById('source-form');if(form)form.scrollIntoView({block:'center'});
  }
  else if(a==='generate'||a==='resolve'||a==='extract-knowledge'){await post(a);toast('后台任务已提交，进度会自动更新。');}
  else if(a==='confirm'){
    if(state.dirty)await saveDraft();
    const missing=missingDraftLabels(state.project.draft);
    if(missing.length)throw Error('稿件还是空的，写几句再确认。');
    try{await post('confirm',{draft_revision:state.project.draft_revision,compliance_waivers:collectComplianceWaivers()});}
    catch(e){if(e.code==='compliance_blocked'&&e.data?.compliance){state.project.compliance=e.data.compliance;paintCompliance(e.data.compliance);const box=$('#compliance-box');if(box)box.scrollIntoView({behavior:'smooth',block:'center'});}throw e;}
    toast('已建立不可覆盖的定稿版本。');location.hash=purl(p.id,'pack');
  }
  else if(a==='catchphrase-save'){await saveCatchphrases();}
  else if(a==='style-card-generate'){
    const label=el.textContent;el.disabled=true;el.textContent='正在读已采集作品…';
    try{const r=await api('/profile/style-card','POST',{use_llm:true});
      if(!r.ok){toast(r.message||'样本不足，暂时生成不了风格卡。');await route();return;}
      state.boot.profile=r.profile||state.boot.profile;
      toast(`风格卡已生成，来自 ${(r.style_card?.source||{}).text_work_count||0} 条真实作品。`);await route();
    }finally{if(el.isConnected){el.disabled=false;el.textContent=label;}}
  }
  else if(a==='accept'){
    try{await post('accept-suggestion',{suggestion_id:el.dataset.id,compliance_waivers:collectComplianceWaivers()});}
    catch(e){if(e.code==='compliance_blocked'&&e.data?.compliance){state.project.compliance=e.data.compliance;paintCompliance(e.data.compliance);const box=$('#compliance-box');if(box)box.scrollIntoView({behavior:'smooth',block:'center'});}throw e;}
    $('#modal').close();toast('建议已采纳并生成定稿版本，可直接按稿拍摄。');location.hash=purl(p.id,'pack');
  }
  else if(a==='auto-cut'||a==='manual-cut'){
    const data={asset_id:$('#active-asset').value,mode:$('#cut-mode').value};
    if(a==='manual-cut')data.segments=$('#manual-segments').value.trim().split(/\n+/).map(line=>{const m=line.trim().match(/^(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)$/);if(!m)throw Error('保留区间每行写“开始秒-结束秒”，例如 0-5.2。');return {start_ms:Math.round(Number(m[1])*1000),end_ms:Math.round(Number(m[2])*1000)};});
    await post('cut',data);toast('真实剪辑任务已提交，源文件会保留。');
  }
  else if(a==='remove-words'){const ids=Array.from(document.querySelectorAll('[name="remove-word"]:checked')).map(x=>x.value);if(!ids.length)throw Error('请先核对并勾选需要删除的词。');await post('remove-words',{transcript_id:el.dataset.id,word_ids:ids});toast('已提交按词时间戳剪切任务。');}
  else if(a==='semantic-propose'){await post('semantic-cut',{asset_id:$('#semantic-asset').value});toast('已提交「剪掉停顿和口误」的分析；完成后在这里核对删除建议。');}
  else if(a==='direct-run'){await post('direct-run',{asset_id:el.dataset.asset,silence_mode:($('#direct-silence')||{value:'conservative'}).value});toast('已提交直剪自动处理：转写 → 净剪 → 字幕。可离开页面。');}
  else if(a==='semantic-auto'){await post('semantic-auto-apply',{asset_id:$('#semantic-asset').value,silence_mode:($('#semantic-auto-silence')||{value:'conservative'}).value});toast('已提交一键净剪：转写 → 自动采纳高置信度 → 静音清理 → 实际出片。');}
  else if(a==='semantic-select-auto'){
    const plan=(p.semantic_plans||[]).find(x=>x.id===el.dataset.id);
    const ids=new Set((plan&&plan.auto_eligible_ids)||[]);let n=0;
    document.querySelectorAll('[name="semantic-deletion"]').forEach(x=>{x.checked=ids.has(x.value);if(x.checked)n+=1;const mark=document.querySelector(`#semantic-preview del[data-del="${x.value}"]`);if(mark)mark.hidden=!x.checked;});
    toast(`已采纳 ${n} 条高置信度；其余 ${Math.max(0,((plan&&plan.deletions)||[]).length-n)} 条请逐条核对后再剪切。`);
  }
  else if(a==='semantic-undo'){const r=await post('apply-semantic-cut',{artifact_id:el.dataset.id,undo:true});toast(r.restored_artifact_id?`已撤销，回到上一版${r.original_seconds?`（${r.original_seconds}s）`:''}。`:'已撤销，回到原片；原始素材仍在素材列表。');}
  else if(a==='semantic-apply'){const ids=Array.from(document.querySelectorAll('[name="semantic-deletion"]:checked')).map(x=>x.value);if(!ids.length)throw Error('请先勾选要剪掉的重复、改口或口头禅。');await post('apply-semantic-cut',{asset_id:$('#semantic-asset').value,plan_id:el.dataset.id,deletion_ids:ids,silence_mode:($('#semantic-silence')||{value:'conservative'}).value});toast('已提交真实音视频剪切任务，源文件保留。');}
  else if(a==='rebuild-estimate'){
    const target=Number($('#rebuild-target').value||196);
    const r=await post('rebuild-estimate',{asset_id:$('#rebuild-asset').value,target_s:target});
    const box=$('#rebuild-estimate');
    if(!r.available){box.textContent=r.message||'还不能出成片。';return;}
    box.textContent=r.reachable
      ?`这条素材净 ${r.net_seconds}s，治理后 ${r.segments} 段 ${r.cuts} 刀，按 ${r.base_speed}x 可到 ${r.estimated_seconds}s，目标 ${r.target_seconds}s 可达。`
      :(r.message||`到不了：净 ${r.net_seconds}s 在 ${(r.params||{}).speed_max||'1.25'}x 上限内也压不到 ${r.target_seconds}s。要么回「剪掉停顿和口误」多删内容，要么把目标放宽；不会拉倍速凑时长。`);
  }
  else if(a==='rebuild-render'){
    const target=Number($('#rebuild-target').value||196);
    await post('rebuild-render',{asset_id:$('#rebuild-asset').value,target_s:target});
    toast('已提交成片重建：渲染 → 静音体检 → 响度归一 → 双闸。可以离开页面。');
  }
  else if(a==='anchor-align'){await post('anchor-align',{asset_id:$('#anchor-asset').value});toast('已提交稿本锚定分析；完成后在这里逐段选 take。');}
  else if(a==='anchor-apply'){
    const sel={};document.querySelectorAll('[name="anchor-take"]:checked').forEach(x=>{const [pid,ti]=x.value.split(':');sel[pid]=Number(ti);});
    const improv=Array.from(document.querySelectorAll('[name="anchor-improv"]:checked')).map(x=>x.value);
    const planEl=$('#anchor-plan');if(!planEl)throw Error('请先运行稿本锚定分析。');
    await post('anchor-apply',{asset_id:$('#anchor-asset').value,plan_id:planEl.dataset.id,selection:sel,keep_improvised:improv,silence_mode:($('#anchor-silence')||{value:'conservative'}).value});
    toast('已提交按所选 take 的真实剪切任务，源文件保留。');}
  else if(a==='director-brief'){await post('director-brief',{item:el.dataset.item||null});toast('导演三件套生成任务已提交。');}
  else if(a==='package-lite'){await post('package-lite',{artifact_id:el.dataset.id});toast('学习包装（轻）任务已提交：转发卡+双语字幕+章节+BGM。');}
  else if(a==='transcribe'){await post('transcribe',{asset_id:$('#active-asset').value});toast('已提交语音识别任务。');}
  else if(a==='asr-mode-save'){const r=await api('/asr/mode','POST',{mode:$('#asr-mode-select').value});toast(`语音识别通道已切换为「${r.mode}」。${r.available?'当前通道可用。':(r.hint||'当前没有可用通道，请检查本地识别环境。')}`);await route();}
  else if(a==='asr-diarize-save'){
    const on=$('#asr-diarize')&&$('#asr-diarize').checked;
    const r=await api('/asr/diarize','POST',{enabled:!!on});
    toast(`说话人分离已${r.diarize?'开启':'关闭'}。${r.restarted?'常驻模型已释放，下次转写会按新设置重新加载。':'下次转写生效。'}`);
    await route();
  }
  else if(a==='report-false-positive'){
    const term=el.dataset.term||'';
    if(!term)throw Error('没有可提交的词。');
    await api('/compliance/feedback','POST',{term,level:el.dataset.level||''});
    toast('已记录「'+term+'」的误报反馈，我们会复核；本次仍按原规则处理。');
  }
  else if(a==='asr-warmup'){
    const st=await api('/asr/warmup','POST',{});
    if(st.status==='running')toast('已开始预热语音模型：首次加载约 1–2 分钟，期间可以继续写稿。');
    const timer=setInterval(async()=>{try{const s=await api('/asr/status');if(s.warmup.status!=='running'){clearInterval(timer);toast(s.warmup.status==='ok'?('语音模型已就绪，用时 '+Math.round(s.warmup.seconds||0)+' 秒，首次转写会更快。'):('预热失败：'+(s.warmup.message||'请检查语音识别环境，可改用云端转写。')));}}catch{clearInterval(timer);}},5000);
  }
  else if(a==='review'){await post('review',{artifact_id:el.dataset.id});toast('已记录这版成片的审阅确认。');if(p.mode==='direct'){await route();return;}location.hash=purl(p.id,'publish');}
  else if(a==='motion-preview'||a==='motion-render'){
    let briefData;try{briefData=JSON.parse($('#motion-brief').value);}catch{throw Error('模板摘要 JSON 格式有误，请检查。');}
    await post('motion',{artifact_id:el.dataset.id,brief:briefData,render:a==='motion-render'});toast('已提交本地模板任务。渲染期间可离开页面。');
  }
  else if(a==='publish-refresh'){await refreshPublishPanel(true);toast('账号状态已刷新。');return;}
  else if(a==='publish-login'){await api('/publish/login','POST',{});toast('登录页已打开，请用抖音 APP 扫码。');pollPublishLogin();return;}
  else if(a==='publish-plan'){await api('/publish/plan','POST',{project_id:p.id,artifact_id:p.reviewed_artifact_id});toast('发布演练已提交：上传草稿并核对发布表单，不会点击发布。');}
  else if(a==='publish-submit'){if(!$('#publish-confirm')?.checked)throw Error('请先勾选“我已核对成片与发布资料”，确认发布到你的抖音账号。');await api('/publish/submit','POST',{project_id:p.id,confirm:true});toast('正式发布已提交，请留意后台任务结果与本人确认要求。');}
  await route();
}

document.addEventListener('click',async event=>{
  if(state.jobsOpen&&!event.target.closest('.jobs-drawer')&&!event.target.closest('[data-action="jobs-toggle"]'))toggleJobs(false);
  const el=event.target.closest('[data-action]');if(!el||el.disabled)return;
  // 2026-11-16（异常态）：请求失败且可重试时，状态条直接给「重试」，不用再找原来的按钮。
  const run=async()=>{el.disabled=true;try{await action(el);}catch(e){ipFail(e,run);}finally{el.disabled=false;}};
  await run();
});
// 2026-11-16（到期态口径）：导出入口保留可见可点，不隐藏；
// 额度为 0（到期）或本月用完时，点一下先弹「开通会员」说明，避免直接落到 JSON 报错页。
document.addEventListener('click',async event=>{
  const link=event.target.closest('a[href*="/export"]');
  if(!link||!link.getAttribute('href').includes('/api/'))return;
  let st;
  try{st=await api('/billing/usage');}catch{return;}
  const item=(st.items||[]).find(x=>x.feature==='export');
  if(!item||!item.blocked)return;
  event.preventDefault();
  modal('导出需要开通会员',
    '<div class="rule"><p>'+esc(item.message||'当前账号暂无导出额度。开通会员后可恢复导出，已有内容仍可查看。')+'</p></div>'+
    '<div class="row"><button type="button" data-open-billing>开通会员</button></div>');
  const btn=document.querySelector('#modal-body [data-open-billing]');
  if(btn)btn.addEventListener('click',()=>{$('#modal').close();openBilling();});
});
document.addEventListener('submit',async event=>{
  const form=event.target;if(!form.id)return;event.preventDefault();const button=form.querySelector('[type="submit"]');if(button)button.disabled=true;
  const values=Object.fromEntries(new FormData(form));
  try{
    if(form.id==='release-register-form'){await api('/releases/register','POST',{...values,confirmed:values.confirmed==='on'});$('#main').innerHTML=await feedback();toast('本人发布记录已保存，平台状态尚未核验。');}
    else if(form.id==='packaging-content-form'){await packagingSaveContent(form,values);}
    else if(form.id==='cover-form'){await coverGenerate(form);}
    else if(form.id==='redeem-form'){await redeemSubmit(form,values);}
    else if(form.id==='payee-info-form'){
      await api('/billing/payee/info','POST',{title:values.title||'',contact:values.contact||'',note:values.note||''});
      await refreshPayeeBlock();toast('收款方式文字已保存到本机。');
    }
    else if(form.id==='payee-image-form'){
      const input=form.querySelector('input[type=file]');const file=input&&input.files&&input.files[0];
      if(!file)throw Error('请先选择一张收款码图片。');
      if(file.size>4*1024*1024)throw Error('图片不能超过 4MB。');
      const dataUrl=await payeeReadFile(file);
      await api('/billing/payee/image','POST',{image:dataUrl});
      await refreshPayeeBlock();toast('收款码已保存到本机，客户刷新页面即可看到。');
    }
    else if(form.id==='creation-topic-form'){await creationSubmit(form);}
    else if(form.id==='creation-write-form'){if(state.dirty)await saveDraft();await api('/projects/'+state.project.id+'/writing-suggestions','POST',{...values,draft_revision:state.project.draft_revision});$('#modal').close();toast('正在生成建议，完成后可以比较。');await route();}
    else if(form.id==='quick-start-form'){
      const share=values.share_text.trim();if(!/https:\/\//.test(share))throw Error('请先粘贴一个抖音或视频号的 HTTPS 分享链接。');
      await startProjectFromShare(share);
    }else if(form.id==='home-quick-form'){
      // 20261104 A 方案主干：一句话主题 → 直接生成完整初稿；只复用既有接口，不新增 job 类型。
      const topic=String(values.topic||'').trim();
      if(!topic)throw Error('先写一句你想讲的内容。');
      if(!state.boot.capabilities.llm)throw Error('写作模型还没连接，先到「个人档案 → 服务连接」检查。');
      const title=topic.replace(/\s+/g,' ').slice(0,60);
      const p=await api('/projects','POST',{title});
      await api('/projects/'+p.id+'/writing-suggestions','POST',{mode:'draft',draft_revision:p.draft_revision,context:topic});
      location.hash=purl(p.id,'script');
      toast('正在生成完整初稿，完成后逐字稿会自动出现。');
      await waitForDraft(p.id);
    }else if(form.id==='create-project'){
      const mode=values.create_mode==='script'?'script':'video';
      const p=await api('/projects','POST',{title:values.title,url:mode==='video'?values.url:'',source_text:mode==='video'?values.source_text:'',knowledge_ids:values.knowledge_id?[values.knowledge_id]:[]});
      $('#modal').close();
      location.hash=purl(p.id,mode==='script'?'script':'source');
      toast(mode==='script'?'作品已建好，直接开始写讲稿。':'作品与参考资料已保存。');
    }else if(form.id==='new-knowledge-form'){
      // 2026-11-16（异常态）：正文不再静默用核心主张顶替。
      // 纯空白拦下；空正文必须本人确认，并且保存后标注「由核心主张自动带入」。
      const rawBody=String(values.body||'');
      if(rawBody&&!rawBody.trim())throw Error('正文只有空白字符，不能保存。请补充正文，或清空正文后改用核心主张填充。');
      if(!rawBody.trim()){
        if(!await confirmBar(form,'正文为空。要用「核心主张」填充正文吗？确定＝用核心主张填充，详情里会标注「由核心主张自动带入」；取消＝返回修改，本次不保存。',{okText:'用核心主张填充',cancelText:'返回修改'}))throw Error('正文为空，已取消保存。请补充正文后再存。');
        values.body=values.claim;values.body_origin='claim';
      }else values.body_origin='manual';
      const x=await api('/knowledge','POST',values);$('#modal').close();location.hash='#/knowledge/'+x.id;toast(values.body_origin==='claim'?'认知已保存，正文由核心主张自动带入。':'认知已保存。');
    }
    else if(form.id==='brain-search'){location.hash='#/brain?'+new URLSearchParams(values);}
    else if(form.id==='brain-chat-form'){
      // 搜认知：只用关键词过滤主列表，不调用 AI；空关键词回到默认列表。
      // 2026-11-25：「问知识库」对话页已取消，这个表单只剩搜索。
      const q=(values.question||'').trim();
      const p=new URLSearchParams(location.hash.split('?')[1]||'');
      if(q)p.set('q',q);else p.delete('q');
      p.delete('mode');
      location.hash='#/brain'+(p.toString()?'?'+p:'');
    }
    else if(form.id==='bench-account-form'){const url=(values.url||'').trim();if(!/^https:\/\//.test(url))throw Error('请粘贴以 https:// 开头的账号主页链接。');await api('/bench/accounts','POST',{url});toast('账号已保存。点“采集”获取作品与热评。');await route();}
    else if(form.id==='bench-link-form'){return await benchLinkSubmit(form);}
    else if(form.id==='matrix-account-form'){const payload={platform:values.platform,display_name:(values.display_name||'').trim()};if(!payload.platform)throw Error('请选择平台。');if(!payload.display_name)throw Error('请填写账号显示名。');if(values.session_ref&&values.session_ref.trim())payload.session_ref=values.session_ref.trim();if(values.capabilities&&values.capabilities.trim())payload.capabilities=values.capabilities.trim().split(/[,，、\s]+/).filter(Boolean);await api('/matrix/accounts','POST',payload);toast('平台账号已保存；接入状态以平台能力返回为准，未接入平台不能登录。');return route();}
    else if(form.id==='matrix-task-form'){
      const accountIds=Array.from(form.querySelectorAll('[name="matrix-account"]:checked')).map(x=>x.value);
      if(!accountIds.length)throw Error('请先勾选至少一个目标平台账号。');
      const p=state.matrix.project;
      if(!p||!p.reviewed_artifact_id)throw Error('请选择一个已审片的项目。');
      if(!p.confirmed_version_id)throw Error('该项目缺少定稿版本，无法创建发布任务。');
      const planOnly=Boolean(values.plan_only);
      const targets=[];
      for(const id of accountIds){
        const title=String(values['title-'+id]||'').trim(),caption=String(values['caption-'+id]||'').trim();
        if(!title)throw Error('每个勾选的账号都需要填写平台专用标题。');
        const target={platform_account_id:id,platform_payload:{title,caption,tags:String(values['tags-'+id]||'').split(/[,，、\n]+/).map(x=>x.trim()).filter(Boolean),cover_note:String(values['cover-'+id]||'').trim()}};
        const sv=String(values['scheduled-'+id]||'').trim();
        if(sv){const d=new Date(sv);if(!Number.isFinite(d.getTime()))throw Error('定时发布时间格式有误。');target.scheduled_for=d.toISOString();}
        targets.push(target);
      }
      const key=await matrixIdemKey(targets.flatMap(t=>[t.platform_account_id,p.reviewed_artifact_id,p.confirmed_version_id,t.scheduled_for||'immediate',planOnly?'plan':'submit']));
      await api('/matrix/tasks','POST',{project_id:p.id,artifact_id:p.reviewed_artifact_id,script_version_id:p.confirmed_version_id,targets,idempotency_key:key,plan_only:planOnly});
      toast(planOnly?'演练任务已创建：只生成发布计划并核对表单，不会点击发布。':'任务已创建：仍需演练通过并由本人勾选确认，才会真实提交。');
      return route();
    }
    else if(form.id==='script-form'){await saveDraft();toast('稿件已保存。');}
    else if(form.id==='source-form'){await api(`/projects/${state.project.id}/source`,'PATCH',{text:values.text,revision:state.project.source.revision});toast('资料已保存。');if(values.text.trim()){if(state.boot.capabilities.llm)await api(`/projects/${state.project.id}/prepare-content`,'POST',{});location.hash=purl(state.project.id,'script');}else await route();}
    else if(form.id==='caption-revision-form'){const artifact=state.project.artifacts.find(x=>x.id===form.dataset.artifact);const edits=artifact.caption_cues.filter(c=>values[c.id]!==c.text).map(c=>({id:c.id,text:values[c.id]}));if(!edits.length)throw Error('字幕没有变化，无需重出。');await api(`/projects/${state.project.id}/revise-captions`,'POST',{artifact_id:artifact.id,edits});toast('字幕校对已保存，后台重出视频。');await route();}
    else if(form.id==='metadata-form'){await api(`/projects/${state.project.id}/metadata`,'PATCH',{...values,tags:values.tags.split(/[,，、\n]+/).map(x=>x.trim()).filter(Boolean)});toast('发布资料已保存，可以拍摄了。');location.hash=purl(state.project.id,'shoot');}
    else if(form.id==='profile-form'){await api('/profile','PATCH',values);toast('个人档案已保存。');await route();}
    else if(form.id==='brand-name-form'){
      const r=await api('/branding','PATCH',{name:values.name||''});
      if(state.boot){state.boot.brand_name=r.name;if(state.boot.profile)state.boot.profile.name=r.name;}
      applyBrand(r.name);$('#modal').close();
      toast(r.name===r.default_name?'已清空，名字回到「'+r.default_name+'」。':'名字已保存，侧栏、标题、封面和导出文件名都已同步。');
    }
    else if(form.id==='knowledge-state'){const x=state.knowledgeItem;await api('/knowledge/'+x.id,'PATCH',{endorsement:values.endorsement,validation:values.validation,lifecycle:values.lifecycle,content_use:values.allow_content?'available':x.content_use,revision:x.revision});toast('已保存你的判断。');await route();}
    else if(form.id==='import-brain-form'){const file=form.elements.file.files[0];if(file.size>1024*1024)throw Error('请导入 1MB 以内的认知 Markdown。');const text=await file.text();/* t177：上传段 XHR 真字节进度（JSON body），处理段轮询作业 done/total。 */modal('正在导入认知', jobWorkingHtml(null,'上传文档 '+file.name));const started=await new Promise((resolve,reject)=>{const xhr=new XMLHttpRequest();xhr.open('POST','/api/knowledge/import');xhr.setRequestHeader('Content-Type','application/json');xhr.upload.onprogress=e=>{if(!e.lengthComputable)return;const stage=document.querySelector('[data-job-stage]');const nums=document.querySelector('[data-job-nums]');const fill=document.querySelector('[data-job-fill]');const pct=Math.round(e.loaded/e.total*100);if(stage)stage.textContent='上传文档（'+backupSize(file.size)+'）';if(nums)nums.textContent='上传 '+pct+'%';if(fill)fill.style.width=pct+'%';};xhr.onload=()=>{let data={};try{data=JSON.parse(xhr.responseText||'{}');}catch(err){}if(xhr.status<200||xhr.status>=300||data.ok===false){reject(Error((data&&(data.error||data.message))||('导入失败（HTTP '+xhr.status+'）')));return;}resolve(data);};xhr.onerror=()=>reject(Error('上传失败：网络中断或服务没响应。'));xhr.send(JSON.stringify({filename:file.name,text:text}));});const job=await pollJob(started.job_id,jobModalUpdate);if(job.status==='failed'){modal('导入失败','<div class="notice">'+esc(job.message||'服务端没有给出原因。')+'</div><p class="small muted">支持「### 三观-01 · 标题」这类格式的 Markdown。</p><button class="primary" type="button" data-action="close-modal">知道了</button>');return;}const r=job.result||{};const n=(r.imported!=null?r.imported:(r.count||0));modal('导入完成','<div class="notice">已导入 '+esc(String(n))+' 条认知。</div><button class="primary" type="button" data-action="close-modal">知道了</button>');await route();}
    else if(form.id==='ask-form'){state.answer=await api('/brain/ask','POST',values);await route();}
  }catch(e){ipFail(e,()=>{if(typeof form.requestSubmit==='function')form.requestSubmit();else form.dispatchEvent(new Event('submit',{cancelable:true,bubbles:true}));});}finally{if(button)button.disabled=false;}
});
document.addEventListener('input',event=>{
  if(event.target.matches('[data-draft]')){state.dirty=true;$('#save-status').textContent='正在编辑…';updateScriptGate($('#script-form'));clearTimeout(saveTimer);saveTimer=setTimeout(()=>saveDraft().catch(e=>{toast(e.message);$('#save-status').textContent='保存失败，修改仍在页面中';}),900);}
  if(event.target.id==='prompt-size'){const t=$('#prompt-text');if(t){t.dataset.size=event.target.value;t.style.fontSize=event.target.value+'px';}}
  if(event.target.id==='prompt-speed'){promptSpeed=+event.target.value;localStorage.setItem('ip-prompt-speed',String(promptSpeed));const l=$('#prompt-speed-label');if(l)l.textContent=promptSpeed+' 字/分';}
});
document.addEventListener('change',event=>{
  if(event.target.id==='pay-agree'){
    payAgreed=!!event.target.checked;
    const btn=document.querySelector('[data-action="pay-start"]');
    if(btn){btn.classList.toggle('is-locked',!payAgreed);btn.setAttribute('aria-disabled',payAgreed?'false':'true');}
    const hint=$('#pay-agree-hint');if(hint)hint.hidden=payAgreed;
  }
  if(event.target.id==='video-upload'&&event.target.files[0])upload(event.target.files[0]);
  if(event.target.id==='reference-upload'&&event.target.files[0]){const file=event.target.files[0];(async()=>{try{let p=state.project;if(!p){const key='ip-reference-project:'+file.name+':'+file.size+':'+file.lastModified,old=localStorage.getItem(key);if(old){try{p=await api('/projects/'+old);}catch{}}if(!p){p=await api('/projects','POST',{title:file.name.replace(/\.[^.]+$/,'')});localStorage.setItem(key,p.id);}}await upload(file,'reference',p);}catch(e){toast(e.message);}})();}
  if(event.target.id==='direct-upload'&&event.target.files[0]){const file=event.target.files[0];(async()=>{try{let p=null;const key='ip-direct-project:'+file.name+':'+file.size+':'+file.lastModified,old=localStorage.getItem(key);if(old){try{p=await api('/projects/'+old);if(p&&p.mode!=='direct')p=null;}catch{p=null;}}if(!p){p=await api('/projects','POST',{title:file.name.replace(/\.[^.]+$/,''),mode:'direct'});localStorage.setItem(key,p.id);}await upload(file,'direct',p);}catch(e){toast(e.message);}})();}
  if(event.target.id==='caption-preset'){const pr=(state.boot.capabilities.caption_presets||[]).find(x=>x.key===event.target.value);const f=$('#caption-font'),en=$('#caption-enhanced');if(pr){if(f)f.value=pr.font;if(en)en.checked=!!pr.enhanced;}}
  if(event.target.name==='title-choice')$('#selected-title').value=event.target.value;
  if(event.target.name==='matrix-project'){state.matrix.projectId=event.target.value;state.matrix.project=null;state.matrix.tasks=[];return route();}
  if(event.target.name==='feedback-project'){state.feedback.projectId=event.target.value;state.feedback.collect=null;return route();}
  if(event.target.name==='matrix-confirm'){const btn=document.getElementById('matrix-submit-'+event.target.value);if(btn)btn.disabled=!event.target.checked;}
  if(event.target.name==='bench-work'){const sel=state.bench.sel;if(event.target.checked)sel[event.target.value]=1;else delete sel[event.target.value];const n=Object.keys(sel).length,gen=$('#bench-gen');if(gen){gen.disabled=!n;gen.textContent=`生成选题（${n}）`;}}
  if(event.target.name==='semantic-deletion'){const mark=document.querySelector(`#semantic-preview del[data-del="${event.target.value}"]`);if(mark)mark.hidden=!event.target.checked;}
});
$('#close-modal').addEventListener('click',()=>$('#modal').close());
// 收款码图片加载失败降级：手机局域网访问码失效/图片被清空时会拿到 404，
// 不能只留一张破图。error 事件不冒泡，必须用 capture 阶段监听。
// 不用内联 onerror 属性——全站 CSP 是 script-src 'self'，内联处理器会被拦。
document.addEventListener('error',function(ev){
  const img=ev.target;
  if(!img||img.tagName!=='IMG'||!img.classList||!img.classList.contains('payee-qr'))return;
  const wrap=img.closest('.payee-qr-wrap');
  img.hidden=true;
  if(wrap&&!wrap.querySelector('.payee-qr-fallback')){
    const tip=document.createElement('p');
    tip.className='small muted payee-qr-fallback';
    tip.textContent='收款码没能显示：请刷新页面重试；仍不显示请联系管理员获取兑换码，不要向任何私人账户转账。';
    wrap.appendChild(tip);
  }
},true);
$('#modal').addEventListener('close',clearDouyinWatch);
$('#theme-toggle').addEventListener('click',()=>{document.documentElement.classList.toggle('light');localStorage.setItem('ip-theme',document.documentElement.classList.contains('light')?'light':'dark');});
/* t65：头像下拉（系统设置/登录设备/退出/主题）；保留 #auth-devices/#auth-logout id 与原逻辑，点外/Esc 收起。 */
function avatarMenuSet(open){
  const btn=document.getElementById('avatar-menu-btn'),menu=document.getElementById('avatar-menu');
  if(!btn||!menu)return;
  menu.hidden=!open;btn.setAttribute('aria-expanded',String(!!open));
  if(open){const first=menu.querySelector('[role="menuitem"]:not([hidden])');if(first&&first.focus)first.focus();}
}
function syncThemeLabel(){
  const t=document.getElementById('theme-toggle');if(!t)return;
  const light=document.documentElement.classList.contains('light');
  t.textContent='主题切换：'+(light?'深色':'浅色');
}
syncThemeLabel();
document.getElementById('theme-toggle').addEventListener('click',syncThemeLabel);
document.addEventListener('click',e=>{
  const btn=document.getElementById('avatar-menu-btn'),menu=document.getElementById('avatar-menu');
  if(!btn||!menu)return;
  if(e.target.closest('#avatar-menu-btn')){avatarMenuSet(menu.hidden);return;}
  if(menu.hidden)return;
  if(e.target.closest('#avatar-menu')){avatarMenuSet(false);return;}
  avatarMenuSet(false);
});
document.addEventListener('keydown',e=>{
  if(e.key!=='Escape')return;
  const menu=document.getElementById('avatar-menu');if(menu&&!menu.hidden){avatarMenuSet(false);document.getElementById('avatar-menu-btn')?.focus();}
});

if(localStorage.getItem('ip-theme')==='light')document.documentElement.classList.add('light');
/* 2026-11-25 侧栏改两级：「创作中心」默认收起（不再三层嵌套）；手动展开/收起后记住；
   停在创作页时自动展开，让客户看得到自己在哪，不用自己找 */
(function(){const group=document.getElementById('sidebar-create-group');if(!group)return;const KEY='ip-sidebar-create-open';group.open=localStorage.getItem(KEY)==='1';
  const sync=()=>{if(/^#\/(create|bench|feedback|matrix|checkin)(\?|\/|$)/.test(location.hash))group.open=true;};
  sync();window.addEventListener('hashchange',sync);
  group.addEventListener('toggle',()=>{try{localStorage.setItem(KEY,group.open?'1':'0');}catch(e){}});})();
(function(){const nav=document.querySelector('.sidebar nav');if(!nav)return;const add=(key,icon,label)=>{if(document.querySelector('[data-nav="'+key+'"]'))return;const a=document.createElement('a');a.href='#/'+key;a.dataset.nav=key;a.innerHTML='<span class="nav-icon">'+icon+'</span>'+label;nav.appendChild(a);};add('feedback','◍','发布与复盘');})();
document.addEventListener('click',e=>{if(location.hash.startsWith('#/prompter')&&e.target.closest('#teleprompt'))togglePrompt($('#prompt-toggle'));});
/* 2026-11-16 第二大脑页窄宽断点：对话窗口停靠时视口不变、变的是 #main 的内容宽度，
   所以按内容宽度（不是视口）判断，<=520px 切窄版；窗口收起后宽度回来会自动摘掉。 */
(function(){
  const main=document.getElementById('main');
  if(!main||typeof ResizeObserver!=='function')return;
  const apply=w=>main.classList.toggle('bc-narrow',w<=520);
  apply(main.clientWidth-parseFloat(getComputedStyle(main).paddingLeft)-parseFloat(getComputedStyle(main).paddingRight));
  new ResizeObserver(entries=>{for(const e of entries)apply(e.contentRect.width);}).observe(main);
})();
window.addEventListener('hashchange',()=>{state.navPending=true;route();});
window.addEventListener('beforeunload',e=>{if(state.dirty||state.uploading){e.preventDefault();e.returnValue='';}});
// t141：转写/本地转写长时间无进度（同 stage ≥2 分钟）→ 可见提示，不假死。
const JOB_STALL_MS=120000;
function markJobStalls(results){
  const now=Date.now();state.jobWatch=state.jobWatch||{};state.jobStalls=state.jobStalls||{};
  const kinds=new Set(['transcribe','prepare_reference']);let changed=false;const seen=new Set();
  (results||[]).forEach(r=>{
    if(!r||!kinds.has(r.kind))return;seen.add(r.id);
    if(!['queued','running'].includes(r.status)){delete state.jobWatch[r.id];delete state.jobStalls[r.id];return;}
    const w=state.jobWatch[r.id];
    if(!w||w.stage!==r.stage){state.jobWatch[r.id]={stage:r.stage,at:now};if(state.jobStalls[r.id]){delete state.jobStalls[r.id];changed=true;}}
    else if(now-w.at>=JOB_STALL_MS&&!state.jobStalls[r.id]){state.jobStalls[r.id]=true;changed=true;}
  });
  Object.keys(state.jobWatch).forEach(id=>{if(!seen.has(id)){delete state.jobWatch[id];delete state.jobStalls[id];}});
  return changed;
}
const _jobDoneSeen=new Set();
setInterval(async()=>{
  if(polling||document.hidden||state.dirty||state.uploading||$('#modal').open||!state.project)return;
  const jobs=state.project.jobs.filter(j=>['queued','running'].includes(j.status));if(!jobs.length)return;
  polling=true;try{const results=await Promise.all(jobs.map(j=>api('/jobs/'+j.id)));const stalledChanged=markJobStalls(results);
    // 2026-11-26 同类排查：作业跑完必须有一句人话收尾（每作业只报一次），
    // 让「点了只说已提交」的入口也有完成告知；失败仍弹抽屉给原因和重试。
    const justDone=results.filter((r,i)=>r.status==='succeeded'&&jobs[i].status!=='succeeded'&&!_jobDoneSeen.has(r.id));
    if(justDone.length){justDone.forEach(r=>_jobDoneSeen.add(r.id));toast(justDone.map(r=>(JOB_KIND_LABELS[r.kind]||r.kind)+' 已完成').join('；')+'。结果已在页面更新，也可在后台任务里回看。');}
    // 2026-11-25：有任务刚失败时必须让客户看见原因和重试。光标在输入框里就不整页重绘，改为直接弹出任务抽屉（抽屉在 #main 之外，不打断编辑）。
    const justFailed=results.some((r,i)=>r.status==='failed'&&jobs[i].status!=='failed');
    if(justFailed){state.project={...state.project,jobs:state.project.jobs.map(j=>results.find(r=>r.id===j.id)||j)};state.jobsOpen=true;}
    const changed=stalledChanged||justFailed||results.some((r,i)=>r.status!==jobs[i].status||r.stage!==jobs[i].stage);
    if(changed){const active=document.activeElement;if(active?.matches('input,textarea,select')){if(justFailed)syncJobsFab();return;}await route();}
    pollOk('jobs');}catch(e){pollFail('jobs',e);}finally{polling=false;}
},3000);
document.addEventListener('click',event=>{
  const tag=event.target.closest('[data-tag-field]');
  if(!tag)return;
  event.preventDefault();
  const input=document.querySelector('#profile-form [name="'+tag.dataset.tagField+'"]');
  if(!input)return;
  const value=tag.dataset.tagValue||'';
  const parts=String(input.value||'').split(/[、,，]/).map(s=>s.trim()).filter(Boolean);
  const hit=parts.indexOf(value);
  if(hit>=0)parts.splice(hit,1);else parts.push(value);
  input.value=parts.join('、');
  tag.classList.toggle('is-on',hit<0);
});
route();

/* 手机「记一条」（2026-11-08 建；2026-11-25 合并）：不再分「一句话 / 粘链接 / 视频音频」三个 tab。
   浮层里直接放首页那个单对话口（intake-ui.js）：文字、链接、图片、音视频，一次点击自动识别，
   只给逐字稿；进度、取消、超时、失败原因都由 intake-ui 负责。
   同一时刻页面上只允许有一个输入口（intake-ui 按固定 id 读写）：当前页已经有（首页）就滚过去，不叠第二个。 */
(function(){
  const root=document.getElementById('qc');
  if(!root)return;
  const host=document.getElementById('qc-intake-host');
  const statusEl=document.getElementById('qc-status');
  let mountedHere=false;
  function say(msg,kind){
    if(!statusEl)return;
    statusEl.className='qc-status'+(kind?' '+kind:'');
    statusEl.textContent=msg||'';
  }
  function open(){
    if(document.getElementById('intake-panel')){
      const box=document.getElementById('intake-text');
      if(box){box.scrollIntoView({behavior:'smooth',block:'center'});setTimeout(function(){box.focus();},320);}
      toast('输入口就在这一页，直接写在这里。');
      return;
    }
    if(typeof window.intakePanel!=='function'){say('输入口还没准备好，等页面加载完再点一次。','error');return;}
    say('');
    host.innerHTML=window.intakePanel();
    mountedHere=true;
    root.classList.add('open');
    root.setAttribute('aria-hidden','false');
    document.body.classList.add('qc-lock');
    setTimeout(function(){const t=document.getElementById('intake-text');if(t)t.focus();},160);
  }
  function close(){
    root.classList.remove('open');
    root.setAttribute('aria-hidden','true');
    document.body.classList.remove('qc-lock');
    // 卸掉浮层里的输入口，保证同一时刻只有一个。处理进度存在 intake-ui 自己那里，下次打开会接着显示。
    if(mountedHere){host.innerHTML='';mountedHere=false;}
  }
  document.addEventListener('click',function(ev){
    const t=ev.target.closest?ev.target.closest('[data-qc-open],[data-qc-close]'):null;
    if(!t)return;
    if(t.hasAttribute('data-qc-open')){ev.preventDefault();open();return;}
    if(t.hasAttribute('data-qc-close')){ev.preventDefault();close();return;}
  });
  document.addEventListener('keydown',function(ev){if(ev.key==='Escape'&&root.classList.contains('open'))close();});
})();

// t111：点徽标——已登录则刷新确认；未登录/过期直接走现有扫码弹窗，登录后刷新徽标。
document.addEventListener('click',event=>{
  const el=event.target.closest('#douyin-login-badge');
  if(!el)return;
  event.preventDefault();
  if(el.dataset.configured==='1'){refreshDouyinBadge();return;}
  if(typeof showDouyinLogin==='function'){
    showDouyinLogin(()=>refreshDouyinBadge());
    setTimeout(()=>{const open=document.getElementById('douyin-login-open');if(open)open.click();},80);
  }
});
setTimeout(()=>refreshDouyinBadge(),0);

// t128：两个文件入口（设置页 / 升级 banner）共用同一导入函数。
document.addEventListener('change',async event=>{
  const input=event.target.closest('#brain-import-file,#upgrade-import-file');
  if(!input||!input.files||!input.files[0])return;
  const file=input.files[0];
  try{await importBrainFile(file);}
  catch(e){toast(e.message||'导入失败。');}
  finally{input.value='';}
});
