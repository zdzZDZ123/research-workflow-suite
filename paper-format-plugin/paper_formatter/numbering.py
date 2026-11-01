"""Native multilevel heading numbering, without rewriting heading text."""
from lxml import etree
from .docx_io import q, xml, serialize, child, set_attrs, R, PKG, CT, W, NS


def apply_heading_numbering(parts, root, heading_styles, config):
    numbering = xml(parts["word/numbering.xml"]) if "word/numbering.xml" in parts else etree.Element(q("numbering"), nsmap={"w": W})
    aid = max([int(x.get(q("abstractNumId"))) for x in numbering.findall(q("abstractNum"))] + [-1]) + 1
    nid = max([int(x.get(q("numId"))) for x in numbering.findall(q("num"))] + [0]) + 1
    abstract = etree.SubElement(numbering, q("abstractNum"))
    set_attrs(abstract, abstractNumId=aid)
    set_attrs(child(abstract, "multiLevelType"), val="multilevel")
    for level in range(6):
        node = etree.SubElement(abstract, q("lvl"))
        set_attrs(node, ilvl=level)
        set_attrs(child(node, "start"), val=1)
        set_attrs(child(node, "numFmt"), val="decimal")
        set_attrs(child(node, "lvlText"), val=".".join("%" + str(n) for n in range(1, level + 2)))
        set_attrs(child(node, "lvlJc"), val="left")
    number = etree.SubElement(numbering, q("num"))
    set_attrs(number, numId=nid)
    set_attrs(child(number, "abstractNumId"), val=aid)
    for paragraph in root.findall(".//w:p", NS):
        style = paragraph.find("w:pPr/w:pStyle", NS)
        level = heading_styles.get(style.get(q("val"))) if style is not None else None
        if level and "numbered" in config.get(level, {}):
            props = child(paragraph, "pPr", first=True)
            num = child(props, "numPr")
            set_attrs(child(num, "ilvl"), val=int(level) - 1)
            set_attrs(child(num, "numId"), val=nid if config[level]["numbered"] else 0)
    parts["word/numbering.xml"] = serialize(numbering)
    rels = xml(parts["word/_rels/document.xml.rels"])
    if not any(x.get("Type") == R + "/numbering" for x in rels):
        etree.SubElement(rels, "{" + PKG + "}Relationship", Id="rIdPaperFormatNumbering", Type=R + "/numbering", Target="numbering.xml")
    parts["word/_rels/document.xml.rels"] = serialize(rels)
    types = xml(parts["[Content_Types].xml"])
    if not any(x.get("PartName") == "/word/numbering.xml" for x in types):
        etree.SubElement(types, "{" + CT + "}Override", PartName="/word/numbering.xml", ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.numbering+xml")
    parts["[Content_Types].xml"] = serialize(types)
