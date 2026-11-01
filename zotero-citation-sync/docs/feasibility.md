# Word 动态引文可行性记录

调查日期：2026-10-28。当前可访问的主机为 Windows，未发现 Microsoft Word 或已连接的 Word 会话；Mac 实机测试尚未进行。下面区分官方文档支持和本项目实测，不能把字段 XML 可读称为 Zotero 已验证可编辑。

| 方案 | 官方依据 | 本轮实测 | 决策 |
| --- | --- | --- | --- |
| A 官方 Word 插件 | 提供 Add/Edit Citation、Add/Edit Bibliography、Refresh、Document Preferences | 当前缺 Word，无法验证 | 确认式流程以此为候选；由官方插件产生字段 |
| B Office JavaScript | Word 桌面支持 Addin 字段；Zotero 另有完整 HTTP citing 事务协议 | 无 Office add-in 运行会话 | 不创建自制字段，不启用写入；仅凭 insertField 不构成兼容性证明 |
| C macOS 自动化及官方对话框 | Zotero Mac 集成使用 Word 模板、原生库和 AppleScript/ScriptingBridge | 无 Mac，无法调用 | 首版只提供打开独立工作副本与人工官方操作指引；自动定位、宏调用和批量事务待 Mac 验收 |

首版实现扫描、来源映射、身份候选、目标定位、用户核对、备份及独立工作副本、保存后结构检查和日志。应用不写入任何引用字段。用户在工作副本中通过 Zotero 官方按钮完成插入/刷新/样式切换，保存并重开，通过 Add/Edit Citation 检查新旧引文后再提交验收确认。自动结构检查与用户官方操作确认分别存档。

本地匹配优先精确 DOI、PMID 或 arXiv ID；标题匹配提供候选并要求人工确认。研究结论与论点是否匹配需核对实际研究证据，不能根据标题或 DOI 自动判定。没有明确位置或有重复位置时不启动相应任务。

批量自动写入必须在 Mac 上完成十项验收后另行开发，不能以普通编号、字符串、OOXML 字段合成或 LibreOffice 渲染代替官方可编辑性实测。

## 官方来源

- [Zotero Word 插件操作说明](https://www.zotero.org/support/word_processor_plugin_usage)：插入、刷新和文档 CSL 设置。
- [Zotero HTTP 引文事务协议](https://www.zotero.org/support/dev/client_coding/http_integration_protocol)：需要完整的客户端响应循环；不是直接修改 DOCX 的单次 HTTP 请求。
- [Zotero Mac Word 集成源码](https://github.com/zotero/zotero-word-for-mac-integration)：官方原生通信实现。
- [Microsoft Word 字段 API](https://learn.microsoft.com/en-us/office/dev/add-ins/word/fields-guidance)：通用 Addin 字段能力；不承诺 Zotero 兼容。
- [Zotero Local API 源码](https://github.com/zotero/zotero/blob/main/chrome/content/zotero/xpcom/server/server_localAPI.js)：当前文档区分无认证 GET 与需本地授权的写入，并提供实例标识。本项目只实现 GET，拒绝非回环地址和重定向。

来源仅用于接口调查，没有向这些网站发送论文正文、批注或本地参考文献。
