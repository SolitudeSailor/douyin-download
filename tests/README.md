# tests/ —— 测试脚本

这些是独立脚本；原有脚本自己 `print` + `assert` + 计数，新增系统测试使用标准库 unittest。
统一通过 `run_all.py` 执行，不依赖 pytest。

## 怎么跑

```bash
# 一键全部（推荐）
python tests/run_all.py

# 或单独跑某一个
python tests/test_link_parse.py
```

推荐使用项目虚拟环境：

```
.\.venv\Scripts\python.exe tests/run_all.py
```

## 各脚本覆盖什么

| 脚本 | 用例数 | 覆盖范围 | 联网 |
| --- | --- | --- | --- |
| `test_link_parse.py` | 42 | 链接解析：分享文本、短链、`/video/`、`/note/`、`?modal_id=`、主页、纯 ID、`LinkParseError` 透传 | ❌ |
| `test_history.py` | 22 | 增量下载历史 v2：v1 迁移、**删掉文件后判定为 stale 并重下**、作用域隔离、脏数据容错 | ❌ |
| `test_desc_truncate.py` | 8 | 文案削成前 6 字、文件名/整段路径长度可控；并反证 f2 原生截断会产出 100+ 字符名 | ❌ |
| `test_web_api.py` | 13 | Web API：配置读取、未登录拦截、SSRF 防护（恶意域名/内网/伪装）、链接解析、任务列表 | 仅本机 |

全部不访问外网，可随时跑。`test_web_api.py` 会起一个真实 Flask 服务（默认端口 `8815`，
可用环境变量 `TEST_PORT` 改）。

现有入口一共运行 12 个脚本，其中 `test_agents_document.py` 检查协作规范，
`test_git_tracking.py` 检查工程文件与凭据、运行数据、构建产物的 Git 忽略边界，
`test_github_release.py` 检查 GitHub CI、安全说明和脚本可移植性，
`test_notes_demo.py` 检查视频笔记 Demo 的关键信息架构与交互。
还包括 `test_date_filter.py`（日期解析）、
`test_post_date_flow.py`（分页与日期筛选流程）、`test_lifecycle.py`（启动和退出），
以及 `test_system_integrity.py` 的 28 项系统回归：多线程/跨进程事务、写入失败、
强制下载成功/失败/取消、下载器释放、队列容量、取消状态、登录并发和接口输入校验。

`run_all.py` 自动启用 UTF-8 并创建临时数据目录，所有子进程继承该目录，
不会清空或修改真实 Cookie、登录 profile 和下载历史。每个脚本有 150 秒超时。
涉及 f2 的离线测试用 `support.prepare_offline_f2()` 替换模块导入时的令牌请求，
并阻止外网连接；本机回环连接用于 asyncio 和 Flask 测试。
独立运行 Web、日期流程、文件名、系统及 exe 测试也会自动隔离数据。

`test_frozen_exe.py` 不进入默认入口，需要先构建 exe。
可以设置 `TEST_FROZEN_EXE` 指定候选程序；测试只结束自己的进程树，
发现已有下载器实例时会停止，不会按程序名称批量结束其他实例。

## 两个容易踩的坑

1. **不要用 `pytest tests/`**
   函数名是 `test_*`，但签名是零参、靠 `print` 报结果 —— pytest 一收集就报
   `fixture 'root' not found` 之类的假错误。每个脚本里都写了 `__test__ = False`
   阻止 pytest 收集，所以现在 `pytest` 会干净地说 "no tests ran"。

2. **测试不该往项目根写文件**
   `f2` 一被 import 就往「当前工作目录」写 `logs/`。
   `test_desc_truncate.py` 里会 import f2，所以它在 import f2 **之前**先调用了
   `core._prepare_f2_env()` 把工作目录切到 `data/`，避免跑完测试在项目根凭空
   冒出一个 `logs/`。**新加会 import f2 的测试时，记得照做。**
