# Zotero Citation Sync — Mac 安装指南

版本：0.1.1-preview｜安装包整理日期：2026-10-28

这是独立的本地引文工作台，交付为 Zotero Citation Sync.app。解压后安装到 Mac 的“应用程序”，不通过 Zotero 的扩展管理器安装。当前版本提供扫描、文献核对、正文定位、备份、官方操作指引和结果检查。正式动态引文仍由你在 Word 的 Zotero 官方界面确认插入、刷新。

## 1. 准备软件

- Python 3.11 或更高版本。安装包不内置 Python；仅使用标准库，无需 pip 或付费模型服务。
- Zotero 10，保持启动，并在高级设置中允许本机 Local API 访问。
- Microsoft Word，以及 Word 中正常显示的 Zotero 功能区。

Python 官方 Mac 安装程序：https://www.python.org/downloads/macos/

按该页面及安装程序的系统要求选择适合你 macOS 的正式版本。本项目的 Windows 测试使用 Python 3.12.14；其他 Python 版本及 Intel / Apple Silicon Mac 的实际运行均须在本机检查，尚未声称通过。

如果 Word 没有 Zotero 功能区，可在 Zotero 设置的“引用 → 文字处理软件插件”重新安装官方 Word 插件，随后重启 Word。
官方说明：https://www.zotero.org/support/word_processor_plugin_installation

## 2. 安装应用

1. 将整个 Mac-Install.zip 拷到 Mac 本机并解压，打开解压出的文件夹。
2. 如安装过旧版，先在旧界面点击“退出本地工作台”，再安装。
3. 把 Zotero Citation Sync.app 拖入“应用程序”（/Applications）。也可以放入你个人目录的 Applications 文件夹。保留外面的检查脚本和说明文件。
4. 双击应用。它会在默认浏览器打开本机工作台；不需要安装或启动 Codex。

这是未签名、未公证的预览包。如果 macOS 提示开发者无法验证，在确认文件来自本次交付后，按 Apple 的说明到“系统设置 → 隐私与安全性”查看该应用的“仍要打开”。不要关闭全局安全保护；若系统提示恶意内容或文件损坏，先停止并记录提示。
Apple 官方说明：https://support.apple.com/en-us/102445

需要看启动错误时，在允许该应用运行后，可双击包内“终端启动.command”；该入口会在终端显示日志，Control-C 可退出。不需要管理员权限。

## 3. 首次运行环境检查

先启动 Zotero，再双击“检查Mac环境.command”。它会：

- 查找本机 Python、Zotero、Word 及 Word 的 Zotero 模板。
- 通过本机 Local API 只读扫描，检查条目、批注和笔记是否可读。
- 运行包内的 72 项便携测试，并记录本机系统和 Python 版本。
- 在检查结束后打开报告所在文件夹。

输出保存到 ~/Library/Application Support/Zotero Citation Sync/diagnostics/ 下新建的独立目录，包含 environment_report.md、test-results.json 和诊断日志。请逐项阅读结果；脚本运行完毕不等于 Word 官方引文集成已经通过验收。

该检查只读 Zotero，不扫描论文，也不修改文库。报告目录内 private/diagnostic-snapshot.json 含文库元数据、笔记和批注，仅保存在本机；不要把整个目录上传。排查时优先提供错误信息或经过检查的摘要。

## 4. 开始使用

1. 在本地工作台点击“连接 Zotero”，选择一份用于测试的 DOCX 副本。
2. 点击“扫描新增批注”，核对来源论文、待引用论文和正文位置。
3. 确认需要的项目，创建备份和工作副本。
4. 在工作副本中，通过 Word 的 Zotero Add/Edit Citation 插入；用 Add/Edit Bibliography 和 Refresh 更新书目。
5. 保存、关闭再重开，检查新旧引文均可通过 Zotero 编辑，再回工作台执行读回检查。

一条批注请求可以采用下面的格式。把示例占位文字换成你已经核实的信息：

    [cite]
    DOI: 已核实的待引用论文 DOI
    Target: 从 Word 粘贴的目标原句（至少六个字符）
    Claim: 这篇论文的研究结果如何支持目标论点

工作台不会猜测缺失文献，也不会自动生成 Word 引文字段。先在测试副本完成流程，再处理正式稿件。

## 5. 数据、退出和升级

工作数据、备份和日志位于 ~/Library/Application Support/Zotero Citation Sync/。应用不会覆盖原稿。关闭浏览器标签不会停止服务；请点击“退出本地工作台”。

升级前退出旧版本，备份上述数据目录，再替换 .app；不要同时运行两个版本。移除 .app 不会删除论文备份和状态目录。

## 6. 本次交付验证范围

- 已通过：Windows 上从发布源码重新解压后运行的 72 项测试；ZIP 完整性、文件指纹、应用目录结构、脚本执行权限、私有数据排除检查。
- 待你在 Mac 实测：应用启动、文件选择、进程锁、官方 Word 插入与刷新、保存重开后的动态可编辑性。
- 尚未实现：一键批量写入、自动操控 Zotero 官方引用对话框、自动修改或删除既有引用。

包内“Mac实机验收单.md”列出了十项核心场景。Windows 自动化测试不能替代 Mac Word 的动态引文验收。

安装包的文件哈希及测试来源见 INSTALL-MANIFEST.json；ZIP 的 SHA-256 见下载旁边的 .sha256 文件。包内没有论文、Zotero 数据库、个人批注快照或 API 密钥。
