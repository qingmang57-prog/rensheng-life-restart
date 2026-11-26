# 人生重启系统

在 AI 把所有人抹平的时代，让每个普通人把自己的真实经历，炼成不可复制的认知与作品。

一个本地优先的内容创作工作台：对话记录 → 知识卡片 → 选题 → 写稿 → 出片 → 发布 → 复盘 → 60 天打卡，全部数据保存在你自己的电脑上。

## 功能

- **对话**：粘贴文字 / 上传音视频，自动转写入库，生成知识卡片
- **重启系统**：成长总览、命题与主线、知识资产图谱
- **创作中心**：工作总览、找选题（对标账号评论挖掘）、发布与复盘、多平台矩阵、60 天打卡
- **出片**：字幕净剪、模板包装（ffmpeg / Node / 浏览器组件由安装器自动补齐）
- **发布**：抖音扫码连接、发布演练、本人确认后正式发布（patchright 执行器自动补齐）
- **分享**：口播稿一键生成公开分享页（作者署名＋阅读数回流）

## 安装：把链接交给任意 AI 助手（推荐）

把本仓库地址发给 Qoder、千问 WorkBuddy、豆包 Work 等任何 AI 助手，让它照 [INSTALL.md](INSTALL.md) 执行即可全功能安装：

```
git clone https://github.com/qingmang57-prog/rensheng-life-restart.git
cd rensheng-life-restart
python tools/setup_full.py
```

安装器会自检「缺什么」，并按清单「从哪里补」：官网镜像 → GitHub Release 自动回退，sha256 校验、断点续传、重跑不重复下载。仓库只含源码，克隆快；二进制组件按 `tools/components.json` 清单补齐。

**兜底来源**：网络不好时，用旧安装目录或官方网盘完整安装包解压目录零下载补齐：
`python tools/setup_full.py --from-package <目录>`（网盘完整包链接与老客户安装包同渠道发放）。

### 手动安装（不用 AI 助手）

1. 安装 [Python 3.10+](https://www.python.org/downloads/)（勾选 Add to PATH）
2. 双击 `tools\setup_full.bat`（补齐全部组件并自检）
3. 双击 `start.bat`

只跑核心功能（对话、转写、选题、写稿、打卡）也可以：依赖不要自己装，全部由 `tools\setup_full.bat` 补齐（见 INSTALL.md 第 2 节），缺组件时界面会用人话提示缺什么、怎么补。云端转写与大模型通过 `api.jidixh.com` 代理，注册账号即可试用。

首次启动会打开桌面窗口（WebView2）；没有 WebView2 时自动回落浏览器。
数据默认保存在「文档 \ 人生重启系统数据」，删软件不会丢数据。

## 老客户升级

- **链接安装的新副本**：`python tools/setup_full.py --migrate-from <旧包目录>`
  答题信息、认知卡、上传与成片资产自动承接到新资料夹，旧包原样不动。
- **便携包覆盖解压**：第一次打开新版本时自动把包内资料搬到「文档 \ 人生重启系统数据」（只复制不删除）。
- 两边都有资料时程序不覆盖任何一边，会提示用「资产与备份」导出导入合并。

## 出问题怎么办（装好之后）

- **启动器、`tools\chromium` 等被杀毒软件拦截**（窗口不出、后端一启动就退出、界面提示「缺少程序文件」）：到杀毒软件隔离区恢复，只给本软件文件夹加信任，再重跑一次安装命令。步骤见 INSTALL.md「6.1」。
- **想自己体检**：装得齐不齐 → `python tools/setup_full.py --check`；运行问题 → 应用内「导出诊断包」，把生成的 zip 原样发给客服；启动不了、怀疑端口 → `tools\python\python.exe tools\launcher\port_doctor.py`。见 INSTALL.md「6.2」。
- **试用还剩多久、第二次双击没反应、实际端口**：试用剩余看应用内右上角「会员中心」；同一台电脑只能开一个，再双击只是把已开的窗口叫到前台；默认端口 8791，被占会自动往后换，以启动输出为准。见 INSTALL.md「6.3」。

## 组件清单（安装器自动补齐）

| 组件包 | 用途 | 缺少时 |
|---|---|---|
| tools/ffmpeg | 视频导出、净剪出片 | 文稿功能照常，出片按钮提示缺组件 |
| tools/node + tools/smart-packaging | 模板包装渲染 | 包装页提示缺组件 |
| tools/chromium + app/.runtime/browser-env | 抖音采集与发布执行器 | 发布页显示「执行器未安装」 |
| app/.runtime/ai-env | 离线转写（依赖怎么补以 INSTALL.md 第 2 节为准，不要自己跑 pip；--from-package 旧包时零下载） | 自动改用云端转写 |
| tools/motion-template / motion-template-244 / jd-motion-kit / jd-spatial / eva | 包装与动效模板 | 对应模板不可选 |
| tools/avatar-model | 照片抠图（克隆分身） | 抠图按钮提示缺模型 |
| tools/bgm | 配乐 / 音效（CC BY 4.0，署名见 tools/bgm/CREDITS.md） | 成片不带配乐 |
| app/assets/fonts | 字幕字体（HarmonyOS / 思源黑体 / 思源宋体 / 得意黑，均免费商用） | 非默认字幕字体不可选 |
| tools/yt-dlp | 链接取视频 | 提示改用本地文件拖入 |

## 目录结构

```
app/            后端（Python 标准库 http.server）+ 前端（app/web，原生 JS）
tools/launcher/ 桌面外壳启动器（单实例、托盘、WebView2 窗口、看门狗）
tools/boot.py   后端入口（资料夹定位、升级闸、venv 修复）
tools/setup_full.py  全功能安装器（INSTALL.md 是 AI 助手安装契约）
tools/components.json 组件清单（sha256＋双镜像 URL）
start.bat       启动脚本（随包 Python 优先，否则用系统 Python）
```

## 数据与隐私

- 所有记录进本地 SQLite（`workbench.sqlite3`），随备份 / 导入导出走
- 云端令牌用 Windows DPAPI 加密，只为本机本用户可解
- 删除记录仅限本人显式请求，无任何自动清理路径