#!/usr/bin/env node
/* 硬核解说 · EVA — 模板助手 CLI
 * 用法：
 *   node assistant/motion.mjs check   --params content/template/params.showcase.json
 *   node assistant/motion.mjs preview --params ... --time 3.8 [--scale 0.5] [--out chk.png]
 *   node assistant/motion.mjs render  --params ... [--out demo.mp4] [--scale 0.5] [--dur 30] [--force]
 *   node assistant/motion.mjs mix     --video demo-raw.mp4 --params ... [--out demo-final.mp4] [--force]
 *   node assistant/motion.mjs types                     # 列出 14 种版式
 */
import fs from 'node:fs';
import path from 'node:path';
import { spawnSync } from 'node:child_process';

const here = path.dirname(decodeURIComponent(new URL(import.meta.url).pathname)).replace(/^\/([A-Za-z]:)/, '$1');
const TPL = path.resolve(here, '../content/template');
const args = process.argv.slice(2);
const cmd = args[0];
const val = (k, d) => (args.includes(k) ? args[args.indexOf(k) + 1] : d);
const has = k => args.includes(k);

const TYPES = {
  hero: ['word', 'sub', 'kicker'],
  chapter: ['kicker', 'zh', 'en', 'suben'],
  rail: ['hint', 'hintEn', 'nodes[]'],
  stat: ['num', 'unit', 'cap', 'gauge{}', 'vbar{}'],
  comic: ['bubbles[]', 'punch', 'tags[]', 'emojis[]'],
  diagram: ['head', 'nodes[]', 'edges[]', 'qmark{}'],
  list: ['items[]'],
  paper: ['mast', 'sub', 'photos[]'],
  astro: ['lines[]', 'lineTop', 'lineGap', 'sub', 'bokeh[]'],
  wall: ['rows[]', 'stack[]', 'rowSpeed'],
  replay: ['zh', 'en', 'tag', 'thumbs[]'],
  broll: ['chips[]', 'floats[]', 'tcode{}'],
  illustration: ['img', 'kicker', 'title', 'lead', 'kenburns'],
  product: ['img', 'specs[]', 'price', 'priceUnit', 'cta']
};

/* 任何场景都能挂的四个扩展字段 */
const FX_KEYS = {
  texture: 'grain|halftone|scanlines|vignette|sparkle',
  stickers: '[{src,x,y,w,h,at,anim,rot}]',
  particles: '{ch,n,size,color,speed}',
  fx: 'shake|punch|parallax|glitch'
};

/* 素材根：打包后= content/template；开发态= 模板根目录（assets/ 在这里） */
const ROOTS = [TPL, process.cwd(), path.resolve(TPL, '../..')];
const existsAny = rel => ROOTS.some(r => fs.existsSync(path.join(r, rel)));

function die(m) { console.error('✗ ' + m); process.exit(1); }
function ok(m) { console.log('✓ ' + m); }

if (!cmd || cmd === 'help' || has('--help')) {
  console.log(fs.readFileSync(new URL(import.meta.url), 'utf8').split('*/')[0].replace('/*', ''));
  process.exit(0);
}

if (cmd === 'types') {
  for (const [k, v] of Object.entries(TYPES)) console.log(k.padEnd(13), Object.values(v).join(', '));
  console.log('');
  console.log('任意场景可加：');
  for (const [k, v] of Object.entries(FX_KEYS)) console.log('  ' + k.padEnd(12) + v);
  console.log('');
  console.log('音频：params.audio = { bgm:{src,gain}, sfx:[{name,at,gain}] }');
  process.exit(0);
}

const paramsPath = val('--params');
if (!paramsPath) die('缺少 --params <file.json>');
if (!fs.existsSync(paramsPath)) die('参数文件不存在：' + paramsPath);
let P;
try { P = JSON.parse(fs.readFileSync(paramsPath, 'utf8')); }
catch (e) { die('JSON 解析失败：' + e.message); }

/* ---------------- check ---------------- */
if (cmd === 'check') {
  const errs = [], warns = [];
  const scenes = P.scenes || [];
  if (!scenes.length) errs.push('scenes 为空');
  if (!P.duration) warns.push('未设 duration，将按 24s 出片');
  const types = new Set(Object.keys(TYPES));
  let last = -1;
  scenes.forEach((s, i) => {
    const tag = `scenes[${i}](${s.type || '?'}@${s.t0})`;
    if (!types.has(s.type)) errs.push(`${tag} 未知 type：${s.type}`);
    if (typeof s.t0 !== 'number') errs.push(`${tag} 缺 t0`);
    if (typeof s.t1 !== 'number') errs.push(`${tag} 缺 t1`);
    if (s.t0 < last) warns.push(`${tag} t0 早于上一镜 t1，重叠`);
    if (s.t1 <= s.t0) errs.push(`${tag} t1 <= t0`);
    last = s.t1;
    if (s.type === 'rail' && !(s.nodes || []).length) errs.push(`${tag} rail 需要 nodes`);
    if (s.type === 'diagram') {
      (s.edges || []).forEach((e, k) => {
        if (!s.nodes?.[e.from] || !s.nodes?.[e.to]) errs.push(`${tag} edges[${k}] 指向不存在的节点`);
      });
    }
    /* 素材可达性 */
    const srcs = [];
    if (s.media?.src) srcs.push(s.media.src);
    if (s.host?.src) srcs.push(s.host.src);
    if (s.img) srcs.push(s.img);
    (s.stickers || []).forEach(p => p.src && srcs.push(p.src));
    (s.photos || []).forEach(p => p.src && srcs.push(p.src));
    (s.thumbs || []).forEach(p => p.src && srcs.push(p.src));
    srcs.forEach(src => {
      if (/^https?:/.test(src)) return;
      if (path.isAbsolute(src)) errs.push(`${tag} 素材必须用相对路径（http 根=模板目录）：${src}`);
      else if (!existsAny(src)) errs.push(`${tag} 素材不存在：${src}`);
    });
  });
  (P.captions || []).forEach((c, i) => {
    if (typeof c.t0 !== 'number' || typeof c.t1 !== 'number') errs.push(`captions[${i}] 缺 t0/t1`);
    if (c.t1 <= c.t0) errs.push(`captions[${i}] t1 <= t0`);
  });
  if (P.duration && last < P.duration - 0.01) warns.push(`最后一镜止于 ${last}s，duration=${P.duration}s（尾部会静止）`);
  (P.wipe || []).concat(P.flash || []).forEach(w => {
    if (w.at > (P.duration || 0)) warns.push(`转场 at=${w.at} 超出 duration`);
  });
  /* 音频可达性 */
  const A = P.audio || {};
  if (A.bgm?.src && !existsAny(path.join('assets/audio', A.bgm.src)))
    errs.push(`audio.bgm 不存在：assets/audio/${A.bgm.src}`);
  (A.sfx || []).forEach((x, i) => {
    if (x.at > (P.duration || 0)) warns.push(`audio.sfx[${i}](${x.name}) at=${x.at} 超出 duration`);
    if (!existsAny(path.join('assets/audio/sfx', x.name + '.wav')))
      errs.push(`audio.sfx[${i}] 音效不存在：assets/audio/sfx/${x.name}.wav`);
  });
  warns.forEach(w => console.log('! ' + w));
  if (errs.length) { errs.forEach(e => console.error('✗ ' + e)); process.exit(1); }
  ok(`校验通过：${scenes.length} 镜 / ${(P.captions || []).length} 条字幕 / ${P.duration || '?'}s`);
  process.exit(0);
}

/* ---------------- mix（画面 + BGM + 音效） ---------------- */
if (cmd === 'mix') {
  const video = val('--video');
  if (!video) die('缺少 --video <无声母版.mp4>');
  const mp = ['--video', path.resolve(video), '--params', path.resolve(paramsPath),
    '--audio-dir', path.join(TPL, 'assets/audio'),
    '--out', path.resolve(process.cwd(), val('--out', 'demo-final.mp4'))];
  if (has('--force')) mp.push('--force');
  const env2 = { ...process.env };
  const r2 = spawnSync(process.execPath, [path.join(TPL, 'mix-audio.mjs'), ...mp], { stdio: 'inherit', env: env2, cwd: TPL });
  process.exit(r2.status ?? 1);
}

/* ---------------- preview / render ---------------- */
const pass = [];
const outArg = val('--out');
const map = { '--params': path.resolve(paramsPath), '--out': outArg ? path.resolve(process.cwd(), outArg) : undefined, '--time': val('--time'),
  '--times': val('--times'), '--scale': val('--scale'), '--dur': val('--dur'), '--fps': val('--fps'), '--crf': val('--crf') };
for (const [k, v] of Object.entries(map)) if (v != null) pass.push(k, String(v));
if (cmd === 'preview') pass.unshift('--preview');
if (has('--force')) pass.push('--force');
if (!map['--out']) pass.push('--out', path.resolve(process.cwd(), cmd === 'preview' ? 'chk.png' : 'demo.mp4'));

const env = { ...process.env };
const pkgModules = path.resolve(here, '../../../node/node_modules');
if (!env.NODE_PATH && fs.existsSync(pkgModules)) {
  env.NODE_PATH = pkgModules;
}
const r = spawnSync(process.execPath, [path.join(TPL, 'render.mjs'), ...pass], { stdio: 'inherit', env, cwd: TPL });
process.exit(r.status ?? 1);
