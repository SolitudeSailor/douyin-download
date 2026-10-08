# 安全说明

## 报告漏洞

请不要在公开 Issue 中提交 Cookie、浏览器配置、日志、下载记录或其他账号信息。
发现安全问题时，请使用 GitHub 仓库的私密漏洞报告功能，并提供最小复现步骤、影响范围和版本信息。

## 本地敏感数据

程序的登录态和运行数据存放在 `data/`，相关内容已通过 `.gitignore` 排除。
提交代码前请运行：

```powershell
python -X utf8 tests/run_all.py
git status --short
```

发布安装包或绿色包前，另需执行：

```powershell
python -X utf8 build/audit_release.py
```
