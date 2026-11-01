"""Exact quote/context anchoring and validated manual annotation placement."""
from __future__ import annotations

import copy
import math
import unicodedata

from .contracts import FormatterError, normalized_annotations, verify_annotation_preservation
from .render import pdf_pages


def normalized_text(text):
    return "".join(c for c in unicodedata.normalize("NFKC", text) if not c.isspace() and c not in ("\u00ad", "\u200b"))


def text_index(page):
    text, mapping = [], []
    for index, char in enumerate(page["chars"]):
        for value in normalized_text(char["text"]):
            text.append(value)
            mapping.append(index)
    return "".join(text), mapping


def occurrences(text, query):
    start = 0
    while query and (index := text.find(query, start)) >= 0:
        yield index
        start = index + 1


def inside(char, rect):
    x0, y0, x1, y1 = char["box"]
    return rect[0] - .6 <= (x0 + x1) / 2 <= rect[2] + .6 and rect[1] - .6 <= (y0 + y1) / 2 <= rect[3] + .6


def selected_text(page, rects):
    return "".join(char["text"] for char in page["chars"] if any(inside(char, rect) for rect in rects))


def _rects(chars):
    rows = []
    for char in chars:
        if not char["text"].strip():
            continue
        x0, y0, x1, y1 = char["box"]
        if x1 <= x0 or y1 <= y0:
            continue
        if rows and abs((rows[-1][1] + rows[-1][3]) / 2 - (y0 + y1) / 2) < max(2, (y1 - y0) * .55) and x0 <= rows[-1][2] + max(15, y1 - y0):
            row = rows[-1]
            row[:] = [min(row[0], x0), min(row[1], y0), max(row[2], x1), max(row[3], y1)]
        else:
            rows.append([x0, y0, x1, y1])
    return [[round(x, 3) for x in rect] for rect in rows]


def validate_position(position, pages):
    index = position.get("pageIndex")
    if not isinstance(index, int) or not 0 <= index < len(pages):
        raise FormatterError("MANUAL_PAGE_INVALID")
    rects = position.get("rects", [])
    if not rects or len(rects) > 1000:
        raise FormatterError("MANUAL_RECTS_REQUIRED")
    page = pages[index]
    for rect in rects:
        if not isinstance(rect, list) or len(rect) != 4 or any(not isinstance(v, (int, float)) or not math.isfinite(v) for v in rect):
            raise FormatterError("MANUAL_RECT_INVALID")
        x0, y0, x1, y1 = rect
        if not (0 <= x0 < x1 <= page["width"] + .5 and 0 <= y0 < y1 <= page["height"] + .5):
            raise FormatterError("MANUAL_RECT_OUTSIDE_PAGE")


def _original_context(item, original):
    position = item.get("position", {})
    page_index = position.get("pageIndex", -1)
    if not 0 <= page_index < len(original):
        return "", ""
    page = original[page_index]
    text, index_map = text_index(page)
    query = normalized_text(item.get("text", ""))
    hits = list(occurrences(text, query))
    rects = position.get("rects", [])
    anchored = [hit for hit in hits if any(inside(page["chars"][index_map[k]], rect) for k in range(hit, hit + len(query)) for rect in rects)]
    if len(anchored) != 1:
        return "", ""
    at = anchored[0]
    return text[max(0, at - 48):at], text[at + len(query):at + len(query) + 48]


def _manual(item, position, pages):
    validate_position(position, pages)
    if item.get("type") in ("highlight", "underline"):
        selected = normalized_text(selected_text(pages[position["pageIndex"]], position["rects"]))
        if selected != normalized_text(item.get("text", "")):
            raise FormatterError("MANUAL_QUOTE_MISMATCH")
    output = copy.deepcopy(item.get("position", {}))
    if item.get("type") == "ink":
        paths = output.get("paths", [])
        points = [(path[i], path[i + 1]) for path in paths for i in range(0, len(path) - 1, 2)]
        if not points:
            raise FormatterError("INK_PATHS_MISSING")
        x0, y0 = min(x for x, y in points), min(y for x, y in points)
        x1, y1 = max(x for x, y in points), max(y for x, y in points)
        left, bottom, right, top = position["rects"][0]
        sx, sy = (right - left) / max(x1 - x0, .001), (top - bottom) / max(y1 - y0, .001)
        output["paths"] = [[left + (v - x0) * sx if i % 2 == 0 else bottom + (v - y0) * sy for i, v in enumerate(path)] for path in paths]
        output["width"] = output.get("width", 1) * min(sx, sy)
    else:
        output["rects"] = copy.deepcopy(position["rects"])
    output["pageIndex"] = position["pageIndex"]
    return output


def migrate_annotations(annotations, original_pdf, output_pdf, *, manual=None):
    if not annotations:
        return {"complete": True, "annotations": [], "unresolved": []}
    original = pdf_pages(original_pdf, chars=True)
    updated = pdf_pages(output_pdf, chars=True)
    indexed = [text_index(page) for page in updated]
    mapped, unresolved = [], []
    manual = manual or {}
    keys = {x["key"] for x in annotations}
    if set(manual) - keys:
        raise FormatterError("UNKNOWN_MANUAL_ANNOTATION")
    for item in normalized_annotations(annotations):
        current = copy.deepcopy(item)
        key = item["key"]
        try:
            offset = 0
            if key in manual:
                position = _manual(item, manual[key], updated)
            else:
                kind = item.get("type")
                if kind not in ("highlight", "underline", "note", "text"):
                    raise FormatterError("MANUAL_POSITION_REQUIRED")
                query = normalized_text(item.get("text", ""))
                # Sticky notes have no quoted text: use only the text actually under their original region.
                if not query and kind in ("note", "text"):
                    old = item.get("position", {})
                    page = old.get("pageIndex", -1)
                    if 0 <= page < len(original):
                        query = normalized_text(selected_text(original[page], old.get("rects", [])))
                if len(query) < 4:
                    raise FormatterError("ANCHOR_TOO_SHORT")
                hits = [(page, index) for page, (text, _) in enumerate(indexed) for index in occurrences(text, query)]
                if len(hits) > 1:
                    before, after = _original_context(item, original)
                    if len(before) + len(after) >= 12:
                        hits = [(page, at) for page, at in hits if (not before or indexed[page][0][max(0, at - len(before)):at] == before) and (not after or indexed[page][0][at + len(query):at + len(query) + len(after)] == after)]
                if len(hits) != 1:
                    raise FormatterError("ANCHOR_AMBIGUOUS" if hits else "ANCHOR_NOT_FOUND")
                page, at = hits[0]
                indexes = indexed[page][1][at:at + len(query)]
                rects = _rects([updated[page]["chars"][i] for i in dict.fromkeys(indexes)])
                if not rects:
                    raise FormatterError("ANCHOR_RECTS_EMPTY")
                position = {**copy.deepcopy(item.get("position", {})), "pageIndex": page, "rects": rects}
                offset = at
            current["position"] = position
            current["pageLabel"] = str(position["pageIndex"] + 1)
            top = max((r[3] for r in position.get("rects", [[0, 0, 0, 0]])), default=0)
            current["sortIndex"] = f"{position['pageIndex']:05}|{offset:06}|{max(0, round(updated[position['pageIndex']]['height'] - top)):05}"
            mapped.append(current)
        except FormatterError as exc:
            unresolved.append({"key": key, "code": exc.code, "type": item.get("type"), "text": item.get("text", ""), "original_position": item.get("position")})
    if not unresolved:
        verify_annotation_preservation(annotations, mapped)
    return {"complete": not unresolved, "annotations": mapped, "unresolved": unresolved}
