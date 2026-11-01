# 二次开发

Fork 仓库并克隆。Python 3.12 环境中执行 Install.ps1 -SkipConfigure；本地修改后重新运行 notion-workflows/build_extension.py，并在 Zotero 更新生成的 XPI。

核心模块：paper_reading.py 读取 Zotero；notion_sync.py 管理精读事务；selection_sync.py 管理选区；relation_evidence.py 核对 PDF 摘录；relation_model.py 调用兼容 API；relation_notion.py 发布关系、回链与 PNG；relation_zotero.py 限定分类。Zotero 前端位于 zotero-extension。

测试：在 notion-workflows 执行 `python -m unittest -q`；在 zotero-citation-sync 执行 `python scripts/run_tests.py`；Formatter 安装 `pip install -e "./paper-format-plugin[test]"` 后执行 `python -m pytest paper-format-plugin/tests` 与 `node --test paper-format-plugin/tests/test_bridge.js`。

修改后通过 PR 说明行为变化和验证。不要提交 .runtime、private、.venv、Notion ID 配置、密钥、用户文档或本机生成的插件。测试使用合成数据。兼容 API 不得把自报置信度当成发布证据。未知 Notion 写入结果必须先读回，人工编辑优先保留。格式转换必须保留恢复能力。

CI 检查核心测试、引文测试、格式转换测试、Python 语法和发布文件隐私。发布脚本 `python scripts/package_release.py` 只打包 Git 跟踪文件，ZIP 内附 SHA256 清单。新发布版本请同步脚本与说明中的版本号。
