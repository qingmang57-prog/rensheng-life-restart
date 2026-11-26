'use strict';
window.ipGrowthAsk = (() => {
  let progress = null, session = null, current = null, probe = null, busy = false, mounted = false, timeline = null;

  const e = value => esc(value);
  const icon = name => (window.growthIcon ? window.growthIcon(name) : '');
  const pending = () => busy;

  // 2026-11-18：问问自己/追问加语音输入，复用碎片记录同款：MediaRecorder 录音 → /growth/voice 本机转写 → 填入当前输入框。
  let voiceStream = null, voiceRecorder = null, voiceBlob = null, voiceTimer = null;
  const voiceReady = () => !!(state.boot?.capabilities?.local_asr_ready && window.isSecureContext && navigator.mediaDevices && navigator.mediaDevices.getUserMedia && window.MediaRecorder);

  function voiceControls() {
    const ok = voiceReady();
    return `<div class="g-voice-controls">`
      + `<button type="button" data-ga="voice-record" ${ok ? '' : 'disabled'}>开始录音</button>`
      + `<button type="button" data-ga="voice-transcribe" disabled>转成文字</button>`
      + `<span id="g-voice-status" role="status">${ok ? '仅用本机转写。' : '本地录音不可用；可以按下面「微信转文字」说一遍贴进来，或直接打字。'}</span>`
      + `</div>` + (ok ? '' : (window.growthVoiceGuideHtml ? window.growthVoiceGuideHtml() : ''));
  }

  function stopVoice() {
    clearTimeout(voiceTimer);
    if (voiceRecorder && voiceRecorder.state === 'recording') voiceRecorder.stop();
    if (voiceStream) voiceStream.getTracks().forEach(t => t.stop());
    voiceStream = null;
  }

  async function recordVoice(button) {
    if (voiceRecorder && voiceRecorder.state === 'recording') { stopVoice(); return; }
    try {
      voiceStream = await navigator.mediaDevices.getUserMedia({audio: true});
    } catch (err) {
      toast('没打开麦克风：' + (err.message || '请检查系统权限后重试'));
      return;
    }
    const stream = voiceStream, parts = [];
    voiceRecorder = new MediaRecorder(stream);
    voiceRecorder.addEventListener('dataavailable', ev => { if (ev.data.size) parts.push(ev.data); });
    voiceRecorder.addEventListener('stop', () => {
      stream.getTracks().forEach(t => t.stop());
      voiceBlob = new Blob(parts, {type: voiceRecorder.mimeType});
      const status = document.getElementById('g-voice-status');
      if (status) status.textContent = '录音已留在当前页面，点「转成文字」后再保存回答。';
      const t = document.querySelector('[data-ga="voice-transcribe"]');
      if (t) t.disabled = false;
      button.textContent = '重新录音';
    });
    voiceRecorder.start();
    button.textContent = '结束录音';
    const status = document.getElementById('g-voice-status');
    if (status) status.textContent = '正在录音，最长 90 秒。';
    voiceTimer = setTimeout(stopVoice, 90000);
  }

  async function transcribeVoice() {
    if (!voiceBlob) { toast('请先录一段语音。'); return; }
    if (voiceBlob.size > 8 * 1024 * 1024) { toast('录音超过 8MB，请缩短后重录。'); return; }
    const status = document.getElementById('g-voice-status');
    const audio = await new Promise((resolve, reject) => {
      const r = new FileReader();
      r.onload = () => resolve(r.result.split(',')[1]);
      r.onerror = reject;
      r.readAsDataURL(voiceBlob);
    });
    if (status) status.textContent = '正在本机转写，首次需要加载模型，请保留这个窗口…';
    try {
      const result = await api('/growth/voice', 'POST', {audio, mime: voiceBlob.type});
      const area = document.getElementById('g-probe-text') || document.getElementById('g-ask-text');
      if (area) {
        area.value += (area.value ? '\n\n' : '') + result.text;
        area.focus();
      }
      if (status) status.textContent = '转写文字已填入，请核对后保存。';
      voiceBlob = null;
    } catch (err) {
      if (status) status.textContent = '转写失败：' + err.message;
    }
  }

  async function refresh() {
    progress = await api('/growth/ask/progress');
    session = progress.active_session || null;
    current = null;
    probe = null;
    if (session) {
      const state = await api('/growth/ask/sessions/' + encodeURIComponent(session.id));
      session = state.session;
      current = state.current;
    }
    timeline = await api('/growth/timeline').catch(() => null);
    return view();
  }

  function moment(value) {
    const at = new Date(value);
    if (Number.isNaN(at.getTime())) return '';
    return at.toLocaleString('zh-CN', {timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit'});
  }

  function timelineView() {
    const items = (timeline && timeline.items) || [];
    if (!items.length) return '';
    const rows = items.slice(-12).reverse().map(item => `<li><time datetime="${e(item.at || '')}">${e(moment(item.at))}</time><div>${item.fragment_id ? `<button type="button" data-g="fragment" data-id="${e(item.fragment_id)}">${e(item.title)}</button>` : `<strong>${e(item.title)}</strong>`}<p>${e(item.text || '')}</p></div></li>`).join('');
    return `<section class="g-panel g-space"><div class="g-section-head"><div><h2>人生时间线</h2><p class="g-note">按真实回答时间排序，显示最近 ${Math.min(items.length, 12)} 条；点标题可回到对应碎片。</p></div></div><ol class="g-ask-timeline">${rows}</ol></section>`;
  }

  function view() {
    if (!progress) return '<section class="g-panel g-space"><p class="g-note">正在读取问答进度…</p></section>';
    const counts = progress.counts || {};
    const head = `<section class="g-panel g-ask-hero"><div><span class="g-eyebrow">${icon('brain')}问问自己</span><h2>先把经历问出来，再让系统接进目标</h2><p class="g-note">一次一题，可以跳过；跳过的题会进入待补池，之后可以继续。答案保存在本机。</p></div><div class="g-ask-stats">${[['已答', counts.answered || 0], ['待补', counts.skipped || 0], ['待投影', counts.pending || 0]].map(([label, n]) => `<div><span>${label}</span><strong>${n}</strong></div>`).join('')}</div></section>`;
    if (!session) {
      return head + `<section class="g-panel"><h3>${progress.unanswered ? '下一轮准备好了' : '这一版题库都问完了'}</h3><p class="g-note">${progress.unanswered ? '每轮最多 5 题，优先补你跳过的地方。' : '之后可以修改答案，也可以等题库更新。'}</p><button class="g-primary" data-ga="start" ${progress.unanswered ? '' : 'disabled'}>开始新一轮</button><p class="g-note g-space">${progress.has_goals ? '答案会自动进入对应支柱。' : '还没设定人生主线：答案会先保存，设定后自动进入碎片。'}</p></section>` + timelineView();
    }
    if (!current) {
      return head + `<section class="g-panel"><h3>这一轮完成了</h3><p class="g-note">可以开始下一轮，或先去搜索看看已经沉淀了什么。</p><div class="g-heading-actions"><button class="g-primary" data-ga="start" ${progress.unanswered ? '' : 'disabled'}>开始新一轮</button><button data-ga="end">先到这里</button></div></section>` + timelineView();
    }
    const position = Math.min((session.position || 0) + 1, (session.question_ids || []).length);
    const total = (session.question_ids || []).length || 1;
    const previous = current.previous || {};
    return head + `<section class="g-panel g-ask-card"><div class="g-ask-progress"><span>第 ${position} / ${total} 题</span><span>${e(current.pillar)} · ${e(current.level)}</span></div><h3 class="g-ask-question">${e(current.text)}</h3>${previous.text ? `<details class="g-ask-previous"><summary>查看上次回答</summary><pre>${e(previous.text)}</pre></details>` : ''}<textarea id="g-ask-text" rows="5" maxlength="40000" placeholder="想到什么写什么；具体的人、数字和转折，会帮你追一层。">${e(previous.text || '')}</textarea>${voiceControls()}<div class="g-ask-actions"><button class="g-primary" data-ga="answer">保存这条回答</button><button data-ga="empty">没有 / 不知道</button><button data-ga="skip">跳过这题</button><button class="g-danger" data-ga="end">先到这里</button></div><p class="g-note">跳过不是作答；“没有”也是信息，但不会生成碎片。</p></section>` + timelineView();
  }

  function probeView() {
    return `<section class="g-panel g-ask-card"><div class="g-ask-progress"><span>追问</span><span>${e(current.pillar)}</span></div><h3 class="g-ask-question">${e(probe.prompt)}</h3><p class="g-note">你刚才提到了值得深挖的细节。只追这一层，可以不答。</p><textarea id="g-probe-text" rows="4" maxlength="40000"></textarea>${voiceControls()}<div class="g-ask-actions"><button class="g-primary" data-ga="probe-save">保存追问</button><button data-ga="probe-skip">不追了</button></div></section>`;
  }

  function paint() {
    const host = document.getElementById('g-ask-view');
    if (!host) return;
    host.innerHTML = probe ? probeView() : view();
    const area = document.getElementById(probe ? 'g-probe-text' : 'g-ask-text');
    if (area && !probe) area.focus();
  }

  async function start() {
    if (pending()) return;
    busy = true;
    try {
      session = await api('/growth/ask/sessions', 'POST', {});
      await refresh();
      paint();
    } catch (err) {
      toast(err.message);
    } finally {
      busy = false;
    }
  }

  async function submit(status) {
    if (pending() || !current) return;
    const area = document.getElementById('g-ask-text');
    const value = area ? area.value.trim() : '';
    if (status === 'answered' && !value) {
      toast('先写一点内容，或选择“没有 / 不知道”。');
      return;
    }
    busy = true;
    try {
      const result = await api('/growth/ask/sessions/' + encodeURIComponent(session.id) + '/answers', 'POST', {
        question_id: current.id, status, text: status === 'answered' ? value : (value || '没有 / 不知道')
      });
      if (result.probe && result.answer) {
        probe = {prompt: result.probe, answerId: result.answer.id};
        paint();
      } else {
        await refresh();
        paint();
      }
    } catch (err) {
      toast(err.message);
    } finally {
      busy = false;
    }
  }

  async function saveProbe(skip = false) {
    if (pending() || !probe) return;
    const area = document.getElementById('g-probe-text');
    const value = skip ? '' : (area ? area.value.trim() : '');
    if (!skip && !value) {
      toast('不想追可以点“不追了”。');
      return;
    }
    busy = true;
    try {
      if (!skip) {
        await api('/growth/answers/' + encodeURIComponent(probe.answerId) + '/probes', 'POST', {answer: value});
      } else {
        await api('/growth/answers/' + encodeURIComponent(probe.answerId) + '/probes', 'POST', {skip: true});
      }
      probe = null;
      await refresh();
      paint();
    } catch (err) {
      toast(err.message);
    } finally {
      busy = false;
    }
  }

  async function end() {
    if (pending() || !session) return;
    busy = true;
    try {
      await api('/growth/ask/sessions/' + encodeURIComponent(session.id) + '/end', 'POST', {});
      await refresh();
      paint();
    } catch (err) {
      toast(err.message);
    } finally {
      busy = false;
    }
  }

  document.addEventListener('click', async ev => {
    const button = ev.target.closest('[data-ga]');
    if (!button || button.disabled || pending()) return;
    const action = button.dataset.ga;
    if (action === 'start') await start();
    if (action === 'answer') await submit('answered');
    if (action === 'empty') await submit('empty');
    if (action === 'skip') await submit('skipped');
    if (action === 'probe-save') await saveProbe(false);
    if (action === 'probe-skip') await saveProbe(true);
    if (action === 'end') await end();
    if (action === 'voice-record') await recordVoice(button);
    if (action === 'voice-transcribe') await transcribeVoice();
  });

  async function render() {
    return '<div id="g-ask-view">' + await refresh() + '</div>';
  }

  function mount() {
    if (mounted) return;
    mounted = true;
  }

  function unmount() {
    stopVoice();
    voiceBlob = null;
    probe = null;
    progress = null;
    session = null;
    current = null;
    timeline = null;
    mounted = false;
  }

  return {render, mount, unmount};
})();
