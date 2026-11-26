// 2026-11-17（t47）：发布与复盘 · 多平台发布工作台（合规人工引导 + 四件套）
// 只做三件事：展示能力位 / 拼四件套文本（复制由前端做）/ 导航到创作页 + 登记入口。
// 绝不注入文件、不填平台表单、不点发布；抖音仍走既有「本人扫码 + 本人确认」辅助面板。
(function () {
  var state = {platform: '', platforms: [], guide: null, kit: null, project: null, loading: false};

  function el(id) { return document.getElementById(id); }
  function esc(v) {
    return String(v == null ? '' : v).replace(/[&<>"']/g, function (c) {
      return {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c];
    });
  }
  function api(path, opts) {
    return fetch(path, Object.assign({credentials: 'same-origin', cache: 'no-store'}, opts || {}))
      .then(function (r) { return r.json().then(function (d) { return {ok: r.ok, status: r.status, data: d}; }); });
  }

  function capabilityPill(p) {
    return '<span class="pill ' + (p.manual ? 'warn' : 'ok') + '">' + esc(p.label) + '</span>';
  }

  function cardsHtml() {
    return state.platforms.map(function (p) {
      var active = p.platform === state.platform;
      return '<button type="button" class="pub-card' + (active ? ' is-active' : '') +
        '" data-pub-platform="' + esc(p.platform) + '" aria-pressed="' + (active ? 'true' : 'false') + '">' +
        '<strong>' + esc(p.name) + '</strong>' + capabilityPill(p) +
        '<span class="small muted">' + (p.manual ? '人工发布' : '辅助发布') + '</span></button>';
    }).join('');
  }

  function kitRows(k) {
    if (!k || !k.fields) return '';
    var rows = [
      ['标题', 'title', k.fields.title, k.title_max ? ('平台上限 ' + k.title_max + ' 字' + (k.title_truncated ? '（已按上限截断）' : '')) : ''],
      ['描述', 'desc', k.fields.desc, ''],
      ['话题', 'tags', k.fields.tags, ''],
      ['封面文案', 'cover', k.fields.cover, '封面大字建议 6–10 字，可自行精简']
    ];
    return rows.map(function (row) {
      return '<div class="pub-kit-row"><div class="row spread"><strong>' + row[0] + '</strong>' +
        '<button class="small-button" type="button" data-pub-copy="' + row[1] + '">复制</button></div>' +
        '<p class="pub-kit-text" data-pub-text="' + row[1] + '">' + esc(row[2] || '（暂无，先回定稿资料补）') + '</p>' +
        (row[3] ? '<p class="small muted">' + esc(row[3]) + '</p>' : '') + '</div>';
    }).join('');
  }

  function manualBody(g) {
    return '<div class="pub-kit">' +
      '<p class="pub-compliance">' + esc(g.note) + '</p>' +
      '<p class="small muted">' + esc(g.reason || '') + '</p>' +
      (g.ratio ? '<p class="small muted">规格提示：' + esc(g.ratio) + '</p>' : '') +
      kitRows(state.kit) +
      '<details class="pub-steps"><summary class="small">人工步骤（点开看）</summary><ol class="small">' +
      (g.steps || []).map(function (s) { return '<li>' + esc(s) + '</li>'; }).join('') + '</ol></details>' +
      '<div class="row space">' +
      '<a class="button-link primary" href="' + esc(g.publish_url || '#') + '" target="_blank" rel="noopener noreferrer" data-pub-open="1">打开创作页 ↗</a>' +
      '<button class="small-button" type="button" data-pub-register="' + esc(g.platform) + '">我已在平台发布，去登记</button>' +
      '</div></div>';
  }

  function autoBody(g) {
    return '<div class="pub-kit"><p class="pub-compliance">' + esc(g.note) + '</p>' +
      '<p class="small muted">抖音在右侧「抖音发布」面板里完成：本人扫码登录 → 本人点确认 → 才提交。' +
      '本工作台不代点发布。</p>' +
      (g.publish_url ? '<a class="link small" href="' + esc(g.publish_url) + '" target="_blank" rel="noopener noreferrer">打开抖音创作页 ↗</a>' : '') +
      '</div>';
  }

  function bodyHtml() {
    if (state.loading) return '<p class="small muted">正在读取平台能力…</p>';
    if (!state.guide || !state.guide.ok) {
      return '<p class="small muted">选一个平台，看它的发布方式和四件套。</p>';
    }
    return state.guide.mode === 'manual' ? manualBody(state.guide) : autoBody(state.guide);
  }

  function render() {
    var box = el('publish-workbench');
    if (!box) return;
    box.innerHTML = '<div class="pub-workbench">' +
      '<div class="pub-cards" role="group" aria-label="发布平台">' + cardsHtml() + '</div>' +
      '<div class="pub-body">' + bodyHtml() + '</div></div>';
  }

  function loadPlatform(platform) {
    state.platform = platform || '';
    state.loading = true;
    state.guide = null; state.kit = null;
    render();
    api('/api/publish/guide?platform=' + encodeURIComponent(state.platform))
      .then(function (r) {
        state.guide = r.data || null;
        if (state.guide && state.guide.ok && state.guide.mode === 'manual' && state.project && state.project.id) {
          return api('/api/publish/kit', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({project_id: state.project.id, platform: state.platform})
          }).then(function (k) { state.kit = (k.data && k.data.ok) ? k.data : null; });
        }
        return null;
      })
      .catch(function () { state.guide = null; })
      .then(function () { state.loading = false; render(); });
  }

  function copyText(text, onDone) {
    if (!text) { if (window.toast) toast('这一项还没有内容，先回定稿资料补。'); return; }
    var done = function (ok) {
      if (window.toast) toast(ok ? '已复制，粘贴到平台即可。' : '复制失败，请手动选中复制。');
      if (onDone) onDone(ok);
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(function () { done(true); },
        function () { fallback(); });
    } else { fallback(); }
    function fallback() {
      var ta = document.createElement('textarea');
      ta.value = text; ta.style.position = 'fixed'; ta.style.opacity = '0';
      document.body.appendChild(ta); ta.select();
      var ok = false;
      try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
      ta.remove(); done(ok);
    }
  }

  document.addEventListener('click', function (event) {
    var card = event.target.closest('[data-pub-platform]');
    if (card) { event.preventDefault(); loadPlatform(card.getAttribute('data-pub-platform')); return; }
    var cp = event.target.closest('[data-pub-copy]');
    if (cp) {
      event.preventDefault();
      var key = cp.getAttribute('data-pub-copy');
      var v = state.kit && state.kit.fields ? state.kit.fields[key] : '';
      copyText(v);
      return;
    }
    var rg = event.target.closest('[data-pub-register]');
    if (rg) {
      event.preventDefault();
      var plat = rg.getAttribute('data-pub-register');
      var form = document.getElementById('release-register-form');
      if (!form) { if (window.toast) toast('回填表单还没出现：先确认稿件已定稿、发布资料已保存。'); return; }
      var sel = form.querySelector('select[name=platform]');
      if (sel) sel.value = plat;
      form.scrollIntoView({block: 'center'});
      var url = form.querySelector('input[name=url]');
      if (url) url.focus();
    }
  });

  window.ipPublish = {
    mount: function (project) {
      state.project = project || null;
      if (!state.platforms.length) {
        api('/api/publish/platforms').then(function (r) {
          state.platforms = (r.data && r.data.items) || [];
          var first = state.platforms.filter(function (p) { return p.platform === 'douyin'; })[0] || state.platforms[0];
          if (first) loadPlatform(first.platform); else render();
        }).catch(function () { render(); });
      } else {
        loadPlatform(state.platform || (state.platforms[0] && state.platforms[0].platform) || '');
      }
    }
  };
})();
