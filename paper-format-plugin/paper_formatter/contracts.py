"""Shared contracts; no document content is a valid RuleInput field."""
from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypedDict


class FormatterError(Exception):
    def __init__(self, code: str, detail: str = ""):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


class FormatRequest(TypedDict):
    schema_version: int
    attachment: dict
    source_sha256: str
    annotations: list[dict]


class FormatProfile(TypedDict):
    schema_version: int
    rules: dict
    sources: list[dict]
    unresolved: list[str]


class JobResult(TypedDict):
    run_id: str
    status: str
    report: dict


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def fingerprint(path: str | Path) -> str:
    with Path(path).open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def read_json(path: str | Path) -> dict:
    value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise FormatterError("JSON_OBJECT_REQUIRED")
    return value


def write_json(path: str | Path, value: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".writing")
    with temp.open("w", encoding="utf-8", newline="\n") as file:
        file.write(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
        file.flush()
        os.fsync(file.fileno())
    os.replace(temp, path)


def attachment_identity(raw: dict) -> dict:
    if not isinstance(raw, dict) or not isinstance(raw.get("library_id"), int) or raw["library_id"] < 1:
        raise FormatterError("INVALID_LIBRARY_ID")
    for key in ("key", "parent_key"):
        if raw.get(key) and not re.fullmatch(r"[A-Z0-9]{8}", raw[key]):
            raise FormatterError("INVALID_ITEM_KEY", key)
    if not raw.get("key") or not Path(raw.get("path", "")).is_absolute():
        raise FormatterError("INVALID_ATTACHMENT")
    path = Path(raw["path"])
    if path.is_symlink():
        raise FormatterError("SYMLINK_ATTACHMENT", "Use the real file path")
    if path.suffix.lower() not in (".docx", ".pdf"):
        raise FormatterError("UNSUPPORTED_INPUT", "仅支持 DOCX 和 PDF；请先将 .doc 转为 DOCX")
    return {"library_id": raw["library_id"], "key": raw["key"],
            "parent_key": raw.get("parent_key", ""), "path": str(path.resolve())}


ANNOTATION_KEYS = ("key", "type", "text", "comment", "color", "pageLabel", "sortIndex", "position", "tags", "authorName", "isExternal")


def normalized_annotations(annotations: list[dict]) -> list[dict]:
    result = []
    seen = set()
    for item in annotations:
        key = item.get("key")
        if not isinstance(key, str) or not re.fullmatch(r"[A-Z0-9]{8}", key) or key in seen:
            raise FormatterError("INVALID_ANNOTATION_KEY")
        seen.add(key)
        value = {field: item[field] for field in ANNOTATION_KEYS if field in item}
        if isinstance(value.get("position"), str):
            value["position"] = json.loads(value["position"])
        if "tags" in value:
            value["tags"] = sorted(value["tags"], key=canonical)
        result.append(value)
    return sorted(result, key=lambda item: item["key"])


def annotations_digest(annotations: list[dict]) -> str:
    return digest(normalized_annotations(annotations))


def verify_annotation_preservation(original: list[dict], updated: list[dict]) -> None:
    left, right = normalized_annotations(original), normalized_annotations(updated)
    if [x["key"] for x in left] != [x["key"] for x in right]:
        raise FormatterError("ANNOTATION_SET_CHANGED")
    for before, after in zip(left, right):
        for field in ANNOTATION_KEYS:
            if field not in ("position", "pageLabel", "sortIndex") and before.get(field) != after.get(field):
                raise FormatterError("ANNOTATION_CONTENT_CHANGED", before["key"] + ":" + field)
        position = after.get("position", {})
        if not isinstance(position.get("pageIndex"), int) or position["pageIndex"] < 0:
            raise FormatterError("ANNOTATION_POSITION_REQUIRED", before["key"])


def default_home() -> Path:
    return Path(os.environ.get("PAPER_FORMATTER_HOME") or Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "PaperFormatter")


def issue(code: str, detail: str, *, blocking: bool = True, **context) -> dict:
    return {"code": code, "detail": detail, "blocking": blocking, **context}
