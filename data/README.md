# data/ —— 运行时数据目录

这里放的是**程序跑起来才会产生、并且会变**的东西。
源码目录（`src/` `web/` `tests/` `build/`）只保留工程文件，运行时产物一律下沉到这里。

> **两种运行方式现在共用这一份数据**：
> - **开发态**（`python -m src.main`）→ 直接用 `data/`；
> - **冻结态**（打包后的 `douyin-tool.exe`）→ 默认写在 exe 同级目录。
>   本仓库的 `dist/` 里放了一个 `data_dir.txt`（内容 `../data`），
>   靠它把数据引到这里 —— 于是 `dist/` 得以只留构建产物。
>
> 对外分享的程序**不含**那个指针文件，接收方拿到的仍是「数据就在 exe 旁边」的
> 原始行为，老用户的 Cookie、登录态、下载记录不会搬家。
> 详见 `src/config.py::get_data_dir()`。

## 内容一览

| 路径 | 说明 | 删掉的后果 |
| --- | --- | --- |
| `config.yaml` | 主配置：Cookie、下载目录、代理、批量条数、文件名模板 | 回到默认配置，需重新登录 |
| `browser_profile/` | 独立浏览器 profile，**登录态存在这里** | 需要重新扫码登录（约 52 MB） |
| `webview_profile/` | 界面窗口（WebView2）的持久 profile，**不含任何凭据**，只放缓存和主题偏好 | 自动重建；界面会短暂回落到默认主题 |
| `download_history.json` | 增量下载记录：`作品ID → 文件相对路径`（v2 格式） | 已下载的作品会被重新下载一遍 |
| `douyin_users.db` | f2 的用户库（作者信息缓存） | 自动重建，仅影响缓存 |
| `Download/` | 默认下载产物目录，按作者昵称分子目录 | 已下载的视频丢失 |
| `logs/` | f2 运行日志 + `error.log` 崩溃日志 | 只是丢历史日志 |

## 清理建议

- 想**清空下载记录**但保留文件 → 用界面上的「清空下载记录」，不要手删 `download_history.json`
  （手删等效，但界面操作更直观）。
- 想**彻底退出登录** → 用界面上的「退出登录」，它会同时清 Cookie 和 `browser_profile/`。
- `logs/` 和 `webview_profile/` 都可以随时整个删掉，程序会自己重建
  （`webview_profile/` 删了只会丢主题偏好，不会要求重新登录）。
- **别把 `webview_profile/` 换成临时目录**：窗口用的 WebView2 一旦走"私有模式"
  （临时 profile），关窗时它要删掉整个 user-data 目录，本机实测**窗口会卡约 14 秒
  才消失**（持久 profile 是 0.5 秒）。这就是 `src/win_window.py` 里显式传
  `private_mode=False` + `storage_path` 的原因。
- 换机器迁移时，拷 `config.yaml` + `browser_profile/` 两个就够了（其余会自愈）。

> ⚠️ `config.yaml` 里含**抖音登录 Cookie**，等同于账号凭据，不要外发或提交到仓库。
> 本目录已在 `.gitignore` 中整体忽略（仅本 README 例外保留）。
