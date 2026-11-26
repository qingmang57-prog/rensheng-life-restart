/* 抖音封面模板库 · 10 套 · 3:4 (1080x1440) · 浅色系
 * UMD：浏览器 <script src> 挂 window.CoverTemplates；Node require() 走 module.exports。
 * 每套 render(f, o) 返回完整自包含画布 HTML（含内联 style），f=字段，o=人物/安全区选项。
 * 字段：title(<=12字) subtitle(<=18字) badge account；主标题超 12 字由后端提示改写，不静默截断。
 * 人物：o.person = {on, x: 'left'|'center'|'right'}；占位剪影，接入后换真实照片层。
 */
(function (root, factory) {
  if (typeof module !== 'undefined' && module.exports) module.exports = factory();
  else root.CoverTemplates = factory();
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  /* ---------- 公共工具 ---------- */
  var W = 1080, H = 1440;
  var FONT_SANS = "'HarmonyOS Sans SC','Microsoft YaHei','PingFang SC',sans-serif";
  var FONT_HEI = "'SimHei','Microsoft YaHei','HarmonyOS Sans SC',sans-serif";
  var FONT_SONG = "'SimSun','STSong',serif";

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"]/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c];
    });
  }
  // 主标适配（2026-11-10 试用反馈 4a+c）：先降字号，10–12 字自动换行；
  // 超过 12 字不再静默截断，由 cover_packaging.sanitize 提示改写。
  var TITLE_MAX = 12;
  function _wrapTitle(t, perLine) {
    var plain = t.replace(/[【】]/g, '');
    if (plain.length <= perLine) return t;
    var mid = Math.ceil(plain.length / 2), punct = /[，,、；;：:！!？?\s]/;
    var cut = -1;
    for (var d = 0; d < plain.length; d++) {
      var r = mid + d, l = mid - d;
      if (r < plain.length && punct.test(plain.charAt(r))) { cut = r + 1; break; }
      if (l > 0 && punct.test(plain.charAt(l))) { cut = l + 1; break; }
    }
    if (cut <= 0 || cut >= plain.length) cut = mid;
    var out = '', n = 0, done = false;
    for (var i = 0; i < t.length; i++) {
      var ch = t.charAt(i);
      if (ch === '【' || ch === '】') { out += ch; continue; }
      if (!done && n === cut) { out += '\n'; done = true; }
      out += ch; n++;
    }
    return out;
  }
  function fitTitle(t) {
    t = String(t == null ? '' : t).trim();
    var plain = t.replace(/[【】]/g, '');
    if (plain.length > TITLE_MAX) return { text: plain, size: 'sm', over: true, need_rewrite: true };
    if (plain.length > 9) return { text: _wrapTitle(t, Math.ceil(plain.length / 2)), size: 'md', over: false };
    return { text: t, size: 'lg', over: false };
  }
  function fitSub(t) {
    t = String(t == null ? '' : t).trim();
    var plain = t.replace(/[【】]/g, '');
    if (plain.length > 18) return { text: plain.slice(0, 18) + '…', over: true };
    return { text: t, over: false };
  }
  // 关键词高亮：文案里用【】包裹的词做强调；\n 由 fitTitle 插入，渲染成换行
  function hl(t, cls) {
    return esc(t).replace(/【([^】]+)】/g, '<em class="' + (cls || 'hl') + '">$1</em>').replace(/\n/g, '<br>');
  }
  function clip(s, n) {
    s = String(s == null ? '' : s).replace(/[【】]/g, '').trim();
    return s.length > n ? s.slice(0, n) + '…' : s;
  }
  // 两行大字拆分：显式 | / 换行优先，其次空格/逗号，否则对半拆；每行独立限长
  function splitTitle2(t, maxPerLine) {
    t = String(t == null ? '' : t).replace(/[【】]/g, '').trim();
    if (!t) return ['', ''];
    var br = t.split(/[|\n]+/).map(function (x) { return x.trim(); }).filter(Boolean);
    if (br.length >= 2) return [clip(br[0], maxPerLine), clip(br.slice(1).join(' '), maxPerLine)];
    var sp = t.split(/\s+/).filter(Boolean);
    if (sp.length >= 2) return [clip(sp[0], maxPerLine), clip(sp.slice(1).join(' '), maxPerLine)];
    var p = /^(.{2,}?)[，,、；;：:]\s*(.+)$/.exec(t);
    if (p) return [clip(p[1], maxPerLine), clip(p[2], maxPerLine)];
    if (t.length <= maxPerLine) return [t, ''];
    var half = Math.ceil(Math.min(t.length, maxPerLine * 2) / 2);
    return [clip(t.slice(0, half), maxPerLine), clip(t.slice(half, maxPerLine * 2), maxPerLine)];
  }
  // 双色标题：逗号前后分色；无逗号则对半拆
  function splitColor(t, maxPerLine) {
    t = String(t == null ? '' : t).replace(/[【】]/g, '').trim();
    if (!t) return ['', ''];
    var p = /^(.{1,}?)[，,]\s*(.+)$/.exec(t);
    if (p) return [clip(p[1], maxPerLine), clip(p[2], maxPerLine)];
    if (t.length <= maxPerLine) return [t, ''];
    var half = Math.ceil(Math.min(t.length, maxPerLine * 2) / 2);
    return [clip(t.slice(0, half), maxPerLine), clip(t.slice(half, maxPerLine * 2), maxPerLine)];
  }
  function itemsOf(v) {
    var arr = Array.isArray(v) ? v : String(v == null ? '' : v).split(/[\n|]/);
    arr = arr.map(function (x) { return String(x == null ? '' : x).replace(/[【】]/g, '').trim(); }).filter(Boolean);
    return arr.slice(0, 3);
  }
  function brandText(account) {
    var t = String(account == null ? '' : account).trim().replace(/^@+/, '');
    return t || '我的账号';
  }
  function starSVG(size, color) {
    return '<svg width="' + size + '" height="' + size + '" viewBox="0 0 100 100" fill="' + color + '" aria-hidden="true" style="display:block">' +
      '<path d="M50 4 L61 37 L96 38 L68 59 L78 93 L50 72 L22 93 L32 59 L4 38 L39 37 Z"/></svg>';
  }
  // 中文字体按字数降档，避免长标题溢出画布
  function sizeByLen(len, sizes) {
    for (var i = 0; i < sizes.length; i++) if (len <= sizes[i][0]) return sizes[i][1];
    return sizes[sizes.length - 1][1];
  }

  /* 人物占位剪影（接入后替换为真实照片层：<img class="person-photo">） */
  function personSVG(color) {
    return '<svg class="psvg" viewBox="0 0 240 300" fill="' + (color || '#CDC3B4') + '">' +
      '<ellipse cx="120" cy="72" rx="52" ry="62"/>' +
      '<path d="M120 148 C 52 154 30 226 40 300 H 200 C 210 226 188 154 120 148 Z"/></svg>';
  }

  /* ---------- 人像资产（我的形象 · 透明 PNG） ---------- */
  // 贴纸描边：用 feMorphology 膨胀 alpha 再填色，比多层 drop-shadow 更干净
  var OUTLINE_DEFS =
    '<svg class="cover-defs" width="0" height="0" aria-hidden="true" style="position:absolute;width:0;height:0;overflow:hidden">' +
      '<filter id="cover-outline-yellow" x="-25%" y="-25%" width="150%" height="150%">' +
        '<feMorphology in="SourceAlpha" operator="dilate" radius="9" result="d"/>' +
        '<feFlood flood-color="#FFD84D"/><feComposite in2="d" operator="in" result="o"/>' +
        '<feMerge><feMergeNode in="o"/><feMergeNode in="SourceGraphic"/></feMerge></filter>' +
      '<filter id="cover-outline-white" x="-25%" y="-25%" width="150%" height="150%">' +
        '<feMorphology in="SourceAlpha" operator="dilate" radius="9" result="d"/>' +
        '<feFlood flood-color="#FFFFFF"/><feComposite in2="d" operator="in" result="o"/>' +
        '<feMerge><feMergeNode in="o"/><feMergeNode in="SourceGraphic"/></feMerge></filter>' +
    '</svg>' +
    '<style>.pimg{width:100%;height:100%;object-fit:contain;object-position:center bottom;display:block}' +
    '.pimg.outline-yellow{filter:url(#cover-outline-yellow)}.pimg.outline-white{filter:url(#cover-outline-white)}</style>';

  // 有真人抠图用真人，否则回退剪影（绝不静默生成缺人的成片由后端拦截）
  function avatarFigure(o, opt) {
    opt = opt || {};
    var p = o.person || {};
    if (p.image) {
      var outline = p.outline && p.outline !== 'auto' ? p.outline : (opt.outlineDefault || 'none');
      var cls = 'pimg' + (outline !== 'none' ? ' outline-' + outline : '');
      return OUTLINE_DEFS + '<img class="' + cls + '" src="' + esc(p.image) + '" alt="我的形象">';
    }
    return personSVG(opt.color);
  }
  function hasAvatar(o) { return !!(o.person && o.person.image); }

  /* 人物层：position = left/center/right；offsetPx 控制垂直 */
  function personLayer(o, opt) {
    opt = opt || {};
    var p = o.person || {};
    if (!p.on) return '';
    if (p.image) {
      // 真实人像：容器高度按 4:5 估，图片 contain 底部对齐
      return personLayerWithAvatar(o, opt);
    }
    var x = p.x || 'center';
    var size = opt.size || 520; // 剪影宽
    var bottom = opt.bottom != null ? opt.bottom : 0;
    var label = opt.label === undefined ? '人物位' : opt.label;
    var leftCss;
    if (x === 'left') leftCss = 'left:' + (opt.padX != null ? opt.padX : 70) + 'px;';
    else if (x === 'right') leftCss = 'right:' + (opt.padX != null ? opt.padX : 70) + 'px;';
    else leftCss = 'left:50%;transform:translateX(-50%);';
    return '<div class="player" style="' + leftCss + 'bottom:' + bottom + 'px;width:' + size + 'px;' + (opt.extra || '') + '">' +
      personSVG(opt.color) +
      (label ? '<div class="ptag">' + label + '</div>' : '') + '</div>';
  }
  function personLayerWithAvatar(o, opt) {
    var p = o.person || {};
    var x = p.x || 'center';
    var size = opt.size || 520;
    var height = Math.round(size * 1.32);
    var bottom = opt.bottom != null ? opt.bottom : 0;
    // 有真人透明图时不再显示「人物位」占位标签（只影响有形象的场景）
    var label = '';
    var leftCss;
    if (x === 'left') leftCss = 'left:' + (opt.padX != null ? opt.padX : 70) + 'px;';
    else if (x === 'right') leftCss = 'right:' + (opt.padX != null ? opt.padX : 70) + 'px;';
    else leftCss = 'left:50%;transform:translateX(-50%);';
    return '<div class="player" style="' + leftCss + 'bottom:' + bottom + 'px;width:' + size + 'px;height:' + height + 'px;' + (opt.extra || '') + '">' +
      avatarFigure(o, { outlineDefault: opt.outlineDefault || 'none' }) +
      (label ? '<div class="ptag">' + label + '</div>' : '') + '</div>';
  }

  /* 安全区标注层：文字安全区(四周6%) + 平台遮挡(顶8%/底12%/右15%) */
  var SAFE_CSS =
    '.safezones{position:absolute;inset:0;pointer-events:none;z-index:90;display:none}' +
    '.safezones.show{display:block}' +
    '.sz-text{position:absolute;left:65px;top:87px;right:65px;bottom:87px;border:3px dashed #3FA34D;background:rgba(63,163,77,.05)}' +
    '.sz-text::after{content:"文字安全区 四周≥6%";position:absolute;top:-38px;left:0;font:600 26px ' + FONT_SANS + ';color:#3FA34D;letter-spacing:2px}' +
    '.sz-top,.sz-bottom,.sz-right{position:absolute;background:repeating-linear-gradient(45deg,rgba(224,82,60,.16) 0 18px,rgba(224,82,60,.05) 18px 36px);border:2px dashed rgba(224,82,60,.6)}' +
    '.sz-top{left:0;top:0;width:100%;height:115px}' +
    '.sz-bottom{left:0;bottom:0;width:100%;height:173px}' +
    '.sz-right{right:0;top:0;width:162px;height:100%}' +
    '.sz-note{position:absolute;left:50%;bottom:180px;transform:translateX(-50%);font:600 24px ' + FONT_SANS + ';color:#C24B36;background:rgba(255,255,255,.85);padding:8px 18px;border-radius:12px;white-space:nowrap}' +
    '.ptag{position:absolute;left:50%;bottom:-34px;transform:translateX(-50%);font:600 22px ' + FONT_SANS + ';color:#9A8F7D;background:rgba(255,255,255,.9);padding:4px 14px;border-radius:20px;white-space:nowrap}';

  function safeLayer(o) {
    if (!o.showSafe) return '';
    return '<div class="safezones show">' +
      '<div class="sz-top"></div><div class="sz-bottom"></div><div class="sz-right"></div>' +
      '<div class="sz-text"></div>' +
      '<div class="sz-note">平台遮挡区：顶8% · 底12% · 右15%（右下为播放量标签位）</div></div>';
  }

  /* 画布骨架 */
  function canvas(id, style, inner) {
    return '<div class="cv cv-' + id + '" style="' + style + '">' + inner + '</div>';
  }
  var BASE_CSS =
    '.cv{width:' + W + 'px;height:' + H + 'px;position:relative;overflow:hidden;font-family:' + FONT_SANS + ';box-sizing:border-box}' +
    '.cv *{box-sizing:border-box;margin:0}' +
    '.player{position:absolute;z-index:5}' +
    '.psvg{width:100%;display:block}';

  function page(inner, extraCss) {
    return '<!DOCTYPE html><html><head><meta charset="utf-8"><style>' +
      'html,body{margin:0;padding:0;background:#fff}' + BASE_CSS + SAFE_CSS + (extraCss || '') +
      '</style></head><body>' + inner + '</body></html>';
  }

  /* ---------- 10 套模板 ---------- */

  var T = [];

  /* 01 大字宣言 —— 通用观点 / 认知思维 */
  T.push({
    id: '01-bigtype', name: '大字宣言',
    types: ['通用观点', '认知/思维'],
    structure: '奶米白纯底 + 中轴大字铺满 + 暖橘关键词高亮',
    personDefault: { on: false, x: 'right' },
    fieldsDefault: { title: '别再【瞎努力】了', subtitle: '高手都在用这一套底层逻辑', badge: '深度观点', account: '' },
    render: function (f, o) {
      f = Object.assign({}, this.fieldsDefault, f);
      var t = fitTitle(f.title), s = fitSub(f.subtitle);
      var inner =
        '<div style="position:absolute;left:65px;top:118px;display:flex;align-items:center;gap:16px;z-index:10">' +
          '<span style="width:52px;height:6px;background:#D97C59;border-radius:3px"></span>' +
          '<span style="font:600 32px ' + FONT_SANS + ';color:#D97C59;letter-spacing:6px">' + esc(f.badge) + '</span></div>' +
        personLayer(o, { size: 560, bottom: 60, padX: 90, label: '人物位（可关）' }) +
        '<div style="position:absolute;left:65px;right:65px;top:400px;z-index:20">' +
          '<h1 class="tt ' + t.size + '">' + hl(t.text, 'hl') + '</h1>' +
          '<p style="margin-top:56px;font:400 44px ' + FONT_SANS + ';color:#6B584A;letter-spacing:3px">' + esc(s.text) + '</p>' +
        '</div>' +
        '<div style="position:absolute;left:65px;bottom:196px;right:230px;display:flex;justify-content:space-between;align-items:center;z-index:20">' +
          '<span style="font:500 30px ' + FONT_SANS + ';color:#9A8F7D;letter-spacing:2px">' + esc(f.account) + '</span>' +
          '<span style="font:500 26px ' + FONT_SANS + ';color:#C9BFB0;letter-spacing:4px">VOL.01</span></div>' +
        safeLayer(o);
      return page(canvas('01', 'background:#FBF9F5', inner),
        '.tt{color:#222;font-family:' + FONT_HEI + ';line-height:1.22;letter-spacing:2px}' +
        '.tt.lg{font-size:150px;font-weight:900}' +
        '.tt.md{font-size:122px;font-weight:900}' +
        '.tt.sm{font-size:100px;font-weight:900}' +
        '.hl{font-style:normal;background:linear-gradient(transparent 62%,#F6C9A8 62%);padding:0 6px}');
    }
  });

  /* 02 白卡悬浮 —— 知识科普 / 认知思维 */
  T.push({
    id: '02-whitecard', name: '白卡悬浮卡',
    types: ['知识科普', '认知/思维'],
    structure: '浅薄荷青底 + 居中白圆角卡（角标/主副标/标签三段式）',
    personDefault: { on: false, x: 'center' },
    fieldsDefault: { title: '孩子顶嘴【不是坏事】', subtitle: '儿童心理学里的 3 个真相', badge: '知识科普', account: '' },
    render: function (f, o) {
      f = Object.assign({}, this.fieldsDefault, f);
      var t = fitTitle(f.title), s = fitSub(f.subtitle);
      var inner =
        personLayer(o, { size: 470, bottom: 560, label: '人物位（可关）' }) +
        '<div style="position:absolute;left:74px;right:74px;top:300px;bottom:270px;background:#fff;border-radius:30px;box-shadow:0 24px 70px rgba(99,168,160,.22);padding:90px 76px;z-index:10">' +
          '<div style="display:flex;align-items:center;gap:18px">' +
            '<span style="width:46px;height:5px;background:#63A8A0;border-radius:3px"></span>' +
            '<span style="font:600 30px ' + FONT_SANS + ';color:#63A8A0;letter-spacing:8px">' + esc(f.badge) + '</span></div>' +
          '<h1 class="tt ' + t.size + '" style="margin-top:64px">' + hl(t.text, 'hl') + '</h1>' +
          '<p style="margin-top:46px;font:400 40px ' + FONT_SANS + ';color:#555;letter-spacing:2px;line-height:1.5">' + esc(s.text) + '</p>' +
          '<div style="position:absolute;left:76px;bottom:84px;display:flex;gap:20px">' +
            ['认知升级', '家长必读', '干货清单'].map(function (x) {
              return '<span style="font:500 27px ' + FONT_SANS + ';color:#4E837C;background:#E2EFED;padding:12px 26px;border-radius:999px">' + esc(x) + '</span>';
            }).join('') + '</div>' +
        '</div>' +
        '<div style="position:absolute;left:74px;top:210px;display:flex;gap:14px;align-items:center;z-index:11">' +
          '<span style="font:600 28px ' + FONT_SANS + ';color:#63A8A0;background:#fff;border-radius:999px;padding:10px 26px;box-shadow:0 8px 24px rgba(99,168,160,.18)">' + esc(f.account) + '</span></div>' +
        safeLayer(o);
      return page(canvas('02', 'background:radial-gradient(circle at 8% 6%,rgba(99,168,160,.14),transparent 40%),radial-gradient(circle at 94% 92%,rgba(99,168,160,.16),transparent 44%),#F2F8F7', inner),
        '.tt{color:#222;font-family:' + FONT_SANS + ';font-weight:900;line-height:1.3;letter-spacing:1px}' +
        '.tt.lg{font-size:96px}.tt.md{font-size:82px}.tt.sm{font-size:70px}' +
        '.hl{font-style:normal;color:#2F8C82}');
    }
  });

  /* 03 手账胶带 —— 生活方式 / 自我成长 */
  T.push({
    id: '03-journal', name: '手账便签',
    types: ['生活方式', '自我成长'],
    structure: '米白网格纸 + 中央便签 + 和纸胶带 + 拍立得人物位',
    personDefault: { on: true, x: 'right' },
    fieldsDefault: { title: '过好【想要的日子】', subtitle: '我的一周日程分享', badge: '生活手记', account: '' },
    render: function (f, o) {
      f = Object.assign({}, this.fieldsDefault, f);
      var t = fitTitle(f.title), s = fitSub(f.subtitle);
      var inner =
        '<div style="position:absolute;left:80px;top:130px;width:400px;transform:rotate(-3deg);background:rgba(242,168,138,.55);height:44px;z-index:30;box-shadow:0 2px 6px rgba(0,0,0,.06)"></div>' +
        '<div style="position:absolute;right:120px;top:150px;width:300px;transform:rotate(4deg);background:rgba(99,168,160,.4);height:44px;z-index:30;box-shadow:0 2px 6px rgba(0,0,0,.06)"></div>' +
        '<div style="position:absolute;left:96px;right:96px;top:236px;height:760px;background:#FFFDF6;border-radius:6px;transform:rotate(-1.5deg);box-shadow:0 18px 50px rgba(107,88,74,.16);padding:110px 70px 0;z-index:10">' +
          '<div style="font:500 28px ' + FONT_SANS + ';color:#C4B8A4;letter-spacing:6px">' + esc(f.badge) + ' · 2026.11</div>' +
          '<h1 class="tt ' + t.size + '" style="margin-top:40px">' + hl(t.text, 'hl') + '</h1>' +
          '<p style="margin-top:44px;font:400 38px ' + FONT_SANS + ';color:#8A7C6A;letter-spacing:2px">' + esc(s.text) + '</p>' +
          '<div style="margin-top:52px;display:flex;flex-direction:column;gap:26px">' +
            ['周一 · 早起清单', '周三 · 断舍离', '周日 · 复盘时刻'].map(function (x) {
              return '<div style="display:flex;align-items:center;gap:18px;font:500 32px ' + FONT_SANS + ';color:#6B584A">' +
                '<span style="width:34px;height:34px;border:3px solid #D97C59;border-radius:8px;flex:none;position:relative"><span style="position:absolute;inset:7px;background:#D97C59;border-radius:3px"></span></span>' + esc(x) + '</div>';
            }).join('') + '</div>' +
        '</div>' +
        '<div style="position:absolute;right:110px;bottom:250px;width:360px;background:#fff;padding:18px 18px 60px;transform:rotate(3deg);box-shadow:0 16px 44px rgba(107,88,74,.2);z-index:20">' +
          (o.person && o.person.on
            ? '<div style="height:380px;position:relative">' + personLayer({ person: Object.assign({}, o.person, { on: true, x: 'center' }) }, { size: 300, bottom: -30, label: '' }) + '</div>'
            : '<div style="height:380px;border:3px dashed #D8CDBB;border-radius:8px;display:flex;align-items:center;justify-content:center;font:500 30px ' + FONT_SANS + ';color:#C4B8A4">贴照片处</div>') +
          '<div style="text-align:center;margin-top:18px;font:500 28px ' + FONT_SANS + ';color:#B7AB97">everyday ☀</div></div>' +
        '<div style="position:absolute;left:96px;bottom:200px;font:500 30px ' + FONT_SANS + ';color:#9A8F7D;letter-spacing:3px;z-index:20">' + esc(f.account) + '</div>' +
        '<div style="position:absolute;left:150px;bottom:320px;width:120px;height:120px;border-radius:50%;background:#FAD6C8;z-index:25;opacity:.85"></div>' +
        safeLayer(o);
      return page(canvas('03', 'background:repeating-linear-gradient(0deg,transparent 0 79px,#EFE8D8 79px 80px),repeating-linear-gradient(90deg,transparent 0 79px,#EFE8D8 79px 80px),#FFFDF6', inner),
        '.tt{color:#4A3F32;font-family:' + FONT_SANS + ';font-weight:900;line-height:1.4}' +
        '.tt.lg{font-size:88px}.tt.md{font-size:76px}.tt.sm{font-size:64px}' +
        '.hl{font-style:normal;border:5px solid #E8916B;border-radius:46% 54% 52% 48%/60% 42% 58% 40%;padding:0 10px}');
    }
  });

  /* 04 杂志边框 —— 自我成长 / 情感（粗宋） */
  T.push({
    id: '04-magazine', name: '成长画报',
    types: ['自我成长', '情感'],
    structure: '米灰底 + 双细线杂志边框 + 中轴人像 + 粗宋标题',
    personDefault: { on: true, x: 'center' },
    fieldsDefault: { title: '三十岁才懂【松弛感】', subtitle: '与自己和解的第一课', badge: '成长画报', account: '' },
    render: function (f, o) {
      f = Object.assign({}, this.fieldsDefault, f);
      var t = fitTitle(f.title), s = fitSub(f.subtitle);
      var personBlock = (o.person && o.person.on)
        ? '<div style="position:absolute;left:50%;transform:translateX(-50%);bottom:' + (hasAvatar(o) ? '450px;height:560px;width:430px;' : '330px;width:480px;') + 'z-index:5">' + avatarFigure(o, { color: '#D8CFC0', outlineDefault: o.person.outline || 'none' }) + (hasAvatar(o) ? '' : '<div class="ptag">人物位</div>') + '</div>'
        : '<div style="position:absolute;left:50%;transform:translateX(-50%);bottom:330px;width:340px;height:340px;border:2px solid #B9AE9B;border-radius:50%;z-index:5;display:flex;align-items:center;justify-content:center;font:900 150px ' + FONT_SONG + ';color:#C9BFA9">悟</div>';
      var inner =
        '<div style="position:absolute;inset:44px;border:2px solid #2E2A24;z-index:8"></div>' +
        '<div style="position:absolute;inset:56px;border:1px solid #B9AE9B;z-index:8"></div>' +
        '<div style="position:absolute;left:110px;right:110px;top:120px;display:flex;justify-content:space-between;font:500 26px ' + FONT_SANS + ';color:#6B584A;letter-spacing:8px;z-index:10">' +
          '<span>VOL.04</span><span>' + esc(f.badge) + '</span><span>2026 / 11</span></div>' +
        '<div style="position:absolute;left:110px;right:110px;top:210px;text-align:center;z-index:10">' +
          '<h1 class="tt ' + t.size + '">' + hl(t.text, 'hl') + '</h1></div>' +
        personBlock +
        '<div style="position:absolute;left:110px;right:110px;bottom:300px;text-align:center;z-index:10">' +
          '<div style="font:500 26px ' + FONT_SANS + ';color:#B9AE9B;letter-spacing:14px">GROWTH ISSUE</div>' +
          '<p style="margin-top:26px;font:400 40px ' + FONT_SONG + ';color:#4A4238;letter-spacing:6px">' + esc(s.text) + '</p></div>' +
        '<div style="position:absolute;left:0;right:0;bottom:150px;text-align:center;z-index:10">' +
          '<div style="display:inline-block;width:520px;border-top:1px solid #B9AE9B;padding-top:24px;font:500 28px ' + FONT_SANS + ';color:#6B584A;letter-spacing:6px">' + esc(f.account) + '</div></div>' +
        safeLayer(o);
      return page(canvas('04', 'background:#F5F1EA', inner),
        '.tt{color:#2E2A24;font-family:' + FONT_SONG + ';font-weight:900;line-height:1.35;letter-spacing:4px}' +
        '.tt.lg{font-size:104px}.tt.md{font-size:88px}.tt.sm{font-size:74px}' +
        '.hl{font-style:normal;color:#A0522D;text-decoration:underline;text-decoration-color:#C9A227;text-decoration-thickness:6px;text-underline-offset:14px}');
    }
  });

  /* 05 左文右人 —— 认知/思维 / 营销方法 */
  T.push({
    id: '05-lefttext', name: '左文右人',
    types: ['认知/思维', '营销方法'],
    structure: '雾蓝白底 + 左 55% 文字区 + 右 45% 人物色块区',
    personDefault: { on: true, x: 'right' },
    fieldsDefault: { title: '会说话的人【赢在哪】', subtitle: '结构化表达的 3 个层次', badge: '认知升级', account: '' },
    render: function (f, o) {
      f = Object.assign({}, this.fieldsDefault, f);
      var t = fitTitle(f.title), s = fitSub(f.subtitle);
      var rightBlock = (o.person && o.person.on)
        ? personLayer(o, { size: 460, bottom: 200, padX: 120, label: '人物位' })
        : '<div style="position:absolute;right:100px;bottom:300px;width:420px;height:420px;border:3px dashed #A8BCCB;border-radius:50%;display:flex;align-items:center;justify-content:center;z-index:5">' +
            '<span style="font:900 300px ' + FONT_SANS + ';color:#C9D6E2">“</span></div>';
      var inner =
        '<div style="position:absolute;right:0;top:0;bottom:0;width:42%;background:#DCE6EC;border-radius:0 0 0 80px"></div>' +
        rightBlock +
        '<div style="position:absolute;left:65px;top:120px;display:flex;align-items:center;gap:16px;z-index:10">' +
          '<span style="font:600 30px ' + FONT_SANS + ';color:#fff;background:#5B7A99;padding:10px 26px;border-radius:8px;letter-spacing:6px">' + esc(f.badge) + '</span></div>' +
        '<div style="position:absolute;left:65px;width:600px;top:330px;z-index:10">' +
          '<h1 class="tt ' + t.size + '">' + hl(t.text, 'hl') + '</h1>' +
          '<p style="margin-top:48px;font:400 40px ' + FONT_SANS + ';color:#5A6B7A;letter-spacing:2px;line-height:1.5">' + esc(s.text) + '</p>' +
          '<div style="margin-top:70px;width:80px;height:8px;background:#5B7A99;border-radius:4px"></div></div>' +
        '<div style="position:absolute;left:65px;bottom:200px;z-index:10;font:500 30px ' + FONT_SANS + ';color:#5B7A99;letter-spacing:3px">' + esc(f.account) + '</div>' +
        '<div style="position:absolute;right:70px;top:120px;writing-mode:vertical-rl;font:500 26px ' + FONT_SANS + ';color:#7A93AB;letter-spacing:10px;z-index:10">THINK · STRUCTURE · SAY</div>' +
        safeLayer(o);
      return page(canvas('05', 'background:#F4F8FA', inner),
        '.tt{color:#22303C;font-family:' + FONT_HEI + ';font-weight:900;line-height:1.3;letter-spacing:2px}' +
        '.tt.lg{font-size:112px}.tt.md{font-size:94px}.tt.sm{font-size:80px}' +
        '.hl{font-style:normal;color:#2F6DA8;background:linear-gradient(transparent 60%,#BFDCEC 60%)}');
    }
  });

  /* 06 数字冲击 —— 如何赚钱 */
  T.push({
    id: '06-money', name: '数字冲击',
    types: ['如何赚钱'],
    structure: '奶油底 + 超大数字高亮 + 黑底黄字强调条',
    personDefault: { on: false, x: 'right' },
    fieldsDefault: { title: '下班后【2小时】多赚5000', subtitle: '普通人可复制的 3 个副业', badge: '副业实战', account: '' },
    render: function (f, o) {
      f = Object.assign({}, this.fieldsDefault, f);
      var t = fitTitle(f.title), s = fitSub(f.subtitle);
      var inner =
        '<div style="position:absolute;right:70px;top:120px;font:600 30px ' + FONT_SANS + ';color:#8A6D1F;background:#F7E9B8;padding:12px 30px;border-radius:999px;z-index:10;letter-spacing:4px">' + esc(f.badge) + '</div>' +
        personLayer(o, { size: 440, bottom: 220, padX: 110, label: '人物位（可关）' }) +
        '<div style="position:absolute;left:70px;right:70px;top:330px;z-index:10">' +
          '<h1 class="tt ' + t.size + '">' + hl(t.text, 'hl') + '</h1></div>' +
        '<div style="position:absolute;left:70px;top:830px;width:560px;background:#222;padding:44px 50px;border-radius:20px;z-index:10;transform:rotate(-1deg);box-shadow:10px 14px 0 #F0D98C">' +
          '<div style="font:500 30px ' + FONT_SANS + ';color:#F0D98C;letter-spacing:6px">本期拆解</div>' +
          '<div style="margin-top:16px;font:900 78px ' + FONT_HEI + ';color:#FFF7DE;letter-spacing:2px">¥ 5000<span style="font-size:38px;font-weight:500"> /月</span></div></div>' +
        '<div style="position:absolute;left:70px;bottom:280px;right:70px;z-index:10">' +
          '<p style="font:400 42px ' + FONT_SANS + ';color:#6B584A;letter-spacing:2px">' + esc(s.text) + '</p></div>' +
        '<div style="position:absolute;left:70px;bottom:200px;display:flex;gap:18px;z-index:10">' +
            ['低门槛', '可复制', '长期复利'].map(function (x) {
              return '<span style="font:600 28px ' + FONT_SANS + ';color:#8A6D1F;background:#FBF3D9;padding:12px 26px;border-radius:999px">' + esc(x) + '</span>';
            }).join('') + '</div>' +
        '<div style="position:absolute;left:70px;bottom:120px;font:500 28px ' + FONT_SANS + ';color:#B0A488;letter-spacing:3px;z-index:10">' + esc(f.account) + '</div>' +
        safeLayer(o);
      return page(canvas('06', 'background:#FFFDF5', inner),
        '.tt{color:#222;font-family:' + FONT_HEI + ';font-weight:900;line-height:1.28;letter-spacing:1px}' +
        '.tt.lg{font-size:124px}.tt.md{font-size:104px}.tt.sm{font-size:88px}' +
        '.hl{font-style:normal;color:#111;background:linear-gradient(transparent 55%,#F7E9B8 55%);padding:0 8px;border-radius:6px}');
    }
  });

  /* 07 促销分割 —— 商家带货 */
  T.push({
    id: '07-banner', name: '带货横幅',
    types: ['商家带货'],
    structure: '上下分割：上产品/人物展示区 + 下白底促销信息条',
    personDefault: { on: false, x: 'center' },
    fieldsDefault: { title: '秋冬被 Factory 直供', subtitle: '原价 399 · 直播间限时 199', badge: '限时特惠', account: '' },
    render: function (f, o) {
      f = Object.assign({}, this.fieldsDefault, f);
      var t = fitTitle(f.title), s = fitSub(f.subtitle);
      var topBlock = (o.person && o.person.on)
        ? personLayer(o, { size: 520, bottom: 480, label: '人物位' })
        : '<div style="position:absolute;left:50%;transform:translateX(-50%);bottom:520px;width:560px;height:420px;border:4px dashed #E8B48A;border-radius:24px;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:14px;z-index:5">' +
            '<span style="font:900 120px ' + FONT_SANS + ';color:#E8B48A">📦</span>' +
            '<span style="font:500 34px ' + FONT_SANS + ';color:#D97C59;letter-spacing:4px">产品图 / 人物位</span></div>';
      var inner =
        '<div style="position:absolute;left:0;right:0;top:0;height:56%;background:linear-gradient(160deg,#FDF0E8,#FAE3D3)"></div>' +
        '<div style="position:absolute;left:70px;top:130px;transform:rotate(-6deg);background:#E8652E;color:#fff;font:900 44px ' + FONT_HEI + ';padding:16px 36px;border-radius:14px;z-index:20;box-shadow:6px 8px 0 rgba(232,101,46,.25)">' + esc(f.badge) + '</div>' +
        topBlock +
        '<div style="position:absolute;left:0;right:0;bottom:0;height:44%;background:#fff;border-radius:56px 56px 0 0;box-shadow:0 -20px 60px rgba(217,124,89,.18);padding:80px 70px 0;z-index:10">' +
          '<h1 class="tt ' + t.size + '">' + hl(t.text, 'hl') + '</h1>' +
          '<p style="margin-top:34px;font:500 42px ' + FONT_SANS + ';color:#8A7466;letter-spacing:1px">' + esc(s.text) + '</p>' +
          '<div style="margin-top:44px;display:flex;align-items:center;gap:26px">' +
            '<span style="font:900 76px ' + FONT_HEI + ';color:#E8652E">199<span style="font-size:34px">元</span></span>' +
            '<span style="font:400 36px ' + FONT_SANS + ';color:#B9AA9C;text-decoration:line-through">399元</span>' +
            '<span style="font:600 28px ' + FONT_SANS + ';color:#fff;background:#E8652E;padding:10px 24px;border-radius:999px">立省 200</span></div>' +
          '<div style="margin-top:52px;display:flex;justify-content:space-between;align-items:center">' +
            '<span style="font:500 30px ' + FONT_SANS + ';color:#9A8F7D">' + esc(f.account) + '</span>' +
            '<span style="font:600 30px ' + FONT_SANS + ';color:#D97C59;border:3px solid #D97C59;padding:12px 34px;border-radius:999px">点击小黄车 →</span></div>' +
        '</div>' +
        safeLayer(o);
      return page(canvas('07', 'background:#FDF0E8', inner),
        '.tt{color:#2E241C;font-family:' + FONT_HEI + ';font-weight:900;line-height:1.3;letter-spacing:1px}' +
        '.tt.lg{font-size:86px}.tt.md{font-size:74px}.tt.sm{font-size:64px}' +
        '.hl{font-style:normal;color:#D97C59}');
    }
  });

  /* 08 门店招牌 —— 商家营销/门店 */
  T.push({
    id: '08-storefront', name: '门店招牌',
    types: ['商家营销/门店'],
    structure: '青绿招牌条 + 门店场景框 + 信息便签 + 到店引导',
    personDefault: { on: false, x: 'left' },
    fieldsDefault: { title: '巷子里的宝藏咖啡', subtitle: '藏在老城区的手冲小店', badge: '到店打卡', account: '' },
    render: function (f, o) {
      f = Object.assign({}, this.fieldsDefault, f);
      var t = fitTitle(f.title), s = fitSub(f.subtitle);
      var sceneBlock = (o.person && o.person.on)
        ? personLayer(o, { size: 440, bottom: 300, padX: 110, label: '人物位' })
        : '<div style="position:absolute;left:80px;bottom:290px;width:520px;height:560px;border:4px dashed #8FBFB7;border-radius:20px;display:flex;align-items:center;justify-content:center;z-index:5">' +
            '<span style="font:500 32px ' + FONT_SANS + ';color:#63A8A0;letter-spacing:4px">门店照片 / 人物位</span></div>';
      var inner =
        '<div style="position:absolute;left:0;right:0;top:110px;height:250px;background:#4E837C;border-radius:24px;margin:0 60px;display:flex;align-items:center;justify-content:center;z-index:10;box-shadow:0 18px 50px rgba(78,131,124,.3)">' +
          '<span style="font:900 72px ' + FONT_SONG + ';color:#F2F8F7;letter-spacing:10px">本期探店</span></div>' +
        '<div style="position:absolute;right:60px;top:300px;width:150px;height:150px;background:#F6C9A8;border-radius:50%;display:flex;align-items:center;justify-content:center;z-index:15;font:900 30px ' + FONT_SANS + ';color:#fff;transform:rotate(8deg);box-shadow:0 10px 30px rgba(232,101,46,.3)">★ 开业</div>' +
        '<div style="position:absolute;left:80px;top:430px;z-index:10">' +
          '<div style="display:inline-block;font:600 30px ' + FONT_SANS + ';color:#4E837C;background:#DEEEEA;padding:10px 28px;border-radius:999px;letter-spacing:6px">' + esc(f.badge) + '</div>' +
          '<h1 class="tt ' + t.size + '" style="margin-top:36px">' + hl(t.text, 'hl') + '</h1>' +
          '<p style="margin-top:32px;font:400 40px ' + FONT_SANS + ';color:#5A6B66;letter-spacing:2px">' + esc(s.text) + '</p></div>' +
        sceneBlock +
        '<div style="position:absolute;right:80px;bottom:290px;width:330px;background:#fff;border-radius:18px;padding:44px 40px;z-index:10;transform:rotate(2deg);box-shadow:0 16px 44px rgba(78,131,124,.2)">' +
          '<div style="font:600 26px ' + FONT_SANS + ';color:#8FBFB7;letter-spacing:8px">店铺信息</div>' +
          '<div style="margin-top:28px;display:flex;flex-direction:column;gap:24px;font:500 30px ' + FONT_SANS + ';color:#4A5B55">' +
            '<div>📍 城南 · 老巷 12 号</div><div>🕙 10:00 - 21:00</div><div>☕ 人均 35 元</div></div></div>' +
        '<div style="position:absolute;left:0;right:0;bottom:180px;display:flex;justify-content:center;z-index:10">' +
          '<span style="font:900 44px ' + FONT_SANS + ';color:#fff;background:#63A8A0;padding:22px 70px;border-radius:999px;letter-spacing:8px;box-shadow:0 14px 40px rgba(99,168,160,.4)">' + esc(f.account) + ' · 到店打卡</span></div>' +
        safeLayer(o);
      return page(canvas('08', 'background:#F2F8F7', inner),
        '.tt{color:#22332F;font-family:' + FONT_SANS + ';font-weight:900;line-height:1.3;letter-spacing:2px}' +
        '.tt.lg{font-size:100px}.tt.md{font-size:86px}.tt.sm{font-size:72px}' +
        '.hl{font-style:normal;color:#2F8C82;border-bottom:8px solid #BFE0DA;padding-bottom:4px}');
    }
  });

  /* 09 备忘录 —— 情感 */
  T.push({
    id: '09-notes', name: '备忘录树洞',
    types: ['情感'],
    structure: 'iOS 备忘录界面 + 荧光笔高亮 + 清单体',
    personDefault: { on: false, x: 'center' },
    fieldsDefault: { title: '和妈妈【好好说话】', subtitle: '这 3 句话，我练了十年', badge: '情感树洞', account: '' },
    render: function (f, o) {
      f = Object.assign({}, this.fieldsDefault, f);
      var t = fitTitle(f.title), s = fitSub(f.subtitle);
      var avatar = (o.person && o.person.on)
        ? '<div style="position:absolute;right:70px;top:120px;width:150px;height:150px;border-radius:50%;background:#F3EDE2;display:flex;align-items:flex-end;justify-content:center;overflow:hidden;z-index:10;box-shadow:0 10px 26px rgba(107,88,74,.18)">' +
            '<div style="width:110px;' + (hasAvatar(o) ? 'height:140px;' : '') + '">' + avatarFigure(o, { color: '#C9BEB2', outlineDefault: o.person.outline || 'none' }) + '</div></div>'
        : '';
      var inner =
        '<div style="position:absolute;left:70px;top:120px;display:flex;flex-direction:column;gap:10px;z-index:10">' +
          '<span style="font:400 30px ' + FONT_SANS + ';color:#B9B0A2;letter-spacing:4px">备忘录 · ' + esc(f.badge) + '</span>' +
          '<span style="font:500 27px ' + FONT_SANS + ';color:#C9BFB0">2026年11月2日 21:47</span></div>' +
        avatar +
        '<div style="position:absolute;left:70px;right:70px;top:400px;z-index:10">' +
          '<h1 class="tt ' + t.size + '">' + hl(t.text, 'hl') + '</h1>' +
          '<div style="margin-top:56px;border-bottom:2px solid #EFE9DC"></div>' +
          '<div style="margin-top:64px;display:flex;flex-direction:column;gap:44px">' +
            ['「我不是不爱你，是不知道怎么说」', '「先听完整，再开口」', '「对不起」比「我爱你」更难'].map(function (x) {
              return '<div style="display:flex;gap:24px;align-items:flex-start">' +
                '<span style="width:20px;height:20px;border-radius:50%;background:#E6B800;flex:none;margin-top:22px"></span>' +
                '<span style="font:400 44px ' + FONT_SANS + ';color:#555;line-height:1.5">' + esc(x) + '</span></div>';
            }).join('') + '</div>' +
          '<p style="margin-top:70px;font:400 38px ' + FONT_SANS + ';color:#9A8F7D;letter-spacing:2px">' + esc(s.text) + '</p></div>' +
        '<div style="position:absolute;left:70px;bottom:190px;right:70px;display:flex;justify-content:space-between;align-items:center;z-index:10">' +
          '<span style="font:500 30px ' + FONT_SANS + ';color:#9A8F7D">' + esc(f.account) + '</span>' +
          '<span style="font:500 28px ' + FONT_SANS + ';color:#C9BFB0">与你的第 102 次对话</span></div>' +
        safeLayer(o);
      return page(canvas('09', 'background:#FFFEFB', inner),
        '.tt{color:#222;font-family:' + FONT_HEI + ';font-weight:900;line-height:1.3;letter-spacing:1px}' +
        '.tt.lg{font-size:108px}.tt.md{font-size:92px}.tt.sm{font-size:78px}' +
        '.hl{font-style:normal;background:linear-gradient(transparent 52%,#F9E27A 52%);padding:0 6px}');
    }
  });

  /* 10 便签拼贴 —— 情感 / 通用观点 */
  T.push({
    id: '10-sticky', name: '情绪便签',
    types: ['情感', '通用观点'],
    structure: '米灰底 + 三色便签错落拼贴 + 马克笔圈重点',
    personDefault: { on: false, x: 'left' },
    fieldsDefault: { title: '允许自己【偶尔没用】', subtitle: '不必事事都有意义', badge: '今日份', account: '' },
    render: function (f, o) {
      f = Object.assign({}, this.fieldsDefault, f);
      var t = fitTitle(f.title), s = fitSub(f.subtitle);
      var inner =
        personLayer(o, { size: 520, bottom: 120, padX: 60, label: '人物位（可关）' }) +
        '<div style="position:absolute;left:50%;transform:translateX(-50%) rotate(-2deg);top:280px;width:760px;background:#F7EF9E;padding:70px 60px 64px;box-shadow:0 18px 50px rgba(107,88,74,.18);z-index:10">' +
          '<div style="position:absolute;left:50%;transform:translateX(-50%) rotate(2deg);top:-26px;width:280px;height:52px;background:rgba(242,168,138,.6);box-shadow:0 2px 6px rgba(0,0,0,.08)"></div>' +
          '<h1 class="tt ' + t.size + '">' + hl(t.text, 'hl') + '</h1></div>' +
        '<div style="position:absolute;right:96px;top:840px;width:400px;background:#FDF0E8;transform:rotate(3deg);padding:44px 40px;box-shadow:0 14px 40px rgba(107,88,74,.16);z-index:11">' +
          '<div style="font:600 26px ' + FONT_SANS + ';color:#D97C59;letter-spacing:6px">' + esc(f.badge) + '</div>' +
          '<p style="margin-top:20px;font:400 36px ' + FONT_SANS + ';color:#6B584A;line-height:1.5">' + esc(s.text) + '</p></div>' +
        '<div style="position:absolute;left:96px;bottom:290px;width:330px;background:#DCEDE9;transform:rotate(-3deg);padding:36px 36px;box-shadow:0 12px 36px rgba(107,88,74,.14);z-index:11">' +
          '<div style="font:500 30px ' + FONT_SANS + ';color:#4E837C;letter-spacing:2px">' + esc(f.account) + '</div>' +
          '<div style="margin-top:14px;display:flex;gap:10px">' +
            ['放松', '发呆', '躺平'].map(function (x) {
              return '<span style="font:500 26px ' + FONT_SANS + ';color:#4E837C;background:rgba(255,255,255,.7);padding:6px 16px;border-radius:999px">' + esc(x) + '</span>';
            }).join('') + '</div></div>' +
        '<div style="position:absolute;right:150px;bottom:210px;width:110px;height:110px;border-radius:50%;background:#F6C9A8;opacity:.9;z-index:12"></div>' +
        '<div style="position:absolute;left:180px;bottom:170px;font:500 28px ' + FONT_SANS + ';color:#B7AB97;letter-spacing:4px;z-index:10">slow down 🌿</div>' +
        safeLayer(o);
      return page(canvas('10', 'background:#F5F3EF', inner),
        '.tt{color:#4A3F32;font-family:' + FONT_SANS + ';font-weight:900;line-height:1.42;letter-spacing:2px}' +
        '.tt.lg{font-size:96px}.tt.md{font-size:82px}.tt.sm{font-size:70px}' +
        '.hl{font-style:normal;border:6px solid #E86A4A;border-radius:50% 50% 48% 52%/62% 45% 55% 38%;padding:0 12px}');
    }
  });

  /* ---------- 新增 3 套人像模板（2026-11-03） ---------- */
  var FONT_STICKER = "'CoverHeiBold','HarmonyOS Sans SC','Microsoft YaHei',sans-serif";
  var FONT_BRUSH = "'CoverBrush','Source Han Serif SC Heavy','SimSun',serif";
  var FONT_FACE_CSS =
    '@font-face{font-family:"CoverHeiBold";src:url("fonts/HarmonyOS_Sans_SC_Bold.ttf") format("truetype");font-weight:400}' +
    '@font-face{font-family:"CoverHeiBold";src:url("fonts/HarmonyOS_Sans_SC_Bold.ttf") format("truetype");font-weight:900}' +
    '@font-face{font-family:"CoverBrush";src:url("fonts/SourceHanSerifSC-Heavy.ttf") format("truetype");font-weight:400}' +
    '@font-face{font-family:"CoverBrush";src:url("fonts/SourceHanSerifSC-Heavy.ttf") format("truetype");font-weight:900}';

  /* 11 人像贴纸 —— 亮黄贴纸人像 + 顶部两行描边大字 + 左侧三条勾选清单 + 底部横幅 */
  T.push({
    id: '11-sticker', name: '人像贴纸',
    types: ['自我成长', '知识科普', '通用观点', '情感'],
    structure: '亮黄贴纸人像 + 顶部两行描边大字 + 左侧三条勾选清单 + 底部浅色横幅',
    personDefault: { on: true, x: 'right', outline: 'yellow' },
    requiresPerson: true,
    fieldsExtra: [
      { key: 'items', label: '勾选清单（每行一条，最多 3 条）', type: 'lines', max: 3 }
    ],
    fieldsDefault: {
      title: '把复杂的事 讲简单', subtitle: '三分钟看懂这件事', badge: '方法清单', account: '',
      items: ['先看结论', '再拆步骤', '最后给例子']
    },
    render: function (f, o) {
      f = Object.assign({}, this.fieldsDefault, f);
      var lines = splitTitle2(f.title, 6);
      var s = fitSub(f.subtitle);
      var items = itemsOf(f.items);
      if (!items.length) items = ['清单待填写'];
      var empty = !itemsOf(f.items).length;
      var pBlock = (o.person && o.person.on)
        ? (hasAvatar(o)
            ? '<div class="s11-person">' + avatarFigure(o, { outlineDefault: 'yellow' }) + '<div class="s11-ptag">我的形象</div></div>'
            : personLayer(o, { size: 540, bottom: 270, padX: 20, label: '人物位（先上传形象）', color: '#D9C7A5' }))
        : '';
      var l1 = lines[0] || clip(f.title, 6);
      var l2 = lines[1];
      var banner = s.text || f.badge || '这一期讲清楚';
      var rows = items.map(function (x, i) {
        return '<div class="s11-row" style="transform:rotate(' + (i % 2 ? 1 : -1) + 'deg);' + (empty ? 'opacity:.7;' : '') + '">' +
          '<span class="s11-box">' + (empty ? '·' : '✓') + '</span>' +
          '<span class="s11-txt">' + esc(clip(x, 8)) + '</span></div>';
      }).join('');
      var inner =
        '<div class="s11-bgblob"></div>' +
        '<div class="s11-star" style="left:52px;top:36px">' + starSVG(62, '#FFD84D') + '</div>' +
        '<div class="s11-star" style="right:74px;bottom:300px">' + starSVG(40, '#FFD84D') + '</div>' +
        '<div class="s11-title">' +
          '<div class="s11-l1" style="font-size:' + sizeByLen(l1.length, [[3, 168], [4, 158], [5, 148], [6, 136]]) + 'px">' + esc(l1) + '</div>' +
          (l2 ? '<div class="s11-l2" style="font-size:' + sizeByLen(l2.length, [[4, 150], [5, 140], [6, 130]]) + 'px">' + esc(l2) + '</div>' : '') +
        '</div>' +
        (f.badge ? '<div class="s11-tag">' + esc(clip(f.badge, 8)) + '</div>' : '') +
        pBlock +
        '<div class="s11-list">' + rows + '</div>' +
        '<div class="s11-banner"><span class="s11-emoji">💪</span><span>' + esc(banner) + '</span></div>' +
        '<div class="s11-account">' + esc(clip(f.account, 20)) + '</div>' +
        safeLayer(o);
      var fs = sizeByLen(banner.length, [[10, 62], [14, 54], [18, 48]]);
      return page(canvas('11', 'background:#FFF7E6', inner),
        FONT_FACE_CSS +
        '.s11-bgblob{position:absolute;right:-160px;top:360px;width:760px;height:900px;border-radius:50%;background:#FFE9A8;opacity:.55;z-index:1}' +
        '.s11-title{position:absolute;left:45px;top:56px;right:36px;z-index:20;line-height:1.04}' +
        '.s11-l1{font-family:' + FONT_STICKER + ';font-weight:900;color:#FFE66E;-webkit-text-stroke:9px #5B3A16;paint-order:stroke fill;letter-spacing:2px;transform:rotate(-3deg);text-shadow:7px 10px 0 rgba(91,58,22,.28);transform-origin:left top}' +
        '.s11-l2{font-family:' + FONT_STICKER + ';font-weight:900;color:#FFFDF2;-webkit-text-stroke:9px #5B3A16;paint-order:stroke fill;letter-spacing:2px;margin-left:66px;margin-top:10px;transform:rotate(-2deg);text-shadow:7px 10px 0 rgba(91,58,22,.28);transform-origin:left top}' +
        '.s11-tag{position:absolute;right:38px;top:300px;z-index:22;background:#FFD84D;border:4px solid #5B3A16;border-radius:12px;padding:10px 24px;font:900 38px/1 ' + FONT_STICKER + ';color:#5B3A16;transform:rotate(7deg);box-shadow:7px 7px 0 rgba(91,58,22,.22)}' +
        '.s11-person{position:absolute;right:8px;bottom:236px;width:600px;height:790px;z-index:6}' +
        '.s11-ptag{position:absolute;left:50%;bottom:-6px;transform:translateX(-50%);font:600 22px ' + FONT_SANS + ';color:#8A6A3F;background:#FFFDF5;padding:4px 14px;border-radius:20px;white-space:nowrap}' +
        '.s11-list{position:absolute;left:45px;top:690px;width:384px;z-index:25}' +
        '.s11-row{display:flex;align-items:center;gap:14px;background:#FFFDF6;border-radius:14px;padding:13px 16px;box-shadow:5px 7px 0 rgba(91,58,22,.14);margin-bottom:18px}' +
        '.s11-box{width:44px;height:44px;flex:none;background:#FFD84D;border:3px solid #5B3A16;border-radius:9px;display:flex;align-items:center;justify-content:center;font:900 28px/1 ' + FONT_SANS + ';color:#5B3A16}' +
        '.s11-txt{font:900 34px/1.1 ' + FONT_STICKER + ';color:#3A2A1A;letter-spacing:1px}' +
        '.s11-banner{position:absolute;left:62px;right:78px;bottom:128px;z-index:30;background:#FFFDF5;border:4px solid #5B3A16;border-radius:14px;padding:18px 28px;display:flex;align-items:center;gap:16px;transform:rotate(-2deg);box-shadow:9px 11px 0 rgba(91,58,22,.18)}' +
        '.s11-banner span:last-child{font:900 ' + fs + 'px/1.15 ' + FONT_STICKER + ';color:#4A3418;letter-spacing:1px}' +
        '.s11-emoji{font-size:52px;line-height:1;flex:none}' +
        '.s11-account{position:absolute;left:64px;bottom:62px;z-index:20;font:600 26px ' + FONT_SANS + ';color:#9A805E;letter-spacing:2px}' +
        '.s11-star{position:absolute;z-index:18}');
    }
  });

  /* 12 巨字压人像 —— 品牌行 + 圆角照片窗（巨字在人像之后）+ 左下毛笔大字 + 期数徽章 + 底部双色标题 */
  T.push({
    id: '12-giant', name: '巨字压人像',
    types: ['认知/思维', '营销方法', '通用观点', '知识科普'],
    structure: '顶部品牌行 + 圆角照片窗（巨字压在人像身后）+ 左下毛笔大字 + 期数徽章 + 底部双色大标题',
    personDefault: { on: true, x: 'center', outline: 'none' },
    requiresPerson: true,
    fieldsExtra: [
      { key: 'keyword', label: '照片窗巨字（2–6 字）', type: 'text', max: 12 },
      { key: 'episode', label: '期数徽章（如 第 01 期）', type: 'text', max: 16 },
      { key: 'tagline', label: '品牌行短句（≤14 字）', type: 'text', max: 30 }
    ],
    fieldsDefault: {
      title: '这一步，决定后面怎么走', subtitle: '', badge: '本期', account: '',
      keyword: '选择', episode: '第 01 期', tagline: '说清楚，再行动'
    },
    render: function (f, o) {
      f = Object.assign({}, this.fieldsDefault, f);
      var s = fitSub(f.subtitle);
      var brand = brandText(f.account);
      var tagline = clip(f.tagline || s.text || f.badge, 14);
      var keyword = clip(f.keyword, 6);
      var brush = clip(brand, 8);
      var brushLines = brush.length <= 6 ? [brush, ''] : splitTitle2(brush, 4);
      var episode = clip(f.episode || f.badge, 12);
      var twin = splitColor(f.title, 9);
      var isImg = hasAvatar(o);
      var pBlock = (o.person && o.person.on)
        ? (isImg
            ? '<div class="s12-person">' + avatarFigure(o, { outlineDefault: 'none' }) + '</div>'
            : personLayer(o, { size: 520, bottom: 0, label: '人物位（先上传形象）', color: '#8FA79A' }))
        : '';
      var bottomLines =
        '<span class="s12-b1" style="font-size:' + sizeByLen(twin[0].length, [[6, 112], [8, 104], [9, 96]]) + 'px">' + esc(twin[0]) + '</span>' +
        (twin[1] ? '<br><span class="s12-b2" style="font-size:' + sizeByLen(twin[1].length, [[6, 112], [8, 104], [9, 96]]) + 'px">' + esc(twin[1]) + '</span>' : '');
      var inner =
        '<div class="s12-brandrow">' +
          '<div class="s12-logo">' + esc(brand.slice(0, 1)) + '</div>' +
          '<div class="s12-brandtxt"><div class="s12-brandname">' + esc(brand) + '</div>' +
          '<div class="s12-tagline">' + esc(tagline) + '</div></div></div>' +
        '<div class="s12-window">' +
          '<div class="s12-giant" style="font-size:' + sizeByLen(keyword.length, [[2, 230], [3, 210], [4, 186], [5, 158], [6, 136]]) + 'px">' + esc(keyword) + '</div>' +
          pBlock +
        '</div>' +
        '<div class="s12-brush"><div class="s12-brush-l1">' + esc(brushLines[0]) + '</div>' +
          (brushLines[1] ? '<div class="s12-brush-l2">' + esc(brushLines[1]) + '</div>' : '') + '</div>' +
        '<div class="s12-episode">[ ' + esc(episode) + ' ]</div>' +
        '<div class="s12-search"><span class="s12-search-brand">' + esc(brand) + '</span>' +
          '<span class="s12-search-btn">🔍 抖音搜索</span></div>' +
        '<div class="s12-bottom">' + bottomLines + '</div>' +
        safeLayer(o);
      return page(canvas('12', 'background:#0B3B2C', inner),
        FONT_FACE_CSS +
        '.s12-brandrow{position:absolute;left:44px;right:44px;top:38px;display:flex;align-items:center;gap:16px;z-index:20}' +
        '.s12-logo{width:56px;height:56px;flex:none;border-radius:50%;border:3px solid #E8DCA8;color:#E8DCA8;display:flex;align-items:center;justify-content:center;font:900 28px ' + FONT_STICKER + '}' +
        '.s12-brandname{font:900 38px/1.1 ' + FONT_STICKER + ';color:#F7F2E0;letter-spacing:3px}' +
        '.s12-tagline{font:500 23px/1.4 ' + FONT_SANS + ';color:#9FBDAF;letter-spacing:2px;margin-top:6px}' +
        '.s12-window{position:absolute;left:36px;right:36px;top:158px;height:690px;border-radius:34px;border:7px solid #F7F2E0;background:radial-gradient(circle at 50% 30%,#F4F0E2,#DCE2D2 78%);overflow:hidden;z-index:3}' +
        '.s12-giant{position:absolute;left:0;right:0;top:18px;text-align:center;font-family:' + FONT_BRUSH + ';font-weight:900;color:#13251D;letter-spacing:6px;line-height:1;opacity:.92}' +
        '.s12-person{position:absolute;left:56%;transform:translateX(-50%);bottom:0;width:520px;height:560px;z-index:8}' +
        '.s12-brush{position:absolute;left:48px;top:862px;z-index:20;font-family:' + FONT_BRUSH + ';font-weight:900;color:#FFFDF2;line-height:1.02;text-shadow:4px 6px 0 rgba(0,0,0,.28);letter-spacing:3px}' +
        '.s12-brush-l1{font-size:100px}.s12-brush-l2{font-size:100px;margin-left:34px}' +
        '.s12-episode{position:absolute;right:58px;top:884px;z-index:21;border:4px solid #E8DCA8;color:#F2E7BD;font:900 32px/1 ' + FONT_STICKER + ';padding:14px 20px;transform:rotate(3deg);background:rgba(11,59,44,.55);letter-spacing:1px}' +
        '.s12-search{position:absolute;left:48px;right:48px;top:1074px;height:64px;border-radius:999px;background:#FDFBF0;display:flex;align-items:center;justify-content:space-between;padding:0 10px 0 30px;z-index:20}' +
        '.s12-search-brand{font:700 28px ' + FONT_SANS + ';color:#17392C;letter-spacing:1px}' +
        '.s12-search-btn{height:48px;border-radius:999px;background:#12C47C;color:#05251A;font:900 24px/48px ' + FONT_STICKER + ';padding:0 22px}' +
        '.s12-bottom{position:absolute;left:46px;right:46px;top:1144px;z-index:20;font-family:' + FONT_STICKER + ';font-weight:900;line-height:1.14;letter-spacing:2px;-webkit-text-stroke:3px rgba(6,30,22,.55);paint-order:stroke fill}' +
        '.s12-b1{color:#F5E6A8}.s12-b2{color:#3AD79B}');
    }
  });

  /* 13 色块清单 —— 橙色色块错位标题 + 白人像描边 + 左侧橙色方块清单 + 底部深色主张条 */
  T.push({
    id: '13-blocks', name: '色块清单',
    types: ['通用观点', '自我成长', '如何赚钱', '商家带货', '情感'],
    structure: '橙色色块错位标题 + 白人像描边居中 + 左侧三条橙色方块清单 + 底部深色主张条',
    personDefault: { on: true, x: 'center', outline: 'white' },
    requiresPerson: true,
    fieldsExtra: [
      { key: 'items', label: '勾选清单（每行一条，最多 3 条）', type: 'lines', max: 3 },
      { key: 'claim', label: '底部主张（≤18 字）', type: 'text', max: 30 }
    ],
    fieldsDefault: {
      title: '职场新人 生存指南', subtitle: '牢记3点，80%的问题都能解决', badge: '新人必看', account: '',
      items: ['先看场合', '守时靠谱', '少说多做'], claim: '牢记3点，80%的问题都能解决'
    },
    render: function (f, o) {
      f = Object.assign({}, this.fieldsDefault, f);
      var lines = splitTitle2(f.title, 6);
      var s = fitSub(f.subtitle);
      var items = itemsOf(f.items);
      var empty = !items.length;
      if (empty) items = ['清单待填写'];
      var claim = clip(f.claim || s.text || f.badge || '这一期讲清楚', 18);
      var pBlock = (o.person && o.person.on)
        ? (hasAvatar(o)
            ? '<div class="s13-person">' + avatarFigure(o, { outlineDefault: 'white' }) + '</div>'
            : personLayer(o, { size: 580, bottom: 250, label: '人物位（先上传形象）', color: '#D8C3A7' }))
        : '';
      var l1 = lines[0] || clip(f.title, 6);
      var l2 = lines[1];
      var rows = items.map(function (x, i) {
        return '<div class="s13-row" style="transform:rotate(' + (i % 2 ? 1.2 : -1.2) + 'deg);' + (empty ? 'opacity:.7;' : '') + '">' +
          '<span class="s13-box">' + (empty ? '·' : '✓') + '</span>' +
          '<span class="s13-txt">' + esc(clip(x, 7)) + '</span></div>';
      }).join('');
      var inner =
        '<div class="s13-corner" style="right:34px;top:30px">' + starSVG(54, '#F5841F') + '</div>' +
        '<div class="s13-corner" style="left:24px;bottom:280px">' + starSVG(38, '#F5841F') + '</div>' +
        '<div class="s13-corner" style="right:40px;bottom:274px">' + starSVG(30, '#FFEFC9') + '</div>' +
        '<div class="s13-t1" style="font-size:' + sizeByLen(l1.length, [[4, 150], [5, 140], [6, 128]]) + 'px">' + esc(l1) + '</div>' +
        (l2 ? '<div class="s13-t2" style="font-size:' + sizeByLen(l2.length, [[4, 144], [5, 134], [6, 122]]) + 'px">' + esc(l2) + '</div>' : '') +
        (f.badge ? '<div class="s13-badge">' + esc(clip(f.badge, 8)) + '</div>' : '') +
        '<div class="s13-marks">！！</div>' +
        pBlock +
        '<div class="s13-list">' + rows + '</div>' +
        '<div class="s13-bar"><span class="s13-thumb">👍</span><span class="s13-claim" style="font-size:' + sizeByLen(claim.length, [[12, 54], [16, 46], [18, 40]]) + 'px">' + esc(claim) + '</span>' +
          '<span class="s13-acc">' + esc(clip(f.account, 16)) + '</span></div>' +
        safeLayer(o);
      return page(canvas('13', 'background:#FDF4E6', inner),
        FONT_FACE_CSS +
        '.s13-t1{position:absolute;left:-22px;top:48px;z-index:21;background:#F5841F;color:#FFFDF6;font-family:' + FONT_STICKER + ';font-weight:900;line-height:1.06;padding:6px 48px;letter-spacing:2px;transform:rotate(-2deg);box-shadow:12px 12px 0 rgba(42,33,24,.16)}' +
        '.s13-t2{position:absolute;left:54px;top:218px;z-index:22;background:#FFEDBB;color:#2A2118;border:5px solid #F5841F;font-family:' + FONT_STICKER + ';font-weight:900;line-height:1.06;padding:4px 42px;letter-spacing:2px;transform:rotate(1deg);box-shadow:12px 14px 0 rgba(245,132,31,.22)}' +
        '.s13-badge{position:absolute;right:-6px;top:176px;z-index:23;background:#2A2118;color:#FFEDBB;font:900 28px/1 ' + FONT_STICKER + ';padding:12px 24px;transform:rotate(8deg);letter-spacing:1px}' +
        '.s13-marks{position:absolute;right:26px;top:330px;z-index:23;font:900 92px/1 ' + FONT_STICKER + ';color:#F5841F;transform:rotate(12deg);letter-spacing:0}' +
        '.s13-person{position:absolute;left:50%;transform:translateX(-50%);bottom:238px;width:640px;height:840px;z-index:6}' +
        '.s13-list{position:absolute;left:36px;top:700px;width:372px;z-index:25}' +
        '.s13-row{display:flex;align-items:center;gap:12px;background:#FFF4D6;padding:12px 14px;box-shadow:5px 6px 0 rgba(42,33,24,.18);margin-bottom:16px}' +
        '.s13-box{width:46px;height:46px;flex:none;background:#F5841F;color:#FFFDF6;display:flex;align-items:center;justify-content:center;font:900 28px/1 ' + FONT_SANS + '}' +
        '.s13-txt{font:900 34px/1.1 ' + FONT_STICKER + ';color:#2A2118;letter-spacing:1px}' +
        '.s13-bar{position:absolute;left:24px;right:24px;bottom:108px;height:134px;z-index:30;background:#201A14;border-radius:10px;display:flex;align-items:center;gap:20px;padding:0 28px 0 78px;box-shadow:0 14px 34px rgba(32,26,20,.28)}' +
        '.s13-thumb{position:absolute;left:-18px;top:20px;width:92px;height:92px;border-radius:50%;background:#F5841F;display:flex;align-items:center;justify-content:center;font-size:48px;transform:rotate(-6deg);box-shadow:6px 7px 0 rgba(0,0,0,.2)}' +
        '.s13-claim{font-family:' + FONT_STICKER + ';font-weight:900;color:#FFEFC9;letter-spacing:1px;line-height:1.15}' +
        '.s13-acc{position:absolute;right:26px;top:-34px;font:600 24px ' + FONT_SANS + ';color:#B7A98F;letter-spacing:2px}' +
        '.s13-corner{position:absolute;z-index:19}');
    }
  });

  /* Shared offline XHS templates: one source, same IDs in both products. */
  if (typeof require === "function") {
    T.unshift.apply(T, require("./shared-xhs/templates.cjs").templates);
  }

  /* 类型 → 推荐模板映射（接入工作台用） */
  var TYPE_MAP = {
    '情感': ['09-notes', '10-sticky', '04-magazine', '11-sticker', '13-blocks'],
    '认知/思维': ['05-lefttext', '01-bigtype', '02-whitecard', '12-giant'],
    '如何赚钱': ['06-money', '13-blocks'],
    '营销方法': ['05-lefttext', '02-whitecard', '12-giant'],
    '商家带货': ['07-banner', '13-blocks'],
    '商家营销/门店': ['08-storefront'],
    '通用观点': ['01-bigtype', '10-sticky', '11-sticker', '12-giant', '13-blocks'],
    '知识科普': ['02-whitecard', '11-sticker', '12-giant'],
    '生活方式': ['03-journal', '11-sticker'],
    '自我成长': ['04-magazine', '03-journal', '11-sticker', '13-blocks']
  };

  return {
    templates: T,
    typeMap: TYPE_MAP,
    canvasSize: { width: W, height: H },
    fitTitle: fitTitle,
    fitSub: fitSub,
    byId: function (id) { return T.find(function (x) { return x.id === id; }); }
  };
});
