# Zotero 论文格式转换插件

Approved specification: user implementation request, 2026-10-22.

One independent Zotero extension accepts DOCX/PDF attachments and journal names, DOCX/DOTX/PDF templates or natural-language formatting instructions. Preserve scientific content, attachment identity and all annotations. Replace the original only after validation; uncertain extraction and annotation mapping require local correction. Keep immutable original versions and a recoverable two-phase file/Zotero commit.

Paper content, images, annotations, references and template prose remain local. Only user-authored format instructions and public journal guidelines may reach the separate rule-resolution network process. Zotero's existing sync policy is unchanged. Windows and Zotero 10 are the first acceptance target. No runtime Codex dependency.

Architecture: Zotero official MenuManager and a local dialog coordinate a Python subprocess. Rule resolution returns a validated FormatProfile; offline workers return candidates and a validation report. Zotero owns item/annotation database changes. A local append-preserving job journal coordinates file replacement and Zotero acknowledgement. An unacknowledged file replacement must be reconciled before any other operation on the same attachment.

Formatting supports page geometry, columns, body/headings, spacing, headers/footers/page numbers, captions, tables/figures and CSL citations. Explicit requirements override templates, which override journal guidelines. Unknown or unimplemented requests are reported and prevent automatic commit. Official compliance is never inferred from CSL selection alone. Normal supported documents auto-commit; scans and ambiguous content require a local reviewed baseline. Annotation migration must be complete, including manual positioning where necessary.

Implementation boundaries: do not modify existing Paper Chat/Notion code or credentials, do not run tests against a real user's attachment, do not flatten active citations silently, do not execute macros or fetched code, and do not erase backup history during uninstall.
