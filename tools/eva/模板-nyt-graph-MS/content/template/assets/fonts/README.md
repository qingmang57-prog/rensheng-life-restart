# assets/fonts — 字体来源与授权（施工D t11 · 2026-11-19）

上游 `nyt-graph`（HyperFrames 官方示例，Apache-2.0）经 Google Fonts 外链使用两族字体；
本包按 OFL 范式本地化（参照 模板-竖屏锐金-MS engine/fonts/licenses/），**禁止运行时外网字体请求**。

| 文件 | 字族/字重 | 来源（google/fonts 仓库 ofl/） | 授权 |
| --- | --- | --- | --- |
| LibreBaskerville-VF.ttf | Libre Baskerville（可变，覆盖 400/700） | librebaskerville/LibreBaskerville\[wght\].ttf | SIL OFL 1.1（licenses/OFL-LibreBaskerville.txt） |
| LibreFranklin-VF.ttf | Libre Franklin（可变，覆盖 300/400/600） | librefranklin/LibreFranklin\[wght\].ttf | SIL OFL 1.1（licenses/OFL-LibreFranklin.txt） |

- 下载时间：2026-11-19；TTF magic `00 01 00 00` 已核验。
- 可变字体以 `font-weight` 区间声明（Baskerville 400–700 / Franklin 300–700），正文字重需求全部落在区间内，无需静态实例。
- 授权要点（OFL 1.1）：可随包分发、可嵌入渲染；**不得**单独出售字体文件本身；保留版权声明与 OFL 文本（licenses/ 内原件）。
- `Reserved Font Name`：Libre Baskerville —— 改名再分发需遵循 OFL RFN 条款；本包未改名。
