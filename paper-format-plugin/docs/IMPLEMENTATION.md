# Paper Formatter Implementation Plan

> For agentic workers: use superpowers:executing-plans. The user explicitly approved implementation; no second approval gate is needed.

**Goal:** Ship a standalone Zotero extension with local manuscript formatting, validated replacement, annotation migration and restore.

**Architecture:** Zotero JS owns library state and presents the UI. Python owns isolated rule resolution, offline conversion, checks and filesystem transactions.

**Tech Stack:** Python 3.12, lxml, python-docx, PDFium, Docling/RapidOCR, LibreOffice, Zotero JS/CSL.

**Spec:** DESIGN.md.

## Global constraints
- Windows; independent XPI; existing integrations remain intact.
- No paper-derived data in remote requests. Keys use Credential Manager.
- Preserve attachment keys, content and annotations; fail closed on unresolved problems.
- Same-type replacement with immutable backups and crash recovery.

## Review focus
- A source or annotation changing between preview and commit must not be lost.
- Crash after file replacement but before Zotero acknowledgement must be recoverable.
- Unknown format fields and incomplete PDF extraction must not appear successful.
- DOCX field codes, merged tables and equations must survive formatting.
- Paper/template sentinel text must never reach a network payload.

## Task 1: State and recoverable file transactions
- [x] Implement contracts.py and store.py; tests/test_store.py covers changed inputs, candidate tampering, duplicate commits, rollback and restore.
- [x] Verify red then green tests before UI integration.

## Task 2: Rule resolution and offline boundary
- [x] Implement profiles.py, credentials.py and rules.py. Test strict validation, precedence, template privacy and network allowlists in tests/test_profiles.py and tests/test_privacy.py.
- [x] Separate rule input from all document requests and test sentinels.

## Task 3: DOCX/PDF processing and validation
- [x] Implement docx_io.py, pdf_io.py, render.py, annotations.py, citations.py and engine.py.
- [x] Test preservation, layout geometry, all-page rendering, OCR review, ambiguous annotation anchors and corrected review revalidation.

## Task 4: Zotero extension and deployment
- [x] Implement extension bootstrap, dialog/controller, local citation adapter, transaction acknowledgement and recovery.
- [x] Implement CLI, setup/doctor, build XPI and isolated Zotero acceptance runner. Check JS syntax and exercise the real installed application's APIs and DOM with a test harness.

## Task 5: Acceptance and independent review
- [x] Run offline, privacy, failure injection, rendering and isolated Zotero checks. Inspect rendered fixture pages.
- [x] Run one independent final review; fix important findings with regression tests.
- [x] Produce README, ACCEPTANCE.md, XPI and explicit remaining environmental limitations.

Acceptance boundary: provider credentials have not been configured, so live Brave/model calls and provider-side request auditing remain unverified. Zotero independently starts the backend, but this running Codex session was not physically closed. These limitations are explicit in ACCEPTANCE.md.
