# secure/ —— 凭据保险箱

这个目录专门用来**集中存放账号凭据的加密归档**。

登录凭据（等同于账号密码）**统一收在 `data/` 一处**。
`dist/` 只放构建产物，里面不含任何凭据 —— 它靠一个 `data_dir.txt`
指针把运行时数据引到 `data/`（见 `dist/README.md`）。

| 位置 | 内容 | 敏感度 |
| --- | --- | --- |
| `data/config.yaml` | 抖音登录 **Cookie** | ★★★ 可直接冒充登录 |
| `data/browser_profile/` | 独立浏览器 **登录态**（含 IndexedDB / LocalStorage） | ★★★ 免扫码登录的全部凭据 |
| `data/douyin_users.db` | f2 用户缓存库 | ★ 缓存 |
| `data/download_history.json` | 下载记录（含视频路径） | ★ 隐私 |

`vault.7z` 就是上述全部内容的 **AES-256 加密归档**（连文件名也加密），
放在这里集中保管 —— 别的目录都不用再单独担心。

## 目录内容

| 文件 | 说明 |
| --- | --- |
| `seal.py` / `seal.bat` | **封存**：把上面清单里的凭据打包成 `vault.7z` |
| `unseal.py` / `unseal.bat` | **解封**：从 `vault.7z` 还原回原位 |
| `vault.7z` | 加密归档本体（运行封存后生成，已加入 `.gitignore`） |
| `vault.manifest.json` | 归档清单：时间、文件数、包含哪些条目（**不含密码**） |

## 怎么用

```
封存（加密归档，默认不删明文）：
    双击 secure\seal.bat
    → 设置密码 → 生成 secure\vault.7z

真·锁起来（归档后抹掉明文，磁盘上只剩加密包）：
    双击 secure\seal.bat 时选 --purge，或命令行：
    python secure\seal.py --purge

解封（还原到原位，程序才能跑）：
    双击 secure\unseal.bat → 输入密码
```

密码也可以通过环境变量给，方便脚本化：

```bat
set DYD_VAULT_PASSWORD=你的密码
python secure\seal.py
```

> ⚠️ **密码丢了 = 数据找不回来。** 没有后门、没有找回。
> 建议用一个你能记住、且和别处不复用的短语。

## 为什么不是「一直加密着」

程序运行时**必须直接读到明文**的 `config.yaml` 和 `browser_profile/`——
浏览器要真的打开那个 profile 目录才能复用登录态。
把在线数据加密会让 exe 直接失效（读不到 cookie、无法登录）。

所以这里采用**「保险箱 + 封存」模型**：

- **日常开发 / 要用程序** → 保持解封状态（明文在 `data/`）
- **要离开电脑 / 存档 / 备份** → 跑一次 `seal.bat --purge`，磁盘上只剩加密包
- **回来要用** → `unseal.bat` 还原

换句话说：**加密是你主动按下的一个动作，而不是常态。**

## 额外加固：文件夹权限收紧

Windows 11 **家庭版不支持 EFS**（`cipher /e` 会报「不支持该请求」），
所以没法做「文件夹透明加密」。作为替代，可以把凭据目录的 NTFS 权限
收紧到只剩当前账号可访问（去掉继承的 Administrators / Users 组）：

```bat
icacls secure /inheritance:r /grant:r "%USERNAME%:(OI)(CI)F"
icacls data   /inheritance:r /grant:r "%USERNAME%:(OI)(CI)F"
```

效果：同一个 Windows 下的**其他用户账号**默认访问不了这些目录。
（管理员提权后仍可访问 —— 这只是提高门槛，不是绝对防护。）

未执行也不影响程序运行，按需选择。

## 与打包链的关系

`build/make_portable.py`（绿色包）和 `build/audit_release.py`（发布审计）
都是**白名单**机制，只取 `dist/douyin-tool.exe` + `build/使用说明.txt`，
不扫描目录，所以凭据不会被误打进分享包。

跑完打包后建议再执行一次审计，确认没有泄露：

```bat
python build\audit_release.py
```
