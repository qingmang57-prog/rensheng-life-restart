# NOTICE-hf（施工E t12 · 2026-11-19）

模板 SPDX-License-Identifier: Apache-2.0（全文未随包：项目指示以官方链接为准，见本目录 LICENSE；
如需离线文本由青芒后续决定补附）。

本目录模板改编自 HyperFrames 官方示例 `warm-grain`（registry type `hyperframes:example`，
本地安装源 npm hyperframes/dist/templates/warm-grain，2026-11-19 快照，只读）。

- 原作版权：Apache License 2.0，Copyright 2026 HeyGen Inc.（上游仓库无独立 NOTICE 文件，
  本文件即为归属声明；全文以 https://www.apache.org/licenses/LICENSE-2.0 为准）。
- 本包改动（相对上游）：
  1. index.html：GSAP CDN → 本包 assets/vendor/gsap.min.js（3.14.2，banner 逐字保留）；
     新增 @font-face 本地字体（assets/fonts，Outfit/Inter 可变字体，SIL OFL 1.1，许可全文随包）；
     新增 data-composition-src 挂载点（intro/graphics/captions 三组合，存根 div 带 data-composition-id）；
     新增双端离线垫片（变量解析链 + 组合挂载 + 人脸时间轴 + 颗粒时间轴驱动 + renderAt + CONSUMER_READY，
     见 assets/hf-adapter.js，非上游文件）。lint 0 error / 21 warning / 4 info 与上游基线完全一致。
  2. compositions/*.html：移除 Google Fonts 外链引用改为继承 index 本地字体；动画时间轴逻辑与上游逐句等价
     （graphics 时间轴 14s、captions 16.02s 超出包长 10s，超范围段从未触发——上游原状，未改动）。
  3. #a-roll 重构：上游组合为直接 video 子元素结构；为离线人脸追踪改为「窗口 div（#a-roll，带计时）+
     内层框 + video（视频层）」三层结构，视觉同构（cover 裁切几何不变，探针验证窗口矩形一致）。
     lint 提示项 timed_element_missing_clip_class / video_nested_in_timed_element 均由此规避
     （计时属性只落在 video 与窗口 div 的 clip 类上，video 不嵌套在带计时属性的元素内）。
  4. 颗粒层：上游 .grain-texture 为 CSS 无限动画（repeat:-1，违反确定性红线）→ 改为垫片按 0.05s 步长
     时间轴驱动同一 10 档位移表（seek 安全、逐帧确定）。纹理 assets/grain-texture.png 为自渲染生成
     （transparenttextures.com 许可不明确，未使用外部素材）。
  5. 人脸追踪：assets/demo/faces.demo.json（自产演示轨迹，60 样本 @6Hz）驱动 #a-roll 内 video 的
     GSAP 姿态时间轴（人脸居中不变量：任意相机节拍下映射误差 0px，探针 4 时间点验证）。
     离线静态回退：loadPack({faceMode:"static"}) → 无变换（上游外观）。真人实时驱动=运行时待验证，
     不在本包承诺范围内。
  6. 新增 settings.json、LICENSE（本包非上游文件）。
- 字体：Outfit、Inter —— SIL Open Font License 1.1（全文见 assets/fonts/*.txt）。
- 演示素材：assets/demo/a-roll-demo.mp4、faces.demo.json、grain-texture.png 为内部自产演示数据，
  不含第三方版权内容。

## 自测证据（2026-11-19，非独立核验）

- hyperframes lint --json：两树（本目录、人生 MS 包）均 rc=0，0 error / 21 warning / 4 info，findings 与上游一致。
- 人生 MS 包泵 render.mjs：--out renders/warm-grain-demo.mp4 rc=0，300 帧 @30fps 1920×1080 bt709，
  ffmpeg -v error 解码干净；帧 150（t=5）与预览 PNG(t=5) mad=1.14（纯 h264 量化）。
- 工坊共享引擎泵（motion-kit/engine/render.mjs，未改动）：CHROME_PATH 指向本机 chromium-1248，
  --root=本目录 rc=0；PNG 预览(t=5) 与 MS 包预览 mad=0.36（跨泵结构一致；mp4 级差 10.35 为两条编码链
  JPEG q95/q96 + libx264 各自量化噪声，窗口内集中属正常）。
- t14 注入对照探针：--inject 预置 window.USER_PARAMS → 挂载点回写 data-variable-values match=true（3 挂载点）；
  默认态 mountVarAttrs=null → 注入态≠默认态。本模板组合声明 0 变量，机制在场、探针可证。
- 几何探针（MS 包）：t=1/5/9/5.25 四点 gsapProps≡期望、faceMapped≡窗口中心（err=0）、颗粒 actual≡expected、
  媒体 currentTime/readyState=4。
