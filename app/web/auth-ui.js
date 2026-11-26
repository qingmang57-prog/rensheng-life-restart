'use strict';
// 登录门禁（2026-11-14）：登录 / 邮箱验证码注册 / 邮箱找回 / 多设备会话。
// 账号走云端 api.jidixh.com（见 auth_service.py），本机只缓存登录态；
// 未登录时门禁默认遮住主界面，登录成功后才放行。
window.ipAuth = (() => {
  let mode = 'login', busy = false;
  const timers = {};
  const EMAIL_RE = /^[^@\s]+@[^@\s]+\.[^@\s]{2,}$/;
  const $ = id => document.getElementById(id);

  const gate = () => $('auth-gate');
  // 2026-11-17 t99：门禁检查态不再吞全站鼠标（检查窗口期也能点/滚），
  // 登录态恢复后统一隐藏门禁；CSP 允许 CSSOM，不写内联 style。
  const hideGate = () => { const g = gate(); if (!g) return; g.hidden = true; g.classList.remove('is-checking'); };
  try {
    const sheet = new CSSStyleSheet();
    sheet.insertRule('.auth-gate.is-checking{pointer-events:none}', 0);
    document.adoptedStyleSheets = [...document.adoptedStyleSheets, sheet];
  } catch (err) { /* 老浏览器不支持 adoptedStyleSheets 时保持原样，不影响登录 */ }
  const msg = () => $('auth-msg');
  const user = () => $('auth-user');
  const pass = () => $('auth-pass');
  const submit = () => $('auth-submit');

  // 注册协议勾选（2026-11-17，PIPL 告知同意）：注册必须勾选《服务协议》《隐私政策》。
  // 勾选行由本文件动态注入 #auth-register-fields（index.html 不在本任务写域），
  // 切到登录态时随整块 hidden 隐藏。样式复用现有 pay-agree-row / pay-agree /
  // auth-switch-btn / auth-hint，不新增 CSS、不写内联 style（CSP style-src 'self'）。
  const LEGAL_VERSION = 'v1.0 · 2026-11-16';
  const AGREE_MSG = '请先勾选同意《服务协议》与《隐私政策》，再创建账号。';

  function legalLink(doc, title) {
    const a = document.createElement('a');
    a.className = 'auth-switch-btn';
    a.href = '/legal/' + doc + '.html';
    a.target = '_blank';
    a.rel = 'noopener';
    a.textContent = title;
    return a;
  }

  function ensureAgreeRow() {
    const box = $('auth-register-fields');
    if (!box || $('auth-agree-row')) return;
    const row = document.createElement('div');
    row.className = 'pay-agree-row';
    row.id = 'auth-agree-row';
    const label = document.createElement('label');
    label.className = 'pay-agree';
    const check = document.createElement('input');
    check.type = 'checkbox';
    check.id = 'auth-agree';
    check.name = 'auth-agree';
    const text = document.createElement('span');
    text.textContent = '我已阅读并同意';
    label.append(check, text);
    const and = document.createElement('span');
    and.textContent = '与';
    const ver = document.createElement('span');
    ver.className = 'auth-hint';
    ver.textContent = '（' + LEGAL_VERSION + '）';
    row.append(label, legalLink('service', '《服务协议》'), and,
               legalLink('privacy', '《隐私政策》'), ver);
    box.appendChild(row);
  }

  function showMsg(text, ok) {
    const el = msg();
    el.textContent = text || '';
    el.classList.toggle('is-ok', !!ok);
  }

  function setMode(next) {
    mode = next;
    document.querySelectorAll('[data-auth-tab]').forEach(btn => {
      const on = btn.dataset.authTab === mode;
      btn.classList.toggle('is-active', on);
      btn.setAttribute('aria-selected', on ? 'true' : 'false');
    });
    $('auth-register-fields').hidden = mode !== 'register';
    const label = $('auth-user-label');
    if (label) label.textContent = mode === 'register' ? '账号' : '账号或邮箱';
    user().placeholder = mode === 'register' ? '给自己起个名字' : '账号名或注册邮箱';
    const sw = $('auth-switch');
    if (sw) sw.hidden = mode !== 'register';
    const resetEntry = $('auth-switch-reset');
    if (resetEntry) resetEntry.hidden = true;
    const forgot = $('auth-forgot');
    if (forgot) forgot.hidden = mode === 'register';
    const tip = $('auth-tip');
    if (tip) tip.hidden = mode === 'register';
    pass().autocomplete = mode === 'register' ? 'new-password' : 'current-password';
    submit().textContent = mode === 'register' ? '创建账号并进入' : '进入系统';
    showMsg('');
    user().focus();
  }

  function showReset(on) {
    $('auth-form').hidden = on;
    $('auth-reset-form').hidden = !on;
    if (on) $('reset-email').focus();
    else user().focus();
  }

  function startCountdown(btn, seconds) {
    const total = seconds || 60;
    if (!btn.dataset.label) btn.dataset.label = btn.textContent;
    let left = total;
    btn.disabled = true;
    btn.textContent = left + 's 后重发';
    clearInterval(timers[btn.id]);
    timers[btn.id] = setInterval(() => {
      left -= 1;
      if (left <= 0) {
        clearInterval(timers[btn.id]);
        btn.textContent = btn.dataset.label;
        btn.disabled = false;
      } else {
        btn.textContent = left + 's 后重发';
      }
    }, 1000);
  }

  async function sendCode(purpose, emailInput, btn, out) {
    const address = (emailInput.value || '').trim();
    if (!address) {
      out.textContent = '先填邮箱。';
      out.classList.remove('is-ok');
      emailInput.focus();
      return;
    }
    btn.disabled = true;
    out.textContent = '正在发送…';
    out.classList.remove('is-ok');
    try {
      await api('/auth/email-code', 'POST', {email: address, purpose});
      out.textContent = '验证码已发出，10 分钟内有效。';
      out.classList.add('is-ok');
      startCountdown(btn);
    } catch (err) {
      out.textContent = err.message || '没发出去，再试一次。';
      out.classList.remove('is-ok');
      btn.disabled = false;
    }
  }

  // 提交前即时校验：只在失焦/重新输入时提示，错误沿用固定的 auth-msg 位置，不撑高版面。
  function bindLiveCheck() {
    const email = $('auth-email');
    const pwd = pass();
    if (email) {
      email.addEventListener('blur', () => {
        if (mode !== 'register') return;
        const value = email.value.trim();
        if (value && !EMAIL_RE.test(value)) showMsg('邮箱格式不对，请再检查一次。');
      });
      email.addEventListener('input', () => {
        if (msg().textContent === '邮箱格式不对，请再检查一次。' && EMAIL_RE.test(email.value.trim())) showMsg('');
      });
    }
    pwd.addEventListener('blur', () => {
      if (pwd.value && pwd.value.length < 8) showMsg('密码至少 8 位。');
    });
    pwd.addEventListener('input', () => {
      if (msg().textContent === '密码至少 8 位。' && pwd.value.length >= 8) showMsg('');
    });
  }

  // 首启欢迎屏要叫出名字：登录/注册后把账号名放进会话，并广播一次（晚挂载的页面也能补上）。
  function rememberName(person) {
    let name = '';
    try {
      name = String((person && (person.nickname || person.username)) || '').trim();
      if (!name) return;
      sessionStorage.setItem('ip.user-name', name);
    } catch (err) {
      return;
    }
    document.dispatchEvent(new CustomEvent('ip:user', {detail: {name}}));
  }

  async function init() {
    // 门禁默认就盖着；先问登录态，已登录才放行，未登录停在登录/注册页。
    // 品牌名并行补，别让门禁等 bootstrap（它比较慢）。
    const _slot = (window.__bootPrefetch && Date.now() - window.__bootPrefetch.at < 5000) ? window.__bootPrefetch : (window.__bootPrefetch = {at: Date.now(), p: api('/bootstrap').catch(() => null)});
    const bootP = _slot.p;
    const brandP = bootP.then(b => (b && b.brand_name) || '人生重启系统').catch(() => null);
    const state = await api('/auth/me').catch(() => ({authenticated: false, has_users: false}));
    const logoutBtn = $('auth-logout');
    const devicesBtn = $('auth-devices');
    if (logoutBtn) logoutBtn.hidden = !state.authenticated;
    if (devicesBtn) devicesBtn.hidden = !state.authenticated;
    if (state.authenticated) {
      hideGate();
      rememberName(state.user);
      brandP.then(name => { if (name) document.title = name + ' · 工作台'; });
      return;
    }
    brandP.then(name => {
      if (!name) return;
      $('auth-brand').textContent = name;
      document.title = name;
    });
    setMode(state.has_users ? 'login' : 'register');
    gate().classList.remove('is-checking');
    gate().hidden = false;
    user().focus();
  }

  async function onSubmit(event) {
    event.preventDefault();
    if (busy) return;
    const name = user().value.trim();
    const word = pass().value;
    if (!name) { showMsg('先给自己起个名字。'); user().focus(); return; }
    if (word.length < 8) { showMsg('密码至少 8 位。'); pass().focus(); return; }
    const body = {username: name, password: word};
    if (mode === 'register') {
      const address = $('auth-email').value.trim();
      const code = $('auth-code').value.trim();
      if (!address) { showMsg('注册要填邮箱，用来收验证码和找回密码。'); $('auth-email').focus(); return; }
      if (!EMAIL_RE.test(address)) {
        showMsg('邮箱格式不对，请再检查一次。');
        $('auth-email').focus();
        return;
      }
      if (!/^\d{6}$/.test(code)) { showMsg('验证码是 6 位数字。'); $('auth-code').focus(); return; }
      // 协议闸门：没勾选就不发注册请求（服务端 /api/auth/register 另有 agreed 强制校验兜底）。
      const agree = $('auth-agree');
      if (!agree || !agree.checked) {
        showMsg(AGREE_MSG);
        if (agree) agree.focus();
        return;
      }
      body.email = address;
      body.code = code;
      body.agreed = true;  // 勾选结果一并送服务端，防绕过接口直接注册。
    }
    busy = true;
    submit().disabled = true;
    submit().textContent = mode === 'register' ? '正在创建…' : '正在进入…';
    showMsg('');
    try {
      const path = mode === 'register' ? '/auth/register' : '/auth/login';
      const result = await api(path, 'POST', body);
      if (mode === 'register') {
        // 回显云端实际落库的用户名，避免用户以为叫 A、实际叫 B。
        const created = (result && result.user && result.user.username) || '';
        rememberName(result && result.user);
        showMsg(created ? '已创建账号：' + created + '，正在进入…' : '账号已创建，正在进入…', true);
        await new Promise(done => setTimeout(done, 1600));
      }
      // 2026-11-25（方案 A）：登录通过后落在对话页（打开即对话）；注册成功后仍先进引导（十二条问答）入口，由 app.js 路由接管
      const target = mode === 'register' ? '#/start' : '#/chat';
      if (location.hash !== target) location.hash = target;
      location.reload();  // 成功：整页刷新，重新引导工作台
    } catch (err) {
      const code = String(err.code || '');
      const text = err.message || '没有成功，再试一次。';
      const exists = code === 'ACCOUNT_EXISTS' || /已注册|已被占用|已存在/.test(text);
      if (mode === 'register' && exists) {
        // 云端 409：邮箱或账号名已被占用。说清原因，并当场给出登录/找回入口。
        showMsg(/邮箱/.test(text) ? '该邮箱已注册，请直接登录，或用邮箱找回密码。' : text);
        const sw = $('auth-switch');
        if (sw) sw.hidden = false;
        const resetEntry = $('auth-switch-reset');
        if (resetEntry) resetEntry.hidden = false;
      } else if (mode === 'login' && /账号或密码|密码不对|用户不存在|凭据/.test(text)) {
        showMsg(text + '可改用注册邮箱登录，或用邮箱找回密码。');
      } else {
        showMsg(text);  // 注册其它失败（用户名被占用/验证码错/格式错）照实显示云端原因
      }
    } finally {
      busy = false;
      submit().disabled = false;
      submit().textContent = mode === 'register' ? '创建账号并进入' : '进入系统';
    }
  }

  async function onResetSubmit(event) {
    event.preventDefault();
    if (busy) return;
    const out = $('reset-msg');
    const address = $('reset-email').value.trim();
    const code = $('reset-code').value.trim();
    const word = $('reset-pass').value;
    out.classList.remove('is-ok');
    if (!address) { out.textContent = '先填注册邮箱。'; $('reset-email').focus(); return; }
    if (!/^\d{6}$/.test(code)) { out.textContent = '验证码是 6 位数字。'; $('reset-code').focus(); return; }
    if (word.length < 8) { out.textContent = '新密码至少 8 位。'; $('reset-pass').focus(); return; }
    busy = true;
    $('reset-submit').disabled = true;
    out.textContent = '正在重设…';
    try {
      await api('/auth/password/reset', 'POST', {email: address, code, password: word});
      out.textContent = '密码已重设，已登录的设备都退出了，请用新密码登录。';
      out.classList.add('is-ok');
      setTimeout(() => { showReset(false); showMsg('密码已重设，请用新密码登录。', true); }, 1200);
    } catch (err) {
      out.textContent = err.message || '没有成功，再试一次。';
    } finally {
      busy = false;
      $('reset-submit').disabled = false;
    }
  }

  async function onLogout() {
    try { await api('/auth/logout', 'POST', {}); } catch (err) { /* 已经失效也照样刷新 */ }
    location.reload();
  }

  function formatTime(ts) {
    if (!ts) return '—';
    const d = new Date(ts * 1000);
    if (Number.isNaN(d.getTime())) return '—';
    return d.toLocaleString('zh-CN', {hour12: false});
  }

  function shortDevice(text) {
    const value = String(text || '未知设备');
    return value.length > 46 ? value.slice(0, 46) + '…' : value;
  }

  let currentSessionId = null;

  function renderSessions(items) {
    const list = $('auth-devices-list');
    const out = $('auth-devices-msg');
    list.replaceChildren();
    currentSessionId = null;
    if (!items.length) { out.textContent = '没有有效会话。'; return; }
    out.textContent = '';
    items.forEach(item => {
      if (item.current) currentSessionId = item.id;
      const li = document.createElement('li');
      li.className = 'auth-device';
      const info = document.createElement('div');
      info.className = 'auth-device-info';
      const name = document.createElement('span');
      name.className = 'auth-device-name';
      name.textContent = shortDevice(item.device);
      if (item.current) {
        const tag = document.createElement('span');
        tag.className = 'auth-device-tag';
        tag.textContent = '当前';
        name.appendChild(tag);
      }
      const meta = document.createElement('span');
      meta.className = 'auth-device-meta';
      meta.textContent = '最近活跃 ' + formatTime(item.last_seen_at) + ' · 到期 ' + formatTime(item.expires_at);
      info.append(name, meta);
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'auth-device-revoke';
      btn.textContent = item.current ? '撤销这台' : '撤销';
      btn.addEventListener('click', () => revokeSession(item, btn));
      li.append(info, btn);
      list.appendChild(li);
    });
  }

  async function loadSessions() {
    $('auth-devices-msg').textContent = '正在读取…';
    try {
      const res = await api('/auth/sessions');
      renderSessions(res.items || []);
    } catch (err) {
      $('auth-devices-msg').textContent = err.message || '读取失败。';
    }
  }

  async function revokeSession(item, btn) {
    // 2026-11-18 t142：原生 confirm → 复用 t54 页内确认条；危险操作保留二次确认。
    const host = (btn && (btn.closest('.rule') || btn.parentElement)) || document.getElementById('auth-devices');
    const ok = await confirmBar(host, '撤销后这台设备要重新登录，继续吗？', {okText: '撤销设备', cancelText: '取消', danger: true});
    if (!ok) return;
    btn.disabled = true;
    try {
      await api('/auth/sessions/revoke', 'POST', {id: item.id});
      if (item.current) { location.reload(); return; }
      await loadSessions();
      $('auth-devices-msg').textContent = '已撤销一台设备。';
    } catch (err) {
      $('auth-devices-msg').textContent = err.message || '撤销失败。';
      btn.disabled = false;
    }
  }

  async function refreshSession() {
    // 云端没有单独的续期接口：每次列设备都会刷新 last_seen，列表即最新状态。
    await loadSessions();
    if (!$('auth-devices-msg').textContent) $('auth-devices-msg').textContent = '已刷新。';
  }

  function openDevices() {
    const dialog = $('auth-devices-dialog');
    if (!dialog) return;
    if (typeof dialog.showModal === 'function') dialog.showModal();
    else dialog.setAttribute('open', '');
    loadSessions();
  }

  function closeDevices() {
    const dialog = $('auth-devices-dialog');
    if (!dialog) return;
    if (typeof dialog.close === 'function') dialog.close();
    else dialog.removeAttribute('open');
  }

  document.addEventListener('click', ev => {
    const tab = ev.target.closest('[data-auth-tab]');
    if (tab) setMode(tab.dataset.authTab);
    if (ev.target.closest('#auth-forgot')) {
      showReset(true);
    }
    if (ev.target.closest('#auth-switch-reset')) {
      showReset(true);
      $('reset-email').value = ($('auth-email').value || '').trim();
    }
    if (ev.target.closest('#auth-back')) { showReset(false); setMode('login'); }
    if (ev.target.closest('#auth-send')) sendCode('register', $('auth-email'), $('auth-send'), msg());
    if (ev.target.closest('#reset-send')) sendCode('reset', $('reset-email'), $('reset-send'), $('reset-msg'));
    if (ev.target.closest('#auth-logout')) onLogout();
    if (ev.target.closest('#auth-devices')) openDevices();
    if (ev.target.closest('#auth-devices-close')) closeDevices();
    if (ev.target.closest('#auth-devices-refresh')) refreshSession();
    const dialog = $('auth-devices-dialog');
    if (dialog && ev.target === dialog) closeDevices();
  });

  document.addEventListener('submit', ev => {
    if (ev.target.id === 'auth-form') onSubmit(ev);
    if (ev.target.id === 'auth-reset-form') onResetSubmit(ev);
  });

  // 勾上之后即时清掉协议提示，和邮箱/密码的即时校验同一手感。
  document.addEventListener('change', ev => {
    if (ev.target.id === 'auth-agree' && ev.target.checked && msg().textContent === AGREE_MSG) showMsg('');
  });

  ensureAgreeRow();
  bindLiveCheck();
  init();

  return {init};
})();
