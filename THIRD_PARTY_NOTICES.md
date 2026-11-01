# 第三方软件与许可

本仓库不重分发 Zotero、Codex、Notion、Microsoft Word、LibreOffice、AI Paper Chat、Notero 或 llm-for-zotero 的安装包。它们的许可、账号和服务条款独立于本仓库 MIT 许可。

- Zotero 与其官方 Word/LibreOffice 插件：https://www.zotero.org/download/ 和 https://www.zotero.org/support/word_processor_integration
- Zotero 插件安装：https://www.zotero.org/support/plugins
- Codex：https://developers.openai.com/codex/ （可选开发工具，需自己的账号）
- Notion：https://www.notion.so/ 与 https://developers.notion.com/ （同步使用自己的集成）
- LibreOffice：https://www.libreoffice.org/download/download-libreoffice/
- AI Paper Chat：可选的选区来源。请向原插件维护者获取合法安装包，已测试 3.7.0；未包含其源码或服务凭据。
- Notero（https://github.com/dvanoni/notero）、llm-for-zotero：可选；核心工作流使用 Zotero Local API，不要求它们。

Python 依赖在 requirements.txt、pyproject.toml 和 requirements-windows.lock 声明。模型下载由 Formatter 的 install_models.py 执行，使用前查看上游模型许可。没有重新许可第三方依赖或模型的权利。
