/* ===== t12 云适配垫片（上游无此块；HyperFrames 原生/云渲染不走此路径，仅离线帧泵与人生运行时使用） ===== */
  // 组合挂载：fetch data-composition-src → <template> 内容 importNode 进挂载点（与 CLI 引擎内联同构，离线可复现）。
  // 变量注入（t14 垫片标准）：声明默认 ← 挂载点 data-variable-values ← window.USER_PARAMS，
  // 解析结果幂等回写挂载点 data-variable-values（组合根=querySelectorAll 首命中=挂载点自身，宿主在此取值）。
  // 本模板组合当前声明 0 变量——机制在场、探针可证（注入对照 match=true）；组合内容不受影响。
  function __hfResolveVars(mountEl) {
    let declared = {};
    try { declared = JSON.parse(mountEl.getAttribute("data-variable-values") || "{}") || {}; } catch (e) { declared = {}; }
    const user = (window.USER_PARAMS && typeof window.USER_PARAMS === "object" && !Array.isArray(window.USER_PARAMS)) ? window.USER_PARAMS : {};
    const merged = Object.assign({}, declared, user);
    if (Object.keys(merged).length) mountEl.setAttribute("data-variable-values", JSON.stringify(merged));
    return merged;
  }
  async function __hfMountCompositions() {
    const mounts = document.querySelectorAll("[data-composition-src]");
    for (const m of mounts) {
      const src = m.getAttribute("data-composition-src");
      const res = await fetch(src);
      if (!res.ok) throw new Error("composition fetch failed: " + src);
      const doc = new DOMParser().parseFromString(await res.text(), "text/html");
      const tpl = doc.querySelector("template");
      if (!tpl) throw new Error("no <template> in composition: " + src);
      m.appendChild(document.importNode(tpl.content, true));
      __hfResolveVars(m);
    }
  }

  // 颗粒：与上游 CSS 动画同一 10 档位移表（0.5s 周期，每档 0.05s），由 renderAt 每帧驱动（seek 安全、确定性）。
  const __hfGrainSteps = [[0, 0], [-5, -5], [-10, 5], [5, -10], [-5, 15], [-10, 5], [15, 0], [0, 10], [-15, 0], [10, 5]];
  function __hfApplyGrain(t) {
    const g = document.querySelector(".grain-texture");
    if (!g) return;
    const [dx, dy] = __hfGrainSteps[Math.floor(t / 0.05) % 10];
    g.style.transform = "translate(" + dx + "%, " + dy + "%)";
  }

  // 人物窗内层取景：face 轨迹 {t,x,y,w,h}（归一化，x/y=脸中心；rs 桥输出 6Hz 已平滑，本层只线性插值+clamp，不再平滑）。
  // 变换全部相对窗口盒（xPercent/yPercent/scale），与外层相机节拍解耦：任何窗口尺寸/位置下脸中心=窗中心、脸高≈60%窗高。
  const __hfFaceCfg = { fillH: 0.6, minScale: 1.0, maxScale: 1.8 };
  let __hfFaceTl = null;
  function __hfBuildFaceTl(faces) {
    if (!Array.isArray(faces) || faces.length === 0) return null;
    const el = document.getElementById("a-roll-video");
    if (!el) return null;
    faces = faces.slice().sort(function (a, b) { return a.t - b.t; });
    const pose = function (f) {
      const sc = Math.min(Math.max(__hfFaceCfg.fillH / Math.max(f.h, 1e-6), __hfFaceCfg.minScale), __hfFaceCfg.maxScale);
      return { xPercent: (0.5 - sc * f.x) * 100, yPercent: (0.5 - sc * f.y) * 100, scale: sc };
    };
    gsap.set(el, { transformOrigin: "0px 0px" });
    const tl = gsap.timeline({ paused: true });
    // 相位对齐：tween k 精确覆盖 [faces[k].t, faces[k+1].t]，fromTo 显式起止（无惰性捕获、无一步滞后）。
    // seek 落在样本点 t=faces[k].t 时 progress=0 → 恰为 faces[k] 姿态（脸心=窗心），段内线性插值。
    for (let k = 0; k + 1 < faces.length; k++) {
      const A = pose(faces[k]), B = pose(faces[k + 1]);
      tl.fromTo(el, { xPercent: A.xPercent, yPercent: A.yPercent, scale: A.scale }, {
        xPercent: B.xPercent, yPercent: B.yPercent, scale: B.scale,
        duration: faces[k + 1].t - faces[k].t, ease: "none",
      }, faces[k].t);
    }
    // 尾样本保持：末样本之后（至包时长）保持末姿态（GSAP 超出末 tween 按终值钳制，显式 set 兜底）。
    const L = pose(faces[faces.length - 1]);
    tl.set(el, { xPercent: L.xPercent, yPercent: L.yPercent, scale: L.scale }, faces[faces.length - 1].t);
    tl.progress(0).pause();
    return tl;
  }

  // 媒体同步（离线帧泵专用；云渲染由宿主统一 seek）。可用 window.__hfOfflineMediaSync=false 关闭。
  // 偏差声明：工坊共享帧泵(engine/render.mjs)无媒体 seek 能力（禁改共享件），故同步放在 renderAt 内；真实云宿主行为待验。
  window.__hfOfflineMediaSync = window.__hfOfflineMediaSync !== false;
  function __hfSyncMedia(t) {
    if (!window.__hfOfflineMediaSync) return Promise.resolve();
    const seek = function (m) {
      return new Promise(function (done) {
        if (!m || m.readyState < 1) { done(); return; }
        let settled = false;
        const fin = function () { if (!settled) { settled = true; m.removeEventListener("seeked", fin); done(); } };
        m.addEventListener("seeked", fin);
        try { if (Math.abs(m.currentTime - t) > 1e-4) m.currentTime = t; } catch (e) {}
        if (Math.abs(m.currentTime - t) <= 1e-4) fin();
        setTimeout(fin, 2000);
      });
    };
    return Promise.all([seek(document.getElementById("a-roll-video")), seek(document.getElementById("a-roll-audio"))]);
  }

  window.renderAt = async function (t) {
    const tls = window.__timelines || {};
    for (const id of Object.keys(tls)) {
      if (tls[id] && typeof tls[id].seek === "function") tls[id].seek(t, false);
    }
    if (__hfFaceTl) __hfFaceTl.seek(t, false);
    __hfApplyGrain(t);
    await __hfSyncMedia(t);
  };

  // 参数入口：loadPack({duration,video,captions?,faces,faceMode})；caption 词表重建留给运行时适配层（待验），此处不改字幕。
  window.loadPack = function (cfg) {
    cfg = cfg || {};
    const root = document.getElementById("main-composition");
    if (cfg.duration && cfg.duration > 0) {
      root.dataset.duration = String(cfg.duration);
      ["grain-overlay-comp", "graphics-layer", "captions-layer", "a-roll"].forEach(function (id) {
        const el = document.getElementById(id);
        if (el) el.dataset.duration = String(cfg.duration);
      });
    }
    if (cfg.video) {
      const v = document.getElementById("a-roll-video");
      const a = document.getElementById("a-roll-audio");
      if (v) v.src = cfg.video;
      if (a) a.src = cfg.video;
    }
    if (__hfFaceTl) { __hfFaceTl.kill(); __hfFaceTl = null; }
    gsap.set("#a-roll-video", { clearProps: "transform" });
    const mode = cfg.faceMode || (Array.isArray(cfg.faces) && cfg.faces.length ? "track" : "static");
    if (mode === "track") __hfFaceTl = __hfBuildFaceTl(cfg.faces);
    window.__hfFaceMode = mode;
    return { faceMode: mode, duration: root.dataset.duration };
  };

  // face-track 注入 hook：运行时可预置 window.__faceTrack={mode:"track"|"static",faces:[...]}; 未注入则静态降级（纯相机编排）。
  // 包默认：自产演示轨迹（与 assets/demo/a-roll-demo.mp4 画面同公式生成，确定性）。
  window.__faceTrack = window.__faceTrack || { mode: "track", faces: [{"t":0.0,"x":0.5,"y":0.585705,"w":0.197917,"h":0.444444},{"t":0.1667,"x":0.514155,"y":0.595274,"w":0.197917,"h":0.444444},{"t":0.3333,"x":0.528155,"y":0.600679,"w":0.197917,"h":0.444444},{"t":0.5,"x":0.541846,"y":0.601683,"w":0.197917,"h":0.444444},{"t":0.6667,"x":0.555079,"y":0.598244,"w":0.197917,"h":0.444444},{"t":0.8333,"x":0.567708,"y":0.590511,"w":0.197917,"h":0.444444},{"t":1.0,"x":0.579596,"y":0.578822,"w":0.197917,"h":0.444444},{"t":1.1667,"x":0.590611,"y":0.563688,"w":0.197917,"h":0.444444},{"t":1.3333,"x":0.600634,"y":0.545771,"w":0.197917,"h":0.444444},{"t":1.5,"x":0.609554,"y":0.525853,"w":0.197917,"h":0.444444},{"t":1.6667,"x":0.617274,"y":0.504805,"w":0.197917,"h":0.444444},{"t":1.8333,"x":0.623709,"y":0.483548,"w":0.197917,"h":0.444444},{"t":2.0,"x":0.628789,"y":0.463009,"w":0.197917,"h":0.444444},{"t":2.1667,"x":0.632457,"y":0.444087,"w":0.197917,"h":0.444444},{"t":2.3333,"x":0.634675,"y":0.427609,"w":0.197917,"h":0.444444},{"t":2.5,"x":0.635417,"y":0.414295,"w":0.197917,"h":0.444444},{"t":2.6667,"x":0.634675,"y":0.404726,"w":0.197917,"h":0.444444},{"t":2.8333,"x":0.632457,"y":0.399321,"w":0.197917,"h":0.444444},{"t":3.0,"x":0.628789,"y":0.398317,"w":0.197917,"h":0.444444},{"t":3.1667,"x":0.623709,"y":0.401756,"w":0.197917,"h":0.444444},{"t":3.3333,"x":0.617274,"y":0.409489,"w":0.197917,"h":0.444444},{"t":3.5,"x":0.609554,"y":0.421178,"w":0.197917,"h":0.444444},{"t":3.6667,"x":0.600634,"y":0.436312,"w":0.197917,"h":0.444444},{"t":3.8333,"x":0.590611,"y":0.454229,"w":0.197917,"h":0.444444},{"t":4.0,"x":0.579596,"y":0.474147,"w":0.197917,"h":0.444444},{"t":4.1667,"x":0.567708,"y":0.495195,"w":0.197917,"h":0.444444},{"t":4.3333,"x":0.555079,"y":0.516452,"w":0.197917,"h":0.444444},{"t":4.5,"x":0.541846,"y":0.536991,"w":0.197917,"h":0.444444},{"t":4.6667,"x":0.528155,"y":0.555913,"w":0.197917,"h":0.444444},{"t":4.8333,"x":0.514155,"y":0.572391,"w":0.197917,"h":0.444444},{"t":5.0,"x":0.5,"y":0.585705,"w":0.197917,"h":0.444444},{"t":5.1667,"x":0.485845,"y":0.595274,"w":0.197917,"h":0.444444},{"t":5.3333,"x":0.471845,"y":0.600679,"w":0.197917,"h":0.444444},{"t":5.5,"x":0.458154,"y":0.601683,"w":0.197917,"h":0.444444},{"t":5.6667,"x":0.444921,"y":0.598244,"w":0.197917,"h":0.444444},{"t":5.8333,"x":0.432292,"y":0.590511,"w":0.197917,"h":0.444444},{"t":6.0,"x":0.420404,"y":0.578822,"w":0.197917,"h":0.444444},{"t":6.1667,"x":0.409389,"y":0.563688,"w":0.197917,"h":0.444444},{"t":6.3333,"x":0.399366,"y":0.545771,"w":0.197917,"h":0.444444},{"t":6.5,"x":0.390446,"y":0.525853,"w":0.197917,"h":0.444444},{"t":6.6667,"x":0.382726,"y":0.504805,"w":0.197917,"h":0.444444},{"t":6.8333,"x":0.376291,"y":0.483548,"w":0.197917,"h":0.444444},{"t":7.0,"x":0.371211,"y":0.463009,"w":0.197917,"h":0.444444},{"t":7.1667,"x":0.367543,"y":0.444087,"w":0.197917,"h":0.444444},{"t":7.3333,"x":0.365325,"y":0.427609,"w":0.197917,"h":0.444444},{"t":7.5,"x":0.364583,"y":0.414295,"w":0.197917,"h":0.444444},{"t":7.6667,"x":0.365325,"y":0.404726,"w":0.197917,"h":0.444444},{"t":7.8333,"x":0.367543,"y":0.399321,"w":0.197917,"h":0.444444},{"t":8.0,"x":0.371211,"y":0.398317,"w":0.197917,"h":0.444444},{"t":8.1667,"x":0.376291,"y":0.401756,"w":0.197917,"h":0.444444},{"t":8.3333,"x":0.382726,"y":0.409489,"w":0.197917,"h":0.444444},{"t":8.5,"x":0.390446,"y":0.421178,"w":0.197917,"h":0.444444},{"t":8.6667,"x":0.399366,"y":0.436312,"w":0.197917,"h":0.444444},{"t":8.8333,"x":0.409389,"y":0.454229,"w":0.197917,"h":0.444444},{"t":9.0,"x":0.420404,"y":0.474147,"w":0.197917,"h":0.444444},{"t":9.1667,"x":0.432292,"y":0.495195,"w":0.197917,"h":0.444444},{"t":9.3333,"x":0.444921,"y":0.516452,"w":0.197917,"h":0.444444},{"t":9.5,"x":0.458154,"y":0.536991,"w":0.197917,"h":0.444444},{"t":9.6667,"x":0.471845,"y":0.555913,"w":0.197917,"h":0.444444},{"t":9.8333,"x":0.485845,"y":0.572391,"w":0.197917,"h":0.444444}] };

  window.CONSUMER_READY = (async () => {
    await __hfMountCompositions();
    const ft = window.__faceTrack;
    if (ft && ft.mode === "track" && Array.isArray(ft.faces) && ft.faces.length) {
      __hfFaceTl = __hfBuildFaceTl(ft.faces);
    }
    window.__hfFaceMode = __hfFaceTl ? "track" : "static";
    await window.renderAt(0);
    await document.fonts.ready;
  })();
