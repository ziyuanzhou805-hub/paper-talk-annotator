"""Extend an annotated paper's right margin with visible, editable reading notes."""

import argparse
from collections import Counter, defaultdict
from dataclasses import dataclass
from html import escape
from io import BytesIO
from math import isclose
from pathlib import Path
import re
from tempfile import NamedTemporaryFile
from uuid import uuid4

from pypdf import PdfReader, PdfWriter
from pypdf.generic import NameObject, NumberObject, RectangleObject, TextStringObject
from reportlab.lib.colors import HexColor
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen.canvas import Canvas
from reportlab.platypus import Paragraph

from annotate_pdf import check_talk_cues, existing_signature, numbers, page_annotations, pdfdict, stream


AUTHOR = "Codex 页边精读"
PADDING = 12
CARD_PADDING = 10
CARD_GAP = 8
TOP = 31
BOTTOM = 30


@dataclass(frozen=True)
class Note:
    title: str
    anchor: str
    body: str


def read_plan(path, total_pages):
    notes = defaultdict(list)
    for line_number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        fields = [part.strip() for part in line.split("|", 3)]
        if len(fields) != 4 or not all(fields) or not fields[0].isdigit():
            raise ValueError(f"Invalid margin note at line {line_number}: expected page|title|anchor|body")
        page = int(fields[0])
        if not 1 <= page <= total_pages:
            raise ValueError(f"Margin note page {page} is out of range")
        note = Note(*fields[1:])
        if note in notes[page]:
            raise ValueError(f"Duplicate margin note at line {line_number}")
        notes[page].append(note)
    if not notes:
        raise ValueError("The margin plan is empty")
    return notes


def paragraphs(note, font_size, width):
    parts = (
        (note.title, "MarginBold", font_size + 1.2, font_size + 4.7, "143A59", 3),
        (note.anchor, "MarginRegular", font_size - 1.1, font_size + 1.8, "45627B", 5),
        (note.body, "MarginRegular", font_size, font_size + 4.2, "172D40", 0),
    )
    items = []
    for text, font, size, leading, color, gap in parts:
        style = ParagraphStyle("Margin", fontName=font, fontSize=size, leading=leading,
                               textColor=HexColor(f"#{color}"), wordWrap="CJK")
        paragraph = Paragraph(escape(text), style)
        _, height = paragraph.wrap(width - 2 * CARD_PADDING, 1000)
        items.append((paragraph, height, gap))
    return items, 2 * CARD_PADDING + sum(height + gap for _, height, gap in items)


def appearance(writer, width, height, draw):
    buffer = BytesIO()
    canvas = Canvas(buffer, pagesize=(width, height))
    draw(canvas)
    canvas.showPage()
    canvas.save()
    artwork = PdfReader(buffer).pages[0]
    return stream(writer, (0, 0, width, height), artwork.get_contents().get_data(),
                  artwork["/Resources"].clone(writer))


def add_text(writer, page_index, rect, contents, subject, ap, size):
    annotation_id = str(uuid4())
    writer.add_annotation(page_index, pdfdict(
        Subtype=NameObject("/FreeText"), Rect=numbers(rect), T=TextStringObject(AUTHOR),
        NM=TextStringObject(annotation_id), Subj=TextStringObject(subject),
        Contents=TextStringObject(contents), AP=ap, F=NumberObject(4),
        IT=NameObject("/FreeTextTypewriter"),
        DA=TextStringObject(f"/SimSun {size:.2f} Tf 0.09 0.18 0.25 rg"),
        Border=numbers((0, 0, 0)),
    ))
    return annotation_id


def draw_card(canvas, width, height, items):
    canvas.setFillColor(HexColor("#F2F6FA"))
    canvas.roundRect(0, 0, width, height, 5, fill=1, stroke=0)
    canvas.setFillColor(HexColor("#477B9D"))
    canvas.roundRect(0, 7, 2.5, height - 14, 1.2, fill=1, stroke=0)
    y = height - CARD_PADDING
    for paragraph, line_height, gap in items:
        y -= line_height
        paragraph.drawOn(canvas, CARD_PADDING, y)
        y -= gap


def draw_heading(canvas, page, width):
    canvas.setFillColor(HexColor("#143A59"))
    canvas.setFont("MarginBold", 14)
    canvas.drawString(0, 18, "页边精读")
    canvas.setFont("MarginRegular", 9)
    canvas.drawRightString(width, 19, f"PDF 第 {page} 页")
    canvas.setStrokeColor(HexColor("#B7C9D8"))
    canvas.setLineWidth(0.65)
    canvas.line(0, 8, width, 8)


def page_bounds(page):
    box = tuple(float(value) for value in page.mediabox)
    if page.rotation or box[:2] != (0, 0) or tuple(page.cropbox) != tuple(page.mediabox):
        raise ValueError("Margin extension requires unrotated pages with equal, origin-aligned media and crop boxes")
    return box[2:]


def page_data(page):
    content = page.get_contents()
    return content.get_data() if content is not None else b""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("annotated_pdf", type=Path)
    parser.add_argument("plan", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--font", required=True, type=Path)
    parser.add_argument("--bold-font", type=Path)
    parser.add_argument("--rail-width", default=240, type=float)
    parser.add_argument("--font-size", default=10.5, type=float)
    parser.add_argument("--talk", type=Path, help="Check all talk cues are present in the expanded page margins")
    args = parser.parse_args()
    if args.output.resolve() == args.annotated_pdf.resolve() or args.output.exists():
        parser.error("Use a new output file")
    if not 180 <= args.rail_width <= 360 or not 9.5 <= args.font_size <= 14:
        parser.error("Use 180–360 pt for the margin and 9.5–14 pt for the body font")

    pdfmetrics.registerFont(TTFont("MarginRegular", str(args.font)))
    pdfmetrics.registerFont(TTFont("MarginBold", str(args.bold_font or args.font)))
    source = PdfReader(args.annotated_pdf)
    notes = read_plan(args.plan, len(source.pages))
    cue_pages = defaultdict(set)
    for page, group in notes.items():
        for note in group:
            for cue in re.findall(r"\[(\d+)\]", note.title):
                cue_pages[cue].add(page)
    if args.talk:
        print(f"Checked {check_talk_cues(args.talk, cue_pages)} talk cues in page margins")

    writer = PdfWriter(clone_from=source)
    expected = {}
    card_width = args.rail_width - 2 * PADDING
    for index, (source_page, target_page) in enumerate(zip(source.pages, writer.pages), start=1):
        old_width, page_height = page_bounds(source_page)
        extended = (0, 0, old_width + args.rail_width, page_height)
        target_page[NameObject("/MediaBox")] = RectangleObject(extended)
        target_page[NameObject("/CropBox")] = RectangleObject(extended)
        if index not in notes:
            continue
        cards = [paragraphs(note, args.font_size, card_width) for note in notes[index]]
        needed = sum(height for _, height in cards) + CARD_GAP * (len(cards) - 1)
        if needed > page_height - TOP - 35 - BOTTOM:
            raise ValueError(f"Page {index} margin needs {needed:.0f} pt; widen it or combine related notes")
        x = old_width + PADDING
        top = page_height - TOP
        heading = appearance(writer, card_width, 32, lambda c: draw_heading(c, index, card_width))
        heading_id = add_text(writer, index - 1, (x, top - 32, x + card_width, top),
                              f"页边精读 · PDF 第 {index} 页", "页边精读", heading, 14)
        expected[heading_id] = index
        top -= 35
        for note, (items, height) in zip(notes[index], cards):
            ap = appearance(writer, card_width, height, lambda c: draw_card(c, card_width, height, items))
            note_id = add_text(writer, index - 1, (x, top - height, x + card_width, top),
                               "\n".join((note.title, note.anchor, note.body)), note.title, ap, args.font_size)
            expected[note_id] = index
            top -= height + CARD_GAP

    with NamedTemporaryFile(dir=args.output.parent, suffix=".pdf", delete=False) as handle:
        temporary = Path(handle.name)
    try:
        writer.write(temporary)
        result = PdfReader(temporary)
        if len(result.pages) != len(source.pages):
            raise ValueError("Page count changed")
        found = Counter()
        for index, (before, after) in enumerate(zip(source.pages, result.pages), start=1):
            old_width, old_height = page_bounds(before)
            new_width, new_height = page_bounds(after)
            if (not isclose(new_width, old_width + args.rail_width, abs_tol=0.001)
                    or not isclose(new_height, old_height, abs_tol=0.001)
                    or page_data(before) != page_data(after)):
                raise ValueError(f"Original content or geometry changed on page {index}")
            retained = []
            for annotation in page_annotations(after):
                identifier = str(annotation.get("/NM"))
                if identifier not in expected:
                    retained.append(annotation)
                    continue
                rect = [float(value) for value in annotation["/Rect"]]
                if (expected[identifier] != index or annotation["/Subtype"] != "/FreeText"
                        or not annotation["/Contents"] or not annotation["/AP"]["/N"].get_data()
                        or not (old_width <= rect[0] < rect[2] <= new_width
                                and 0 <= rect[1] < rect[3] <= new_height)):
                    raise ValueError(f"Invalid editable margin note on page {index}")
                found[identifier] += 1
            if existing_signature(retained) != existing_signature(page_annotations(before)):
                raise ValueError(f"Existing annotations changed on page {index}")
        if found != Counter(expected.keys()):
            raise ValueError("Some margin notes are missing or duplicated")
        temporary.replace(args.output)
    finally:
        temporary.unlink(missing_ok=True)
    print(f"{len(result.pages)} pages, {sum(map(len, notes.values()))} visible editable notes on {len(notes)} pages")


if __name__ == "__main__":
    main()
