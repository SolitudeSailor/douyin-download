# 抖音视频下载器

[![Tests](https://github.com/SolitudeSailor/douyin-download/actions/workflows/tests.yml/badge.svg)](https://github.com/SolitudeSailor/douyin-download/actions/workflows/tests.yml)

一个双击即用的抖音视频下载工具。底层使用开源库 [f2](https://github.com/Johnserf-Seed/f2)
处理签名与接口，本项目提供友好的 Web 界面与一键打包能力。

> ⚠️ **仅限个人学习与自有内容备份使用。** 请遵守抖音用户协议与《个人信息保护法》，
> 禁止批量抓取他人隐私数据、禁止商业分发。

安全问题及敏感数据提交规范见 [`SECURITY.md`](SECURITY.md)。

---

## 功能

- **一键登录** —— 点按钮打开专属浏览器窗口，扫码登录一次，之后长期有效
- **单个作品下载** —— 粘贴分享链接，下载无水印视频
- **链接形态全兼容** —— 分享文本 / 短链 / `/video/`、`/note/` 页 / **精选页 `?modal_id=`** 等
- **用户主页批量** —— 一次性下载某作者的全部/部分作品
- **增量下载** —— 重复运行自动跳过已下载的作品；**文件被删掉会自动重新下载**
- **日期区间筛选** —— 只下载指定时间范围内的作品
- **自定义命名** —— 默认 `日期_作者_文案`，自动清理非法字符
- **实时进度** —— 网页里看下载进度
- **本地运行** —— 只绑定 `127.0.0.1`，数据不出本机

## 快速开始

1. 从 [Releases](https://github.com/SolitudeSailor/douyin-download/releases) 下载 `douyin-tool.exe`，放到固定文件夹。
2. 双击运行。首次启动通常需要 5～15 秒，请勿重复点击。
3. 点击「打开登录窗口」，在弹出的专属浏览器中完成抖音登录。
4. 粘贴作品链接、分享文本或用户主页链接。
5. 选择下载方式后点击「开始下载」，在任务区域查看进度。

首次登录后会自动保存登录状态，失效时重新登录即可。详细操作见 [使用说明](docs/使用说明.md)。

---

## 登录（必读）

抖音接口需要登录态才能访问。**没有登录会返回 403 `Uifid Not Found`。**

### 用法：点一次按钮，登录一次

1. 点界面上的「**打开登录窗口**」
2. 会弹出一个**专属浏览器窗口** —— 它与你的日常浏览器**完全隔离**，
   不读取也不影响你现有的账号、书签、历史
3. 在这个窗口里扫码（或账号密码）登录抖音
4. 程序检测到登录成功后**自动读取登录态并保存**，窗口自动关闭

之后**无需再登录**，直接粘贴网址就能下载。登录态可长期使用，
失效时界面会提示重新登录。

> 换电脑 / 删掉 `browser_profile/` 目录后，需要重新登录一次。


---

## 命令行用法

```
python -m douyin_tool [选项]

选项：
  --login                打开专属浏览器窗口登录抖音（自动保存登录态）
  -u, --url URL          抖音链接（作品或主页）
  -f, --file FILE        链接清单文件（每行一条）
  -M, --mode {one,post}  one=单个作品（默认），post=用户主页批量
  -o, --out DIR          下载目录
  -k, --cookie COOKIE    抖音 Cookie
  -P, --proxy PROXY      代理，如 http://127.0.0.1:7890
  -n, --naming TEMPLATE  文件名模板，默认 {create}_{nickname}_{desc}
  --max-counts N         最大下载数，0=不限
  --date-start DATE      起始日期 YYYY-MM-DD
  --date-end DATE        结束日期 YYYY-MM-DD
  --no-skip              不跳过已下载的作品
  --port N               Web 端口，0=自动
  --no-browser           启动 Web 但不自动开浏览器
```

命名模板可用变量：`{create}` 日期、`{nickname}` 作者、`{desc}` 文案、
`{aweme_id}` 作品 ID、`{uid}` 作者 ID。

---

## 常见问题

| 现象 | 原因 | 解决 |
|---|---|---|
| 403 `Uifid Not Found` | 登录态缺失或失效 | 点「打开登录窗口」重新登录 |
| 提示"调试通道启动超时" | 上次登录窗口没关干净 | 关掉所有本程序打开的浏览器窗口后重试 |
| 找不到浏览器 | 未装 Chrome/Edge | 安装任一款 Chromium 内核浏览器 |
| 提示"无法从链接中解析出作品 ID" | 链接形态不在识别范围内 | 已修复：现支持 `?modal_id=` 等查询参数形态；若仍失败请把完整分享文本粘进来 |
| 403 `Signature Not Found` | 抖音网关风控规则再次升级 | 需更新程序版本（补新的签名头） |
| 目录建好但里面没视频 | 下载任务未被执行 | 已修复：需 `execute_tasks()` 触发 |
| 下载 0 条但没报错 | 触发风控静默限流 | 降低条数、换网络、稍后重试 |
| 提示"本地已存在，跳过下载" | 文件确实还在下载目录里 | 删掉那个文件再点一次，或勾「强制重新下载」 |
| 提示"文件已丢失，已重新下载" | 记录在案但文件被你删/移走了 | 正常行为，已自动补下 |
| 提示"没有生成视频文件" | 图文作品 / 已被屏蔽 / 私密 | 换一条作品 |
| 历史记录想重来 | 记录失真 | 点顶部「清空下载记录」，会重新校验一遍文件 |
| 网页打不开 | 端口被占 | 用 `--port 8800` 指定端口 |
| 杀软报毒 | PyInstaller 通病 | 添加白名单，或用源码方式运行 |

---

## 项目结构

原则：**目录按「来源」分三层**，根目录只留第 ① 层。

- **① 工程文件（人写）** —— `src/` `tests/` `build/` `docs/`，进仓库
- **② 构建产物（脚本生成）** —— `dist/` `build_tmp/` `release/`，可删、可重建
- **③ 运行时数据（本地生成，含凭据）** —— `data/` `secure/`，不进仓库

```
├── pyproject.toml              包元数据、依赖、命令行入口与工具配置
├── src/douyin_tool/            可安装的应用包（标准 src 布局）
│   ├── __main__.py              `python -m douyin_tool` 入口
│   ├── cli.py                   命令行与程序入口
│   ├── core.py                  下载编排、链接解析、落盘、文件名
│   ├── config.py                配置读写、下载历史、路径解析
│   ├── storage.py               文件锁与原子写入
│   ├── validation.py            Web/CLI 共用输入校验
│   ├── browser_login.py         CDP 自动登录
│   ├── cookie_reader.py         浏览器探测与 Cookie 解析
│   ├── console.py               日志与无控制台兼容
│   ├── win_window.py            Windows 桌面窗口适配
│   ├── web_app.py               Flask Blueprint、应用工厂与服务启动
│   └── web/                     随包分发的模板和静态资源
│
├── tests/                      测试脚本（自带断言的独立脚本，不是 pytest 用例）
│   ├── run_all.py              一键跑全部，汇总结果
│   ├── test_link_parse.py      链接解析（42 例，离线）
│   ├── test_history.py         增量历史与文件级校验（22 例，离线）
│   ├── test_desc_truncate.py   文案截断与文件名长度（8 例，离线）
│   ├── test_web_api.py         Web API + SSRF 防护（13 例，起本机服务）
│   ├── test_lifecycle.py       Web 服务生命周期 / 单实例守卫
│   ├── test_frozen_exe.py      打包态专测（需先打 exe，故意不进 run_all）
│   └── README.md
│
├── docs/                       文档
│   ├── 项目框架结构报告.md      ★ 全项目架构快照（含行号级证据）
│   ├── 使用说明.md              图文版，新手向
│   ├── 分享指南.md              ★ 怎么把工具发给别人 + 防泄露说明
│   ├── 开发计划.md
│   ├── 抖音视频爬取操作文档.md
│   ├── design/draft.html       UI 设计稿（Claude 风格设计令牌）
│   ├── images/                 界面预览截图（含 winbtn/ 窗口按钮调试图）
│   ├── probes/                 ★ 可复用验证脚本（几何量测 / Esc 端到端 / 真实窗口抓图）
│   └── reference/              参考材料（非运行代码）
│       └── douyin_downloader.py   早期教学骨架：不实现签名，只演示 5 步链路
│
├── build/                      打包相关（人写的脚本与配置）
│   ├── douyin_tool.spec        PyInstaller 配置
│   ├── build.bat               一键打包主程序
│   ├── icon.ico                exe 图标（抖音风格双色音符）
│   ├── installer.iss           ★ Inno Setup 安装程序脚本（对外分享用）
│   ├── build_installer.bat     一键：编译安装程序 + 打绿色包 + 同步到分享目录
│   ├── make_portable.py        打绿色免安装包（显式清单，不扫目录）
│   ├── audit_release.py        ★ 发布前安全审计：确认产物无个人信息
│   ├── 使用说明.txt            面向接收者的说明（打包素材）
│   └── ChineseSimplified.isl   Inno Setup 简体中文语言包

├── secure/                     ★ 凭据保险箱 —— 把登录凭据加密归档
│   ├── seal.bat / seal.py      封存：打成 AES-256 加密的 vault.7z（可 --purge 抹掉明文）
│   ├── unseal.bat / unseal.py  解封：还原回原位才能跑程序
│   ├── vault.7z                加密归档本体（生成物，不进仓库）
│   └── README.md               为什么这样做 / 怎么用 / 权限收紧
│
├── data/                       ★ 运行时数据（开发态）—— 全部本地生成，不进仓库
│   ├── config.yaml             配置：Cookie、下载目录、代理、命名模板
│   ├── browser_profile/        独立浏览器 profile（登录态存这里）
│   ├── webview_profile/        WebView2 持久 profile（界面窗口用，约 11MB，不含凭据）
│   ├── download_history.json   增量下载记录（作品ID → 文件路径，v2）
│   ├── douyin_users.db         f2 的用户缓存库
│   ├── Download/               下载的视频，按作者昵称分子目录
│   └── logs/                   运行日志（含 error.log）
│
├── dist/                       构建产物：只有 douyin-tool.exe + data_dir.txt（数据指针）
├── release/   build_tmp/       分享包输出 / PyInstaller 中间产物（跑打包脚本时才出现，可删）
├── requirements.txt / requirements-dev.txt
└── .gitignore
```

### 运行时数据放在哪？

由 `src/douyin_tool/config.py::get_data_dir()` 统一决定，代码里没有第二处硬编码：

| 环境 | 数据目录 | 原因 |
| --- | --- | --- |
| 冻结态（双击 exe） | **exe 同级目录** | 与历史版本一字不差，老用户的登录态、下载记录不会凭空消失 |
| 冻结态 + 指针文件 | `data_dir.txt` 指向的目录 | 本仓库的 `dist/` 用这招把数据引到 `data/`，让 dist 只留构建产物 |
| 开发态（跑源码） | **`<项目根>/data/`** | 源码目录永远只有工程文件，不会被日志和视频淹没 |

> **数据目录指针**：exe 同级放一个 `data_dir.txt`（内容为目标路径，
> 相对路径按 exe 目录解析），冻结态就改写到那里；也可用环境变量
> `DOUYIN_DATA_DIR` 覆盖（优先级更高）。两者都只存在于开发/测试链路，
> **分享包里一个都没有** —— 接收方的行为与历史版本完全一致。

`f2` 的 `logs/`、`douyin_users.db` 都是按**当前工作目录**解析的相对路径，
所以 `core._prepare_f2_env()` 会在导入 f2 前把工作目录切到数据目录，
一次性把所有 f2 产物也收进来。详见 `data/README.md`。

### 凭据怎么保管

登录凭据（Cookie / 浏览器登录态）必然落在上面两处数据目录里。项目根提供了
**`secure/` 凭据保险箱**：一键把 `config.yaml` + `browser_profile/` 等打包成
**AES-256 加密**的归档，可随时封存 / 解封。见 [`secure/README.md`](secure/README.md)。

> 因为程序运行时必须直接读到明文，加密是**主动动作**而非常态：
> 平时保持解封，离开电脑或存档前跑一次 `secure\seal.bat --purge`。

## 打包

先安装开发依赖：`python -m pip install -r requirements-dev.txt`。
脚本优先使用 `DOUYIN_PYTHON` 指定的解释器，其次选择项目 `.venv`、
已激活环境、本机 WorkBuddy 环境，最后查找 PATH 中的 Python。
可用 `DOUYIN_SHARE_DIR` 指定分享包复制目录，省略时仅在本项目生成产物。

```bash
build\build.bat
```

或手动：

```bash
C:/Users/<你>/.workbuddy/binaries/python/envs/default/Scripts/python.exe ^
  -m PyInstaller build/douyin_tool.spec --clean --noconfirm ^
  --workpath build_tmp --distpath dist
```

产物在 `dist/douyin-tool.exe`（约 27 MB，单文件、无控制台窗口、带图标）。

> `--workpath build_tmp` 把 PyInstaller 的中间产物挡在 `build/` 之外，
> 让 `build/` 只保留「人写的」脚本与配置。

> 打包配置要点：`console=False` 隐藏黑框，因此所有 `print` 都改走
> `src/douyin_tool/console.py` 的 `safe_print`（无控制台时静默丢弃，不崩）；
> 崩溃时会弹错误框并写入 exe 同级的 `logs/error.log`。

### 生成对外分享包（安装程序 / 绿色版）

```bash
build\build_installer.bat        # 编译安装程序 + 打绿色免安装 zip
python build\audit_release.py    # 发布前审计（非 0 退出码 = 有泄露风险）
```

产物落在 `release/`。**分享包里只有程序本体** —— 不含 Cookie、登录态、
下载记录或视频，这些由对方首次运行自行生成（`src/douyin_tool/config.py` 会在配置
文件缺失时用内置默认值重建一份空配置）。

安装程序默认装到 `C:\Users\<对方用户名>\DouyinDownloader`，不需要管理员权限；
脚本会拦住误选 `C:\Program Files` 的情况（那里普通用户没有写权限，
程序无法在旁边生成登录态）。

详细说明与审计结果见 [`docs/分享指南.md`](docs/分享指南.md)。

---

## 技术说明

本项目的价值在于**封装**，而非重复造轮子：

- **签名、令牌、接口** 全部由 f2 处理（`a_bogus`、`msToken`、`ttwid` 等）
- 本项目负责：易用的界面、批量能力、增量下载、打包分发
- 关键的工程细节（见 `docs/开发计划.md`）：
  - f2 在导入时会写日志到 `./logs`，需在导入前接管其 logger
  - 打包必须用 `collect_all` 收集 f2 的 conf 与签名 js 文件
  - `handler_download` 不返回文件路径，需自行扫描目录差集
