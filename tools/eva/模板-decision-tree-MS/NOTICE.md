# NOTICE / 溯源与改动清单（hf-decision-tree-169）

## 溯源
- 上游：heygen-com/hyperframes（GitHub），HyperFrames 官方示例 decision-tree
- 注册表条目：registry/examples/decision-tree（registry-item.json：name=decision-tree, type=hyperframes:example, 1920×1080, duration 10）
- 抓取时间：2026-11-19；文件：index.html（jsdelivr 镜像）、compositions/decision_tree.html、registry-item.json（raw）
- 许可：仓库级 Apache-2.0（见 LICENSE）

## 相对上游的改动（t7 云适配打样，2026-11-19）
1. index.html：gsap CDN 脚本 → 本地 `assets/vendor/gsap.min.js`（3.14.2 vendored，禁运行时 CDN）
2. index.html：删除 Google Fonts Inter 外链；字体栈 `"Segoe UI","Microsoft YaHei",Arial,sans-serif`（本机可解析；未内置字体文件——下载红线）
3. compositions/decision_tree.html：组合内二次 gsap CDN 脚本移除（父页已加载）
4. compositions/decision_tree.html：`font-family: "Inter"` → `inherit`
5. compositions/decision_tree.html：IIFE 前置变量注入块——9 个节点/标签文本自挂载点 `data-variable-values` 注入（默认值=上游演示文案）；`#node-python` 只改 `#python-text` span 以保结构
6. compositions/decision_tree.html：打字序列数据驱动——`words` 数组由注入目标文本派生（默认值时与上游逐帧等价）；3 处 innerHTML 硬编回调改安全 DOM 构建
7. index.html：新增离线打样垫片——组合挂载（fetch+template 内联）、`window.renderAt(t)`（seek 全部 `__timelines`，suppressEvents=false）、`window.CONSUMER_READY`
8. index.html：根容器 id `root`→`stage`（MS 帧泵缩放钩子，无脚本引用冲突）
9. 动画逻辑（缓动/时长/标签/时间轴结构）零改动；`data-duration="12"`（组合内）与挂载 10 不一致保持上游原样（挂载为准）

## 演示文案偏差
上游演示含 "Start with Pythom"（原样错拼）→ 高亮 → 修正为 "Python" 的桥段；
变量化后初始文本即为目标文本（默认 "Start with Python"），错拼预览段不再出现（高亮→重打节拍结构保留）。
