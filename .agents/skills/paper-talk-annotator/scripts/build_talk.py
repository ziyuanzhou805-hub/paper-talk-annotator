"""Typeset the page-cued oral script as an editable Word document."""

import argparse
from pathlib import Path

from docx import Document
from docx.enum.style import WD_STYLE_TYPE
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.opc.constants import RELATIONSHIP_TYPE
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Mm, Pt, RGBColor


def configure(style, size, color="000000", bold=False):
    style.font.name = "Microsoft YaHei"
    style.font.size = Pt(size)
    style.font.bold = bold
    style.font.color.rgb = RGBColor.from_string(color)
    fonts = style.element.get_or_add_rPr().rFonts
    for name in ("asciiTheme", "hAnsiTheme", "eastAsiaTheme", "cstheme"):
        fonts.attrib.pop(qn(f"w:{name}"), None)
    fonts.set(qn("w:eastAsia"), "Microsoft YaHei")


def field(paragraph, name):
    element = OxmlElement("w:fldSimple")
    element.set(qn("w:instr"), name)
    paragraph._p.append(element)


def hyperlink(paragraph, address):
    relation = paragraph.part.relate_to(address, RELATIONSHIP_TYPE.HYPERLINK, is_external=True)
    link = OxmlElement("w:hyperlink")
    link.set(qn("r:id"), relation)
    run = OxmlElement("w:r")
    properties = OxmlElement("w:rPr")
    color = OxmlElement("w:color")
    color.set(qn("w:val"), "245181")
    properties.append(color)
    run.append(properties)
    text = OxmlElement("w:t")
    text.text = address
    run.append(text)
    link.append(run)
    paragraph._p.append(link)


def build(source, destination):
    lines = source.read_text(encoding="utf-8-sig").splitlines()
    titles = [line[2:] for line in lines if line.startswith("# ")]
    if len(titles) != 1:
        raise ValueError("The talk must contain exactly one '# ' title")
    document = Document()
    document.core_properties.title = titles[0]
    document.core_properties.subject = "逐页讲述论文问题、方法、证据与结论"
    document.core_properties.author = ""
    section = document.sections[0]
    section.page_height, section.page_width = Mm(297), Mm(210)
    section.top_margin, section.bottom_margin = Mm(22), Mm(21)
    section.left_margin = section.right_margin = Mm(23)
    section.footer_distance = Mm(11)

    normal = document.styles["Normal"]
    configure(normal, 12.5)
    normal.paragraph_format.line_spacing = Pt(21)
    normal.paragraph_format.space_after = Pt(10)

    title = document.styles["Title"]
    configure(title, 22, bold=True)
    title.paragraph_format.line_spacing = Pt(30)
    title.paragraph_format.space_after = Pt(12)
    title.paragraph_format.keep_with_next = True
    properties = title.element.get_or_add_pPr()
    border = properties.find(qn("w:pBdr"))
    if border is not None:
        properties.remove(border)

    for name, size in (("Heading 1", 17), ("Heading 2", 13)):
        style = document.styles[name]
        configure(style, size, bold=True)
        style.paragraph_format.space_before = Pt(13)
        style.paragraph_format.space_after = Pt(5)
        style.paragraph_format.line_spacing = Pt(size + 8)
        style.paragraph_format.keep_with_next = True

    for name, size, color, spacing in (
        ("Talk Meta", 10, "454B53", 5), ("Talk Cue", 10.5, "245181", 12),
        ("Talk Source", 9.5, "454B53", 5), ("Talk Footer", 9, "454B53", 0),
    ):
        style = document.styles.add_style(name, WD_STYLE_TYPE.PARAGRAPH)
        style.base_style = normal
        configure(style, size, color)
        style.paragraph_format.line_spacing = Pt(size + 5)
        style.paragraph_format.space_after = Pt(spacing)
        style.paragraph_format.keep_with_next = name in {"Talk Cue", "Talk Meta"}

    footer = section.footer.paragraphs[0]
    footer.style = "Talk Footer"
    footer.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    footer.add_run("组会讲稿   ")
    field(footer, "PAGE")
    footer.add_run(" / ")
    field(footer, "NUMPAGES")

    new_page = False
    for line in lines:
        if not line:
            continue
        if line == "===":
            new_page = True
        elif line.startswith("# "):
            document.add_paragraph(line[2:], "Title")
        elif line.startswith("## "):
            heading = document.add_heading(line[3:], level=1)
            heading.paragraph_format.page_break_before = new_page
            new_page = False
        elif line.startswith("### "):
            document.add_heading(line[4:], level=2)
        elif line.startswith("@ "):
            document.add_paragraph(line[2:], "Talk Meta")
        elif line.startswith("> "):
            document.add_paragraph(line[2:], "Talk Cue")
        elif line.startswith("* "):
            citation, separator, address = line[2:].partition(" | ")
            paragraph = document.add_paragraph(citation, "Talk Source")
            if separator and address.startswith(("https://", "http://")):
                paragraph.add_run("\n")
                hyperlink(paragraph, address)
            elif separator:
                raise ValueError(f"Expected an HTTP(S) source URL: {address}")
        else:
            document.add_paragraph(line)
    document.save(destination)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()
    if args.source.resolve() == args.output.resolve() or (args.output.exists() and not args.overwrite):
        parser.error("Choose a new output path or explicitly use --overwrite for an existing DOCX")
    if args.output.suffix.lower() != ".docx":
        parser.error("The output extension must be .docx")
    build(args.source, args.output)
