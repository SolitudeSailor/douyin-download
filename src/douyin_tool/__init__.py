# -*- coding: utf-8 -*-
"""抖音视频下载器应用包。

模块结构：
- config.py        配置与路径管理（打包兼容）
- core.py          f2 封装：下载、解析、文件名处理
- cookie_reader.py Cookie 获取（cURL 提取 / 浏览器尝试）
- web_app.py       Flask Web 界面与应用工厂
- cli.py           统一命令行入口
"""

__version__ = "1.0.0"

__all__ = ["__version__"]
