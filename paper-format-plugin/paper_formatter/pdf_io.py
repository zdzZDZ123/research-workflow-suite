"""Local PDF extraction. Uncertain recognition is explicit, editable review data."""
from __future__ import annotations

import copy
import re
from collections import Counter
from pathlib import Path

from .contracts import FormatterError, fingerprint, issue, utc_now, write_json
from .profiles import blank_profile
from .render import pdf_pages
from .annotations import normalized_text


def inspect_pdf_template(path):
    pages = pdf_pages(path, chars=True)
    if not pages:
        raise FormatterError('EMPTY_PDF')
    first = pages[0]
    sizes = Counter(round(c['size'], 1) for p in pages for c in p['chars'] if c['text'].strip())
    rules = {'page': {'width_mm': round(first['width'] * 25.4 / 72, 2),
                      'height_mm': round(first['height'] * 25.4 / 72, 2)}}
    if sizes:
        rules['body'] = {'size_pt': sizes.most_common(1)[0][0]}
    profile = blank_profile(rules)
    profile['sources'] = [{'kind': 'pdf_template', 'path': str(Path(path).resolve()),
                           'sha256': fingerprint(path), 'fetched_at': utc_now()}]
    profile['unresolved'] = ['PDF 样例只能可靠提取纸张大小及主要字号。请在规则编辑器确认页边距、字体、分栏和行距后保存。']
    return profile


def native_blocks(path):
    """Coordinate baseline also exposes parser omissions in the review editor."""
    blocks = []
    for page_index, page in enumerate(pdf_pages(path, chars=True)):
        line = []
        def flush():
            if not line:
                return
            text = ''.join(c['text'] for c in line).strip()
            if text:
                boxes = [c['box'] for c in line if c['box'][2] > c['box'][0]]
                blocks.append({'id': f'n{len(blocks)}', 'kind': 'text', 'text': text,
                    'page': page_index, 'bbox': [min(b[0] for b in boxes), min(b[1] for b in boxes),
                    max(b[2] for b in boxes), max(b[3] for b in boxes)] if boxes else [0,0,0,0],
                    'review_required': False})
            line.clear()
        for char in page['chars']:
            if char['text'] in ('\r', '\n'):
                flush()
            else:
                line.append(char)
        flush()
    return blocks


def _crop(pdf, page_index, box, destination):
    page = pdf[page_index]
    try:
        width, height = page.get_size()
        x0,y0,x1,y1 = box
        if x1 <= x0 or y1 <= y0:
            raise FormatterError('INVALID_PDF_REGION')
        bitmap = page.render(scale=3, crop=(max(0,x0), max(0,y0), max(0,width-x1), max(0,height-y1)))
        bitmap.to_pil().save(destination)
        bitmap.close()
    finally:
        page.close()


def coverage_issues(data):
    """A scan never weakens the independent baseline of another native page."""
    problems = []
    scans = set(data.get('scanned_pages', []))
    for page in range(data.get('page_count', 0)):
        blocks = [b for b in data['blocks'] if b.get('page', 0) == page]
        if not blocks:
            problems.append(issue('PDF_PAGE_EMPTY', '该页没有提取到内容，不能省略页面后覆盖；请修复源文件后重新解析。', page=page))
            continue
        if page not in scans:
            expected = Counter(normalized_text(data['native_text'][page]))
            actual = Counter(normalized_text(''.join(b['text'] for b in blocks if b['kind'] != 'image')))
            if expected != actual:
                problems.append(issue('PDF_TEXT_COVERAGE', '该页解析文字与原生文字不一致；请对照原页补齐或修正内容。', page=page))
    return problems


def extract_pdf(path, directory, models, progress=lambda *args: None):
    """Call only under offline_processing; no runtime model download is permitted."""
    models = Path(models)
    if not models.is_dir() or not any(models.iterdir()):
        raise FormatterError('MODELS_NOT_INSTALLED', '请运行安装脚本预装本地模型')
    from docling.document_converter import DocumentConverter, PdfFormatOption
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions, RapidOcrOptions
    from docling.datamodel.accelerator_options import AcceleratorOptions, AcceleratorDevice
    import pypdfium2 as pdfium
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    options = PdfPipelineOptions(artifacts_path=models, enable_remote_services=False,
        allow_external_plugins=False, do_ocr=True, do_table_structure=True,
        do_formula_enrichment=False, generate_page_images=False, generate_picture_images=False,
        accelerator_options=AcceleratorOptions(device=AcceleratorDevice.CPU, num_threads=4),
        ocr_options=RapidOcrOptions(backend='onnxruntime'))
    progress('parsing', '本地分析 PDF 阅读顺序、表格与图像')
    converter = DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)})
    result = converter.convert(Path(path), raises_on_error=True, max_file_size=512_000_000, max_num_pages=1000)
    doc = result.document
    doc.save_as_json(directory / 'docling.json')
    pages = pdf_pages(path, chars=True)
    scans = {i for i,p in enumerate(pages) if len(normalized_text(p['text'])) < 15}
    blocks, problems = [], []
    with pdfium.PdfDocument(path) as pdf:
        for item, depth in doc.iterate_items():
            label = str(item.label.value)
            if label in ('list', 'group') or not item.prov:
                continue
            # Table/picture child captions are emitted separately by Docling.
            prov = item.prov[0]
            pi = prov.page_no - 1
            box = prov.bbox.to_bottom_left_origin(pages[pi]['height'])
            bbox = [box.l, box.b, box.r, box.t]
            bid = f'b{len(blocks)}'
            base = {'id': bid, 'page': pi, 'bbox': bbox, 'label': label,
                    'review_required': pi in scans, 'text': getattr(item, 'text', '')}
            if label in ('picture', 'table', 'formula'):
                crop = directory / f'{bid}.png'
                _crop(pdf, pi, bbox, crop)
                base.update(kind='image', image=str(crop), image_sha256=fingerprint(crop),
                    width_pt=box.r-box.l, height_pt=box.t-box.b, review_required=True)
                if label == 'table':
                    base['recognized_table'] = item.export_to_markdown(doc=doc)
                    cells=[{'row':c.start_row_offset_idx,'col':c.start_col_offset_idx,
                            'row_span':c.row_span,'col_span':c.col_span,'text':c.text,
                            'header':c.column_header} for c in item.data.table_cells]
                    table={'rows':item.data.num_rows,'cols':item.data.num_cols,'cells':cells}
                    adopt_recognized_table(base,table)
                # Preserve formula/table pixels rather than trusting guessed symbols or cell spans.
                base.setdefault('review_reason',
                    '请核对原区域与重建表格的单元格、合并关系和顺序' if label=='table'
                    else '原区域作为图像保留；请核对区域完整、内容清晰以及阅读顺序')
            else:
                base['kind'] = 'heading' if label in ('section_header','title') else 'text'
                base['level'] = min(6, max(1, getattr(item, 'level', 1) or 1))
            blocks.append(base)
    # Independent side-by-side text regions make reading order uncertain even with perfect OCR.
    for pi in range(len(pages)):
        text_blocks=[b for b in blocks if b['page']==pi and b['kind'] in ('text','heading')]
        columns=any(abs(a['bbox'][3]-b['bbox'][3])<18 and
                    (a['bbox'][2]+30<b['bbox'][0] or b['bbox'][2]+30<a['bbox'][0])
                    for i,a in enumerate(text_blocks) for b in text_blocks[i+1:])
        if columns:
            for block in text_blocks:
                block.update(review_required=True,review_reason='检测到多栏文字，请核对阅读顺序；可调整每块的顺序号')
    # Every native glyph must be represented as text or in a preserved visual region.
    native = []
    for pi,p in enumerate(pages):
        regions = [b['bbox'] for b in blocks if b['page']==pi and b['kind']=='image']
        native.append(''.join(c['text'] for c in p['chars'] if not any(
            r[0]-.5 <= (c['box'][0]+c['box'][2])/2 <= r[2]+.5 and
            r[1]-.5 <= (c['box'][1]+c['box'][3])/2 <= r[3]+.5 for r in regions)))
    if not blocks:
        raise FormatterError('PDF_EMPTY_EXTRACTION')
    if len(pages) != len(doc.pages):
        problems.append(issue('PDF_PAGE_COUNT', '部分页面未被完整解析'))
    data = {'schema_version':1, 'blocks':blocks, 'issues':problems,
        'native_text':native, 'scanned_pages':sorted(scans), 'source_sha256':fingerprint(path),
        'native_blocks':native_blocks(path), 'page_count':len(pages)}
    coverage = coverage_issues(data)
    data['issues'].extend(coverage)
    affected = {i['page'] for i in coverage}
    for block in blocks:
        if block['page'] in affected:
            block['review_required'] = True
    write_json(directory / 'extraction.json', data)
    return data


def apply_review(extraction, reviews):
    data = copy.deepcopy(extraction)
    known = {b['id'] for b in data['blocks']}
    if set(reviews) - known:
        raise FormatterError('UNKNOWN_REVIEW_BLOCK')
    # Always recompute these checks, including older cached extractions.
    data['issues'] = [i for i in data['issues'] if i['code'] not in ('PDF_TEXT_COVERAGE', 'PDF_PAGE_EMPTY', 'PDF_BLOCK_REVIEW_REQUIRED')]
    for index,block in enumerate(data['blocks']):
        block['order']=index+1
        review = reviews.get(block['id'], {})
        if review:
            if review.get('confirmed') is not True:
                raise FormatterError('REVIEW_NOT_CONFIRMED')
            order=review.get('order',index+1)
            if not isinstance(order,(int,float)) or not 0<order<=len(data['blocks'])+1:
                raise FormatterError('REVIEW_ORDER_INVALID')
            block['order']=order
            if 'kind' in review:
                kind=review['kind']
                if kind not in ('text','heading','table','image') or (block['kind']=='image' and kind!='image') or (kind=='table' and not block.get('table')):
                    raise FormatterError('REVIEW_KIND_INVALID')
                block['kind']=kind
            if block['kind'] != 'image':
                if block['kind']=='table':
                    block['table']=review.get('table',block['table'])
                    validate_table(block['table'])
                    text=' '.join(c['text'] for c in block['table']['cells'])
                else:
                    text = review.get('text', block['text'])
                if not isinstance(text,str) or (block['text'].strip() and not text.strip()):
                    raise FormatterError('EMPTY_REVIEW_TEXT')
                block['text'] = text
            block['reviewed'] = True
        if block.get('review_required') and not block.get('reviewed'):
            data['issues'].append(issue('PDF_BLOCK_REVIEW_REQUIRED', block.get('review_reason','需要核对 OCR 文字及阅读顺序'), block_id=block['id'], page=block.get('page',0)))
    data['issues'].extend(coverage_issues(data))
    data['reviews'] = reviews
    data['blocks'].sort(key=lambda b:b['order'])
    return data


def rebuild_docx(extraction, target):
    from docx import Document
    from docx.shared import Pt, Inches
    doc = Document()
    for block in extraction['blocks']:
        if block['kind']=='image':
            if fingerprint(block['image']) != block['image_sha256']:
                raise FormatterError('PRESERVED_REGION_CHANGED')
            doc.add_picture(block['image'], width=Inches(min(6, block['width_pt']/72)))
        elif block['kind']=='table':
            data=block['table']
            validate_table(data)
            table=doc.add_table(rows=data['rows'],cols=data['cols'])
            table.style='Table Grid'
            for cell in data['cells']:
                row,col=cell['row'],cell['col']
                target_cell=table.cell(row,col)
                if cell['row_span']>1 or cell['col_span']>1:
                    target_cell=target_cell.merge(table.cell(row+cell['row_span']-1,col+cell['col_span']-1))
                target_cell.text=cell['text']
        elif block['kind']=='heading':
            doc.add_heading(block['text'], level=block.get('level',1))
        else:
            p=doc.add_paragraph(block['text'])
            if block.get('label') in ('caption','footnote'):
                p.style = 'Caption' if block['label']=='caption' else 'Normal'
    doc.save(target)


def validate_table(table):
    rows,cols=table.get('rows'),table.get('cols')
    if not isinstance(rows,int) or not isinstance(cols,int) or not 0<rows<=1000 or not 0<cols<=100:
        raise FormatterError('TABLE_DIMENSIONS_INVALID')
    occupied=set()
    for cell in table.get('cells',[]):
        if not isinstance(cell.get('text'),str):raise FormatterError('TABLE_CELL_TEXT_REQUIRED')
        r,c,rs,cs=(cell.get(k) for k in ('row','col','row_span','col_span'))
        if any(not isinstance(v,int) for v in (r,c,rs,cs)) or min(r,c)<0 or min(rs,cs)<1 or r+rs>rows or c+cs>cols:
            raise FormatterError('TABLE_CELL_BOUNDS')
        region={(y,x) for y in range(r,r+rs) for x in range(c,c+cs)}
        if occupied&region:raise FormatterError('TABLE_CELL_OVERLAP')
        occupied|=region
    if len(occupied)!=rows*cols:raise FormatterError('TABLE_MISSING_CELLS')


def adopt_recognized_table(block, table):
    """Keep an uncertain detected table as pixels instead of inventing cells."""
    try:
        validate_table(table)
    except FormatterError as exc:
        if not exc.code.startswith('TABLE_'):
            raise
        # The original crop and hash were already saved. A reviewer can check the
        # visible region, while coverage checks exclude its glyphs from text OCR.
        block.update(kind='image', text='', table_recognition_error=exc.code,
                     review_required=True,
                     review_reason='表格识别网格不完整；已保留原区域图像，请核对全部单元格、内容和阅读顺序')
        return False
    block.update(kind='table', table=table, text=' '.join(cell['text'] for cell in table['cells']))
    return True

