# NOTICE（HF-PP · hf/product-promo）

本目录为 product-promo 模板的工坊端（motion-three）载体，与 eva-oss 元数据包
`eva-oss/templates/HF-PP/1.0.0/` 配对；人生端镜像包为
`tools/eva/模板-product-promo-MS/`（content/template 与本目录 index/compositions/assets 同源）。

## 上游与许可

- 上游：heygen-com/hyperframes `registry/examples/product-promo`（Apache-2.0，仓库级），
  2026-11-19 快照。SPDX 许可标识与指针见本目录 `LICENSE-EVA.txt` 第①节；
  Apache-2.0 全文是否随包由青芒在合并/核验阶段统一拍板（本包不自带全文）。
- GSAP 3.14.2 vendored（`assets/vendor/gsap-3.14.2.min.js`），文件头许可 banner 逐字保留，
  sha256 c174bfce…d8280 与 HF-DT 打样包同源。
- Inter 字体（可变字重 latin 子集）vendored（`assets/fonts/Inter-Variable-latin.woff2`），
  SIL Open Font License 1.1，许可证全文随包（`assets/fonts/OFL-Inter-LICENSE.txt`）。

## 商标中性化（改编决策，2026-11-19 施工C）

上游示例以 Figma 品牌为演示素材；为规避商标与品牌色风险，本包做了系统性中性化：

1. 彩色五瓣 logo → 中性五形排布（琥珀/蓝/鼠尾草/陶土/紫罗兰圆角几何），
   SVG 源在本目录 `assets/placeholder/`（含「示例资产」头注）。
2. 品牌色重定向（含 JS 补间色）：#f24e1e→#E8A33D、#a259ff→#3D8FE8、
   #ff7262→#D26B5C、#1abcfe→#59B98A、#0acf83→#8A6FD1。
3. 文案中性化：tagline "Great things ship together."、url example.com、
   英文营销字幕保留（无商标词）。
4. 全模板 grep 校验无 figma 残留（含 SVG 与 JS 补间色值）。

## 时间轴适配（离线垫片）

- 场景窗口经 `HF_PROMO_CONFIG` 变量驱动（URL query → USER_PARAMS → 默认：
  s1 0+1.5 / canvas 1.5+12.5 / s3 14+6，总长 20s）；官方运行时按 data-start/duration 接管。
- 离线端垫片：`window.renderAt(t)` 按窗口映射 seek 全部 `__timelines` +
  宿主窗口显隐 + 组合根内联尺寸（等效官方运行时行为）；`window.CONSUMER_READY` 于挂载完成+字体就绪后 resolve。
- `gsap_studio_edit_blocked` / `composition_self_attribute_selector` 等 lint 警告为上游固有写法，
  按青芒裁决保留原样（0 error 即通过）。
