# Paper Formatter · Zotero 论文格式转换

独立插件 ID：`paper-formatter@local.research`。Windows 首版，用 Zotero 菜单启动本机 Python 子进程；不依赖 Codex、常驻 Web 服务或现有精读插件。

对选定的 DOCX/PDF 附件应用格式要求，检查通过后覆盖原路径，保留 Zotero 附件身份。每次转换保存原稿、批注快照、规则来源、检查报告和恢复日志。识别或批注定位有问题时保持原文件不变，进入本地校对。

当前版本 **0.1.3**：用真实样稿检验 DOCX/PDF 多版式重排，改进不完整表格与 PDF 链接的校对门禁，并修正 PDF 内部跳转的误报。详见 [更新记录](docs/CHANGELOG.md)。已在本项目安装后端的用户，可安装新的本机 XPI 并重新启动 Zotero；模型资源无需重复下载。

## 安装

1. 安装 Python 3.12、LibreOffice 和 Zotero 8–10。实际验收环境是 Zotero 10.0.6、Python 3.12.10、LibreOffice 26.2.4.2；其他版本的兼容性尚未逐一实测。
2. 将整个 `paper-format-plugin` 文件夹放在固定位置。PowerShell 7 中运行：

   ```powershell
   pwsh -NoProfile -ExecutionPolicy Bypass -File .\scripts\Install-Backend.ps1
   ```

   脚本创建独立 `.venv`、按 `requirements-windows.lock` 约束安装文档依赖，预下载 Docling/RapidOCR 模型，并生成绑定本机路径的 XPI。首次安装需要联网和足够磁盘空间，模型较大。`-SkipPdf` 仅安装 Word 路径；之后可不带该选项补装。

3. 在 Zotero 的“工具 → 插件”中选择“从文件安装插件”，选取 `dist/paper-formatter-0.1.3-local.xpi`。安装后在“工具 → 论文格式转换：设置与恢复”运行环境检查。此安装包只含前端和非秘密的本机目录定位，不包含论文、密钥或模型。
4. 对需要复杂自然语言或官方期刊规范的使用场景，填写独立的兼容模型 API 地址、模型 ID、接口类型，以及 Brave Search 密钥。密钥只写入 Windows 凭据管理器的 `PaperFormatter/model` 和 `PaperFormatter/search`，不复用其他插件配置。

后端已安装的机器可以运行 `scripts/Check-Environment.ps1`。移动项目目录后重新运行安装脚本，并重新安装生成的 `-local.xpi`。卸载 XPI 不会自动删除原稿备份。迁移到另一台机器时使用源码交付 ZIP，在目标机器运行安装脚本；不要复用含本机路径的 XPI。

## 使用

在文献条目或附件上右键，选择“转换论文格式”。父条目有多个附件时，在窗口中明确选择本次处理的附件。

格式来源可组合：

- **自然语言**：例如 `A4；双栏；页边距 2.5cm；正文 宋体 12pt；1.5 倍行距`。常见规则在本机直接解析；其他指令由独立配置的模型转换为受限结构。
- **模板**：选择 DOCX/DOTX。仅读取样式和页面设置，不把模板中的文章正文或作者信息复制到稿件中。PDF 样例提取纸张大小和主要字号，其他参数必须在格式编辑器中确认。
- **期刊规范**：输入期刊名称和文章类型，使用 Brave 查找，并选择出版方的作者指南。也可直接提供 HTTPS 官方指南链接，支持网页和含文字的 PDF。插件不会把同名期刊或搜索第一条结果自动认定为目标。

规则优先级为：明确要求 > 模板 > 期刊指南。窗口展示规则、未解决要求和冲突数；报告保留来源 URL、获取时间、模板哈希和覆盖关系。已确认的规则保存在本机，可断网重复使用。

点击“转换并覆盖”后，插件会生成检查稿，核对内容、图像、公式、表格、引用及每页几何异常，重新定位批注。全部检查通过后，DOCX 覆盖 DOCX、PDF 覆盖 PDF。原稿备份不会成为文献列表中的重复附件。

## 校对与恢复

- 原稿和新稿并排显示，可分别切换页面。扫描件、表格、公式区域和多栏顺序有歧义时，逐块核对；修改文字后需要重新确认。可调整块顺序和内容类型，修正误识别表格。
- 表格校对包含行列位置和合并范围。公式识别不确定时保留原区域图像，不猜测补写公式；区域需由使用者核对。
- 未定位的批注选择后，原稿以蓝框显示原区域；在新稿上拖动框选，定位以橙框显示。高亮和下划线可以在同一页逐行追加多个区域，并可撤销上一框或清除定位；重新检查仍会验证完整引用文字。区域和手写批注需手动定位。跨页区域不能合并为一个手动定位。
- 人工定位绑定当前检查稿。修改识别内容或人工排版后，若页面画面变化，旧定位会进入重新定位流程；不能凭“仍在页面内”就沿用坐标。旧版本没有页面绑定记录的人工定位也需重新核对。
- 缺少结构化引用时，可在本机用段落编号、原引文及 Zotero 条目键补齐对应关系。文献元数据只从本机文库读取。复杂跨文字片段的域应先在 Word 中修复。
- “打开本地排版校对稿”用于人工处理版式。“选择修正后的 DOCX”提交修正版后会保存独立快照，关闭窗口或重新进入历史任务后继续使用；外部文件后来被修改不会改变已保存快照。“移除人工修正稿”让下一次检查重新应用格式规则，旧快照仍保留。修正版仍需通过内容和格式检查，没有忽略内容丢失或批注错误的按钮。
- “任务记录与恢复”可继续校对，或恢复某次转换之前的文件及批注。恢复前会再保存当前状态；可再次恢复，不抹除历史。
- 覆盖前文件或批注被修改，或者文件被编辑器占用时，会停止覆盖。Windows 原子替换同时保存实际被移走的版本，替换瞬间发生并发保存时也会保留新修改。关闭占用程序后重试；原稿改变后应基于新稿建立新任务。
- 如果进程在替换文件后退出，下一次插件启动会尝试完成确认或回滚。无法确认一致性时保留日志和所有版本，不猜测覆盖。
- 恢复任务逐项执行，一个任务失败不会阻止其他任务恢复。损坏的任务记录会明确显示并保留；无法判断是否存在未完成事务时，会阻止新的覆盖。阅读器重新打开或索引刷新失败会单独提示，不回滚已成功提交的文件。

## 当前能力边界

这不是适用于任意出版排版的无人校对系统。首版自动处理明确的页面参数、字体字号、标题层级和原生编号、段落间距、页眉页脚与页码、表格边框和宽度、图像缩放、图表标题位置及可明确匹配的编号、Zotero 原生引文和 CSL 参考文献。

以下情况会阻止自动覆盖，需在本地完成修正：

- 宏、活动外部资源、数字签名、加密文档或无法安全处理的域。
- PDF 链接区域、复杂交互对象或不支持的内嵌批注类型；可先在源程序导出静态版本。
- 原生 `SEQ/REF` 图表域需要重编号，但不能可靠同步所有域结果；应先在 Word 刷新到目标顺序。
- 指定字体未安装、字数超限、缺少声明或作者资料。插件不删写论述或补造信息。
- 不支持的精细规则会显示为待处理项。PDF 样例无法唯一推断作者真实的样式意图。

自动检查包含内容指纹、对象与引用字段保留、字体可用性、渲染文字覆盖、越界、明显重复重叠和异常空白；不能证明任意复杂版面都符合出版质量。应查看检查稿。扫描件 OCR 的正确性依赖逐块校对，不会仅凭识别置信度自动覆盖。

## 本机数据和隐私

- 安装脚本默认配置与任务目录：项目内 `private\data`，也可用 `-BackendHome` 指定固定本机路径。这样不会依赖 Codex 应用的 AppData 重定向。
- 后端代码与环境：项目内 `.venv`；模型：项目内 `.runtime\models`。
- `jobs/<任务 ID>` 保存原稿、产物、页面图片、识别结果、批注、报告和事务日志。请将此目录按本机论文数据管理，不要公开分享任务目录。
- 文档处理期间禁止 Python 的 TCP、UDP、DNS 出站调用，设置模型离线和关闭遥测。LibreOffice 使用独立安全配置，不加载文档外部资源；渲染前扫描危险链接和域。
- 联网规则协议拒绝附件路径、论文正文、批注、模板内容等字段。模型仅接收使用者输入的格式要求和从公开链接获取的作者指南。
- Zotero 原有同步设置保持不变；附件变化照常由 Zotero 管理。插件没有额外论文上传功能。

## 开发与验收

```powershell
.\.venv\Scripts\python.exe -m pytest -q
node --check .\extension\bridge.js
node --check .\extension\ui.js
node --test .\tests\test_bridge.js
.\.venv\Scripts\python.exe .\scripts\build.py
```

`scripts/prepare_acceptance.py --xpi dist/paper-formatter-0.1.3-local.xpi --installed-binding` 生成隔离 profile、测试文库和验收 harness，检查真正的 XPI 及安装路径。harness 使用合成样稿，永不打包入正式 XPI；额外的 UI 回归在真实 Zotero DOM 中使用确定的本地接口样例，不调用模型或检索供应商。`scripts/smoke_pdf.py`、`scripts/smoke_extended.py` 用本机模型验证 PDF、双栏、表格和扫描件。`scripts/format_matrix.py` 与 `scripts/format_gallery.py` 生成本地多格式样稿和对比页；结果与边界见 [格式矩阵记录](docs/FORMAT-MATRIX.md)。`scripts/inspect_samples.py`、`scripts/run_sample_matrix.py` 与 `scripts/sample_gallery.py` 可在私有工作目录对用户样稿副本生成对照，`scripts/verify_sample_gallery.py` 检查链接和脚本。验收证据与未完成的外部配置见 [ACCEPTANCE.md](ACCEPTANCE.md)。

来源接口：[Zotero 官方菜单接口](https://www.zotero.org/support/dev/zotero_8_for_developers)、[Docling 离线选项](https://docling-project.github.io/docling/usage/advanced_options/)、[Brave Search API](https://api-dashboard.search.brave.com/app/documentation/web-search/get-started)。
