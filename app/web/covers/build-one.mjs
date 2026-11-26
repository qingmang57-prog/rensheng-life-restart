/* 封面渲染构建器：把模板库 render() 的单页 HTML 落盘给 engine/render.mjs 截图。
 * 两种用法（均在 app/web/covers/ 下由 cover_packaging.py 调用）：
 *   node build-one.mjs --dump                     → 打印模板元数据 JSON（styles 用，不渲染）
 *   node build-one.mjs --params <params.json>     → 按 {template_id, fields, person, out_dir} 写 index.html + settings.json
 * 产物规格固定 1080×1440（3:4），与设计稿一致；字段超限由 templates.js 内部降字号/截断。
 */
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const HERE = path.dirname(fileURLToPath(import.meta.url));
const T = require(path.join(HERE, 'templates.js'));
const args = process.argv.slice(2);
const val = k => args.includes(k) ? args[args.indexOf(k) + 1] : undefined;

// 与 render.mjs --preview 约定一致的桩：页面无动画，CONSUMER_READY 立即兑现
const SHIM = '<script>window.CONSUMER_READY=Promise.resolve();window.renderAt=async()=>{};</script>';

if (args.includes('--dump')) {
  const meta = {
    canvasSize: T.canvasSize,
    templates: T.templates.map(t => ({
      id: t.id, name: t.name, types: t.types, structure: t.structure,
      personDefault: t.personDefault, fieldsDefault: t.fieldsDefault,
      requiresPerson: !!t.requiresPerson, fieldsExtra: t.fieldsExtra || [], shared: !!t.shared, version: t.version || ""
    })),
    typeMap: T.typeMap
  };
  process.stdout.write(JSON.stringify(meta));
  process.exit(0);
}

// ── 方案A导出（t13 · 2026-11-17）：--export-template <id> --out <封面库根> ──
// 导出 = --params 同款构建（index.html，含 SHIM 桩与字体硬链）+ --dump 同款元数据
// 落 meta.json + 人生侧 previews/<id>.png 复制为 preview.png。不新增渲染内核。
if (args.includes('--export-template')) {
  const id = val('--export-template');
  const outRoot = path.resolve(val('--out') || '.');
  const tpl = T.byId(id);
  if (!tpl) { console.error('unknown template: ' + id); process.exit(2); }
  const dir = path.join(outRoot, '封面-' + id);
  fs.mkdirSync(dir, { recursive: true });
  const html = tpl.render(tpl.fieldsDefault || {}, { person: tpl.personDefault, showSafe: false })
    .replace('</body>', SHIM + '</body>');
  fs.writeFileSync(path.join(dir, 'index.html'), html);
  if (html.includes('fonts/')) {
    const fontDir = path.join(HERE, '..', '..', 'assets', 'fonts');
    const need = ['HarmonyOS_Sans_SC_Bold.ttf', 'SourceHanSerifSC-Heavy.ttf'];
    fs.mkdirSync(path.join(dir, 'fonts'), { recursive: true });
    for (const name of need) {
      const src = path.join(fontDir, name);
      const dst = path.join(dir, 'fonts', name);
      if (!fs.existsSync(src) || fs.existsSync(dst)) continue;
      try { fs.linkSync(src, dst); } catch { fs.copyFileSync(src, dst); }
    }
  }
  const prevSrc = path.join(HERE, 'previews', id + '.png');
  if (fs.existsSync(prevSrc)) fs.copyFileSync(prevSrc, path.join(dir, 'preview.png'));
  // 工坊同源件对照（接入点地图§2.3：2026-11-02 移植 6 套）；其余 7 套工坊无对应 key
  const WORKSHOP_DUP = { '05-lefttext': 'ip_lefttext', '07-banner': 'ip_banner',
    '08-storefront': 'ip_storefront', '11-sticker': 'ip_sticker',
    '12-giant': 'ip_bigtype_over', '13-blocks': 'ip_blocklist' };
  const meta = {
    id, name: tpl.name, folder: '封面-' + id,
    canvasSize: T.canvasSize, types: tpl.types, structure: tpl.structure,
    personDefault: tpl.personDefault, fieldsDefault: tpl.fieldsDefault,
    requiresPerson: !!tpl.requiresPerson, fieldsExtra: tpl.fieldsExtra || [],
    source: '人生侧 web/covers/templates.js',
    source_mtime: fs.statSync(path.join(HERE, 'templates.js')).mtimeMs,
    exported_at: new Date().toISOString(),
    duplicateOf: WORKSHOP_DUP[id] || null
  };
  fs.writeFileSync(path.join(dir, 'meta.json'), JSON.stringify(meta, null, 2));
  console.log('exported: ' + dir);
  process.exit(0);
}

const params = JSON.parse(fs.readFileSync(val('--params'), 'utf8'));
const tpl = T.byId(params.template_id);
if (!tpl) { console.error('unknown template: ' + params.template_id); process.exit(2); }
const outDir = path.resolve(params.out_dir);
fs.mkdirSync(outDir, { recursive: true });
const html = tpl.render(params.fields || {}, { person: params.person || tpl.personDefault, showSafe: false })
  .replace('</body>', SHIM + '</body>');
fs.writeFileSync(path.join(outDir, 'index.html'), html);

// 新模板引用了本地字体：按需硬链/复制到 src/fonts，避免 file:// 或跨盘取字失败
if (html.includes('fonts/')) {
  const fontDir = path.join(HERE, '..', '..', 'assets', 'fonts');
  const need = ['HarmonyOS_Sans_SC_Bold.ttf', 'SourceHanSerifSC-Heavy.ttf'];
  fs.mkdirSync(path.join(outDir, 'fonts'), { recursive: true });
  for (const name of need) {
    const src = path.join(fontDir, name);
    const dst = path.join(outDir, 'fonts', name);
    if (!fs.existsSync(src) || fs.existsSync(dst)) continue;
    try { fs.linkSync(src, dst); } catch { fs.copyFileSync(src, dst); }
  }
}
fs.writeFileSync(path.join(outDir, 'settings.json'),
  JSON.stringify({ width: T.canvasSize.width, height: T.canvasSize.height, duration: 1, fps: 1 }));
