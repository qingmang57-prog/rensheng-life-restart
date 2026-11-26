/* 60 天打卡：页面渲染与交互。只做记录，不展示任何金额或扣款。
   2026-11-17 打卡重积累（t32）：重心从「连续 60 天」改成「你的积累节奏」——
   今天这句是第 N 条认知；断一天不算失败，随时回来接着攒；哈希链校验收进「可选」里，不再主打。 */
(function () {
  const CK_KEY = 'ip-checkin-profile';
  let last = null;
  let busy = false;

  // 注意：本包 CSP 为 style-src 'self'，内联 style / 注入 <style> 都会被拦（控制台报错）。
  // 所以积累导向的样式统一放外部 checkin.css（同目录、'self' 允许），JS 里不写任何内联样式；
  // 进度用 <progress value> 表达，也不用 style 属性。

  function esc(value) {
    return String(value == null ? '' : value).replace(/[&<>"']/g, c => (
      { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
    ));
  }

  function mmdd(date) { return String(date || '').slice(5); }

  function streak(days, today) {
    let count = 0;
    for (let i = (days || []).length - 1; i >= 0; i--) {
      const day = days[i];
      if (day.date > today) continue;
      if (day.date === today && day.state !== 'checked') continue;
      if (day.state === 'checked') count += 1;
      else break;
    }
    return count;
  }

  function stateOf(day) {
    if (day.state === 'checked') return '已记下';
    if (day.state === 'missed') return '空着';
    if (day.state === 'today') return '今天';
    return '未到';
  }

  function accum(data) {
    const stats = data.stats || {};
    const today = (data.days || []).find(d => d.date === data.today);
    const done = !!(today && today.state === 'checked');
    const checked = Number(stats.checked) || 0;
    const total = Number(stats.total) || 60;
    const nextNo = checked + (done ? 0 : 1);
    return `<div class="ck-accum" role="img" aria-label="已积累 ${checked} 条，周期 ${total} 天">
      <strong class="ck-accum-num">${checked}</strong>
      <span class="ck-accum-unit">条积累</span>
      ${done ? '' : `<span class="ck-accum-next">今天这句 → 第 ${nextNo} 条</span>`}
      <div class="ck-bar">
        <progress class="ck-bar-track" value="${checked}" max="${total}" aria-label="本周期已积累 ${checked} 天，共 ${total} 天"></progress>
        <div class="ck-bar-meta"><span>本周期 ${checked} / ${total} 天</span><span>积累是复利，不追连续</span></div>
      </div>
    </div>`;
  }

  async function load(profileId) {
    let pid = profileId || localStorage.getItem(CK_KEY) || '';
    let data;
    // 主线/命题：旅程要往哪儿走。读不到就只给「先去立主线」引导，不编造。
    const axisPromise = api('/growth/overview').then(g => {
      const root = (g && (g.goals || []).find(x => x.level === 'root')) || null;
      return root ? { title: root.title || '', thesis: root.thesis || '', milestone: root.milestone || '', thesis_history: root.thesis_history || [], milestone_detail: root.milestone_detail || null } : null;
    }).catch(() => null);
    try {
      data = await api('/checkin/state' + (pid ? '?profile_id=' + encodeURIComponent(pid) : ''));
    } catch (error) {
      if (!pid) throw error;
      localStorage.removeItem(CK_KEY);
      data = await api('/checkin/state');
    }
    data.axis = await axisPromise;
    last = data;
    if (data.profile) localStorage.setItem(CK_KEY, data.profile.id);
    return data;
  }

  // ---- 旅程口径（全部由已有数据算，不新造接口、不显示任何金额）----
  function journey(data) {
    const days = data.days || [];
    const stats = data.stats || {};
    const total = Number(stats.total) || 60;
    const today = days.find(d => d.date === data.today) || null;
    const walked = days.filter(d => d.state !== 'future').length;
    const checked = Number(stats.checked) || 0;
    const knowledge = days.filter(d => d.state === 'checked' && d.card_id).length;
    const content = days.filter(d => d.video || d.publish_url).length;
    const dayNo = today ? Number(today.index) || walked : Math.min(total, walked || total);
    const first = days.find(d => d.state === 'checked' && d.text) || null;
    const latest = [...days].reverse().find(d => d.state === 'checked' && d.text) || null;
    const done = !!stats.period_done || (!today && walked >= total);
    return { days, stats, total, today, walked, checked, knowledge, content, dayNo, first, latest, done,
             start: (data.profile && data.profile.start_date) || (days[0] && days[0].date) || '',
             end: (data.profile && data.profile.end_date) || (days[total - 1] && days[total - 1].date) || '' };
  }

  function dayProgress(data, j) {
    return `<section class="ck-dayline-row">
      <p class="ck-dayline"><b>第 ${j.dayNo} 天</b><span> / ${j.total}</span>${j.done ? ' <span class="ck-done-badge">60 天已走完</span>' : ''}</p>
      <progress class="ck-bar-track ck-slim-bar" value="${Math.min(j.walked, j.total)}" max="${j.total}"
        aria-label="已走 ${j.walked} 天，共 ${j.total} 天"></progress>
    </section>`;
  }

  function noProfile(data) {
    return `<div class="ck-page">
      <section class="ck-empty">
        <p class="ck-eyebrow">60 天 · 人生重启旅程</p>
        <h1>先定下这 60 天的起点</h1>
        <p class="ck-lead">填好姓名和起算日（这 60 天的第 1 天），之后每天来记一句。一台电脑可以管多位主理人。</p>
        <button class="ck-big" data-ck="new-profile" type="button">建立第一位主理人</button>
        <p class="ck-tip">数据只存本机；每一句都会生成一张知识卡，随时能回去看。</p>
      </section>
    </div>`;
  }

  /* 2026-11-17 t87：极简写＋明确提示。写一句才算打卡（积累魂不变，后端 required 不动），
     前端把负担降到底：快捷短语一键打卡、一个字也算、空提交友好引导、微信转文字语音引导
     （复用 t82 无麦引导口径）。不新增复杂交互。 */
  const CK_QUICK = ['今天的一个想法', '一个反思', '没想清楚', '今天很累'];
  const CK_HINT = '写一句才算打卡 · 一个字也算';
  const CK_NUDGE = '写一句才算打卡——写一个字也行，或点上面的快捷短语，一下就好。';

  function todayCard(data, j) {
    const stats = data.stats || {};
    const today = j.today;
    const done = !!(today && today.state === 'checked');
    const editable = today && (!stats.period_done || done);
    const value = done ? String(today.text || '') : '';
    const axis = data.axis || {};
    const main = axis.thesis || axis.title || '';
    // 主线关联只留一句轻文案；没有主线就不显示，不堆引导。
    const mainText = main.replace(/^[「『"']+|[」』"']+$/g, '').trim();
    const brick = mainText
      ? `<p class="ck-brick">为你的主线「${esc(mainText.length > 20 ? mainText.slice(0, 20) + '…' : mainText)}」添一块砖。</p>`
      : '';
    let form = '';
    if (!today) {
      form = `<p class="ck-note">${stats.period_done ? `60 天周期已于 ${esc(data.profile.end_date || '')} 走完，这一页只保留记录。` : '还没到起算日，到了那天再来记。'}</p>`;
    } else if (!editable) {
      form = '<p class="ck-note">60 天周期已结束，这一页只保留记录。</p>';
    } else {
      form = `<form id="ck-text-form" class="ck-write">
        <input type="hidden" name="profile_id" value="${esc(data.profile.id)}">
        <div class="ck-quick" role="group" aria-label="快捷短语：点一下直接打卡">${CK_QUICK.map(q => `<button class="ck-chip" type="button" data-ck="quick" data-text="${esc(q)}">${esc(q)}</button>`).join('')}</div>
        <textarea name="text" rows="3" maxlength="4000" placeholder="今天想记下哪一句？一个字也算。" aria-label="今天记一句">${esc(value)}</textarea>
        <div class="ck-write-actions">
          <button class="ck-sub ck-sub-strong" type="submit">${done ? '更新今天这一句' : '记下今天这一句'}</button>
          <span class="ck-write-hint" data-ck-hint>${CK_HINT}</span>
        </div>
      </form>
      <details class="ck-voice">
        <summary>想用说的？微信「转文字」粘贴进来也算</summary>
        <p>拿手机打开微信，对着对话框按住说话，说完长按这条语音点「转文字」，把文字复制回来粘进上面的写框就行。</p>
        <p class="ck-voice-soft">说不好、说断了、没头绪都没关系，一句一个字都算；实在不想说，直接打字也一样。</p>
      </details>
      ${done ? `<button class="ck-text-button" data-ck="delete" type="button">删除今天这条</button>` : ''}`;
    }
    const kept = streak(data.days, data.today);
    // 断更回坑：今天还没记、且距上次记录 ≥3 天（或第一句还没写但周期已走 ≥3 天）才出现；一句就够。
    const checkedDays = (data.days || []).filter(d => d.state === 'checked');
    const lastChecked = checkedDays[checkedDays.length - 1] || null;
    const gapDays = today ? Math.max(0, Number(today.index) - (lastChecked ? Number(lastChecked.index) : 0)) : 0;
    const comeback = (today && !done && (gapDays >= 3 || (!checkedDays.length && Number(today.index) >= 3)))
      ? `<p class="ck-return"><span class="ck-return-quote">「种一棵树最好的时间是十年前，其次是现在。」</span><span class="ck-return-line">${checkedDays.length ? `空了 ${gapDays} 天没关系——今天就是那个「现在」，记一句就接着走。` : '今天就是那个「现在」：写下第一句，这棵树就开始长了。'}</span></p>`
      : '';
    return `<section class="ck-card ck-today">
      ${comeback}
      ${form}
      ${brick}
      ${kept >= 2 ? `<p class="ck-rhythm">最近连着 ${kept} 天 · 断了也没关系</p>` : ''}
    </section>`;
  }

  function calendar(data) {
    const cells = data.days.map(day => `<button class="ck-day ck-${day.state}" type="button"
      data-ck="day" data-date="${esc(day.date)}" title="${esc(day.date + ' ' + day.weekday + ' · ' + stateOf(day))}">
        <span class="ck-day-index">${day.index}</span>
        <span class="ck-day-date">${mmdd(day.date)}</span>
      </button>`).join('');
    return `<section class="ck-card">
      <div class="ck-card-head">
        <h2>60 天进度</h2>
        <div class="ck-legend">
          <span class="ck-key ck-key-checked">已打卡</span>
          <span class="ck-key ck-key-missed">漏卡</span>
          <span class="ck-key ck-key-future">未到</span>
        </div>
      </div>
      <div class="ck-cal">${cells}</div>
      ${data.stats.missed_dates.length ? `<p class="ck-note ck-note-warn">漏卡日期：${esc(data.stats.missed_dates.join('、'))}</p>` : ''}
    </section>`;
  }

  function recentCard(data) {
    const items = (data.days || []).filter(d => d.state === 'checked' && d.text)
      .slice(-7).reverse();
    if (!items.length) return '';
    return `<section class="ck-card ck-recent">
      <div class="ck-card-head">
        <h2>最近 7 条积累</h2>
        <span class="ck-note">每一句都成了一张知识卡</span>
      </div>
      <ol class="ck-recent-list">${items.map(day => `<li class="ck-recent-item">
        <span class="ck-recent-date">${mmdd(day.date)} ${esc(day.weekday)}</span>
        <span class="ck-recent-text">${esc(day.text)}</span>
        ${day.card_id ? `<a class="ck-recent-link" href="#/knowledge/${encodeURIComponent(day.card_id)}">知识卡</a>` : ''}
      </li>`).join('')}</ol>
    </section>`;
  }

  function chainCard(data) {
    const chain = data.chain || {};
    if (chain.ok) {
      // 日常不需要对账：校验收进「可选」，不再占据页面主打位置；数据与校验能力都在。
      return `<details class="ck-details ck-chain-details">
        <summary>记录校验（可选）</summary>
        <p class="ck-chain-ok">本机记录完整 · ${chain.records} 条 · 需要核对时（例如导出、换机前）再展开逐条校验。</p>
      </details>`;
    }
    // 真出问题时才升级成可见警告——这是数据完整性，不是打卡断更。
    return `<p class="ck-chain ck-chain-bad">记录校验未通过：${esc(chain.reason || '未知原因')}。请先备份数据并核对日志。</p>`;
  }

  function profileEditCard(data) {
    return `<details class="ck-details">
      <summary>修改档案</summary>
      <form id="ck-profile-edit-form" class="ck-form">
        <input type="hidden" name="profile_id" value="${esc(data.profile.id)}">
        <label class="ck-field"><span>主理人姓名</span><input type="text" name="name" maxlength="40" value="${esc(data.profile.name)}"></label>
        <label class="ck-field"><span>起算日（已有打卡后不可改）</span><input type="text" name="start_date" placeholder="YYYY-MM-DD" value="${esc(data.profile.start_date)}"></label>
        <label class="ck-field"><span>备注（可选）</span><input type="text" name="note" maxlength="200" value="${esc(data.profile.note || '')}"></label>
        <button class="ck-sub" type="submit">保存档案</button>
      </form>
    </details>`;
  }

  function profilesBar(data) {
    const chips = data.profiles.map(p => `<button class="ck-chip ${p.id === data.profile.id ? 'active' : ''}"
      type="button" data-ck="switch" data-id="${esc(p.id)}">${esc(p.name)}</button>`).join('');
    return `<div class="ck-profile-bar">${chips}<button class="ck-chip ck-chip-add" type="button" data-ck="new-profile">＋ 代管另一位</button></div>`;
  }

  function beforeAfter(data, j) {
    if (!j.first) return '';
    const first = j.first, latest = j.latest || first;
    return `<section class="ck-card ck-beforeafter">
      <div class="ck-card-head"><h2>第 1 天的你 vs 今天的你</h2><span class="ck-note">积累会说话</span></div>
      <div class="ck-recent-list">
        <div class="ck-recent-item"><span class="ck-recent-date">第 1 天</span><span class="ck-recent-text">「${esc(first.text)}」</span></div>
        <div class="ck-recent-item"><span class="ck-recent-date">今天</span><span class="ck-recent-text">「${esc(latest.text)}」</span></div>
      </div>
      <p class="ck-note">这段时间：积累 ${j.knowledge} 条认知 · 出片 ${j.content} 条</p>
    </section>`;
  }
  function trajectory(data, j) {
    const axis = data.axis || {};
    const evo = (axis.thesis_history || []).length;
    const m = axis.milestone_detail || {};
    const pct = Number(m.progress || 0);
    return `<section class="ck-card ck-trajectory">
      <div class="ck-card-head"><h2>60 天改变轨迹</h2><span class="ck-note">不只是打卡次数</span></div>
      <p class="ck-brick">第 ${j.dayNo} / ${j.total} 天 · 已积累 ${j.knowledge} 条认知 · 出片 ${j.content} 条${evo ? ` · 命题进化 ${evo} 次` : ''}</p>
      <progress class="ck-bar-track" value="${pct}" max="100" aria-label="60 天目标进度 ${pct}%"></progress>
      <p class="ck-note">${axis.thesis ? `当前命题：${esc(axis.thesis)}` : '还没有命题；目标照旧，可以先积累。'}${m.state === 'done' ? ' · 里程碑已完成' : ''}</p>
    </section>`;
  }
  function page(data) {
    if (!data.profile) return noProfile(data);
    const p = data.profile;
    const j = journey(data);
    return `<div class="ck-page ck-simple">
      <header class="ck-head ck-head-quiet">
        <p class="ck-eyebrow">60 天 · 人生重启旅程</p>
        <h1>花 60 天，重启自己人生</h1>
        <p class="ck-motto">「种一棵树最好的时间是十年前，其次是现在。」</p>
      </header>
      ${dayProgress(data, j)}
      ${beforeAfter(data, j)}
      ${trajectory(data, j)}
      ${todayCard(data, j)}
      ${calendar(data)}
      <details class="ck-details ck-more">
        <summary>更多 · 主理人 / 记录 / 校验</summary>
        <div class="ck-more-body">
          ${profilesBar(data)}
          ${recentCard(data)}
          ${chainCard(data)}
          ${profileEditCard(data)}
          <div class="ck-export">
            <button class="ck-sub" type="button" data-ck="csv">导出打卡记录 CSV</button>
            <button class="ck-sub" type="button" data-ck="html">打印打卡记录（A4）</button>
          </div>
          <p class="ck-period">${esc(p.name)} · ${esc(p.start_date)} 至 ${esc(p.end_date)}${data.stats.period_done ? ' · 60 天已结束' : ''}</p>
        </div>
      </details>
    </div>`;
  }

  async function render(profileId) {
    const data = await load(profileId);
    $('#breadcrumb').textContent = '60 天 · 人生重启旅程';
    return page(data);
  }

  function stateLabel(day) {
    return `<span class="ck-state ck-state-${day.state}">${stateOf(day)}</span>`;
  }

  function openDay(date) {
    if (!last || !last.profile) return;
    const day = (last.days || []).find(d => d.date === date);
    if (!day) return;
    const video = day.video || {};
    const isToday = date === last.today;
    const body = `<div class="ck-detail">
      <p class="ck-detail-date">${esc(day.date)} ${esc(day.weekday)} · ${stateLabel(day)}</p>
      ${day.state === 'checked' ? `
        ${day.text ? `<div class="ck-detail-row"><span>记下的</span><b>${esc(day.text)}</b></div>` : ''}
        ${day.card_id ? `<div class="ck-detail-row"><span>知识卡</span><a class="link" href="#/knowledge/${encodeURIComponent(day.card_id)}">打开这一天的知识卡</a></div>` : ''}
        ${video && video.filename ? `<div class="ck-detail-row"><span>成片</span><b>${esc(video.filename)}</b></div>` : ''}
        ${day.publish_url ? `<div class="ck-detail-row"><span>发布链接</span><a class="link" href="${esc(day.publish_url)}" target="_blank" rel="noopener">${esc(day.publish_url)}</a></div>` : ''}
        ${day.note ? `<div class="ck-detail-row"><span>备注</span><b>${esc(day.note)}</b></div>` : ''}
        <div class="ck-detail-row"><span>打卡时间</span><b>${esc(String(day.recorded_at || '').replace('T', ' '))}</b></div>
        <div class="ck-detail-row"><span>记录序号</span><b>${esc(day.seq || '')}</b></div>
        ${isToday ? `<div class="ck-detail-actions"><button class="ck-sub" type="button" data-ck="focus-today">去修改</button>
          <button class="ck-text-button" type="button" data-ck="delete">删除</button></div>` : ''}
      ` : `<p class="ck-note">${day.state === 'missed' ? '这一天还空着，随时可以回来记一句。' : day.state === 'today' ? '今天还没记，写下这一句就是新的一条积累。' : '还没到这一天。'}</p>`}
    </div>`;
    modal(`第 ${day.index} 天`, body);
  }

  function focusToday() {
    $('#modal').close();
    const input = document.querySelector('#ck-text-form textarea');
    if (input) {
      input.focus();
      input.scrollIntoView({ block: 'center', behavior: 'smooth' });
    }
  }

  function openNewProfile() {
    const body = `<form id="ck-profile-form" class="ck-form">
      <label class="ck-field"><span>主理人姓名</span><input type="text" name="name" maxlength="40" required placeholder="例：林一"></label>
      <label class="ck-field"><span>起算日（开始打卡那天）</span><input type="text" name="start_date" required placeholder="YYYY-MM-DD" value="${esc((last && last.today) || '')}"></label>
      <label class="ck-field"><span>备注（可选）</span><input type="text" name="note" maxlength="200" placeholder="例：代跑·抖音号 A"></label>
      <button class="ck-sub ck-sub-strong" type="submit">建立档案</button>
    </form>`;
    modal('建立主理人档案', body);
  }

  async function refresh() {
    if (typeof route === 'function') { await route(); }
  }

  async function submitText(form) {
    const values = Object.fromEntries(new FormData(form));
    const text = String(values.text || '').trim();
    if (!text) {
      // t87：空提交不冷报错——友好引导写一句或点快捷短语，不发请求。
      const hint = form.querySelector('[data-ck-hint]');
      if (hint) { hint.textContent = CK_NUDGE; hint.classList.add('ck-write-hint-nudge'); }
      const input = form.querySelector('textarea');
      if (input) input.focus();
      return;
    }
    const wasChecked = !!(last && (last.days || []).some(d => d.date === last.today && d.state === 'checked'));
    const result = await api('/checkin/checkin', 'POST', { profile_id: values.profile_id, text });
    toast(wasChecked ? '已更新，这一天的知识卡也同步更新了。' : '已记下，并生成了一张知识卡。');
    // POST 已返回完整 state：直接局部重绘，不再整页 route()（省一次等云端的 bootstrap）。
    const main = document.querySelector('#main');
    if (result && result.days && main && location.hash.startsWith('#/checkin')) {
      result.axis = last && last.axis;
      last = result;
      main.innerHTML = page(result);
      if (typeof decorateCheckin === 'function') decorateCheckin();
      return;
    }
    await refresh();
  }

  async function submitNewProfile(form) {
    const values = Object.fromEntries(new FormData(form));
    const result = await api('/checkin/profiles', 'POST', { name: values.name, start_date: values.start_date, note: values.note });
    localStorage.setItem(CK_KEY, result.profile.id);
    $('#modal').close();
    toast('档案已建立，今天就可以打卡。');
    await refresh();
  }

  async function submitProfileEdit(form) {
    const values = Object.fromEntries(new FormData(form));
    await api('/checkin/profiles/' + encodeURIComponent(values.profile_id), 'PATCH', {
      name: values.name, start_date: values.start_date, note: values.note,
    });
    $('#modal').close();
    toast('档案已保存。');
    await refresh();
  }

  async function removeToday() {
    if (!last || !last.profile) return;
    // 2026-11-18 t142：原生 confirm → 页内确认条（危险操作保留二次确认）。
    const host = document.getElementById('modal-body') || document.getElementById('modal');
    const ok = await confirmBar(host, '删除今天这条？删除后今天回到「还没记」，可以再写一句。', {okText: '删除今天', cancelText: '不删', danger: true});
    if (!ok) return;
    await api('/checkin/void', 'POST', { profile_id: last.profile.id, date: last.today });
    $('#modal').close();
    toast('今天的打卡已删除。');
    await refresh();
  }

  function exportUrl(fmt) {
    return media('/api/checkin/settlement?profile_id=' + encodeURIComponent(last.profile.id) + '&format=' + fmt);
  }

  document.addEventListener('click', async event => {
    const el = event.target.closest('[data-ck]');
    if (!el || el.disabled || busy) return;
    const action = el.dataset.ck;
    if (!['delete', 'day', 'switch', 'new-profile', 'csv', 'html', 'focus-today', 'quick'].includes(action)) return;
    if (action === 'html') { event.preventDefault(); window.open(exportUrl('html'), '_blank'); return; }
    if (action === 'csv') {
      event.preventDefault();
      if (!last || !last.profile) return;
      window.location.href = exportUrl('csv');
      return;
    }
    if (action === 'switch') {
      event.preventDefault();
      localStorage.setItem(CK_KEY, el.dataset.id || '');
      await refresh();
      return;
    }
    event.preventDefault();
    busy = true;
    try {
      if (action === 'new-profile') openNewProfile();
      else if (action === 'focus-today') focusToday();
      else if (action === 'delete') await removeToday();
      else if (action === 'day') openDay(el.dataset.date);
      else if (action === 'quick') {
        // t87：快捷短语一键打卡——填入即提交，一步完成；走同一条 submitText 成卡路径。
        const form = el.closest('form');
        const input = form && form.querySelector('textarea');
        if (form && input) { input.value = el.dataset.text || ''; await submitText(form); }
      }
    } catch (e) {
      toast(e.message);
    } finally {
      busy = false;
    }
  });

  document.addEventListener('input', event => {
    const input = event.target.closest('#ck-text-form textarea');
    if (!input) return;
    const form = input.closest('form');
    const button = form && form.querySelector('button[type="submit"]');
    const hint = form && form.querySelector('[data-ck-hint]');
    // t87：按钮常可点（空提交走友好引导）；不再按长度催「再多说一句」，一个字也算。
    if (button) button.disabled = false;
    if (hint && hint.classList.contains('ck-write-hint-nudge')) {
      hint.classList.remove('ck-write-hint-nudge');
      hint.textContent = CK_HINT;
    }
  });

  document.addEventListener('submit', async event => {
    const form = event.target;
    const handler = {
      'ck-text-form': submitText,
      'ck-profile-form': submitNewProfile,
      'ck-profile-edit-form': submitProfileEdit,
    }[form.id];
    if (!handler) return;
    event.preventDefault();
    if (busy) return;
    busy = true;
    try { await handler(form); } catch (e) { toast(e.message); } finally { busy = false; }
  });

  window.ipCheckin = { render };
})();
