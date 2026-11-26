'use strict';
/* 通用语音输入（2026-11-18）：按 12 题语音建档同一套——
 * 短句：浏览器 SpeechRecognition 实时识别，识别出的整句追加进输入框；
 * 长段：MediaRecorder 录音 → POST /api/flow/transcribe（本机 ASR）→ 文字追加进输入框；
 * 不支持时按钮禁用并提示可改用微信转文字。
 * 用 MutationObserver 自动挂到下面列出的输入框，页面切换/重渲染后自动补挂，不改各页面渲染代码。 */
(function () {
  const script = document.currentScript;
  const CSS = [
    '.vi-bar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:8px}',
    '.vi-bar button{font-size:11px;padding:5px 10px;border-radius:7px;border:1px solid var(--border);background:transparent;color:var(--text);cursor:pointer;line-height:1.5}',
    '.vi-bar button:disabled{opacity:.45;cursor:default}',
    '.vi-bar button.is-live{border-color:var(--gold);color:var(--gold)}',
    '.vi-bar .vi-status{font-size:11px;color:var(--muted);flex:1;min-width:160px;line-height:1.7}'
  ];

  let cssInjected = false;
  function injectCss() {
    if (cssInjected) return;
    cssInjected = true;
    try {
      const sheet = new CSSStyleSheet();
      CSS.forEach((rule, i) => sheet.insertRule(rule, i));
      document.adoptedStyleSheets = [...document.adoptedStyleSheets, sheet];
    } catch (err) { /* 样式失败不影响文字 */ }
  }

  const shortSupported = () => Boolean(window.SpeechRecognition || window.webkitSpeechRecognition);
  const longSupported = () => Boolean(navigator.mediaDevices && navigator.mediaDevices.getUserMedia && window.MediaRecorder);

  function blobBase64(blob) {
    return new Promise((resolve, reject) => {
      const fr = new FileReader();
      fr.onload = () => resolve(String(fr.result || '').split(',')[1] || '');
      fr.onerror = () => reject(Error('录音读取失败，请重录一次。'));
      fr.readAsDataURL(blob);
    });
  }

  function appendText(ta, text) {
    const clean = String(text || '').trim();
    if (!clean) return false;
    const cur = ta.value.replace(/\s+$/, '');
    if (cur.endsWith(clean)) return true; // 幂等：同一段结果不重复追加
    ta.value = cur ? (cur + ' ' + clean) : clean;
    ta.dispatchEvent(new Event('input', {bubbles: true})); // 让各页面的自动保存/字数统计跟着更新
    return true;
  }

  function attach(ta) {
    if (!ta || ta.dataset.voiceBound === '1') return;
    if (ta.closest('.g-ask-card, .g-voice-controls')) return; // 问问自己页已有专用控件
    // 2026-11-25：单对话口（#intake-text）只在手机尺寸挂语音条——原「记一条」浮层的语音能力不丢；
    // 桌面首页维持原样（t198 上线时就没有语音条）。
    if (ta.id === 'intake-text' && !window.matchMedia('(max-width: 760px)').matches) return;
    ta.dataset.voiceBound = '1';
    injectCss();

    const bar = document.createElement('div');
    bar.className = 'vi-bar';
    bar.setAttribute('data-voice-bar', '1');
    bar.innerHTML = '<button type="button" data-vi="short">短句识别</button>'
      + '<button type="button" data-vi="long">录长段（本地转写）</button>'
      + '<span class="vi-status" role="status"></span>';
    // 2026-11-24（t174，吸收客户端已验证修复）：独立聊天窗的输入区 form.bc-form 是横向 flex，
    // 语音条作为 flex 子项插在 textarea 后面会把输入框挤成小框；这类页面插到 form 之后单独一行。
    const hostForm = ta.closest('form.bc-form');
    if (hostForm) {
      hostForm.insertAdjacentElement('afterend', bar);
    } else {
      ta.insertAdjacentElement('afterend', bar);
    }

    const shortBtn = bar.querySelector('[data-vi="short"]');
    const longBtn = bar.querySelector('[data-vi="long"]');
    const status = bar.querySelector('.vi-status');
    if (!shortSupported()) { shortBtn.disabled = true; shortBtn.textContent = '短句识别不可用'; }
    if (!longSupported()) { longBtn.disabled = true; longBtn.textContent = '长段转写不可用'; }
    if (!shortSupported() && !longSupported()) {
      status.textContent = '这个环境没有可用的语音识别；可以按微信「语音转文字」把说的话贴进来，或直接打字。';
    } else {
      status.textContent = '短句：点一下说，说完再点一下；长段：录完自动本机转写（首次约 1–2 分钟）。';
    }

    /* ---------- 短句：浏览器实时识别 ---------- */
    let sr = null;
    let seen = new Set();
    let last = '';
    shortBtn.addEventListener('click', () => {
      const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
      if (!SR) return;
      if (sr) { try { sr.stop(); } catch (e) { /* 已结束 */ } return; }
      const rec = new SR();
      sr = rec;
      seen = new Set();
      last = '';
      rec.lang = 'zh-CN';
      rec.continuous = true;
      rec.interimResults = true;
      rec.onresult = e => {
        let interim = '';
        const fresh = [];
        for (let i = e.resultIndex; i < e.results.length; i++) {
          const r = e.results[i];
          if (!r.isFinal) { interim += r[0].transcript; continue; }
          const text = String(r[0].transcript || '').trim();
          if (!text) continue;
          const key = i + ':' + text;
          if (seen.has(key)) continue;
          seen.add(key);
          fresh.push(text);
        }
        const finals = fresh.filter(t => t !== last);
        if (finals.length) {
          appendText(ta, finals.join(' '));
          last = fresh[fresh.length - 1];
        }
        status.textContent = interim ? ('识别中：' + interim) : '正在听，说完再点一下按钮结束。';
      };
      rec.onerror = e => {
        const message = {
          'not-allowed': '没有麦克风权限。可以按微信「语音转文字」把说的话贴进来。',
          'audio-capture': '没找到麦克风。可以按微信「语音转文字」把说的话贴进来，内容一样会保存。',
          'network': '语音识别连不上网。可以直接打字。',
          'no-speech': '没听到声音，再点一次试试。'
        }[e.error] || ('语音识别出错（' + e.error + '）。可以直接打字。');
        status.textContent = message;
      };
      rec.onend = () => {
        shortBtn.classList.remove('is-live');
        shortBtn.textContent = '短句识别';
        if (sr === rec) sr = null;
      };
      try {
        rec.start();
        shortBtn.classList.add('is-live');
        shortBtn.textContent = '正在听…点一下结束';
        status.textContent = '正在听，说完再点一下按钮结束。';
      } catch (e) {
        status.textContent = '浏览器暂时不让开始识别，直接打字也一样。';
        sr = null;
      }
    });

    /* ---------- 长段：本机录音 + /api/flow/transcribe ---------- */
    let mediaRec = null;
    let stream = null;
    let chunks = [];
    let busy = false;
    let done = false;
    longBtn.addEventListener('click', async () => {
      if (busy) return;
      if (mediaRec && mediaRec.state !== 'inactive') {
        try { mediaRec.stop(); } catch (e) { /* 已结束 */ }
        return;
      }
      if (sr) { try { sr.stop(); } catch (e) { /* 已结束 */ } }
      if (!longSupported()) return;
      status.textContent = '正在请求麦克风权限…';
      try {
        stream = await navigator.mediaDevices.getUserMedia({audio: true});
      } catch (e) {
        status.textContent = '没拿到麦克风权限，长段录不了。可以按微信「语音转文字」把说的话贴进来。';
        return;
      }
      let rec;
      try {
        const mime = window.MediaRecorder.isTypeSupported('audio/webm;codecs=opus') ? 'audio/webm;codecs=opus' : '';
        rec = new MediaRecorder(stream, mime ? {mimeType: mime} : undefined);
      } catch (e) {
        if (stream) stream.getTracks().forEach(t => t.stop());
        stream = null;
        status.textContent = '这个环境不能录长段，直接打字也一样。';
        return;
      }
      chunks = [];
      done = false;
      mediaRec = rec;
      rec.ondataavailable = e => { if (e.data && e.data.size) chunks.push(e.data); };
      rec.onstop = () => {
        if (done) return;
        done = true;
        finishLong(rec.mimeType || 'audio/webm');
      };
      rec.start();
      longBtn.classList.add('is-live');
      longBtn.textContent = '录长段中…点一下结束';
      status.textContent = '正在录长段；说完再点一下按钮，转写首次要等 1–2 分钟。';
    });

    function releaseStream() {
      if (stream) {
        try { stream.getTracks().forEach(t => t.stop()); } catch (e) { /* 已释放 */ }
        stream = null;
      }
      mediaRec = null;
    }

    async function finishLong(mime) {
      const parts = chunks.slice();
      chunks = [];
      releaseStream();
      longBtn.classList.remove('is-live');
      longBtn.textContent = '录长段（本地转写）';
      if (!parts.length) { status.textContent = '没录到声音，再试一次，或直接打字。'; return; }
      const blob = new Blob(parts, {type: mime});
      if (blob.size < 300) { status.textContent = '这段太短，没录到内容；说完整一点再试。'; return; }
      busy = true;
      longBtn.disabled = true;
      status.textContent = '正在准备识别（首次约 1–2 分钟），请不要关掉页面…';
      try {
        const audio = await blobBase64(blob);
        const res = await api('/flow/transcribe', 'POST', {audio, mime: blob.type || 'audio/webm'});
        if (!res.ok) throw Error(res.reason || '本地转写没成功。');
        const text = String(res.text || '').trim();
        if (!text) throw Error('没识别出文字，再录一次或直接打字。');
        appendText(ta, text);
        status.textContent = '识别完成，文字已进输入框；可以改一改再继续。';
      } catch (e) {
        status.textContent = (e.message || '本地转写失败') + ' 可以直接打字，内容一样能保存。';
      } finally {
        busy = false;
        longBtn.disabled = false;
      }
    }

    // 页面关闭/重渲染前释放麦克风，避免录音泄漏
    window.addEventListener('beforeunload', () => {
      if (sr) { try { sr.stop(); } catch (e) { /* 忽略 */ } }
      if (mediaRec && mediaRec.state !== 'inactive') { try { mediaRec.stop(); } catch (e) { /* 忽略 */ } }
      releaseStream();
    });
  }

  const SELECTORS = [
    'textarea[aria-label="今天记一句"]',          // 打卡
    '#intake-text',                               // 单对话口（首页 + 手机「记一条」浮层）；只在手机尺寸挂语音条，见 attach()
    'textarea[data-draft]',                       // 写稿分块
    'form#source-form textarea[name="text"]',     // 粘贴字幕/参考资料
    'form#new-knowledge-form textarea',           // 记录一条认知
    'textarea[name="question"]',                  // 知识库搜索框
    'textarea[name="q"]',                         // 成长系统里的 name=q 文本框（原对话窗的输入框已取消）
    'form[data-g-form="review"] textarea',        // 月度复盘
    'form[data-g-form="goal"] textarea',          // 目标描述/命题
    'form[data-g-form="setup"] textarea',         // 成长系统建立
    'form#creation-form textarea',                // 创作上下文
    'form#creation-write-form textarea'           // 参考二创
  ];

  let scheduled = false;
  function scan() {
    scheduled = false;
    for (const sel of SELECTORS) {
      let nodes;
      try { nodes = document.querySelectorAll(sel); } catch (e) { continue; }
      for (const el of nodes) attach(el);
    }
  }
  function schedule() {
    if (scheduled) return;
    scheduled = true;
    requestAnimationFrame(scan);
  }
  window.addEventListener('load', scan);
  try {
    new MutationObserver(schedule).observe(document.documentElement, {childList: true, subtree: true});
  } catch (e) { /* 旧环境没有 MutationObserver 时退回定时扫描 */ setInterval(scan, 1500); }
  if (document.readyState !== 'loading') scan();
})();
