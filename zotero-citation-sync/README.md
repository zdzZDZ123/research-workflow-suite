# Zotero Citation Sync

0.1.1-preview · 2026-10-28

本地确认式引文工作台：读取 Zotero 批注及笔记，区分来源论文 A 与待引用论文 B，匹配真实文献，定位 DOCX 原句，创建完整备份和工作副本，并检查官方 Word 操作后的保存结果。

**当前交付是可移植预览版。Windows 的本地核心、真实 Zotero 10.0.6 读取和浏览器流程已经测试；macOS 启动器和 Mac Word 插入、刷新、重开编辑尚未实测。应用不会自动插入引文。正式引文由使用者在 Microsoft Word 的 Zotero 官方界面逐项插入。批量自动写入没有启用开关。**

## Mac 安装与启动

1. 将发布 ZIP 解压到本机文件夹。准备 Python 3.11 或更高版本、Zotero 10、Microsoft Word，以及 Word 中可正常使用的 Zotero 官方插件。
2. 启动 Zotero，在高级设置中确认本机 Local API 已启用。应用只连接 `http://127.0.0.1:23119`，不需要 API 密钥，也不写 Zotero 文库。
3. 在该模块目录运行下列命令，打开本机浏览器界面，关闭 Codex 后仍独立运行。本合集不提供已签名的 macOS 应用包。

```sh
python3 -m citation_sync
```

只使用 Python 标准库，无需 pip 安装文档处理库。应用包仍需要独立安装的 Python，并非内置运行时的独立二进制。若通过终端运行，可用 Control-C 停止；界面底部的“退出本地工作台”也会关闭服务。

在目标 Mac 先生成诊断报告：

```sh
python3 -m citation_sync diagnose --output ./mac-diagnostics
python3 scripts/run_tests.py
```

报告检测操作系统、Zotero/Word 版本、Word 启动模板及 Local API。找到应用或模板不等于官方 Word 集成已通过测试。实机验收步骤见 [Mac 验收单](docs/MAC-ACCEPTANCE.md)。

## 写一条可可靠识别的请求

在论文 A 的 PDF 批注评论，或归属于 A 的笔记中写：

```text
[cite]
DOI: 这里填写已经核实的论文 B 的真实 DOI
Target: 这里粘贴目标 Word 正文中的精确原句
Claim: 这里说明论文 B 的哪项研究结论支持目标论点
```

也支持“新增引用”“引用请求”“添加引用”等显式提示，以及 `PMID:`、`arXiv:`、`Title:` / `标题:`、`Section:` / `章节:`。一条请求对应一篇文献和一个位置；同一篇文献引用在两处时写两条请求。`Target` 至少六个字符，末尾即插入点。引用请求写在自己的评论中，批注高亮的论文 A 原文不会自动作为论文 B 的身份。

DOI、PMID、arXiv 按本地文库中的标识符精确匹配。多个标识符必须一致。重复文献、只有标题、找不到文献或目标句出现多次时进入待核对。没有 DOI 的标题候选只能由使用者核实，应用不创建推测的条目。缺少文献时先在 Zotero 中核实并添加，再重新扫描。

可在“补充文献身份或指定正文位置”中搜索本地文献、选择段落并填写原句。章节筛选必须能识别到 Word 标题的层级边界，否则要求明确段落。当前默认扫描个人文库；代码接口支持群组文库，界面暂不提供切换。

## 完成一次确认式同步

1. 在 Word 保存原稿，选择 DOCX，点击“连接 Zotero”“扫描新增批注”。扫描只读取保存到磁盘的文档。
2. 比较来源 A 与引用 B，核对 B 的身份和实际研究内容。阅读本地摘要只是辅助，应用不自动断言论据支持关系。
3. 核对正文原句和插入点，勾选所需请求及身份、位置、证据确认。APA、IEEE、Vancouver、Nature 可作为目标 CSL 样式选择，也可保留当前样式。
4. 点击“为勾选项目创建备份和工作副本”。备份及副本使用逐字节复制，原稿不被改写。文库版本、原稿哈希或预览变化时拒绝继续。
5. 打开工作副本，在 Word 的 Zotero **Document Preferences** 中选择相应样式、使用 Fields；在每个目标原句末尾使用 **Add/Edit Citation** 选择已核实的 B。应用提供定位原句和条目信息，当前不控制官方对话框。
6. 如无文末书目，用 **Add/Edit Bibliography** 添加，再点击 **Refresh**。不要使用 Unlink Citations。
7. 保存、关闭并重新打开工作副本，用 **Add/Edit Citation** 分别编辑新旧引文。核对文内与文末样式一致，目视检查正文和全部重要对象。
8. 在工作台填写实际完成的官方操作，点击“读回检查并记录结果”。只有结构检查、Mac Word 环境检测及所有人工确认均通过，才标记“结构通过，用户已确认官方操作”。这仍是人工确认记录，不是应用独立认证 Word 动态行为。

下一次扫描请选用已保存的工作副本。文档字段与持久化来源映射共同防止重复插入；若误选旧原稿，应用提示已有结果。批注被删、撤回或修改会提示冲突，不自动删改旧引文。当前删除或修改旧引文的自动同步尚未实现。

## 来源冲突与中断恢复

修改已经同步的请求后，先在冲突卡片核对来源变化，再选择：

- **保留文档现状并结案**：记录决定，保留既有正文和引文；重复扫描不再要求处理同一个版本。来源以后再次变化，仍需重新核对。
- **保留旧引文，重新核对新增请求**：只对修改后的有效请求提供此选项；随后仍需确认文献、位置和证据，再创建新副本。它不会删除或替换旧引文。

删除或撤回的请求可以确认保留现状，不能直接变成新引用。仅修改候选文献或目标段落不能跳过已同步请求的冲突处理。较旧的未决修改被更新版本取代时仍保留历史记录。

“中断恢复”会检查应用记录是否只写入了一部分。指纹一致且旧进程已退出时，确认关闭其他工作台实例后点击“恢复中断记录”，即可补完保存。它只恢复本应用的状态和任务记录，不写论文或 Zotero 文库。记录被外部修改、备份损坏或已验证副本变化时停止恢复。

没有浏览器时，可先检查，再提供检查返回的 `plan_id`：

```sh
python3 -m citation_sync --home "/path/to/app-data" recover
python3 -m citation_sync --home "/path/to/app-data" recover --apply-plan "检查结果中的 plan_id"
```

恢复后重新扫描或从历史打开任务。未登记或未复制完整的目录会保留并提示人工核查，不自动删除。0.1.0 留下的记录不一致若没有可重放日志，也需人工处理。

## 文件、恢复与隐私

Mac 数据默认保存在 `~/Library/Application Support/Zotero Citation Sync/`，可用 `--home` 指定专属目录。Windows 开发模式默认保存在项目 `private/app/`。

- `scans/`：文库快照、来源映射及论文段落，属于私有内容。
- `sessions/<任务ID>/original.docx`：原始备份；`working.docx`：操作副本；`session.json`：确认及检查记录。
- `state.json`：本应用状态，不是 Zotero 数据库。
- `operations.jsonl`：操作、状态及哈希审计，不记录正文或密钥。
- `journal/pending.json`：尚未完成的提交，可能含私有快照；`journal/receipts/`：完成后的提交回执，保留记录路径、指纹和事件。

“生成恢复副本”会创建新的 `restored-*.docx`，文件与原备份一致；原稿、工作副本及先前恢复副本全部保留。不要把整个数据目录提交到仓库或上传。发布构建只收集明确允许的代码及文档，不包含 `private/`、论文或文库快照。

应用没有远端模型、遥测或联网元数据查询。文档只经本地 ZIP/XML 解析，既不执行宏，也不加载外部资源；浏览器只连接本机服务。Zotero、Word 及系统本身的同步设置不在本应用控制范围内。

## 验证、工程结构与后续工作

```sh
python3 scripts/run_tests.py
python3 -m citation_sync inspect /path/to/paper.docx --output ./private/field-inventory.json
python3 scripts/smoke_readonly.py /path/to/paper.docx
python3 scripts/build_release.py
python3 scripts/verify_release.py
```

`citation_sync/local_api.py` 负责 GET 数据接口；`requests.py` 负责来源、身份与位置；`docx_read.py` 只读解析；`word_review.py` 负责官方操作指引和保存结果检查；`service.py` 管理状态、备份与恢复；`server.py` 和 `web/` 提供本机 UI。

升级前退出旧工作台，再解压新版启动；Mac 的应用数据目录保持不变。不要同时运行新旧版本写入同一数据目录。本轮保留了 0.1.0 源码包及升级前的应用状态备份，完成情况见 [0.1.1 迭代记录](../docs/WORKFLOWS.md)。

详见 [环境报告](../docs/VALIDATION.md)、[三条集成路线可行性](docs/feasibility.md)、[已实测记录](../docs/VALIDATION.md)、[限制与故障恢复](docs/LIMITATIONS.md)。
