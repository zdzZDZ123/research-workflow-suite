# Research Workflow Suite · Zotero / Codex / Notion

可下载、可在本机部署、可二次开发的研究工作流合集。Windows 为主要部署平台。MIT 许可适用于本仓库原创代码；第三方软件与模型遵循各自许可。

## 下载与安装

从右侧 Releases 下载 `research-workflow-suite-v0.1.0.zip`，解压到固定目录。不要在 ZIP 内运行，也不要安装后移动目录。

1. 准备 Windows 10/11、Python 3.12、Zotero 8–10，并启动 Zotero。新电脑可双击 **Install-New-PC.cmd**，或右键在终端运行 `powershell -NoProfile -ExecutionPolicy Bypass -File .\Install.ps1 -InstallApps`，通过 winget 安装所需基础软件；系统安装确认需要本人完成。
2. 双击 **Install.cmd**。自动创建独立 Python 环境、安装核心依赖、生成适合本机路径的 Zotero XPI，并进入 Notion 配置向导。已有环境可重复运行。
3. 向导中输入自己的 Notion 集成密钥、已连接此集成的父页面 URL。密钥使用隐藏输入，保存到 Windows 凭据管理器；向导在该页面下创建原论文库、精读索引库及关系库。也可跳过配置，之后双击 **Configure.cmd**。
4. Zotero → 工具 → 插件 → 从文件安装，选择 `notion-workflows/dist/paper-deep-reading-0.3.0.xpi`。插件安装后从工具菜单发现论文关系；对论文右键可同步已保存精读笔记。

“一键”覆盖本机环境与构建流程；账号登录、Notion 页面授权、输入个人密钥和 Zotero 的安装确认需要用户操作。Codex 是可选开发与研究助手，日常插件运行不依赖它。第三方付费服务、Word 和模型资源不随源码打包。

## 工作流

| 工作流 | 入口 | 当前边界 |
|---|---|---|
| 单篇论文精读 → Notion | Zotero 论文右键；`notion-workflows` | 读取已保存的 Zotero 精读子笔记；原论文必须先匹配稳定 Paper Key |
| Paper Chat 选区 → Notion | AI Paper Chat 回答上的选区按钮 | 需要另装兼容的 AI Paper Chat；实测兼容版本为 3.7.0 |
| 跨论文关系与图谱 | Zotero 工具菜单 | 按选定分类、PDF 页码与哈希核查；必须配置独立兼容模型 API |
| DOCX/PDF 格式转换 | `paper-format-plugin` | 可选安装，保存备份与批注核查；复杂稿件需要人工校对 |
| Word 引文工作台 | **Launch-Citations.cmd** | 生成备份和副本，正式引文由 Word 的 Zotero 官方插件插入；当前为预览版 |
| Obsidian / Codex / Word / WPS | [已有开源工作流](https://github.com/zdzZDZ123/zotero-obsidian-codex-word-wps-workflow) | 独立仓库，包含 Codex 指引、提示词与 Obsidian 模板 |

安装格式转换（Word 路径）：`powershell -ExecutionPolicy Bypass -File .\Install.ps1 -WithFormatter`。完整 PDF/OCR：增加 `-WithPdf`，会下载较大的模型，需 PowerShell 7、LibreOffice 和相应磁盘空间。Windows Formatter 使用独立环境；详见其 README。

## Notion 与模型配置

先按 [Notion 官方说明](https://developers.notion.com/docs/create-a-notion-integration)创建自己的内部集成，并在自己的父页面添加连接。不要使用作者的页面 ID 或密钥。配置向导自动创建相互关联的数据源；中断后重跑会按已保存 ID 或父页下的唯一数据库名称恢复，写入结果不明确时暂停而非盲目重建。

运行关系功能前，在 `notion-workflows` 中使用 `..\.venv\Scripts\python.exe paper_relations.py configure --base-url https://你的服务/v1 --model 你的模型ID --api-style chat_completions --prompt-api-key`，再运行 `model-preflight`。也支持 `responses`；模型必须能按提示返回 JSON。

`Configure.cmd` 可列出本机 Zotero 分类并保存选择，也可把选定分类的可读 PDF 论文登记到自己的原论文库。这一步明确确认后才写入元数据，不调用模型、不生成关系。随后使用右键精读同步或关系菜单。

[安装与工作流说明](docs/WORKFLOWS.md) · [二次开发](CONTRIBUTING.md) · [安全与隐私](SECURITY.md) · [第三方依赖](THIRD_PARTY_NOTICES.md) · [验收范围](docs/VALIDATION.md)

## 平台与验证

精读/关系与格式转换的凭据存储、安装脚本以 Windows 为目标。引文工作台的 Python 核心可在 macOS 使用，但 Mac Word 完整实机验收尚未完成。当前不能宣称在任意电脑零配置成功。

仓库没有作者的 Notion 数据库配置、凭据、论文 PDF、聊天历史、备份、任务数据库或绑定作者路径的 XPI。所有插件在目标电脑生成。
