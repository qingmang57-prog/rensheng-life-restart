'use strict';
/* t198（2026-11-25）：单对话输入界面。
   一个输入口接住四类东西：纯文字、分享文字/链接、图片、音视频；一次点击自动识别。
   原则：只呈现用户要的逐字稿；阶段/数量来自真实任务；刷新可续；取消/超时/重复如实说。
   技术拆分（分类、上传、ASR/OCR、wash）留在内部，不把中间结构倒给用户。 */
(function () {
  const KEY = 'ip-intake-v1';
  const MAX_RUN_MS = 10 * 60 * 1000;
  const IMAGE_RE = /\.(jpe?g|png|webp|bmp|gif|tiff?|heic)$/i;
  const AUDIO_RE = /\.(mp3|m4a|wav|amr|aac|flac|ogg|wma|opus)$/i;
  const VIDEO_RE = /\.(mp4|mov|mkv|webm|m4v|avi|flv|wmv|mpg|mpeg|ts)$/i;
  const URL_RE = /https?:\/\/[^\s<>"'“”‘’()（）【】\[\]]+/i;
  const TRAIL_RE = /[。，,；;！!、：:）)】」』”’"'\]>]+$/;

  let S = load();
  let pendingFile = null;
  let pollSeq = 0;
  let uploadAbort = null;
  let elapsedTimer = null;
  let cssDone = false;
  let saveSeq = 0;
  let historyItems = [];
  let historyError = '';
  let historyOffset = 0;
  let historyHasMore = false;
  let historyTotal = 0;

  function esc2(v) {
    return String(v == null ? '' : v).replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
  }
  function notify(msg) {
    try { if (typeof window.toast === 'function') window.toast(msg); } catch (e) {}
  }
  function now() { return Date.now(); }
  function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }
  function sourceLabel(src) {
    return ({text: '直接输入', link: '链接正文', file: '文件转写', asr: '语音转写',
      ocr: '画面文字识别', image_ocr: '图片文字识别', mixed: '语音＋画面文字'})[src] || '识别结果';
  }
  function stamp() {
    const d = new Date();
    const p = n => String(n).padStart(2, '0');
    return d.getFullYear() + p(d.getMonth() + 1) + p(d.getDate()) + '-' + p(d.getHours()) + p(d.getMinutes());
  }
  function elapsedText() {
    const sec = Math.max(0, Math.floor((now() - (S.startedAt || now())) / 1000));
    return sec < 60 ? sec + ' 秒' : Math.floor(sec / 60) + ' 分 ' + (sec % 60) + ' 秒';
  }
  function pickUrl(text) {
    const raw = String(text || '');
    const m = raw.match(URL_RE);
    return m ? m[0].replace(TRAIL_RE, '') : '';
  }
  function load() {
    let x = {};
    try { x = JSON.parse(localStorage.getItem(KEY) || '{}') || {}; } catch (e) { x = {}; }
    const phase = ['idle', 'running', 'saving', 'done', 'save_error', 'error', 'cancelled', 'timeout'].includes(x.phase) ? x.phase : 'idle';
    return {
      phase,
      kind: x.kind || '',
      stage: x.stage || '',
      percent: Number(x.percent) || 0,
      jobId: x.jobId || '',
      source: x.source || '',
      transcript: String(x.transcript || ''),
      chars: Number(x.chars) || 0,
      title: x.title || '',
      error: x.error || '',
      errorCode: x.errorCode || '',
      // 2026-11-25：失败卡的「下一步」（后端登记表给的人话 / 动作 / 原话）；逐字稿之后建过的作品；建作品失败的那张卡。
      errorView: x.errorView && typeof x.errorView === 'object' ? x.errorView : null,
      nextProject: x.nextProject && x.nextProject.id ? x.nextProject : null, nextError: null, nextBusy: false,
      lastInput: x.lastInput && typeof x.lastInput === 'object' ? x.lastInput : {},
      fileName: x.fileName || '',
      startedAt: Number(x.startedAt) || 0,
      updatedAt: Number(x.updatedAt) || 0,
      savedId: x.savedId || '',
      savedAt: Number(x.savedAt) || 0,
      saveError: x.saveError || '',
      saveRequestId: x.saveRequestId || ''
    };
  }
  function save() {
    S.updatedAt = now();
    try {
      const draft = {
        phase: S.phase, kind: S.kind, stage: S.stage, percent: S.percent, jobId: S.jobId,
        source: S.source, chars: S.chars, title: S.title,
        error: S.error, errorCode: S.errorCode, errorView: S.errorView, nextProject: S.nextProject, lastInput: S.lastInput,
        fileName: S.fileName, startedAt: S.startedAt, updatedAt: S.updatedAt,
        savedId: S.savedId, savedAt: S.savedAt, saveError: S.saveError, saveRequestId: S.saveRequestId
      };
      // 已落库的正文不再写 localStorage；刷新时按 savedId 从后端历史恢复。
      if (!(S.savedId && S.phase === 'done')) draft.transcript = S.transcript;
      localStorage.setItem(KEY, JSON.stringify(draft));
    } catch (e) {}
  }
  function reset(keepInput) {
    pollSeq++;
    saveSeq++;
    if (uploadAbort) { try { uploadAbort.abort(); } catch (e) {} uploadAbort = null; }
    const last = keepInput ? (S.lastInput || {}) : {};
    S = {phase: 'idle', kind: '', stage: '', percent: 0, jobId: '', source: '', transcript: '',
      chars: 0, title: '', error: '', errorCode: '', lastInput: last, fileName: '',
      startedAt: 0, updatedAt: now(), savedId: '', savedAt: 0, saveError: '', saveRequestId: '',
      errorView: null, nextProject: null, nextError: null, nextBusy: false,
      comments: [], note: ''};
    if (!keepInput) pendingFile = null;
    save();
    render();
  }
  function startTimer() {
    stopTimer();
    elapsedTimer = setInterval(() => {
      if (S.phase !== 'running') { stopTimer(); return; }
      const el = document.getElementById('intake-elapsed');
      if (el) el.textContent = elapsedText();
    }, 1000);
  }
  function stopTimer() { if (elapsedTimer) { clearInterval(elapsedTimer); elapsedTimer = null; } }

  function ensureCss() {
    if (cssDone || typeof window.ipAddCssRules !== 'function') return;
    window.ipAddCssRules([
      '.ip-intake{margin:18px 0 10px}',
      '.intake-box{position:relative;border:1px solid rgba(255,255,255,.16);border-radius:16px;background:rgba(255,255,255,.035);padding:12px;transition:border-color .15s,background .15s}',
      '.intake-box.is-drag{border-color:#e6c56a;background:rgba(230,197,106,.08)}',
      '.intake-box textarea{width:100%;min-height:92px;resize:vertical;border:0;outline:0;background:transparent;color:inherit;font:inherit;font-size:16px;line-height:1.65;padding:6px 4px}',
      '.intake-tools{display:flex;align-items:center;gap:10px;flex-wrap:wrap;padding:6px 4px 0;border-top:1px solid rgba(255,255,255,.08)}',
      '.intake-file-name{max-width:min(420px,60vw);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}',
      '.intake-actions{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-top:10px}',
      '.intake-status:empty,.intake-result:empty{display:none}',
      '.intake-status{margin-top:12px}',
      '.intake-progress{width:100%;height:9px;margin:8px 0 2px;accent-color:#e6c56a}',
      '.intake-result{margin-top:14px;border:1px solid rgba(230,197,106,.28);border-radius:14px;padding:14px;background:rgba(230,197,106,.055)}',
      '.intake-transcript{white-space:pre-wrap;word-break:break-word;line-height:1.85;font-size:16px;max-height:52vh;overflow:auto;padding:10px 2px;border-top:1px solid rgba(255,255,255,.08);border-bottom:1px solid rgba(255,255,255,.08);margin:10px 0}',
      '.intake-meta{display:flex;gap:10px;align-items:center;flex-wrap:wrap}',
      '.intake-note{margin:0 0 4px;line-height:1.6}',
      '.intake-next-error{margin-top:10px}',
      '.intake-comments{margin:4px 0 10px;padding:8px 10px;border:1px solid rgba(255,255,255,.1);border-radius:10px}',
      '.intake-comments>summary{cursor:pointer;font-size:13px;opacity:.8}',
      '.intake-comments>ol{margin:10px 0 2px;padding-left:20px}',
      '.intake-comments li{margin:0 0 8px;line-height:1.6}',
      '.intake-comments .small{opacity:.7}',
      '.intake-actions .ghost,.intake-status .ghost,.intake-result .ghost{font-size:13px}',
      '.intake-actions a.ghost{display:inline-flex;align-items:center;padding:5px 10px;border:1px solid rgba(255,255,255,.18);border-radius:8px;text-decoration:none;color:inherit;opacity:.82}',
      '.wash-secondary{margin:6px 0 18px;border-top:1px solid rgba(255,255,255,.08);padding-top:12px}',
      '.wash-secondary>summary{cursor:pointer;color:inherit;opacity:.78;font-size:14px;padding:4px 0}',
      '.intake-history{margin-top:18px;border-top:1px solid rgba(255,255,255,.08);padding-top:12px;max-height:40vh;overflow:auto}',
      '.intake-history-head{display:flex;align-items:baseline;gap:10px;margin-bottom:8px}',
      '.intake-history-item{display:flex;align-items:center;gap:8px;border-bottom:1px solid rgba(255,255,255,.06);padding:6px 0}',
      '.intake-history-open{flex:1;min-width:0;text-align:left;background:none;border:0;color:inherit;cursor:pointer;padding:4px 0;display:flex;flex-direction:column;gap:2px}',
      '.intake-history-open strong{font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:100%}',
      '.intake-history-open:hover strong{color:#e6c56a}',
      '.intake-history-more{margin-top:8px}'
    ]);
    cssDone = true;
  }

  function whenText(ts) {
    if (!ts) return '';
    const d = new Date(ts);
    if (isNaN(d.getTime())) return '';
    const p = n => String(n).padStart(2, '0');
    return (d.getMonth() + 1) + '-' + p(d.getDate()) + ' ' + p(d.getHours()) + ':' + p(d.getMinutes());
  }
  async function loadHistory(more) {
    try {
      const offset = more ? historyOffset : 0;
      const d = await window.api('/intake/history?limit=20&offset=' + offset);
      const items = Array.isArray(d && d.items) ? d.items : [];
      historyItems = more ? historyItems.concat(items) : items;
      historyOffset = historyItems.length;
      historyTotal = Number(d && d.total) || historyItems.length;
      const next = d && d.next_offset;
      historyHasMore = (typeof next === 'number') ? next < historyTotal : historyOffset < historyTotal;
      historyError = '';
    } catch (e) {
      historyError = (e && e.message) || '历史暂时读不到。';
    }
    render();
  }
  function historySourceOf(item) {
    if (item && item.source) return item.source;
    if (item && item.source_kind === 'link') return 'link';
    if (item && item.source_kind === 'image') return 'image_ocr';
    if (item && item.source_kind === 'text') return 'text';
    return 'file';
  }
  function historyHtml() {
    if (!historyItems.length && !historyError) return '';
    let html = '<div class="intake-history-head"><strong>最近转写</strong><span class="small muted">存好的逐字稿和文章</span></div>';
    if (historyError) html += '<p class="small muted">' + esc2(historyError) + '</p>';
    html += historyItems.map(item => {
      const when = whenText(item.created_at);
      return '<div class="intake-history-item">' +
        '<button type="button" class="intake-history-open" data-intake-history="' + esc2(item.id) + '">' +
          '<strong>' + esc2(item.title || '未命名') + '</strong>' +
          '<span class="small muted">' + esc2(when) + ' · ' + esc2(sourceLabel(historySourceOf(item))) + '</span>' +
        '</button>' +
        '<button type="button" class="ghost small-button" data-intake-delete="' + esc2(item.id) + '">删除</button>' +
      '</div>';
    }).join('');
    if (historyHasMore) html += '<div class="intake-history-more"><button type="button" class="ghost small-button" data-intake-more>加载更多</button></div>';
    return html;
  }
  async function openHistory(id) {
    const item = historyItems.find(x => x.id === id);
    if (!item) return;
    let full = null;
    try {
      full = await window.api('/intake/history/' + encodeURIComponent(id));
    } catch (e) {
      notify((e && e.message) || '这条历史暂时读不到。');
      return;
    }
    S.phase = 'done';
    S.nextProject = null; S.nextError = null;   // 打开旧记录：不带上一条的「继续这条作品」
    S.savedId = id;
    S.savedAt = item.created_at ? Date.parse(item.created_at) : now();
    S.source = historySourceOf(item);
    const rec = (full && full.item) || full || {};
    S.transcript = String(rec.content || rec.transcript || '');
    S.chars = S.transcript.replace(/\s/g, '').length;
    S.stage = '已转成逐字稿';
    S.saveError = '';
    S.lastInput = {text: S.source === 'text' ? S.transcript : '', url: item.source_url || rec.source_url || ''};
    save(); render();
  }
  async function deleteHistory(id) {
    if (!window.confirm('删除这条历史？删除后不会出现在最近转写里。')) return;
    try {
      await window.api('/intake/history/' + encodeURIComponent(id), 'DELETE');
      historyItems = historyItems.filter(x => x.id !== id);
      if (S.savedId === id) reset(false);
      else render();
    } catch (e) { notify((e && e.message) || '删除失败，请重试。'); }
  }
  async function restoreSaved() {
    if (!S.savedId || S.transcript) return;
    try {
      const item = await window.api('/intake/history/' + encodeURIComponent(S.savedId));
      const rec = (item && item.item) || item || {};
      S.transcript = String(rec.content || rec.transcript || '');
      S.chars = S.transcript.replace(/\s/g, '').length;
      S.source = historySourceOf(rec);
      S.phase = S.transcript ? 'done' : 'error';
      S.nextProject = null; S.nextError = null;
      if (!S.transcript) S.error = '历史里这条没有正文了。';
    } catch (e) {
      S.phase = 'error';
      S.error = '历史读取失败：' + ((e && e.message) || '请稍后重试。');
    }
    save(); render();
  }

  function panel() {
    ensureCss();
    const savedText = (S.lastInput && S.lastInput.text) || '';
    const fileLine = S.fileName
      ? '<span class="small muted intake-file-name" id="intake-file-name">已选择：' + esc2(S.fileName) + '</span>'
      : '<span class="small muted intake-file-name" id="intake-file-name"></span>';
    setTimeout(() => { render(); loadHistory(); }, 0);
    return '<section class="ip-intake" id="intake-panel">' +
      '<form id="intake-form" class="intake-form" autocomplete="off">' +
        '<div class="intake-box" id="intake-drop">' +
          '<textarea id="intake-text" name="text" rows="3" aria-label="输入要转成逐字稿的内容" ' +
            'placeholder="粘贴分享文字或链接，也可以直接写一段话；图片、音频、视频可以拖进来。">' + esc2(savedText) + '</textarea>' +
          '<div class="intake-tools">' +
            '<button type="button" class="ghost small-button" id="intake-pick">选择图片 / 音视频</button>' +
            fileLine +
            '<button type="button" class="ghost small-button" id="intake-clear-file" data-intake-action="clear-file" hidden>移除文件</button>' +
            '<span class="small muted">也可以把文件拖到这里</span>' +
          '</div>' +
          '<input id="intake-file" type="file" accept="image/*,audio/*,video/*" hidden>' +
        '</div>' +
        '<div class="intake-actions">' +
          '<button class="primary" type="submit" id="intake-run">识别并转成逐字稿</button>' +
          '<button type="button" class="ghost small-button" data-intake-action="reset" id="intake-reset" hidden>重新开始</button>' +
          '<span class="small muted">一次处理一条；完成后只给你逐字稿。</span>' +
        '</div>' +
      '</form>' +
      '<div class="intake-status" id="intake-status" role="status" aria-live="polite"></div>' +
      '<div class="intake-result" id="intake-result"></div>' +
      '<div class="intake-history" id="intake-history"></div>' +
    '</section>';
  }

  function statusHtml() {
    if (S.phase === 'saving') {
      return '<div class="wash-note"><span class="processing-mark" aria-hidden="true">◌</span><div>' +
        '<strong>' + (S.recovering ? '恢复保存中…' : '正在保存到历史…') + '</strong>' +
        '<p class="small muted">保存成功后才算完成；以后换端口、清缓存也能在历史里找回。</p>' +
        '<p class="small"><button type="button" class="ghost small-button" data-intake-action="cancel-save">停止等待</button></p>' +
        '</div></div>';
    }
    if (S.phase === 'save_error') {
      return '<div class="wash-note warn"><div><strong>逐字稿已生成，但历史没保存</strong>' +
        '<p>' + esc2(S.saveError || '连接不上历史服务，请重试。') + '</p>' +
        '<p class="small muted">逐字稿还在下面，点「重试保存」再存一次。</p></div></div>';
    }
    if (S.phase === 'running') {
      const pct = Math.max(0, Math.min(100, Number(S.percent) || 0));
      return '<div class="wash-note"><span class="processing-mark" aria-hidden="true">◌</span><div>' +
        '<strong id="intake-stage">' + esc2(S.stage || '处理中…') + '</strong>' +
        (pct > 0 ? '<progress class="intake-progress" max="100" value="' + pct + '"></progress>' : '') +
        '<p class="small muted"><span id="intake-elapsed">' + esc2(elapsedText()) + '</span> · 完成后会自动显示逐字稿，不用重复点。</p>' +
        '<button type="button" class="ghost small-button" data-intake-action="cancel">取消这次</button>' +
      '</div></div>';
    }
    if (S.phase === 'cancelled') {
      return '<div class="wash-note warn"><div><strong>这次已取消</strong>' +
        '<p class="small muted">后台可能还在收尾，但这里不会再显示它。可以重试，或换一条。</p>' +
        '<div class="row"><button type="button" class="primary small-button" data-intake-action="retry">重试这条</button>' +
        '<button type="button" class="ghost small-button" data-intake-action="reset">换一条</button></div></div></div>';
    }
    if (S.phase === 'timeout') {
      // 2026-11-25：超时只是这里不等了，后台任务还在（S.jobId 没清）——给「继续等这一条」，不用重新发起。
      return '<div class="wash-note warn"><div><strong>等太久了，先停在这里</strong>' +
        '<p>' + esc2(S.error || '超过 10 分钟还没完成。') + '</p>' +
        '<p class="small muted">' + (S.jobId ? '后台多半还在处理：点「继续等这一条」接着等，不用重新来。' : '后台可能还在处理；重试前先确认同一条没有再跑。') + '</p>' +
        '<div class="row">' + (S.jobId ? '<button type="button" class="primary small-button" data-intake-action="continue">继续等这一条</button>' : '') +
        '<button type="button" class="' + (S.jobId ? 'ghost' : 'primary') + ' small-button" data-intake-action="retry">重试这条</button>' +
        '<button type="button" class="ghost small-button" data-intake-action="reset">换一条</button></div></div></div>';
    }
    if (S.phase === 'error') {
      // 2026-11-25：标题按情况说；「下一步」按钮来自后端登记表（S.errorView），没有就只给重试 / 换一条。
      const view = S.errorView || {};
      const next = view.next || {};
      // 旧记录（升级前存的）没有 errorView：prompt_login_required = 账号登录，login_required = 抖音扫码。
      const login = next.action === 'login' || (!S.errorView && S.errorCode === 'prompt_login_required');
      const douyin = next.action === 'douyin_login' || (!S.errorView && S.errorCode === 'login_required');
      const retryable = view.retry === null || view.retry === undefined ? true : view.retry;
      const title = S.errorCode === 'no_transcript' ? '这份内容里没有取到文字'
        : (S.errorCode === 'account' || S.errorCode === 'live' || S.errorCode === 'dead_link') ? '这条链接读不了'
        : next.action === 'upgrade' ? '免费体验已经结束' : '这次没拿到逐字稿';
      return '<div class="wash-note warn"><div><strong>' + esc2(title) + '</strong>' +
        '<p>' + esc2(S.error || '识别没有完成，请重试。') + '</p>' +
        '<div class="row">' +
          (next.action === 'upgrade' ? '<button type="button" class="primary small-button" data-intake-action="upgrade">去开通</button>' : '') +
          (next.action === 'settings' ? '<a class="ghost small-button" href="#/settings">' + esc2(next.label || '去设置页查看') + '</a>' : '') +
          (login ? '<button type="button" class="ghost small-button" data-intake-action="login">去登录</button>' : '') +
          (douyin ? '<button type="button" class="ghost small-button" data-intake-action="douyin-login">' + esc2(next.label || '去登录抖音') + '</button>' : '') +
          (retryable ? '<button type="button" class="' + (next.action && next.action !== 'retry' ? 'ghost' : 'primary') + ' small-button" data-intake-action="retry">再试一次</button>' : '') +
          (next.action === 'back' ? '<button type="button" class="ghost small-button" data-intake-action="back">返回修改</button>' : '') +
          '<button type="button" class="ghost small-button" data-intake-action="reset">换一条</button>' +
        '</div>' +
        (view.detail && view.detail !== S.error ? '<details><summary class="small muted">查看详情</summary><pre class="raw-markdown">' + esc2(view.detail) + '</pre></details>' : '') +
        (S.lastInput && S.lastInput.fileName && !pendingFile
          ? '<p class="small muted">刷新后原文件不在了：重试前请重新选择「' + esc2(S.lastInput.fileName) + '」。</p>' : '') +
      '</div></div>';
    }
    return '';
  }

  function resultHtml() {
    if (!S.transcript || !['done', 'saving', 'save_error'].includes(S.phase)) return '';
    const saved = S.phase === 'done' && S.savedId;
    // 2026-11-25：逐字稿之后的下一步。建过作品就变成「继续这条作品」，不会建第二个。
    const next = S.nextProject, isText = S.source === 'text';
    return '<div class="intake-meta"><span class="pill ok">逐字稿</span>' +
      '<span class="small muted">' + esc2(S.chars) + ' 字<span class="intake-source"> · ' + esc2(sourceLabel(S.source)) + '</span>' + (S.source === 'image_ocr' ? '（可能有漏字）' : '') + '</span>' +
      (saved ? '<span class="small muted">已存入历史</span>' : (S.phase === 'saving' ? '<span class="small muted">保存中…</span>' : '')) +
      '</div>' +
      '<div class="intake-transcript" id="intake-transcript">' + esc2(S.transcript) + '</div>' +
      (S.note ? '<p class="small muted intake-note">' + esc2(S.note) + '</p>' : '') +
      commentsHtml() +
      '<div class="intake-actions">' +
      (next && next.id
        ? '<a class="ghost small-button" href="#/project/' + encodeURIComponent(next.id) + '/script">继续这条作品 →</a>'
        : '<button type="button" class="primary small-button" data-intake-action="to-script"' + (S.nextBusy ? ' disabled' : '') + '>' + (isText ? '把它写成讲稿' : '基于这篇写讲稿') + '</button>' +
          '<button type="button" class="ghost small-button" data-intake-action="to-video"' + (S.nextBusy ? ' disabled' : '') + '>用这篇出视频</button>') +
      '<button type="button" class="ghost small-button" data-intake-action="copy">复制逐字稿</button>' +
      '<button type="button" class="ghost small-button" data-intake-action="export">导出 .txt</button>' +
      (S.phase === 'save_error' ? '<button type="button" class="primary small-button" data-intake-action="retry-save">重试保存</button>' : '') +
      (saved ? '<a class="ghost small-button" href="#/">查看我的作品</a>' : '') +
      '<button type="button" class="ghost small-button" data-intake-action="reset">再来一条</button>' +
      '</div>' + nextErrorHtml();
  }

  // 热评单独一块、默认收起，不混进逐字稿正文（逐字稿拿去直接用，保持干净）。
  function commentsHtml() {
    const rows = S.comments || [];
    if (!rows.length) return '';
    return '<details class="intake-comments"><summary>热评 ' + rows.length + ' 条（按点赞，点开查看）</summary><ol>' +
      rows.map(function (r) {
        return '<li>' + esc2(r.content || '') +
          '<div class="small">' + esc2(r.author || '') +
          (r.likes == null ? '' : ' · ' + esc2(String(r.likes)) + ' 赞') + '</div></li>';
      }).join('') + '</ol></details>';
  }
  function commentRows(block) {
    if (!block || !Array.isArray(block.top) || !block.top.length) return [];
    return block.top.slice(0, 10);
  }

  function render() {
    const panel = document.getElementById('intake-panel');
    if (!panel) return;
    const ta = document.getElementById('intake-text');
    // 对话页：发出去的内容已经在对话流里，输入框不再回填上一条，空着等下一条。
    const inChat = document.body.classList.contains('chat-open') && S.phase !== 'idle';
    if (ta && document.activeElement !== ta && !inChat) ta.value = (S.lastInput && S.lastInput.text) || '';
    const name = document.getElementById('intake-file-name');
    if (name) name.textContent = S.fileName ? ('已选择：' + S.fileName) : '';
    const clearFile = document.getElementById('intake-clear-file');
    if (clearFile) clearFile.hidden = !S.fileName;
    const st = document.getElementById('intake-status');
    if (st) st.innerHTML = statusHtml();
    const rs = document.getElementById('intake-result');
    if (rs) rs.innerHTML = resultHtml();
    const hs = document.getElementById('intake-history');
    if (hs) hs.innerHTML = historyHtml();
    const resetBtn = document.getElementById('intake-reset');
    if (resetBtn) resetBtn.hidden = !(S.phase === 'done' || S.phase === 'save_error' || S.phase === 'error' || S.phase === 'cancelled' || S.phase === 'timeout');
    const runBtn = document.getElementById('intake-run');
    if (runBtn) runBtn.disabled = S.phase === 'running' || S.phase === 'saving';
  }

  function titleFrom(text) {
    const one = String(text || '').replace(/https?:\/\/\S+/g, '').replace(/\s+/g, ' ').trim();
    const cut = Array.from(one).slice(0, 24).join('').replace(/[，。！？；：、,.!?;:]+$/, '');
    return (cut || ('输入稿 ' + stamp())) + (Array.from(one).length > 24 ? '…' : '');
  }
  function historyKind(source) {
    if (source === 'text') return 'text';
    if (source === 'link') return 'article';
    return 'transcript';
  }
  function historySourceKind(source) {
    if (source === 'link') return 'link';
    if (source === 'text') return 'text';
    if (source === 'image_ocr') return 'image';
    return 'video';
  }
  function saveErrorText(e, text) {
    if (e && (e.code === 'rid_conflict' || e.status === 409)) {
      return '该请求 ID 已用于另一份正文，这次保存被拒绝；请重试一次。';
    }
    if (e && (e.code === 'too_long' || e.status === 413)) {
      const d = e.data || {};
      const n = d.chars || d.characters || d.length || (text ? text.length : 0);
      const m = d.max_chars || d.limit || d.max || 200000;
      return '这段内容 ' + n + ' 字，超过一次保存上限 ' + m + ' 字，建议分段保存。';
    }
    return (e && e.message) || '历史保存失败，请重试。';
  }
  async function persistDone(text, source) {
    const seq = ++saveSeq;
    S.savedId = '';
    S.savedAt = 0;
    S.saveError = '';
    S.phase = 'saving';
    S.stage = '正在保存到历史';
    if (!S.saveRequestId) S.saveRequestId = newSaveRequestId();
    save(); render();
    try {
      const p = await window.api('/intake/history', 'POST', {
        kind: historyKind(source),
        title: titleFrom(text),
        source_kind: historySourceKind(source),
        source_url: (S.lastInput && S.lastInput.url) || '',
        transcript: text,
        source: source,
        file_name: S.fileName || '',
        client_request_id: S.saveRequestId,
        meta: {chars: S.chars, source: source, job_id: S.jobId || '', file_name: S.fileName || ''}
      });
      if (seq !== saveSeq) return;
      if (!p || !p.id) throw new Error('历史没有返回稳定编号。');
      S.savedId = p.id;
      S.savedAt = now();
      S.saveError = '';
      S.recovering = false;
      S.phase = 'done';
      S.stage = '已转成逐字稿';
    } catch (e) {
      if (seq !== saveSeq) return;
      S.recovering = false;
      S.phase = 'save_error';
      S.saveError = saveErrorText(e, text);
      S.stage = '逐字稿已生成，历史未保存';
      if (e && (e.code === 'rid_conflict' || e.status === 409)) S.saveRequestId = '';
    }
    save(); render();
    if (S.phase === 'done') loadHistory();
  }
  function begin(kind, stage, source) {
    S.phase = 'running';
    S.kind = kind || '';
    S.stage = stage || '处理中…';
    S.source = source || '';
    S.percent = 0;
    S.jobId = '';
    // 2026-11-25（对话页）：开始新的一条时放开上一条的编号——对话页靠它判断哪条还在底部结果区。
    S.savedId = '';
    S.savedAt = 0;
    S.transcript = '';
    S.chars = 0;
    S.comments = [];
    S.note = '';
    S.error = '';
    S.errorCode = '';
    S.errorView = null;
    S.nextProject = null;
    S.nextError = null;
    S.startedAt = now();
    pollSeq++;
    save();
    render();
    startTimer();
  }
  function newSaveRequestId() {
    return 'ihr_' + now().toString(36) + '_' + Math.random().toString(36).slice(2, 10);
  }
  async function finish(text, source) {
    stopTimer();
    pollSeq++;
    const value = String(text || '').trim();
    if (!value) { fail('识别完成了，但没有读到可用的文字。换个文件或直接粘贴原文再试。', 'empty_result'); return; }
    S.transcript = value;
    S.chars = value.replace(/\s/g, '').length;
    S.source = source || S.source || 'file';
    S.percent = 100;
    S.saveRequestId = newSaveRequestId();
    save();
    notify('逐字稿已生成，正在保存。');
    await persistDone(value, S.source);
    if (S.phase === 'done') notify('逐字稿已生成并存好。');
  }
  function fail(message, code, e) {
    stopTimer();
    pollSeq++;
    S.phase = 'error';
    S.error = message || '识别没有完成，请重试。';
    S.errorCode = code || '';
    S.errorView = viewOf(e);
    S.jobId = '';
    save();
    render();
  }
  // 失败卡的「下一步」来自后端登记表（app.py customer_error）：e 是 api() 抛的错误（e.next / e.retry / e.raw），
  // 或任务状态对象（error_next / error_retry / error_detail）。后端没给就按原逻辑只给重试 / 换一条。
  function viewOf(e) {
    if (!e) return null;
    const d = e.data || e || {};
    const next = e.next || d.error_next || d.next || null;
    const retry = typeof e.retry === 'boolean' ? e.retry : (typeof d.error_retry === 'boolean' ? d.error_retry : null);
    const detail = e.raw || d.error_detail || d.detail || '';
    return next || retry !== null || detail ? {next, retry, detail} : null;
  }
  function timeout() {
    stopTimer();
    pollSeq++;
    S.phase = 'timeout';
    S.error = '超过 10 分钟还没完成。后台可能仍在处理。';
    save();
    render();
  }

  async function rawJson(url, options) {
    let r;
    try { r = await fetch(url, options || {}); }
    catch (e) { const err = new Error('连不上工作台服务，请确认程序还在运行。'); err.code = 'network'; throw err; }
    let d = {};
    try { d = await r.json(); } catch (e) {}
    if (!r.ok) {
      const err = new Error((d && (d.title || d.error || d.message)) || ('请求失败（HTTP ' + r.status + '）'));
      err.code = (d && d.code) || '';
      err.status = r.status;
      err.data = d || {};
      err.raw = (d && d.detail) || '';   // 后端把原话放在 detail；兼容还没有 detail 的出口
      if (d && typeof d.retry === 'boolean') err.retry = d.retry;
      err.next = (d && d.next) || null;
      throw err;
    }
    return d;
  }

  async function uploadFile(file) {
    const session = await rawJson('/api/uploads', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({purpose: 'capture', filename: file.name, size: file.size})
    });
    const size = Number(file.size) || 0;
    let offset = 0;
    uploadAbort = new AbortController();
    try {
      while (offset < size) {
        const end = Math.min(size, offset + 8 * 1024 * 1024);
        const chunk = file.slice(offset, end);
        const r = await fetch('/api/uploads/' + encodeURIComponent(session.id) + '?offset=' + offset, {
          method: 'PUT', headers: {'Content-Type': 'application/octet-stream'},
          body: chunk, signal: uploadAbort.signal
        });
        if (!r.ok) {
          let d = {}; try { d = await r.json(); } catch (e) {}
          const err = new Error((d && (d.error || d.message)) || ('上传失败（HTTP ' + r.status + '）'));
          err.code = (d && d.code) || '';
          throw err;
        }
        offset = end;
        S.percent = size ? Math.min(99, Math.round(offset * 100 / size)) : 0;
        S.stage = '上传文件 ' + Math.round(offset * 100 / Math.max(1, size)) + '%';
        save(); render();
      }
      const asset = await rawJson('/api/uploads/' + encodeURIComponent(session.id) + '/complete', {
        method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'
      });
      return asset;
    } finally {
      uploadAbort = null;
    }
  }

  async function startFile(file) {
    const name = String(file.name || '');
    const isImage = IMAGE_RE.test(name);
    if (!(isImage || AUDIO_RE.test(name) || VIDEO_RE.test(name))) {
      fail('这个文件类型还不支持。可以选图片（jpg/png/webp）、音频（mp3/m4a/wav）或视频（mp4/mov/mkv）。', 'unsupported');
      return;
    }
    if (file.size <= 0) { fail('这个文件是空的，换一个再试。', 'empty_file'); return; }
    if (file.size > 4 * 1024 * 1024 * 1024) { fail('本地单文件上限 4GB，请先截取一段。', 'too_large'); return; }
    const ta = document.getElementById('intake-text');
    const draftText = ta ? String(ta.value || '').trim() : ((S.lastInput && S.lastInput.text) || '');
    begin('file', '准备上传', 'file');
    // 文件优先处理，但输入框里的文字不丢；失败/重试时还能回到这段文字。
    S.lastInput = {text: draftText, fileName: name, url: ''};
    S.fileName = name;
    save(); render();
    try {
      const asset = await uploadFile(file);
      if (S.phase !== 'running') return;
      S.stage = isImage ? '识别图片里的文字' : '上传完成，开始转写';
      save(); render();
      const r = await window.api('/capture/file', 'POST', {
        asset_id: asset.id, mode: isImage ? 'ocr' : 'auto', wait: false
      });
      if (r && r.job_id) {
        S.jobId = r.job_id;
        S.source = 'file';
        S.stage = r.stage || '排队处理中';
        save(); render();
        await pollCapture(r.job_id);
        return;
      }
      if (r && r.text) { finish(r.text, r.source || 'file'); return; }
      fail('文件传上去了，但没有拿到处理任务，请重试。', 'no_job');
    } catch (e) {
      if (e && e.name === 'AbortError') return;
      fail((e && e.message) || '文件处理失败，请重试。', (e && e.code) || '', e);
    }
  }

  async function pollCapture(jobId) {
    const seq = ++pollSeq;
    let errors = 0;
    while (seq === pollSeq && S.jobId === jobId && S.phase === 'running') {
      if (now() - S.startedAt > MAX_RUN_MS) { timeout(); return; }
      try {
        const j = await window.api('/jobs/' + encodeURIComponent(jobId));
        if (seq !== pollSeq) return;
        errors = 0;
        S.stage = j.stage_text || j.stage || j.message || '处理中…';
        if (j.total) S.percent = Math.max(S.percent, Math.min(99, Math.round((Number(j.done) || 0) * 100 / Number(j.total))));
        save(); render();
        if (j.status === 'succeeded') {
          const r = j.result || {};
          if (r.need_login) { fail(r.message || '这条要登录后才能继续。', 'login_required', {next: {label: '去登录抖音', action: 'douyin_login'}, retry: false}); return; }
          finish(r.text, r.source || 'file');
          return;
        }
        if (j.status === 'failed') { fail(j.error_title || j.error || j.message || '转写没有完成，请重试。', j.reason || '', j); return; }
      } catch (e) {
        errors++;
        if (errors >= 3) { fail((e && e.message) || '查询进度失败，请重试。', (e && e.code) || '', e); return; }
      }
      await sleep(1200);
    }
  }

  async function startWashLink(url) {
    S.stage = '读取链接正文';
    save(); render();
    let r;
    try { r = await window.api('/wash/link', 'POST', {url}); }
    catch (e) { fail((e && e.message) || '读取链接失败。', (e && e.code) || '', e); return; }
    if (r && r.ok && r.text) { finish(r.text, 'link'); return; }
    if (r && r.kind === 'video') { await startWashVideo(url); return; }
    fail((r && r.reason) || '这个链接没有读出正文。可以换成原文粘贴，或换一条。', (r && r.kind) || 'link');
  }

  async function startWashVideo(url, resumeJobId) {
    S.lastInput = Object.assign({}, S.lastInput || {}, {url: url || ''});
    S.stage = resumeJobId ? '接着等视频解析' : '解析视频';
    save(); render();
    let jobId = resumeJobId || '';
    if (!jobId) {
      let r;
      try { r = await window.api('/wash/video/start', 'POST', {url}); }
      catch (e) { fail((e && e.message) || '视频解析没有启动。', (e && e.code) || '', e); return; }
      if (!r || !r.job_id) { fail('视频解析没有返回任务号，请重试。', 'no_job'); return; }
      jobId = r.job_id;
    }
    S.jobId = jobId;
    S.source = 'link';
    S.stage = '视频解析中';
    save(); render();
    const seq = ++pollSeq;
    let errors = 0;
    while (seq === pollSeq && S.phase === 'running') {
      if (now() - S.startedAt > MAX_RUN_MS) { timeout(); return; }
      try {
        const j = await window.api('/wash/video/status?job=' + encodeURIComponent(jobId));
        if (seq !== pollSeq) return;
        errors = 0;
        S.stage = j.stage_text || j.stage || '视频解析中';
        if (Array.isArray(j.steps) && j.steps.length) {
          const done = j.steps.filter(x => x.status === 'done').length;
          S.percent = Math.max(S.percent, Math.min(99, Math.round(done * 100 / j.steps.length)));
        }
        save(); render();
        if (j.status === 'done') {
          const p = j.result || {};
          const t = p.transcript || {};
          // 热评和说明先记下，再出逐字稿（finish 里会重绘结果区）。
          S.comments = commentRows(p.comments);
          S.note = String(t.note || '');
          if (t.text) { finish(t.text, 'link'); return; }
          S.comments = []; S.note = '';
          fail((j.error_view && j.error_view.title) || t.reason || p.collection_note || '视频里没有取到口播文字，可以粘贴原文继续。', 'no_transcript', j.error_view ? {next: j.error_view.next, retry: j.error_view.retry, raw: j.error_view.detail} : null);
          return;
        }
        if (j.status === 'failed') { const ev = j.error_view || {}; fail(ev.title || j.error || '视频解析失败，可以粘贴原文继续。', 'video_failed', ev.next ? {next: ev.next, retry: ev.retry, raw: ev.detail} : null); return; }
      } catch (e) {
        errors++;
        if (errors >= 3) { fail((e && e.message) || '查询视频进度失败。', (e && e.code) || '', e); return; }
      }
      await sleep(1500);
    }
  }

  async function startText(text) {
    const value = String(text || '').trim();
    const url = pickUrl(value);
    S.lastInput = {text: value, url: url || ''};
    S.fileName = '';
    pendingFile = null;
    begin('text', url ? '识别链接' : '整理文字', 'text');
    if (!url) { finish(value, 'text'); return; }
    let info = null;
    try { info = await window.api('/wash/classify', 'POST', {url}); } catch (e) {}
    const kind = (info && info.kind) || 'unknown';
    if (kind === 'account') {
      fail('这是账号主页，不是单条作品。账号主页适合做同类账号参考；要转写内容，请贴单条作品/文章链接，或直接粘贴文字。', 'account');
      return;
    }
    if (kind === 'live') {
      fail('直播回放暂时不能直接转写。可以截取其中一段，或用「选择音视频」上传。', 'live');
      return;
    }
    if (kind === 'dead_link') {
      fail((info && info.reason) || '这个链接已经失效，换一条或粘贴原文。', 'dead_link');
      return;
    }
    if (kind === 'video') { await startWashVideo(url); return; }
    await startWashLink(url);
  }

  async function run() {
    if (S.phase === 'running' || S.phase === 'saving') { notify('这条正在处理中，不用重复点。'); return; }
    const ta = document.getElementById('intake-text');
    const text = ta ? String(ta.value || '').trim() : '';
    const file = pendingFile;
    if (!text && !file) { notify('先贴一段文字/链接，或选择图片、音视频。'); return; }
    // 对话页：发出去就清空输入框（两条启动函数都已同步记下这次输入，清空不影响重试）。
    const inChat = document.body.classList.contains('chat-open');
    if (file) { const p = startFile(file); if (inChat) clearInput(); await p; return; }
    const p = startText(text);
    if (inChat) clearInput();
    await p;
  }
  // 2026-11-25：逐字稿之后的两条出路。都不新增输入口，都先建作品记录（现有接口 POST /api/projects）：
  //   写讲稿：有写作模型 → 现有「生成初稿」接口（POST …/writing-suggestions）；没有 → 把逐字稿直接放进讲稿页的编辑框。
  //   出视频：  这套软件没有「文字直接变成片」的功能。出片 = 讲稿页确认稿件 → 上传本人拍摄 → 自动剪（app.js 的 steps）。
  function llmReady() {
    return typeof state !== 'undefined' && !!(state.boot && state.boot.capabilities && state.boot.capabilities.llm);
  }
  async function continueWith(goal) {
    if (S.nextBusy) return;
    const text = String(S.transcript || '').trim();
    if (!text) { notify('逐字稿还没出来，等它出来再点。'); return; }
    S.nextBusy = true;
    S.nextError = null;
    render();
    try {
      // 上一次已经建好作品、只是后面一步没成：沿用那条，不建第二个。
      const prev = S.nextProject && S.nextProject.id ? S.nextProject.id : '';
      const p = prev ? await window.api('/projects/' + encodeURIComponent(prev))
        : await window.api('/projects', 'POST', {title: titleFrom(text), source_text: text});
      S.nextProject = {id: p.id, goal: goal};
      save();
      let started = false;
      if (goal === 'script' && llmReady()) {
        try {
          await window.api('/projects/' + encodeURIComponent(p.id) + '/writing-suggestions', 'POST',
            {mode: 'draft', draft_revision: p.draft_revision, context: '请基于这份逐字稿写一版口播讲稿。'});
          started = true;
        } catch (e) {
          // 没启动成（试用到期、要登录等）：不拦路，逐字稿照常进讲稿页，原因当场说。
          notify('自动写稿没有启动：' + ((e && e.message) || '') + '。逐字稿已放进讲稿页，可以先手动改。');
        }
      }
      if (!started && typeof wholeToParts === 'function') {
        await window.api('/projects/' + encodeURIComponent(p.id) + '/draft', 'PATCH',
          {parts: wholeToParts(text), draft_revision: p.draft_revision});
      }
      location.hash = '#/project/' + encodeURIComponent(p.id) + '/script';
    } catch (e) {
      // 建作品 / 写草稿同步失败：结果区下方出一张卡，原因和下一步来自后端登记表（app.py customer_error）。
      S.nextError = {title: (e && e.message) || '这一步没做成，请重试。', detail: (e && e.raw) || '', next: (e && e.next) || null, goal: goal};
    } finally {
      S.nextBusy = false;
      save(); render();
    }
  }
  function nextButtonHtml(next) {
    if (!next) return '';
    if (next.action === 'settings') return '<a class="ghost small-button" href="#/settings">' + esc2(next.label || '去设置页查看') + '</a>';
    return '<button type="button" class="small-button" data-intake-next="' + esc2(next.action) + '">' + esc2(next.label || '下一步') + '</button>';
  }
  function nextErrorHtml() {
    const v = S.nextError;
    if (!v) return '';
    return '<div class="wash-note warn intake-next-error"><div><strong>' + esc2(v.title) + '</strong>' +
      (v.detail && v.detail !== v.title ? '<details><summary class="small muted">查看详情</summary><pre class="raw-markdown">' + esc2(v.detail) + '</pre></details>' : '') +
      '<div class="row">' + nextButtonHtml(v.next) +
      (!v.next || v.next.action !== 'retry' ? '<button type="button" class="primary small-button" data-intake-action="' + (v.goal === 'video' ? 'to-video' : 'to-script') + '">重试</button>' : '') +
      '<button type="button" class="ghost small-button" data-intake-action="next-dismiss">先不用</button>' +
      '</div></div></div>';
  }
  function clearInput() {
    const ta = document.getElementById('intake-text');
    if (ta) ta.value = '';
  }

  function copyTranscript() {
    const text = S.transcript || '';
    if (!text) return;
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(() => notify('逐字稿已复制。')).catch(() => fallbackCopy(text));
    } else fallbackCopy(text);
  }
  function fallbackCopy(text) {
    try {
      const ta = document.createElement('textarea');
      ta.value = text; ta.setAttribute('readonly', 'readonly');
      ta.style.position = 'fixed'; ta.style.left = '-9999px';
      document.body.appendChild(ta); ta.select(); document.execCommand('copy'); ta.remove();
      notify('逐字稿已复制。');
    } catch (e) { notify('复制没有成功，请手动选择文字复制。'); }
  }
  function exportTranscript() {
    if (!S.transcript) return;
    const rows = S.comments || [];
    // 导出的文件：逐字稿在前、保持干净；有热评时另起一段放在后面。
    const body = S.transcript + '\n' + (rows.length
      ? '\n—— 热评（按点赞）——\n' + rows.map(function (r) {
          return '· ' + (r.content || '') + (r.likes == null ? '' : '（' + r.likes + ' 赞）');
        }).join('\n') + '\n' : '');
    const blob = new Blob([body], {type: 'text/plain;charset=utf-8'});
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = '逐字稿-' + stamp() + '.txt';
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 1500);
    notify('已导出 .txt。');
  }

  function retry() {
    if (pendingFile) { startFile(pendingFile); return; }
    const text = (S.lastInput && S.lastInput.text) || '';
    if (text) { startText(text); return; }
    if (S.lastInput && S.lastInput.fileName) {
      notify('刷新后原文件不在了，请重新选择「' + S.lastInput.fileName + '」。');
      const pick = document.getElementById('intake-pick');
      if (pick) pick.click();
      return;
    }
    notify('没有可重试的内容，请重新输入或选择文件。');
  }

  function resume() {
    if (S.phase !== 'running') return;
    if (now() - (S.startedAt || 0) > MAX_RUN_MS) { timeout(); return; }
    if (!S.jobId) {
      fail('刷新时这次还没拿到后台任务，无法接着等。请重试这条。', 'resume_no_job');
      return;
    }
    if (S.source === 'file') { pollCapture(S.jobId); return; }
    if (S.source === 'link') { startWashVideo((S.lastInput && S.lastInput.url) || '', S.jobId); return; }
    fail('刷新后无法判断这条的进度，请重试。', 'resume_unknown');
  }

  // 「下一步」卡上后端登记表给的动作（[data-intake-next]）：全部走现成落点。
  document.addEventListener('click', event => {
    const btn = event.target && event.target.closest ? event.target.closest('[data-intake-next]') : null;
    if (!btn) return;
    const a = btn.getAttribute('data-intake-next');
    const goal = (S.nextError && S.nextError.goal) || 'script';
    if (a === 'retry') continueWith(goal);
    else if (a === 'back') { const ta = document.getElementById('intake-text'); if (ta) ta.focus(); }
    else if (a === 'login') { if (typeof flowOpenLogin === 'function') flowOpenLogin(); else location.hash = '#/settings'; }
    else if (a === 'upgrade') {
      const m = document.createElement('button'); m.dataset.action = 'member-renew'; m.hidden = true;
      document.body.appendChild(m); m.click(); m.remove();
    }
    else if (a === 'rechoose') reset(false);
  });

  document.addEventListener('submit', event => {
    const form = event.target && event.target.closest ? event.target.closest('#intake-form') : null;
    if (!form) return;
    event.preventDefault();
    event.stopPropagation();
    run();
  }, true);

  document.addEventListener('click', event => {
    const pick = event.target && event.target.closest ? event.target.closest('#intake-pick') : null;
    if (pick) {
      const input = document.getElementById('intake-file');
      if (input) input.click();
      return;
    }
    const open = event.target && event.target.closest ? event.target.closest('[data-intake-history]') : null;
    if (open) { openHistory(open.getAttribute('data-intake-history')); return; }
    const del = event.target && event.target.closest ? event.target.closest('[data-intake-delete]') : null;
    if (del) { deleteHistory(del.getAttribute('data-intake-delete')); return; }
    const more = event.target && event.target.closest ? event.target.closest('[data-intake-more]') : null;
    if (more) { loadHistory(true); return; }
    const btn = event.target && event.target.closest ? event.target.closest('[data-intake-action]') : null;
    if (!btn) return;
    const action = btn.getAttribute('data-intake-action');
    if (action === 'cancel') {
      pollSeq++;
      if (uploadAbort) { try { uploadAbort.abort(); } catch (e) {} uploadAbort = null; }
      stopTimer();
      S.phase = 'cancelled'; S.jobId = '';
      save(); render();
      notify('已取消这次等待；后台可能还在收尾。');
    }
    else if (action === 'continue') {
      // 超时只是这里不等了，后台任务还在：接着轮询同一个任务（resume 按 S.source 分流），不重新发起。
      S.phase = 'running'; S.startedAt = now(); S.error = ''; save(); render(); startTimer(); resume();
    }
    else if (action === 'to-script') continueWith('script');
    else if (action === 'to-video') continueWith('video');
    else if (action === 'next-dismiss') { S.nextError = null; save(); render(); }
    else if (action === 'retry') retry();
    else if (action === 'retry-save') persistDone(S.transcript, S.source);
    else if (action === 'cancel-save') {
      saveSeq++;
      S.recovering = false;
      S.phase = 'save_error';
      S.saveError = '你停止了等待保存；逐字稿还在，可以点「重试保存」再存一次。';
      S.stage = '逐字稿已生成，历史未保存';
      save(); render(); notify('已停止等待；后台保存可能仍在收尾。');
    }
    else if (action === 'clear-file') {
      pendingFile = null;
      S.fileName = '';
      const ta = document.getElementById('intake-text');
      S.lastInput = {text: ta ? String(ta.value || '') : ((S.lastInput && S.lastInput.text) || ''), fileName: '', url: ''};
      save(); render(); notify('已移除文件；输入框里的文字还在。');
    }
    else if (action === 'reset') reset(false);
    else if (action === 'copy') copyTranscript();
    else if (action === 'export') exportTranscript();
    else if (action === 'login') {
      // 「需登录云端模板」是账号登录（门禁），不是抖音扫码；扫码另有 douyin-login。
      if (typeof flowOpenLogin === 'function') flowOpenLogin();
      else location.hash = '#/settings';
    }
    else if (action === 'douyin-login') {
      if (window.showDouyinLogin) window.showDouyinLogin(() => retry());
      else location.hash = '#/settings';
    }
    else if (action === 'upgrade') {
      // 「去开通」：复用 app.js 现成的 member-renew（打开会员中心并停在续费区）。
      const m = document.createElement('button'); m.dataset.action = 'member-renew'; m.hidden = true;
      document.body.appendChild(m); m.click(); m.remove();
    }
    else if (action === 'back') {
      const ta = document.getElementById('intake-text'); if (ta) ta.focus();
    }
  });

  document.addEventListener('change', event => {
    if (event.target && event.target.id === 'intake-file') {
      const f = event.target.files && event.target.files[0];
      if (!f) return;
      pendingFile = f;
      S.fileName = f.name;
      S.phase = S.phase === 'done' ? 'idle' : S.phase;
      const ta = document.getElementById('intake-text');
      S.lastInput = {text: ta ? String(ta.value || '') : ((S.lastInput && S.lastInput.text) || ''), fileName: f.name, url: ''};
      save(); render();
      notify('已选择：' + f.name);
    }
  });

  document.addEventListener('paste', event => {
    const ta = event.target;
    if (!ta || ta.id !== 'intake-text') return;
    const files = event.clipboardData && event.clipboardData.files;
    if (files && files.length) {
      const f = files[0];
      if (f && f.type && f.type.indexOf('image/') === 0) {
        event.preventDefault();
        pendingFile = f;
        S.fileName = f.name || '剪贴板图片.png';
        S.lastInput = {text: ta.value || ((S.lastInput && S.lastInput.text) || ''), fileName: S.fileName, url: ''};
        save(); render();
        notify('已从剪贴板收到图片。');
        return;
      }
    }
    const text = (event.clipboardData && event.clipboardData.getData('text')) || '';
    if (text && pickUrl(text)) {
      event.preventDefault();
      ta.value = text.trim();
      notify('已收下这段分享文字；点「识别并转成逐字稿」就开始。');
    }
  });

  document.addEventListener('dragover', event => {
    const box = document.getElementById('intake-drop');
    if (!box || !box.contains(event.target)) return;
    event.preventDefault();
    box.classList.add('is-drag');
  });
  document.addEventListener('dragleave', event => {
    const box = document.getElementById('intake-drop');
    if (!box || !box.contains(event.target)) return;
    box.classList.remove('is-drag');
  });
  document.addEventListener('drop', event => {
    const box = document.getElementById('intake-drop');
    if (!box || !box.contains(event.target)) return;
    event.preventDefault();
    box.classList.remove('is-drag');
    const f = event.dataTransfer && event.dataTransfer.files && event.dataTransfer.files[0];
    if (!f) return;
    pendingFile = f;
    S.fileName = f.name;
    if (S.phase === 'done') S.phase = 'idle';
    const ta = document.getElementById('intake-text');
    S.lastInput = {text: ta ? String(ta.value || '') : ((S.lastInput && S.lastInput.text) || ''), fileName: f.name, url: ''};
    save(); render();
    notify('已选择：' + f.name);
  });

  async function resumeSaving() {
    if (!S.saveRequestId || !S.transcript) {
      S.phase = 'save_error';
      S.saveError = '刷新时没找到保存请求号；逐字稿还在，点「重试保存」再存一次。';
      S.stage = '逐字稿已生成，历史未保存';
      save(); render();
      return;
    }
    S.recovering = true;
    await persistDone(S.transcript, S.source || 'text');
  }

  async function boot() {
    render();
    // 2026-11-25：本文件排在 app.js 前面加载，刷新时这里可能跑在 app.js 之前，那时请求函数（window.api）还不存在，
    // 会导致「恢复上一条结果」直接失败。需要它的几种情况先等它就绪，最多等 5 秒。
    for (let i = 0; i < 100 && typeof window.api !== 'function'; i++) await sleep(50);
    if (S.phase === 'running') resume();
    else if (S.phase === 'saving' && S.saveRequestId && !S.savedId) resumeSaving();
    else if (S.phase === 'saving') {
      S.phase = 'save_error';
      S.saveError = '刷新时没找到保存请求号；逐字稿还在，点「重试保存」再存一次。';
      S.stage = '逐字稿已生成，历史未保存';
      save(); render();
    } else if (S.savedId && !S.transcript) restoreSaved();
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => setTimeout(boot, 0));
  } else {
    setTimeout(boot, 0);
  }

  window.intakePanel = panel;
  window.ipIntake = {panel, state: () => S, reset: () => reset(false)};
})();
