/* family-overrides.js — 20% 家族覆写注册表（每家族 ≥1 项独家覆写，防通用模板感）
   override() 在 theme.js；此处按 family 注册函数/样式。新增家族必须在此注册独家项。 */
window.FAMILY_OVERRIDES = {
  'type-drama': {
    /* 独家①：描边巨字（空心字压扁加宽，字场签名） */
    keywordWordStyle: `font:900 __WS__px 'NotoSC';color:transparent;-webkit-text-stroke:__SW__px var(--ink);letter-spacing:__LS__px;transform:scaleX(1.06)`,
    /* 独家②：荧光黄爆点条（关键词背后 pop 色条） */
    keywordDecorate(sc, wordEl, L, W, H) {
      const bar = mk(sc.el, '', L.word[0] * W - W * .01, L.word[1] * H + W * .115,
        { style: `width:${W * .36}px;height:${W * .035}px;background:var(--pop);transform:rotate(-1.2deg);border-radius:6px` });
      sc.update = ((orig) => function (t) {
        orig.call(this, t);
        show(bar, t, this.s + .55, { k: 'pop', d: .3 });
      })(sc.update);
    }
  },
  'tech-ring': {
    /* 独家①：金属金渐变字（光环科技签名） */
    keywordWordStyle: `font:900 __WS__px 'NotoSC';letter-spacing:__LS__px;background:linear-gradient(160deg,#fff 0%,#cfd8e8 30%,#8a93a8 52%,#eef2f8 70%,#F5B83D 100%);-webkit-background-clip:text;background-clip:text;-webkit-text-fill-color:transparent;filter:drop-shadow(0 6px 0 rgba(0,0,0,.55))`,
    /* 独家②：金色光轨勾环（svgPath 椭圆绕字，描边进度+端点辉光） */
    keywordDecorate(sc, wordEl, L, W, H) {
      const ww = Math.max(W * .3, measureW(wordEl) + W * .06);   // P0.7 实测字宽
      const d = `M ${W * .01} ${W * .09} C ${W * .08} ${-W * .04}, ${ww * .72} ${-W * .04}, ${ww * .96} ${W * .09} C ${ww * 1.04} ${W * .18}, ${ww * 1.02} ${W * .13}, ${ww * .95} ${W * .21}`;
      const orbit = svgPath(sc.el, d, ww * 1.1, W * .24, 'var(--accent)', Math.round(W * .004), { glow: '245,185,46', hi: true, x: L.word[0] * W - W * .01, y: L.word[1] * H - W * .02 });
      sc._orbit = orbit; sc._orbitT0 = sc.s + .2;
      sc.update = ((orig) => function (t) {
        orig.call(this, t);
        const p = pr(t, this._orbitT0, this._orbitT0 + 1.0);
        if (p > 0) { const [px, py] = pathAt(this._orbit, EZ.out(p)); sparks(FXC, t, t - .02, px, py, { n: 12, life: .4, col: '255,214,120', seed: 5 }); }
      })(sc.update);
    }
  },
  'deep-stage': {
    /* 独家①：立体分层字（三层错位叠影纵深） */
    keywordWordStyle: `font:900 __WS__px 'NotoSC';color:var(--ink);letter-spacing:__LS__px;text-shadow:4px 4px 0 rgba(43,92,255,.45), 8px 8px 0 rgba(43,92,255,.2)`,
    keywordDecorate() { /* 深场覆写点预留：认领屏角标，接多屏认领墙时启用 */ }
  },
  /* 深夜账本（卯叔·持续创业者过来人拆规则风）：深夜办公室底 + 暖金签字 + 朱砂章。
     独家① 签字划线（钢笔金线从字下画出——"这条我给你划出来了"）
     独家② 朱砂章（金线画毕红章砸下带旋转回弹——"这单验证过了"） */
  'night-ledger': {
    keywordWordStyle: `font:900 __WS__px 'NotoSC';color:var(--ink);letter-spacing:__LS__px;text-shadow:0 0 __WS__px rgba(232,169,78,.22),3px 3px 0 rgba(0,0,0,.55)`,
    keywordDecorate(sc, wordEl, L, W, H) {
      const ww = Math.max(W * .3, measureW(wordEl) + W * .06);   // P0.7 实测字宽
      const x0 = L.word[0] * W - W * .01, y0 = L.word[1] * H + W * .118;
      // ① 签字划线：微弧线 + 白色笔尖高光，像钢笔走字
      const d = `M 0 0 C ${ww * .3} ${W * .012}, ${ww * .7} ${-W * .008}, ${ww} ${W * .004}`;
      const pen = svgPath(sc.el, d, ww, W * .03, 'var(--accent)', Math.round(W * .005),
        { glow: '232,169,78', hi: true, x: x0, y: y0 });
      // ② 朱砂章：圆 + 对勾，画完线后砸下；位置取线尾上方并钳进屏内，
      //    刻意压字角——中式压角章的视觉惯例（章盖在字上才是"验过"）
      const cx = Math.min(x0 + ww + W * .02, W * .935);
      const cy = y0 - W * .028, r = W * .017;
      const stamp = mk(sc.el, `<svg width="${r * 2.6}" height="${r * 2.6}" viewBox="0 0 ${r * 2.6} ${r * 2.6}" style="overflow:visible">
          <circle cx="${r * 1.3}" cy="${r * 1.3}" r="${r}" fill="none" stroke="var(--accent2)" stroke-width="${Math.max(2, W * .0028)}"/>
          <path d="M ${r * .75} ${r * 1.32} L ${r * 1.18} ${r * 1.72} L ${r * 1.95} ${r * .82}" fill="none" stroke="var(--accent2)" stroke-width="${Math.max(2, W * .003)}" stroke-linecap="round" stroke-linejoin="round"/>
        </svg>`, cx - r * 1.3, cy - r * 1.3, { ax: 0, ay: 0 });
      stamp.style.opacity = 0;
      sc._pen = pen; sc._stamp = stamp; sc._stampT0 = sc.s + .62;
      sc.update = ((orig) => function (t) {
        orig.call(this, t);
        // 签字划线：0–0.5s
        const p = pr(t, this.s + .1, this.s + .6);
        if (p > 0) {
          pathAt(this._pen, EZ.out(p));
          if (p >= 1 && !this._sparked) { this._sparked = 1;
            const [px, py] = pathAt(this._pen, 1);
            sparks(FXC, t, t - .02, px, py, { n: 8, life: .3, col: '232,169,78', seed: 7 }); }
        }
        // 朱砂章：0.62s 起砸下（弹簧回弹+微旋转），落点红溅；用 place 组合变换，保住定位
        const q = pr(t, this._stampT0, this._stampT0 + .32);
        if (q > 0) {
          const e = EZ.spring(q);
          place(stamp, { x: cx - r * 1.3, y: cy - r * 1.3, r: -10 + 6 * e, s: 1.7 - .7 * e, o: clamp(q * 6) });
          if (q >= 1 && !this._stamped) { this._stamped = 1;
            sparks(FXC, t, t - .02, cx, cy, { n: 12, life: .4, col: '210,82,62', seed: 11 }); }
        }
      })(sc.update);
    }
  },
  /* 纸间破框（亮色存档纸）：独家① 撕纸斜切红条划线（clip-path 毛边，scaleX 扫出）
     独家② 半调网点冲击入场（fx.js halftone 本地件，油墨色） */
  'paper-break': {
    keywordWordStyle: `font:900 __WS__px 'NotoSC';color:var(--ink);letter-spacing:__LS__px;text-shadow:1.5px 1.5px 0 rgba(255,255,255,.5)`,
    keywordDecorate(sc, wordEl, L, W, H) {
      const ww = Math.max(W * .30, measureW(wordEl) + W * .06);
      const x0 = L.word[0] * W - W * .01, y0 = L.word[1] * H + W * .118;
      const bar = mk(sc.el, '', x0, y0, { style: `width:${ww}px;height:${Math.max(8, W * .014)}px;background:var(--ink);transform-origin:left center;clip-path:polygon(0 18%,3% 0,9% 22%,15% 4%,24% 20%,33% 2%,42% 18%,52% 0,61% 20%,70% 5%,79% 19%,88% 3%,96% 18%,100% 8%,99% 82%,94% 100%,86% 80%,77% 97%,68% 82%,58% 100%,48% 83%,39% 98%,29% 81%,20% 99%,11% 84%,4% 100%,0 88%)` });
      const cx = x0 + ww * .5, cy = (L.word[1] + .075) * H;
      sc.update = ((orig) => function (t) {
        orig.call(this, t);
        const p = pr(t, this.s + .05, this.s + .5);
        if (p > 0) place(bar, { x: x0, y: y0, sx: EZ.out(p), o: clamp(p * 4) });
        const q = pr(t, this.s, this.s + .6);
        if (q > 0 && q < 1) halftone(FXC, t, cx, cy, q, .8, '35,32,26', 26);
      })(sc.update);
    }
  },
  /* 活力海报（高饱和撞色）：独家① 撞色弹跳色块衬底（pop 色旋转矩块，弹簧入场垫底） */
  'vibrant-poster': {
    keywordWordStyle: `font:900 __WS__px 'NotoSC';color:var(--ink);letter-spacing:__LS__px;position:relative`,
    keywordDecorate(sc, wordEl, L, W, H) {
      const ww = Math.max(W * .30, measureW(wordEl) + W * .07);
      const x0 = L.word[0] * W - W * .025, y0 = (L.word[1] + .075) * H - W * .052;
      const block = mk(sc.el, '', x0, y0, { style: `width:${ww}px;height:${W * .105}px;background:var(--pop);transform:rotate(-1.4deg)` });
      block.style.zIndex = 0; wordEl.style.zIndex = 2; wordEl.style.position = 'relative';
      sc.update = ((orig) => function (t) {
        orig.call(this, t);
        const p = pr(t, this.s + .05, this.s + .55);
        if (p > 0) place(block, { x: x0, y: y0, r: -1.4, sx: 1.15 - .15 * EZ.spring(p), sy: EZ.spring(p), o: clamp(p * 4) });
      })(sc.update);
    }
  },
  /* 现代图文（亮色 editorial）：独家① 编辑式角标括号 + 基线（accent 双 L 括号夹字） */
  'modern-editorial': {
    keywordWordStyle: `font:900 __WS__px 'NotoSC';color:var(--ink);letter-spacing:__LS__px`,
    keywordDecorate(sc, wordEl, L, W, H) {
      const ww = Math.max(W * .3, measureW(wordEl) + W * .06);
      const x0 = L.word[0] * W - W * .02, y0 = L.word[1] * H - W * .015;
      const k = Math.max(3, W * .004), ext = W * .028;
      const d = `M ${ext} 0 L 0 0 L 0 ${ext} M ${ww} ${W * .19} L ${ww} ${W * .19 + ext} L ${ww - ext} ${W * .19 + ext}`;
      const bracket = svgPath(sc.el, d, ww, W * .22, 'var(--accent)', k, { x: x0, y: y0 });
      const base = mk(sc.el, '', x0, y0 + W * .205, { style: `width:${ww}px;height:${Math.max(2, W * .003)}px;background:var(--accent)` });
      sc._bk = bracket; sc._bkT0 = sc.s + .15;
      sc.update = ((orig) => function (t) {
        orig.call(this, t);
        const p = pr(t, this._bkT0, this._bkT0 + .5);
        if (p > 0) pathAt(this._bk, EZ.out(p));
        const q = pr(t, this.s + .1, this.s + .55);
        if (q > 0) place(base, { x: x0, y: y0 + W * .205, sx: EZ.out(q), o: clamp(q * 4) });
      })(sc.update);
    }
  },
  /* 涂鸦伙伴（手账涂鸦风）：独家① 手绘波浪下划线（svgPath 描边动画，accent 涂鸦线） */
  'doodle-buddy': {
    keywordWordStyle: `font:900 __WS__px 'NotoSC';color:var(--ink);letter-spacing:__LS__px;transform:rotate(-.6deg)`,
    keywordDecorate(sc, wordEl, L, W, H) {
      const ww = Math.max(W * .3, measureW(wordEl) + W * .06);
      const x0 = L.word[0] * W - W * .01, y0 = L.word[1] * H + W * .118;
      let wave = `M 0 0`;
      const n = 14;
      for (let i = 1; i <= n; i++) wave += ` Q ${ww * (i - .5) / n} ${(i % 2 ? -1 : 1) * W * .015}, ${ww * i / n} 0`;
      const doodle = svgPath(sc.el, wave, ww, W * .05, 'var(--accent)', Math.max(4, W * .007), { x: x0, y: y0, blur: 2 });
      sc._dd = doodle; sc._ddT0 = sc.s + .2;
      sc.update = ((orig) => function (t) {
        orig.call(this, t);
        const p = pr(t, this._ddT0, this._ddT0 + .55);
        if (p > 0) {
          pathAt(this._dd, EZ.out(p));
          if (p >= 1 && !this._ddSp) { this._ddSp = 1;
            const [px, py] = pathAt(this._dd, 1);
            sparks(FXC, t, t - .02, px, py, { n: 8, life: .3, col: '255,201,60', seed: 3 }); }
        }
      })(sc.update);
    }
  },
  /* 像素洋（像素游戏风，源自像素海洋学习档案）：独家① 8-bit 像素浪花条（FXC 阶梯正弦波，常驻）
     独家② 金币像素块喷发（入场金块上抛+重力，全部 FXC 方块绘制——本地件零新造） */
  'pixel-ocean': {
    keywordWordStyle: `font:900 __WS__px 'NotoSC';color:var(--accent);letter-spacing:__LS__px;text-shadow:3px 3px 0 #05051a,6px 6px 0 rgba(26,72,168,.55)`,
    keywordDecorate(sc, wordEl, L, W, H) {
      const ww = Math.max(W * .3, measureW(wordEl) + W * .06);
      const x0 = L.word[0] * W - W * .01, y0 = L.word[1] * H + W * .19;
      const s = Math.max(6, Math.round(W * .008));           // 像素块边长
      const cols = Math.ceil(ww / s);
      let coins = null;
      sc.update = ((orig) => function (t) {
        orig.call(this, t);
        // ① 像素浪花：两行阶梯波，8-bit 量化 sin
        for (let r = 0; r < 2; r++)
          for (let i = 0; i < cols; i++) {
            const step = Math.floor((Math.sin(i * .55 + t * 2.4 + r * 1.7) * .5 + .5) * 3);
            const px = x0 + i * s, py = y0 + r * s - step * s * .8;
            FXC.fillStyle = r ? 'rgba(74,125,232,.85)' : 'rgba(232,238,255,.95)';
            FXC.fillRect(px, py, s * .82, s * .82);
          }
        // ② 金币喷发：金块上抛 + 重力 + 闪烁消失
        if (!coins && t >= this.s + .1) coins = Array.from({ length: 14 }, (_, i) => ({
          x: x0 + ww * (.12 + .76 * ((i * 7919) % 100) / 100), y: y0 - s,
          vx: (((i * 104729) % 100) / 100 - .5) * W * .25,
          vy: -H * (.7 + ((i * 1299709) % 100) / 100 * .9),
          t0: this.s + .1 + i * .012 }));
        if (coins) for (const c of coins) {
          const ct = t - c.t0;
          if (ct < 0 || ct > .8) continue;
          const cx = c.x + c.vx * ct, cy = c.y + c.vy * ct + H * 1.1 * ct * ct;
          FXC.globalAlpha = ct > .6 ? 1 - (ct - .6) / .2 : 1;
          FXC.fillStyle = '#F0C020';
          FXC.fillRect(cx, cy, s * .8, s * .8);
          FXC.globalAlpha = 1;
        }
      })(sc.update);
    }
  },
  'doodle-buddy': {
    /* 独家①：胶带角 + 手绘波浪下划线 + 卡片微倾（涂鸦签名，移植自 风格2 样张） */
    keywordWordStyle: `font:900 __WS__px 'NotoSC';color:var(--ink);letter-spacing:__LS__px;transform:rotate(-1.2deg)`,
    keywordDecorate(sc, wordEl, L, W, H) {
      const tape = (x, y, rot) => mk(sc.el, '', x, y,
        { style: `width:${W * .13}px;height:${W * .032}px;background:rgba(255,201,60,.75);border:1px solid rgba(42,39,35,.2);transform:rotate(${rot}deg)` });
      tape(L.word[0] * W - W * .02, L.word[1] * H - W * .035, -6);
      tape((L.word[0] + .55) * W, L.word[1] * H - W * .04, 5);
      const wave = mk(sc.el, `<svg viewBox="0 0 200 20" preserveAspectRatio="none" style="width:100%;height:100%"><path d="M4 14 Q 30 4 55 12 T 105 12 T 155 10 T 196 12" fill="none" stroke="#FF6B57" stroke-width="7" stroke-linecap="round"/></svg>`,
        L.word[0] * W, L.word[1] * H + W * .135, { style: `width:${W * .42}px;height:${W * .032}px` });
      sc.update = ((orig) => function (t) { orig.call(this, t); show(wave, t, this.s + .6, { k: 'mask', d: .35 }); })(sc.update);
    }
  },
  'paper-break': {
    /* 独家①：荧光笔刷底 + 胶带 + 右侧撕纸小口（纸间破框签名，移植自 风格4 样张） */
    keywordWordStyle: `font:900 __WS__px 'NotoSC';color:var(--ink);letter-spacing:__LS__px`,
    keywordDecorate(sc, wordEl, L, W, H) {
      wordEl.style.position = 'relative'; wordEl.style.zIndex = 1;   // 字压在笔刷上
      const hl = mk(sc.el, '', L.word[0] * W - W * .012, L.word[1] * H + W * .085,
        { style: `width:${Math.max(W * .34, (measureW(wordEl) + W * .08))}px;height:${W * .048}px;background:var(--pop);transform:rotate(-.8deg);border-radius:6px 14px 5px 12px;z-index:0` });
      mk(sc.el, '', (L.word[0] + .48) * W, L.word[1] * H - W * .038,
        { style: `width:${W * .11}px;height:${W * .03}px;background:rgba(74,155,255,.4);border:1px solid rgba(35,32,26,.35);transform:rotate(4deg)` });
      mk(sc.el, `<svg width="60" height="120" viewBox="0 0 60 120"><path d="M0 0 L34 14 L18 34 L44 52 L22 76 L40 96 L12 120 L0 120 Z" fill="var(--bg)" stroke="var(--ink)" stroke-width="3"/></svg>`,
        .93 * W, L.word[1] * H, { style: `width:${W * .05}px;height:${W * .1}px` });
      sc.update = ((orig) => function (t) { orig.call(this, t); show(hl, t, this.s + .45, { k: 'mask', d: .3 }); })(sc.update);
    }
  }  ,
  'study-pack': {
    /* 独家①：夜航金句字——暖金光晕 + 灯光白（学习包装签名） */
    keywordWordStyle: `font:900 __WS__px 'NotoSC';color:var(--ink);letter-spacing:__LS__px;text-shadow:0 0 __WS__px rgba(255,209,102,.45), 0 0 __WS__px rgba(255,209,102,.2)`,
    /* 独家②：双色 bokeh 光斑底（fx.stars 金70/蓝30，随节拍呼吸） */
    keywordDecorate(sc, wordEl, L, W, H) {
      sc._bokeh = 1;
      const orig = sc.update;
      sc.update = function (t) {
        orig.call(this, t);
        const breathe = 1 + (window.beatPulse ? beatPulse(t) : 0) * .3;
        stars(FXB, t, 40, .8 * breathe, 7, '255,209,102');
        stars(FXB, t, 18, .5 * breathe, 13, '125,178,255');
      };
    }
  }  ,
  /* ===== 布局级覆写：家族版式分化（2026-10-27 从"换肤"到"换骨"） ===== */
  'type-drama': {
    /* 开幕一锤：巨字压中，贴签分居，口播窗缩为右上小圆窗 */
    keywordLayout: { kicker: [.08, .13], word: [.5, .34], wordSize: .22, sub: [.5, .62], subGap: .06,
                     win: { w: .15, h: .10, x: .85, y: .10 } },
    presenterRound: true,
    /* 原装饰（描边字+爆点条）由字面量下方合并保留 */
  },
  'study-pack': {
    /* 祖传版式：人物大卡居上，金句左下，右下双环圆窗 */
    keywordLayout: { kicker: [.08, .545], word: [.08, .60], wordSize: .115, sub: [.08, .72], subGap: .05,
                     win: { w: .20, h: .135, x: .85, y: .80 } },
    presenterRound: true,
    quoteLayout: { mark: [.08, .545], text: [.10, .60], textSize: .052, textW: .78, who: [.10, .82],
                   win: { w: .20, h: .135, x: .85, y: .80 } }
  },
  'pixel-ocean': {
    /* 游戏界面：右上玩家窗口（方形屏幕带感），关键词=像素大招，右侧留水位尺位 */
    keywordLayout: { kicker: [.08, .545], word: [.08, .60], wordSize: .125, sub: [.08, .73], subGap: .052,
                     win: { w: .30, h: .21, x: .80, y: .16 } },
    presenterRound: false,
    keywordWordStyle: `font:900 __WS__px 'NotoSC';color:var(--pop);letter-spacing:__LS__px;text-shadow:4px 4px 0 #0A0E2C`
  },
  'paper-break': {
    /* 手账纸面：关键词在纸卡下三分之一，口播窗=顶部 taped 照片卡 */
    keywordLayout: { kicker: [.08, .555], word: [.08, .61], wordSize: .135, sub: [.08, .74], subGap: .052,
                     win: { w: .94, h: .48, x: .50, y: .27 } },
    keywordWordStyle: `font:900 __WS__px 'NotoSC';color:var(--ink);letter-spacing:__LS__px`
  },
  'vibrant-poster': {
    /* 海报构图：超大黑字压下三分之一，人物卡缩小居上，字重拉满 */
    keywordLayout: { kicker: [.07, .535], word: [.07, .585], wordSize: .165, sub: [.07, .735], subGap: .055,
                     win: { w: .88, h: .46, x: .50, y: .265 } },
    keywordWordStyle: `font:900 __WS__px 'NotoSC';color:var(--ink);letter-spacing:__LS__px;transform:scaleX(1.04)`
  },
  'doodle-buddy': {
    keywordLayout: { kicker: [.08, .545], word: [.08, .60], wordSize: .13, sub: [.08, .73], subGap: .055,
                     win: { w: .94, h: .50, x: .50, y: .275 } }
  },
  'night-ledger': {
    keywordLayout: { kicker: [.08, .555], word: [.08, .61], wordSize: .125, sub: [.08, .74], subGap: .052,
                     win: { w: .94, h: .50, x: .50, y: .27 } },
    keywordWordStyle: `font:900 __WS__px 'NotoSC';color:var(--ink);letter-spacing:${'__LS__'}px`
  },
  'deep-stage': {
    keywordLayout: { kicker: [.08, .545], word: [.08, .60], wordSize: .14, sub: [.08, .73], subGap: .055,
                     win: { w: .22, h: .15, x: .85, y: .12 } },
    presenterRound: true
  }
};
/* 字面量之后的合并：保留原装饰覆写（避免构造期自引用丢字段） */
Object.assign(window.FAMILY_OVERRIDES['type-drama'],
  { keywordWordStyle: `font:900 __WS__px 'NotoSC';color:transparent;-webkit-text-stroke:__SW__px var(--ink);letter-spacing:__LS__px;transform:scaleX(1.06) scaleY(1.02)`,
    keywordDecorate(sc, wordEl, L, W, H) {
      const bar = mk(sc.el, '', L.word[0] * W - W * .01, L.word[1] * H + W * .115,
        { style: `width:${W * .36}px;height:${W * .035}px;background:var(--pop);transform:rotate(-1.2deg);border-radius:6px` });
      sc.update = ((orig) => function (t) { orig.call(this, t); show(bar, t, this.s + .55, { k: 'pop', d: .3 }); })(sc.update);
    } });
Object.assign(window.FAMILY_OVERRIDES['study-pack'],
  { keywordWordStyle: `font:900 __WS__px 'NotoSC';color:var(--ink);letter-spacing:__LS__px;text-shadow:0 0 __WS__px rgba(255,209,102,.45), 0 0 __WS__px rgba(255,209,102,.2)`,
    keywordDecorate(sc, wordEl, L, W, H) {
      sc.update = ((orig) => function (t) {
        orig.call(this, t);
        const breathe = 1 + (window.beatPulse ? beatPulse(t) : 0) * .3;
        stars(FXB, t, 40, .8 * breathe, 7, '255,209,102');
        stars(FXB, t, 18, .5 * breathe, 13, '125,178,255');
      })(sc.update);
    } });
Object.assign(window.FAMILY_OVERRIDES['doodle-buddy'],
  { keywordWordStyle: `font:900 __WS__px 'NotoSC';color:var(--ink);letter-spacing:__LS__px;transform:rotate(-1.2deg)`,
    keywordDecorate(sc, wordEl, L, W, H) {
      const tape = (x, y, rot) => mk(sc.el, '', x, y,
        { style: `width:${W * .13}px;height:${W * .032}px;background:rgba(255,201,60,.75);border:1px solid rgba(42,39,35,.2);transform:rotate(${rot}deg)` });
      tape(L.word[0] * W - W * .02, L.word[1] * H - W * .035, -6);
      tape((L.word[0] + .55) * W, L.word[1] * H - W * .04, 5);
      const wave = mk(sc.el, `<svg viewBox="0 0 200 20" preserveAspectRatio="none" style="width:100%;height:100%"><path d="M4 14 Q 30 4 55 12 T 105 12 T 155 10 T 196 12" fill="none" stroke="#FF6B57" stroke-width="7" stroke-linecap="round"/></svg>`,
        L.word[0] * W, L.word[1] * H + W * .135, { style: `width:${W * .42}px;height:${W * .032}px` });
      sc.update = ((orig) => function (t) { orig.call(this, t); show(wave, t, this.s + .6, { k: 'mask', d: .35 }); })(sc.update);
    } });
Object.assign(window.FAMILY_OVERRIDES['deep-stage'],
  { keywordWordStyle: `font:900 __WS__px 'NotoSC';color:var(--ink);letter-spacing:__LS__px;text-shadow:4px 4px 0 rgba(43,92,255,.45), 8px 8px 0 rgba(43,92,255,.2)` });
