from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas
from paper_formatter.annotations import migrate_annotations


def test_multiline_manual_regions_exclude_neighboring_words(tmp_path):
    original, output = tmp_path/'original.pdf', tmp_path/'output.pdf'
    c = canvas.Canvas(str(original)); c.setFont('Helvetica', 12)
    c.drawString(72, 750, 'selected first selected second'); c.save()
    c = canvas.Canvas(str(output)); c.setFont('Helvetica', 12)
    c.drawString(72, 750, 'prefix selected first')
    c.drawString(72, 728, 'selected second suffix'); c.save()
    item = {'key':'TESTNOTE', 'type':'highlight', 'text':'selected first selected second',
            'comment':'keep', 'color':'#ffd400', 'tags':[{'name':'review'}],
            'position':{'pageIndex':0, 'rects':[[72,746,270,762]]}}
    left = 72 + stringWidth('prefix ', 'Helvetica', 12)
    rects = [[left,746,left+stringWidth('selected first','Helvetica',12),762],
             [72,724,72+stringWidth('selected second','Helvetica',12),740]]
    result = migrate_annotations([item], original, output, manual={'TESTNOTE':{'pageIndex':0,'rects':rects}})
    assert result['complete'], result
    assert result['annotations'][0]['position']['rects'] == rects
    assert result['annotations'][0]['comment'] == 'keep'
    bad = migrate_annotations([item], original, output, manual={'TESTNOTE':{'pageIndex':0,'rects':[[72,724,300,762]]}})
    assert not bad['complete'] and bad['unresolved'][0]['code']=='MANUAL_QUOTE_MISMATCH'
