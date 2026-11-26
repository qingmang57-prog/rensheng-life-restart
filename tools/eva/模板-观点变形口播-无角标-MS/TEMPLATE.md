# 观点变形口播-无角标 · TEMPLATE.md（交接文档）
Version 0.1.0 · 2026-11-19 · 方法论样板（OpenMotion video-to-template 交接范式）

## 这是什么
可反复换内容的口播包装母版。换原文/品牌/人物即可出新片，风格与运动语言不变。
配套：MOTION_DESIGN.md（创意规格/不变量）＋ replacement-contract.json（机器可读替换契约）。

## 可换什么（详见 replacement-contract.json）
- 分镜正文 scenes[].blocks[].text（字号自适应＋溢出缩放，长文案拆句不缩到不可读）
- 版式 scenes[].layout（导演按语义+mood 指派，7 枚举）
- 字幕 captions（真实语音）/ 人物帧 host / 人脸轨迹 faces
- 品牌 brand/edition / 背景特效 B-ROLL（assets.json 可选注入）

## 怎么换（忠实基线纪律）
1. 先保忠实基线：风格/运动/卡词/居中不变，只换内容。
2. remix（改编）另走一遍，不混进基线。
3. 换内容后按 MOTION_DESIGN §9 审查：技术层 rc0＋体验层原速听看，成对对比留证。

## 字体替换/近似（记录）
- 正文依赖系统 'Microsoft YaHei'/'Noto Sans CJK SC'；quote 用 Georgia；数字 Arial。
- 上云/异机分发：CJK 字体回退质量未验（待青芒重载验）；如需随包，按 OFL 范式补 @font-face＋license（参照竖屏锐金 engine/fonts/licenses/）。
- 当前未做字体随包，异机渲染可能回退系统字体——记为已知近似。

## 已知差距/未核
- 人物居中残差 dy -.027/-.060（已大幅改善，是否二次微调待青芒目检）。
- CJK 注入字体回退质量、运行时加载/UI、eva-oss schema 接受度：待青芒重载验。
- 真实人脸驱动 face-track 已接（YuNet），离线静态裁切跑通。

## 包可移植检查（别处能渲）
- 视觉核心根相对路径（eva-window.js/runtime.js 同目录）；人物帧 ../pattern 相对。
- 无创建者机器绝对路径硬编（assets.json 注入件路径由调用方给）。
- 帧率/帧数/时长以 preview/demo.mp4 为准（10s/1080p30）。
- 许可随附：LICENSE-EVA.txt（MIT）＋本模板自研声明；上云附 Apache-2.0（引擎）＋NOTICE。

## 验收分层（技术≠审美）
文件安装✓ / 目录登记✓ / 渲染函数✓（双端 rc0）/ AI导演✓（mood桥）/ 运行时加载·UI·真实人脸·CJK＝待青芒重载。
渲染成功不是内容验收；静态截图证明不了运动流畅——体验须原速听看，人眼定验收。
