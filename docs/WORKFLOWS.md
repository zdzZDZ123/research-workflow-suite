# 工作流使用

## 首次登记论文

启动 Zotero，启用本机 Local API。在 Configure.cmd 中完成 Notion 初始化，选择分类后确认导入可读 PDF 论文的元数据。原库 Paper Key 使用 user/group、文库 ID 和 Item Key，不以标题猜匹配。若使用已有库，必须满足 notion_sync.EXPECTED 与原库身份字段要求；首次用户建议使用向导创建的新库。

## 精读和选区

将精读结果保存为目标 Zotero 论文的子笔记；模板在 notion-workflows/academic-deep-reading-template.md。右键同步时存在多条候选会要求选择。全文写入原论文正文，精读库保留索引和版本。Paper Chat 选区需要兼容的第三方插件，发送前在预览里明确目标论文。无法可靠转换的 HTML 保留原始文字并给出提示。

## 关系图谱

配置兼容模型后，从工具菜单运行。每次最多 20 个新增或变化配对，重复点击继续。共享术语及准确 DOI 引文经程序校验后才能自动发布；支持、冲突、扩展进入待审核。Notion 内可修改审核状态；源 PDF 变化会标记需复核，已发布回链和图谱同步更新。精读单篇 Knowledge Graph 属性保持独立语义。

## 故障与迁移

先检查 Zotero 是否运行、Local API 是否开启、PDF 是否实际存在；附件记录不代表本机有文件。Notion 404 通常需要检查自己的集成是否连接父页。模型先执行 model-preflight。不要删除 .runtime/state.sqlite3 来处理重复记录。未知写入暂停时先检查远端状态。

本机源码目录移动后重新运行安装脚本并更新 XPI。换电脑从干净 ZIP 开始，各人配置自己的账号。若迁移同一人的状态，使用私有渠道备份配置、凭据及 .runtime，禁止上传公开仓库。
