# NOTICE-hf（施工D t11 · 2026-11-19）

模板 SPDX-License-Identifier: Apache-2.0（全文随包：LICENSE-HF-Apache2.txt；处置按 t7 范式，
全文随包待青芒合并/核验时统一拍板，不单独另行下载）。

本目录模板改编自 HyperFrames 官方示例 `nyt-graph`（registry type `hyperframes:example`，
registry-item.json 见上游；源码经 GitHub 拉取存档于 hf-src/）。

- 原作版权：Apache License 2.0，Copyright 2026 HeyGen Inc.（全文见本包 LICENSE-HF-Apache2.txt；
  上游仓库无独立 NOTICE 文件，本文件即为归属声明）。
- 本包改动（相对上游）：
  1. index.html：GSAP CDN → 本包 assets/vendor/gsap.min.js（3.14.2）；#root → #stage；
     新增 @font-face 本地字体（assets/fonts，SIL OFL 1.1）；
     新增 data-composition-variables 声明与 data-variable-values 演示值；
     新增双端离线垫片（变量解析链 + 组合挂载 + renderAt + CONSUMER_READY）。
  2. compositions/nyt-chart.html：移除 Google Fonts 外链；移除 setTimeout 轮询（无定时器红线）；
     硬编码图表数据/文案 → 变量注入（默认值=上游演示数据）；柱标/线标前后缀变量化；
     动画时间轴逻辑与上游逐句等价。
  3. 新增 assets/fonts（含 OFL 许可文本）、settings.json、render.mjs、params.demo.json（非上游文件）。
- 字体：Libre Baskerville、Libre Franklin —— SIL Open Font License 1.1（见 assets/fonts/licenses/）。
- GSAP 3.14.2：GreenSock 标准许可（"standard no-charge" 许可，随包分发副本与上游示例同源同用）。
