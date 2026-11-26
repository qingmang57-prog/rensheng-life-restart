// 织梦包装引擎 · 渲染帧泵（结构移植自 385712 open-beta render.mjs，MIT）
// 本地HTTP服务 + playwright 无头帧泵 + JPEG 管道喂 ffmpeg。
// 照抄的关键点：①显式 sRGB→BT.709 色彩链（不发灰）②逐帧 renderAt ③产物存在即拒写。
import fs from 'node:fs';
import path from 'node:path';
import http from 'node:http';
import { once } from 'node:events';
import { spawn } from 'node:child_process';
import { createRequire } from 'node:module';
const require = createRequire(import.meta.url);
const { chromium } = require('playwright-core');

const args = process.argv.slice(2);
const val = k => args.includes(k) ? args[args.indexOf(k) + 1] : undefined;
const root = path.resolve(val('--root'));
const out = path.resolve(val('--out'));
const settings = JSON.parse(fs.readFileSync(root + '/settings.json'));
const fps = settings.fps || 30;   // 默认 30fps：抖音竖屏够用，漏设 fps 时不再退化成 60 白烧 3 倍机器时间
const width = settings.width || 1080;
const height = settings.height || 1920;
const count = Math.round(settings.duration * fps);

// 帧区间分段渲染：--frames a:b 左闭右开，单位为帧。不传则整段 0:count。
// renderAt(t) 是绝对时间纯函数，任意顺序渲染任意区间都能得到一致画面。
const frameArg = val('--frames');
const dumpDir = val('--dump-dir');       // 可选：把编码前的原始 JPEG 逐帧落盘，用于比对渲染层一致性
let frameStart = 0, frameEnd = count;
if (frameArg !== undefined) {
  const m = /^(\d+):(\d+)$/.exec(String(frameArg));
  if (!m) throw Error('--frames must be "a:b"');
  frameStart = Number(m[1]);
  frameEnd = Number(m[2]);
  if (frameStart < 0 || frameEnd > count || frameStart >= frameEnd)
    throw Error('--frames ' + frameArg + ' out of range [0, ' + count + ']');
}

if (fs.existsSync(out)) throw Error('Preserve existing outputs');
fs.mkdirSync(path.dirname(out), { recursive: true });

const types = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css',
  '.json': 'application/json', '.woff2': 'font/woff2', '.ttf': 'font/ttf',
  '.svg': 'image/svg+xml', '.png': 'image/png', '.jpg': 'image/jpeg' };
const server = http.createServer((req, res) => {
  try {
    const file = path.resolve(root, '.' + decodeURIComponent(new URL(req.url, 'http://localhost').pathname));
    if (!file.startsWith(root + path.sep) || !fs.statSync(file).isFile())
      return res.writeHead(404).end();
    res.setHeader('Content-Type', types[path.extname(file)] || 'application/octet-stream');
    fs.createReadStream(file).pipe(res);
  } catch { res.writeHead(404).end(); }
});
server.listen(0, '127.0.0.1');
await once(server, 'listening');

let browser, enc;
const errors = [];
try {
  browser = await chromium.launch({ headless: true,
    executablePath: process.env.CHROME_PATH,
    protocolTimeout: 600000,
    args: ['--force-color-profile=srgb', '--disable-gpu', '--disable-dev-shm-usage',
           '--js-flags=--max-old-space-size=4096'] });
  const page = await browser.newPage({ viewport: { width, height } });
  page.on('pageerror', e => errors.push('PE:' + (e.message || '') + ' @ ' + String(e.stack || '').slice(0, 300)));
  await page.goto(`http://127.0.0.1:${server.address().port}/index.html`);
  await page.evaluate(async () => await window.CONSUMER_READY);
  await page.evaluate(async () => { await document.fonts.ready; });   // 字体就绪再量字，防回归抖动（坑⑱）
  const sfxEvents = await page.evaluate(() => ({ sfx: window.SFX || [], owners: window.SFX_OWNERS || [], calls: window.SFX_CALLS || [] }));
  fs.writeFileSync(root + '/runtime-events.json', JSON.stringify(sfxEvents, null, 1));
  if (errors.length) { console.error('PAGE ERRORS:', JSON.stringify(errors)); throw Error(errors.join('\n')); }

  if (args.includes('--preview')) {
    const t = +(val('--time') ?? 1);
    await page.evaluate(async t => await renderAt(t), t);
    await page.evaluate(async () => {                                    // 截图前资源就绪，防加载竞态（坑⑲）
      await document.fonts.ready;
      await Promise.all([...document.images].map(i => i.complete ? 0 :
        new Promise(r => { i.onload = r; i.onerror = r; })));
      await new Promise(r => setTimeout(r, 120));                          // 解码落帧 settle
    });
    await page.screenshot({ path: out });
  } else {
    const gamma = 'format=rgb24,colorspace=ispace=bt709:irange=pc:iprimaries=bt709:itrc=srgb:' +
      'space=bt709:range=tv:primaries=bt709:trc=bt709:format=yuv420p,sidedata=mode=delete';
    enc = spawn('ffmpeg', ['-v', 'error', '-n', '-f', 'image2pipe', '-vcodec', 'mjpeg',
      '-framerate', String(fps), '-i', 'pipe:0', '-vf', gamma, '-c:v', 'libx264',
      '-preset', 'veryfast', '-crf', '18', '-pix_fmt', 'yuv420p',
      '-frames:v', String(frameEnd - frameStart), '-color_primaries', 'bt709', '-color_trc', 'bt709',
      '-colorspace', 'bt709', '-color_range', 'tv', '-movflags', '+faststart', out],
      { stdio: ['pipe', 'ignore', 'pipe'] });
    let log = ''; enc.stderr.on('data', d => { log += d; if (log.length < 500) console.error('FFERR:', String(d).slice(0, 300)); });
    const finish = once(enc, 'exit');
    for (let f = frameStart; f < frameEnd; f++) {
      await page.evaluate(async t => await renderAt(t), f / fps);
      await page.evaluate(async () => await window.imgWait());
      if (errors.length) { console.error('PAGE ERRORS:', JSON.stringify(errors)); throw Error(errors.join('\n')); }
      let image = null;
      for (let attempt = 0; attempt < 3 && !image; attempt++) {
        try { image = await page.screenshot({ type: 'jpeg', quality: 96 }); }
        catch (e) { console.error('screenshot retry', attempt + 1, e.message.slice(0, 80)); await new Promise(r => setTimeout(r, 800)); }
      }
      if (!image) throw Error('screenshot failed after 3 retries');
      if (dumpDir) {
        fs.mkdirSync(dumpDir, { recursive: true });
        fs.writeFileSync(path.join(dumpDir, String(f).padStart(6, '0') + '.jpg'), image);
      }
      if (!enc.stdin.write(image)) await once(enc.stdin, 'drain');
      if ((f - frameStart) % 600 === 0) console.log(JSON.stringify({ frame: f, range: [frameStart, frameEnd], seconds: f / fps, total: settings.duration }));
    }
    enc.stdin.end();
    const [c] = await finish;
    if (c !== 0) throw Error(log);
  }
  if (settings.family === 'jd-spatial-voice-v1') {
    const report = await page.evaluate(() => window.JD_SPATIAL_REPORT);
    if (!report || report.templateId !== settings.family || !report.mode) throw Error('Spatial report missing');
    fs.writeFileSync(root + '/spatial-render-report.json', JSON.stringify(report, null, 2));
  }
} finally {
  if (enc && !enc.killed && enc.exitCode === null) enc.kill('SIGTERM');
  if (browser) await browser.close();
  server.close();
}
