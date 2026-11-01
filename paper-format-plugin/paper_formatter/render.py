"""Isolated LibreOffice rendering and local, per-page PDF geometry checks."""
from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

from .contracts import FormatterError, issue, write_json
from .offline import OFFLINE_ENV


def locate_soffice():
    explicit = os.environ.get("PAPER_FORMATTER_SOFFICE")
    choices = [explicit, shutil.which("soffice"),
               str(Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "LibreOffice/program/soffice.com"),
               str(Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "LibreOffice/program/soffice.exe")]
    for choice in choices:
        if choice and Path(choice).is_file():
            return str(Path(choice).resolve())
    raise FormatterError("LIBREOFFICE_REQUIRED", "请安装 LibreOffice 后运行环境检查")


def render_docx(source, directory):
    from .docx_io import read_package, security_issues
    problems = security_issues(read_package(source))
    if problems:
        raise FormatterError("RENDER_UNSAFE_DOCUMENT", problems[0]["code"])
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    # LibreOffice on Windows crashes with deeply nested profile paths (0xC0000409).
    # Keep this disposable application profile in a short, generated temp directory.
    office_profile = Path(tempfile.mkdtemp(prefix="pflo-"))
    user = office_profile / "user"
    user.mkdir(parents=True)
    # No macros or link updates; a private profile cannot attach to the user's Office instance.
    (user / "registrymodifications.xcu").write_text('''<?xml version="1.0" encoding="UTF-8"?>
<oor:items xmlns:oor="http://openoffice.org/2001/registry"><item oor:path="/org.openoffice.Office.Common/Security/Scripting"><prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop></item><item oor:path="/org.openoffice.Office.Writer/Content/Update"><prop oor:name="Link" oor:op="fuse"><value>2</value></prop></item></oor:items>''', encoding="utf-8")
    command = [locate_soffice(), "-env:UserInstallation=" + office_profile.as_uri(), "--headless", "--nologo", "--nodefault", "--norestore", "--nolockcheck", "--convert-to", "pdf:writer_pdf_Export", "--outdir", str(directory), str(Path(source).resolve())]
    env = {**os.environ, **OFFLINE_ENV}
    for key in list(env):
        if key.lower() in ("http_proxy", "https_proxy", "all_proxy"):
            env.pop(key)
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        result = subprocess.run(command, capture_output=True, timeout=180, env=env, creationflags=flags)
    except subprocess.TimeoutExpired as exc:
        raise FormatterError("RENDER_TIMEOUT") from exc
    output = directory / (Path(source).stem + ".pdf")
    if result.returncode or not output.is_file() or output.stat().st_size < 100:
        raise FormatterError("RENDER_FAILED", f"exit={result.returncode}; " + result.stderr.decode("utf-8", errors="replace")[:500])
    return {"pdf": str(output), "engine": "LibreOffice", "profile": str(office_profile)}


def pdf_pages(path, *, chars=False):
    import pypdfium2 as pdfium
    try:
        pdf = pdfium.PdfDocument(str(path))
    except Exception as exc:
        raise FormatterError("INVALID_OR_ENCRYPTED_PDF") from exc
    results = []
    try:
        for number in range(len(pdf)):
            page = pdf[number]
            textpage = page.get_textpage()
            try:
                record = {"index": number, "width": page.get_width(), "height": page.get_height(), "text": textpage.get_text_range()}
                if chars:
                    record["chars"] = []
                    for index in range(textpage.count_chars()):
                        char = textpage.get_text_range(index, 1)
                        if not char:
                            continue
                        box = list(textpage.get_charbox(index))
                        record["chars"].append({"text": char, "box": box, "index": index, "size": float(pdfium.raw.FPDFText_GetFontSize(textpage.raw, index))})
                results.append(record)
            finally:
                textpage.close()
                page.close()
    finally:
        pdf.close()
    return results


def inspect_render(pdf_path, directory, *, scale=2):
    import pypdfium2 as pdfium
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    records = pdf_pages(pdf_path, chars=True)
    problems = []
    pdf = pdfium.PdfDocument(str(pdf_path))
    try:
        for record in records:
            page = pdf[record["index"]]
            try:
                image_path = directory / f"page-{record['index'] + 1:04}.png"
                bitmap = page.render(scale=scale)
                image = bitmap.to_pil()
                image.save(image_path)
                bitmap.close()
                record["image"] = str(image_path.resolve())
                if not record["text"].strip():
                    # A figure-only page is valid; a visually blank one is a review item.
                    extrema = image.convert("L").getextrema()
                    if extrema[0] > 248:
                        problems.append(issue("BLANK_PAGE", "输出中出现空白页面", page=record["index"]))
                for char in record["chars"]:
                    if not char["text"].strip():
                        continue
                    x0, y0, x1, y1 = char["box"]
                    if x0 < -1 or y0 < -1 or x1 > record["width"] + 1 or y1 > record["height"] + 1:
                        problems.append(issue("TEXT_OUTSIDE_PAGE", "文字超出页面边界", page=record["index"]))
                        break
                # Exact repeated glyphs at essentially the same position usually indicate duplicated/overlaid text.
                occupied = set()
                duplicates = 0
                for char in record["chars"]:
                    if char["text"].strip():
                        key = (char["text"], *(round(x, 1) for x in char["box"]))
                        duplicates += key in occupied
                        occupied.add(key)
                if duplicates > 5:
                    problems.append(issue("OVERLAID_TEXT", "检测到重叠文字，需要本地检查", page=record["index"]))
            finally:
                page.close()
    finally:
        pdf.close()
    result = {"pdf": str(Path(pdf_path).resolve()), "pages": records, "text": "\n".join(x["text"] for x in records), "issues": problems, "inspection": "automated_geometry_and_render"}
    write_json(directory / "pages.json", result)
    return result
