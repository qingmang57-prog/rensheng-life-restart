/* ============================================================
   EVA 人像窗组件（复用，不重写）
   来源：开源 eva-video-style 引擎 engine/engine.js
        第 83–144 行 compilePath / evaInit / evaRender，MIT © 2026 RoverEva
   只做四处适配（算法与参数语义原样保留）：
     1. 全局 S 改为实例闭包，画布/素材尺寸由 opt 传入
     2. 帧路径由 seq(evaDir, …) 改为 opt.frame(t) 回调
     3. ringFn 多传一个当前几何 g（原版只传 t），便于按窗口大小切描边
     4. ring 新增可选 mask（原版无），用于大窗左侧柔化边缘；不传则与原版一致
   语义同原版：
     keys [{ t, d, L:{x,y,w,h,r,s,o} }] —— 每个 key 从"当前所在位置"五点缓动到 L，
     r = min(w,h)/2 即正圆；头部永不裁切（face 标定 + headFill 上限）；
     换镜头时窗口可原地变形（大窗 ↔ 圆窗），不会跳。
   ============================================================ */
window.EVAW = (function () {
  'use strict';
  var GEO = ['x', 'y', 'w', 'h', 'r', 's', 'o'];
  function clamp(x, a, b) {
    a = (a === undefined ? 0 : a); b = (b === undefined ? 1 : b);
    return Math.max(a, Math.min(b, x));
  }
  function lerp(a, b, u) { return a + (b - a) * u; }
  var ease = {
    smooth5: function (u) { u = clamp(u); return u * u * u * (u * (u * 6 - 15) + 10); },
    out3: function (u) { return 1 - Math.pow(1 - clamp(u), 3); }
  };

  function compilePath(keys, CW, CH) {
    function norm(L) { return Object.assign({ x: 0, y: 0, w: CW, h: CH, r: 0, s: 1, o: 1 }, L); }
    var ks = keys.map(function (k) {
      return { t: k.t, d: k.d || 0, L: norm(k.L), e: k.ease || 'smooth5' };
    }).sort(function (a, b) { return a.t - b.t; });
    var at = function (t, upto) {
      var i = -1;
      for (var j = 0; j < upto; j++) if (ks[j].t <= t) i = j;
      if (i < 0) return ks[0].L;
      var k = ks[i];
      if (!k.from) k.from = i === 0 ? k.L : at(k.t, i);
      var u = k.d > 0 ? ease[k.e]((t - k.t) / k.d) : 1;
      var g = {};
      for (var n = 0; n < GEO.length; n++) g[GEO[n]] = lerp(k.from[GEO[n]], k.L[GEO[n]], u);
      return g;
    };
    return { at: function (t) { return at(t, ks.length); } };
  }

  function create(stage, opt) {
    var CW = opt.width, CH = opt.height;
    var srcW = opt.srcW || CW, srcH = opt.srcH || CH;
    var face = opt.face || { x: .5, y: .35, h: .5 };
    var headFill = (opt.headFill == null ? .6 : opt.headFill);

    var box = document.createElement('div');
    box.id = opt.id || 'eva';
    stage.appendChild(box);
    var img = document.createElement('img');
    img.alt = '';
    box.appendChild(img);

    var hud = document.createElement('div');
    Object.assign(hud.style, { position: 'absolute', zIndex: 51, pointerEvents: 'none' });
    stage.appendChild(hud);
    ['tl', 'tr', 'bl', 'br'].forEach(function (c) {
      var d = document.createElement('div');
      d.className = 'hudc ' + c;
      hud.appendChild(d);
    });

    var path = compilePath(opt.keys || [{ t: 0, L: {} }], CW, CH);
    var lastSrc = '';

    function render(t) {
      var g = path.at(t);
      if (opt.faceAt) face = opt.faceAt(t);
      var w = g.w, h = g.h, zoom = Math.max(1, g.s || 1);
      var rad = Math.min(g.r, Math.min(w, h) / 2);
      var vis = g.o > 0.01;

      box.style.visibility = vis ? 'visible' : 'hidden';
      Object.assign(box.style, {
        left: g.x.toFixed(2) + 'px', top: g.y.toFixed(2) + 'px',
        width: w.toFixed(2) + 'px', height: h.toFixed(2) + 'px',
        borderRadius: rad.toFixed(2) + 'px', opacity: clamp(g.o).toFixed(3)
      });

      /* 头部永不裁切：按 headFill 上限压住放大倍率，并把窗口对到脸上 */
      var faceY = opt.centerY == null ? .5 : opt.centerY;
      var headScale = Math.min(w,h) * headFill / (Math.max(.05,face.h) * srcH * 1.30);
      var cover = Math.max(headScale, w/(2*srcW*Math.max(.05,face.x)), w/(2*srcW*Math.max(.05,1-face.x)), h/(2*srcH*Math.max(.05,face.y)), h/(2*srcH*Math.max(.05,1-face.y)));
      var iw=srcW*cover, ih=srcH*cover;
      var left=w/2-face.x*iw, top=h*faceY-face.y*ih;
      Object.assign(img.style, {
        width: iw.toFixed(2) + 'px', height: ih.toFixed(2) + 'px',
        left: left.toFixed(2) + 'px', top: top.toFixed(2) + 'px'
      });

      if (vis && opt.frame) {
        var s = opt.frame(t);
        if (s && s !== lastSrc) { lastSrc = s; img.src = s; }
      }

      var ring = opt.ringFn ? opt.ringFn(t, g) : { color: '#fff', width: 0, shadow: 0 };
      var hb = hud;
      hb.style.display = (ring.hud && vis && w < 1900) ? 'block' : 'none';
      if (ring.hud) {
        var m = 12;
        Object.assign(hb.style, {
          left: (g.x - m) + 'px', top: (g.y - m) + 'px',
          width: (w + 2 * m) + 'px', height: (h + 2 * m) + 'px',
          color: ring.hud, opacity: clamp(g.o)
        });
      }
      box.style.filter = ring.filter || '';
      box.style.maskImage = box.style.webkitMaskImage = ring.mask || '';
      box.style.boxShadow = w >= 1900 ? 'none'
        : '0 0 0 ' + ring.width + 'px ' + ring.color +
          ', 0 28px 70px rgba(0,0,0,' + (ring.shadow == null ? .45 : ring.shadow) + ')' +
          (ring.glow ? ', 0 0 60px ' + ring.glow : '');
    }

    return { render: render, img: img, box: box, path: path };
  }

  return { create: create, compilePath: compilePath, ease: ease };
})();
