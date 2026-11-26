#!/usr/bin/env node
// warm-grain MS 渲染泵（人生端）。与工坊 engine/render.mjs 同构：加载模板页 → CONSUMER_READY → renderAt(t) → 截帧。
// 模板含 t12 垫片（renderAt/loadPack），本泵不改模板 DOM，不做任何逐帧时间推进以外的页面干预。
//
// 用法：
//   node render.mjs --preview 5 --out preview.png          预览某时刻 → PNG
//   node render.mjs --preview 5 --set-static --out s.png   预览（faceMode=static 降级验证）
//   node render.mjs --out renders/warm-grain-demo.mp4      全帧 mp4（默认 10s@30fps）
//   node render.mjs --params params.demo.json --out ...    先 loadPack(params) 再渲染
import { createRequire } from "node:module";
import { spawn } from "node:child_process";
import { existsSync, readdirSync, mkdirSync, writeFileSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";

const args = process.argv.slice(2);
const opt = (name, dflt) => {
  const i = args.indexOf("--" + name);
  return i >= 0 ? args[i + 1] : dflt;
};
const has = (name) => args.includes("--" + name);

const DUR = Number(opt("duration", 10));
const FPS = Number(opt("fps", 30));
const W = Number(opt("width", 1920));
const H = Number(opt("height", 1080));
const OUT = opt("out", "renders/out.mp4");
const PARAMS = opt("params", null);
const SET_STATIC = has("set-static");
const TEMPLATE_DIR = path.resolve(new URL(".", import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, "$1"));

// ---- playwright 解析：本地 → HF_NODE_MODULES → 工坊 motion-kit/node_modules（playwright → playwright-core 回退）----
const req = createRequire(import.meta.url);
function pickPw() {
  for (const m of ["playwright", "playwright-core"]) {
    try { return req(m); } catch (e) { /* try next */ }
  }
  const bases = [process.env.HF_NODE_MODULES, path.resolve(TEMPLATE_DIR, "../../../../jd-motion-kit/motion-kit/node_modules")].filter(Boolean);
  for (const base of bases) {
    try {
      const r2 = createRequire(path.join(base, "package.json"));
      return r2("playwright");
    } catch (e) {
      try {
        const r2 = createRequire(path.join(base, "package.json"));
        return r2("playwright-core");
      } catch (e2) { /* next */ }
    }
  }
  throw new Error("playwright/playwright-core 均不可解析；请设 HF_NODE_MODULES 指向含 playwright 的 node_modules");
}
const pw = pickPw();

// ---- chromium 可执行文件：CHROME_PATH → ms-playwright 缓存（倒序取最高版本）----
function resolveChrome() {
  if (process.env.CHROME_PATH) return process.env.CHROME_PATH;
  const roots = [process.env.LOCALAPPDATA && path.join(process.env.LOCALAPPDATA, "ms-playwright")].filter(Boolean);
  for (const r of roots) {
    if (!existsSync(r)) continue;
    const dirs = readdirSync(r).filter((d) => d.startsWith("chromium-")).sort().reverse();
    for (const d of dirs) {
      for (const sub of ["chrome-win/chrome.exe", "chrome-win64/chrome.exe", "chrome-win/chrome.exe"]) {
        const p = path.join(r, d, sub);
        if (existsSync(p)) return p;
      }
    }
  }
  return null;
}

const main = async () => {
  const executablePath = resolveChrome();
  // --allow-file-access-from-files：file:// 页面内 fetch data-composition-src 组合文件（Chromium 默认禁止 file:// fetch）
  const browser = await pw.chromium.launch({ headless: true, executablePath: executablePath || undefined, args: ["--allow-file-access-from-files"] });
  const page = await browser.newPage({ viewport: { width: W, height: H }, deviceScaleFactor: 1 });
  const errors = [];
  page.on("pageerror", (e) => errors.push(String(e)));
  page.on("console", (m) => { if (m.type() === "error") errors.push(m.text()); });

  const url = pathToFileURL(path.join(TEMPLATE_DIR, "index.html")).href;
  // t14 注入对照探针：页面脚本运行前预置 USER_PARAMS → 垫片回写挂载点 → 断言注入态≠默认态（DOM 级）
  if (has("inject")) {
    await page.addInitScript(() => { window.USER_PARAMS = { probeVar: "INJ-PROBE-VALUE" }; });
  }
  await page.goto(url, { waitUntil: "load" });
  await page.waitForFunction("window.CONSUMER_READY", { timeout: 30000 });
  await page.waitForFunction("!!window.__timelines && !!window.__timelines['main-video']", { timeout: 10000 });

  if (PARAMS) {
    const cfg = JSON.parse(await req("node:fs").promises.readFile(PARAMS, "utf8"));
    const applied = await page.evaluate((c) => window.loadPack(c), cfg);
    console.log("loadPack:", JSON.stringify(applied));
  }
  if (SET_STATIC) {
    const applied = await page.evaluate(() => window.loadPack({ faceMode: "static" }));
    console.log("set-static:", JSON.stringify(applied));
  }
  // 媒体预解码：截图/编码前确保有可绘制帧（否则 seek 后 readyState=1，画面空白）
  await page.evaluate(() => Promise.all(Array.from(document.querySelectorAll("video,audio")).map((m) =>
    new Promise((res) => {
      if (m.readyState >= 2) return res();
      const done = () => res();
      m.addEventListener("loadeddata", done, { once: true });
      m.preload = "auto";
      m.load();
      setTimeout(done, 8000);
    })
  )));
  await page.evaluate(() => window.renderAt(0));
  await settle(page);

  const shot = async (t, file) => {
    await page.evaluate((tt) => window.renderAt(tt), t);
    await settle(page);
    if (file) {
      const buf = await page.screenshot({ type: "png" });
      writeFileSync(file, buf);
      console.log("preview:", t, "->", file);
    }
  };

  if (has("preview")) {
    await shot(Number(opt("preview", 5)), OUT.endsWith(".png") ? OUT : OUT + ".png");
  } else if (has("inject")) {
    // t14 注入对照探针：断言每个挂载点已回写 data-variable-values 且含注入值（注入态≠默认态）
    const inj = await page.evaluate(() => {
      const mounts = [...document.querySelectorAll("[data-composition-src]")];
      const got = mounts.map((m) => { try { return JSON.parse(m.getAttribute("data-variable-values") || "null"); } catch (e) { return "PARSE-FAIL"; } });
      const expect = window.USER_PARAMS && window.USER_PARAMS.probeVar;
      const match = mounts.length > 0 && got.every((v) => v && typeof v === "object" && v.probeVar === expect);
      return { match, mounts: mounts.length, expect: expect || null, got };
    });
    console.log("INJECT-PROBE " + JSON.stringify(inj, null, 1));
    if (!inj.match) { console.error("FATAL: injection contrast probe mismatch"); process.exitCode = 3; }
  } else if (has("probe")) {
    // 几何探针：人脸居中误差 / 期望 vs 实际变换 / 颗粒步进 / 媒体 seek / 组合挂载 / 字体（供验收取证，不产图）
    const pt = Number(opt("probe", 5));
    await page.evaluate((tt) => window.renderAt(tt), pt);
    await settle(page);
    const geo = await page.evaluate((tt) => {
      const GRAIN_STEPS = [[0, 0], [-5, -5], [-10, 5], [5, -10], [-5, 15], [-10, 5], [15, 0], [0, 10], [-15, 0], [10, 5]];
      const [gdx, gdy] = GRAIN_STEPS[Math.floor(tt / 0.05) % 10];
      const out = { t: tt };
      const vid = document.getElementById("a-roll-video");
      const win = document.getElementById("a-roll").getBoundingClientRect();
      out.windowRect = { x: +win.x.toFixed(1), y: +win.y.toFixed(1), w: +win.width.toFixed(1), h: +win.height.toFixed(1) };
      const cs = getComputedStyle(vid);
      const m = cs.transform && cs.transform !== "none" ? new DOMMatrix(cs.transform) : new DOMMatrix();
      out.videoMatrix = { tx: +m.m41.toFixed(2), ty: +m.m42.toFixed(2), sc: +m.a.toFixed(4) };
      out.videoLayout = { w: vid.offsetWidth, h: vid.offsetHeight };
      out.gsapProps = window.gsap ? {
        x: gsap.getProperty(vid, "x"), y: gsap.getProperty(vid, "y"),
        xPercent: +Number(gsap.getProperty(vid, "xPercent")).toFixed(2),
        yPercent: +Number(gsap.getProperty(vid, "yPercent")).toFixed(2),
        scale: +Number(gsap.getProperty(vid, "scale")).toFixed(4),
        origin: gsap.getProperty(vid, "transformOrigin"),
      } : null;
      const ft = (window.__faceTrack || {}).faces || [];
      let f = null;
      if (ft.length) {
        const t0 = ft[0].t, tN = ft[ft.length - 1].t;
        if (tt <= t0) f = ft[0];
        else if (tt >= tN) f = ft[ft.length - 1];
        else {
          let lo = 0, hi = ft.length - 1;
          while (hi - lo > 1) { const mid = (lo + hi) >> 1; if (ft[mid].t <= tt) lo = mid; else hi = mid; }
          const a = ft[lo], b = ft[hi], k = b.t === a.t ? 0 : (tt - a.t) / (b.t - a.t);
          f = { x: a.x + (b.x - a.x) * k, y: a.y + (b.y - a.y) * k, h: a.h + (b.h - a.h) * k };
        }
      }
      const scExp = f ? Math.min(Math.max(0.6 / Math.max(f.h, 1e-6), 1), 1.8) : 1;
      out.faceSample = f ? { x: +f.x.toFixed(4), y: +f.y.toFixed(4), h: +f.h.toFixed(4) } : null;
      out.expected = f ? { sc: +scExp.toFixed(4), xPercent: +((0.5 - scExp * f.x) * 100).toFixed(2), yPercent: +((0.5 - scExp * f.y) * 100).toFixed(2) } : null;
      const vw = vid.videoWidth || 1920, vh = vid.videoHeight || 1080;
      const kw = vw ? vid.offsetWidth / vw : 1, kh = vh ? vid.offsetHeight / vh : 1; // 内容→布局坐标换算
      const facePx = f ? { x: f.x * vw * kw, y: f.y * vh * kh } : { x: vid.offsetWidth / 2, y: vid.offsetHeight / 2 };
      const mapped = { x: m.a * facePx.x + m.m41, y: m.d * facePx.y + m.m42 };
      const winC = { x: win.x + win.width / 2, y: win.y + win.height / 2 };
      // 页面空间对齐：视频盒原点 = 窗口原点（video 100%×100% 填窗），局部点 + 窗口原点 = 页面点
      out.faceMapped = { x: +(win.x + mapped.x).toFixed(1), y: +(win.y + mapped.y).toFixed(1) };
      out.windowCenter = { x: +winC.x.toFixed(1), y: +winC.y.toFixed(1) };
      out.faceCenteringErrorPx = +Math.hypot(win.x + mapped.x - winC.x, win.y + mapped.y - winC.y).toFixed(2);
      out.media = { currentTime: +vid.currentTime.toFixed(3), readyState: vid.readyState, videoWidth: vw, videoHeight: vh };
      const g = document.querySelector(".grain-texture");
      out.grain = { actual: g ? g.style.transform : null, expected: "translate(" + gdx + "%, " + gdy + "%)" };
      out.mountedCompositions = Array.from(document.querySelectorAll("[data-composition-id]")).map((e) => e.getAttribute("data-composition-id"));
      // 默认态对照：无 USER_PARAMS 时挂载点不应有 data-variable-values（t14 注入对照探针的基线）
      out.mountVarAttrs = Array.from(document.querySelectorAll("[data-composition-src]")).map((e) => e.getAttribute("data-variable-values"));
      out.fontsStatus = document.fonts.status;
      return out;
    }, pt);
    console.log("PROBE " + JSON.stringify(geo, null, 1));
  } else {
    // 全帧 mp4：逐帧 renderAt → JPEG 管道 → ffmpeg libx264（与工坊泵同 gamma 管线）
    mkdirSync(path.dirname(OUT), { recursive: true });
    const frames = Math.round(DUR * FPS);
    const ff = spawn("ffmpeg", [
      "-v", "error", "-y",
      "-f", "image2pipe", "-vcodec", "mjpeg", "-framerate", String(FPS), "-i", "pipe:0",
      "-vf", `format=rgb24,scale=${W}:${H}`,
      "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p",
      "-r", String(FPS),
      "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709", "-color_range", "tv",
      "-movflags", "+faststart", OUT,
    ], { stdio: ["pipe", "inherit", "inherit"] });
    let ffErr = null;
    ff.on("error", (e) => (ffErr = e));
    for (let i = 0; i < frames; i++) {
      const t = i / FPS;
      await page.evaluate((tt) => window.renderAt(tt), t);
      await settle(page);
      const buf = await page.screenshot({ type: "jpeg", quality: 95, clip: { x: 0, y: 0, width: W, height: H } });
      if (!ff.stdin.write(buf)) await new Promise((r) => ff.stdin.once("drain", r));
    }
    ff.stdin.end();
    await new Promise((r) => ff.on("close", (c) => r(c)));
    if (ffErr) throw ffErr;
    console.log("mp4:", OUT, frames, "frames");
  }
  await browser.close();
  if (errors.length) { console.error("PAGE_ERRORS:\n" + errors.join("\n")); process.exitCode = 3; }
  console.log("DONE rc=0");
};

const settle = (page) =>
  // 双 rAF 后等视频可绘制帧（readyState≥2，≤2s）：seek 后仅 HAVE_METADATA 时截图会是空窗
  page.evaluate(() => new Promise((r) => {
    const v = document.getElementById("a-roll-video");
    requestAnimationFrame(() => requestAnimationFrame(() => {
      if (!v || v.readyState >= 2) return r();
      const iv = setInterval(() => { if (v.readyState >= 2) { clearInterval(iv); r(); } }, 50);
      setTimeout(() => { clearInterval(iv); r(); }, 2000);
    }));
  }));

main().catch((e) => { console.error("FATAL:", e); process.exit(1); });
