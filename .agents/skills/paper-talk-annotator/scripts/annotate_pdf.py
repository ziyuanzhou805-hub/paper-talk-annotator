"""Apply a page-and-text annotation plan as editable PDF annotations."""

import argparse
from collections import Counter
from hashlib import file_digest
from io import BytesIO
import math
from pathlib import Path
import re
from statistics import median
from tempfile import NamedTemporaryFile
from uuid import uuid4

import pdfplumber
from pypdf import PdfReader, PdfWriter
from pypdf.generic import (
    ArrayObject, DecodedStreamObject, DictionaryObject, FloatObject, NameObject,
    NumberObject, TextStringObject,
)
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen.canvas import Canvas


AUTHOR = "Codex 文献精读"
COLORS = {
    "gray": (0.2, 0.2, 0.2),
    "red": (0.961, 0.133, 0.133),
    "blue": (0.016, 0.243, 0.631),
    "note": (0.122, 0.286, 0.490),
    "yellow": (1, 1, 0),
    "box": (0.024, 0.43, 0.39),
    "comment": (0.86, 0.48, 0.09),
}
SUBJECT = {
    "gray": "摘要要点", "red": "研究主线", "blue": "正文要点",
    "note": "精读旁注", "yellow": "核心定义与发现", "box": "关键参数",
    "comment": "精读核对",
}


def pdfdict(**entries):
    return DictionaryObject({NameObject("/" + key): value for key, value in entries.items()})


def numbers(values):
    return ArrayObject(FloatObject(round(value, 4)) for value in values)


def operands(values):
    return " ".join(f"{value:.4f}" for value in values)


def extract_lines(page, split=0.5, selected="LRF"):
    columns = {}
    for column, left, right in (
        ("L", 0, page.width * split), ("R", page.width * split, page.width),
        ("F", 0, page.width),
    ):
        if column not in selected:
            continue
        region = page.filter(lambda obj: obj["object_type"] != "char" or
                             left <= (obj["x0"] + obj["x1"]) / 2 < right)
        columns[column] = region.dedupe_chars().extract_text_lines(
            x_tolerance=2, y_tolerance=2, strip=True, return_chars=True
        )
    return columns


def get_line(columns, key):
    if key[0] not in columns or not key[1:].isdigit() or int(key[1:]) < 1:
        raise ValueError(f"Invalid line key: {key}")
    return columns[key[0]][int(key[1:]) - 1]


def locate(columns, entry, align_inline_latin):
    if entry.startswith("@"):
        left, top, right, bottom = map(float, entry[1:].split(","))
        if not (left < right and top < bottom and all(map(math.isfinite, (left, top, right, bottom)))):
            raise ValueError(f"Invalid region: {entry}")
        return left, top, right, bottom
    key, quote = entry.split(":", 1)
    row = get_line(columns, key)
    chars = [(letter, glyph) for glyph in row["chars"] for letter in glyph["text"]
             if not letter.isspace()]
    text = "".join(letter for letter, _ in chars)
    quote = "".join(quote.split())
    index = text.find(quote)
    if not quote or index < 0 or text.find(quote, index + 1) >= 0:
        raise ValueError(f"Anchor must match once: {entry} (line: {row['text']})")
    glyphs = [glyph for _, glyph in chars[index:index + len(quote)]]
    top = median(glyph["top"] for glyph in glyphs)
    bottom = median(glyph["bottom"] for glyph in glyphs)
    if align_inline_latin and not any("\u3400" <= letter <= "\u9fff" for letter in quote):
        # Some fonts report displaced text boxes for inline Latin glyphs.
        nearby = [r for r in columns[key[0]] if 6 < top - r["top"] < 10 and
                  any("\u3400" <= c <= "\u9fff" for c in r["text"])]
        if len(nearby) == 1:
            bottom -= top - nearby[0]["top"]
            top = nearby[0]["top"]
    return min(g["x0"] for g in glyphs), top, max(g["x1"] for g in glyphs), bottom


def stream(writer, bbox, commands, resources):
    form = DecodedStreamObject()
    form.update(pdfdict(Type=NameObject("/XObject"), Subtype=NameObject("/Form"),
                        BBox=numbers(bbox), Resources=resources))
    form.set_data(commands)
    return pdfdict(N=writer._add_object(form))


def appearance(writer, rect, body, opacity=1, blend="Normal"):
    resources = pdfdict(ExtGState=pdfdict(GS=pdfdict(
        Type=NameObject("/ExtGState"), CA=FloatObject(opacity), ca=FloatObject(opacity),
        BM=NameObject("/" + blend),
    )))
    return stream(writer, rect, f"q\n/GS gs\n{body}\nQ\n".encode("ascii"), resources)


def wrap_note(content, size, width):
    lines = []
    for paragraph in content.split("/"):
        line = ""
        for char in paragraph:
            if pdfmetrics.stringWidth(line + char, "ReadingNote", size) > width:
                lines.append(line.rstrip())
                line = ""
            line += char
        lines.append(line.rstrip())
    return lines


def annotation(writer, kind, page, columns, anchor, content, placed_notes, note_width,
               align_inline_latin):
    width, height = page.width, page.height
    rgb = COLORS[kind]
    extra = pdfdict()
    if kind in {"note", "comment"}:
        side, key = anchor.split("@", 1)
        if side not in {"left", "right"}:
            raise ValueError(f"Invalid margin: {side}")
        top = get_line(columns, key)["top"] if key[0] in columns else float(key)
        mark_width = note_width if kind == "note" else 12
        x = 6 if side == "left" else width - max(note_width, mark_width) - 5
        y = height - top + 1
        if kind == "note":
            size, leading = 8.5, 9.8
            lines = wrap_note(content, size, note_width - 2)
            note_height = leading * len(lines) + 2
            rect = (x, y - note_height, x + note_width, y)
            buffer = BytesIO()
            canvas = Canvas(buffer, pagesize=(note_width, note_height))
            canvas.setFont("ReadingNote", size)
            canvas.setFillColorRGB(*rgb)
            for i, line in enumerate(lines):
                canvas.drawString(1, note_height - size - i * leading, line)
            canvas.save()
            note = PdfReader(buffer).pages[0]
            ap = stream(writer, (0, 0, note_width, note_height),
                        note.get_contents().get_data(), note["/Resources"].clone(writer))
            content = "\n".join(lines)
            subtype = "FreeText"
            extra = pdfdict(IT=NameObject("/FreeTextTypewriter"),
                            DA=TextStringObject(f"/SimSun {size} Tf {operands(rgb)} rg"),
                            Border=numbers((0, 0, 0)))
        else:
            rect = (x, y - 12, x + 12, y)
            symbol = f"""1 .94 .79 rg {operands((x + .5, y - 11.5, 11, 11))} re f
{operands(rgb)} RG .9 w {operands((x + .5, y - 11.5, 11, 11))} re S
{operands((x + 3, y - 4))} m {operands((x + 9, y - 4))} l S
{operands((x + 3, y - 7))} m {operands((x + 8, y - 7))} l S"""
            ap = appearance(writer, rect, symbol)
            subtype = "Text"
            extra = pdfdict(Name=NameObject("/Comment"))
        for previous in placed_notes:
            if all(rect[i] < previous[i + 2] and previous[i] < rect[i + 2] for i in (0, 1)):
                raise ValueError(f"Overlapping margin notes: {anchor}")
        placed_notes.append(rect)
    else:
        boxes = [(left, height - bottom, right, height - top)
                 for left, top, right, bottom in (
                     locate(columns, part, align_inline_latin) for part in anchor.split(";")
                 )]
        if kind == "yellow":
            rect = (min(b[0] for b in boxes), min(b[1] for b in boxes),
                    max(b[2] for b in boxes), max(b[3] for b in boxes))
            fill = "\n".join(f"{operands((l, b, r - l, t - b))} re f" for l, b, r, t in boxes)
            ap = appearance(writer, rect, f"{operands(rgb)} rg\n{fill}", 0.72, "Multiply")
            subtype = "Highlight"
            extra = pdfdict(QuadPoints=numbers(
                value for l, b, r, t in boxes for value in (l, t, r, t, l, b, r, b)
            ), CA=FloatObject(0.72))
        elif kind == "box":
            if len(boxes) != 1:
                raise ValueError(f"A box requires one anchor: {anchor}")
            l, b, r, t = boxes[0]
            rect = (l - 1.5, b - 0.7, r + 1.5, t + 0.7)
            outline = operands((rect[0] + .5, rect[1] + .5, rect[2] - rect[0] - 1, rect[3] - rect[1] - 1))
            ap = appearance(writer, rect, f"{operands(rgb)} RG 1 w {outline} re S")
            subtype = "Square"
            extra = pdfdict(BS=pdfdict(W=FloatObject(1)))
        else:
            strokes = [(l, b - 0.25, r) for l, b, r, _ in boxes]
            rect = (min(l for l, _, _ in strokes) - 2, min(y for _, y, _ in strokes) - 2,
                    max(r for _, _, r in strokes) + 2, max(y for _, y, _ in strokes) + 2)
            drawing = "\n".join(f"{operands((l, y))} m {operands((r, y))} l S" for l, y, r in strokes)
            ap = appearance(writer, rect, f"{operands(rgb)} RG 2 w 1 J 1 j\n{drawing}", 0.70)
            subtype = "Ink"
            extra = pdfdict(InkList=ArrayObject(numbers((l, y, r, y)) for l, y, r in strokes),
                            BS=pdfdict(W=FloatObject(2)), CA=FloatObject(0.70))
    if not (0 <= rect[0] < rect[2] <= width and 0 <= rect[1] < rect[3] <= height):
        raise ValueError(f"Annotation lies outside the page: {anchor}")
    result = pdfdict(Subtype=NameObject("/" + subtype), Rect=numbers(rect), C=numbers(rgb),
                     T=TextStringObject(AUTHOR), Subj=TextStringObject(SUBJECT[kind]),
                     NM=TextStringObject(str(uuid4())), F=NumberObject(4), AP=ap)
    if content:
        result[NameObject("/Contents")] = TextStringObject(content)
    result.update(extra)
    return result


def page_annotations(page):
    return [ref.get_object() for ref in (page.get("/Annots") or [])]


def existing_signature(marks):
    return Counter(tuple(str(mark.get(key, "")) for key in (
        "/Subtype", "/NM", "/T", "/Rect", "/QuadPoints", "/Contents", "/C", "/F", "/InkList",
    )) for mark in marks)


def check_talk_cues(source, cue_pages):
    cue_pattern = re.compile(r"\[(\d+)\]")
    page_pattern = re.compile(r"第\s*(\d+)\s*(?:[-–—至到]\s*第?\s*(\d+)\s*)?页", re.I)
    found = set()
    for line in source.read_text(encoding="utf-8-sig").splitlines():
        if not line.startswith("> "):
            continue
        codes = set(cue_pattern.findall(line))
        if not codes:
            continue
        pages = {page for first, last in page_pattern.findall(line)
                 for page in range(int(first), int(last or first) + 1)}
        for code in codes:
            if code not in cue_pages:
                raise ValueError(f"Missing PDF cue [{code}]")
            if pages and pages.isdisjoint(cue_pages[code]):
                raise ValueError(f"Cue [{code}] is on PDF pages {sorted(cue_pages[code])}; "
                                 f"the talk points to {sorted(pages)}")
        found.update(codes)
    if not found:
        raise ValueError("The talk does not contain numbered cue lines")
    return len(found)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("plan", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--font", type=Path, required=True)
    parser.add_argument("--split", type=float, default=0.5)
    parser.add_argument("--note-width", type=float, default=47)
    parser.add_argument("--align-inline-latin", action="store_true")
    parser.add_argument("--talk", type=Path, help="Check cue numbers against the UTF-8 talk source")
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()
    if args.source.resolve() == args.output.resolve():
        parser.error("Use a different path for the annotated copy")
    if args.output.exists() and not args.overwrite:
        parser.error("Output already exists. Choose a different path or use --overwrite")
    if not 0 < args.split < 1 or not 12 <= args.note_width <= 100:
        parser.error("--split must be between 0 and 1; --note-width must be between 12 and 100")
    plan = args.plan.read_text(encoding="utf-8-sig").splitlines()
    for line in plan:
        if line.startswith("# source-sha256="):
            with args.source.open("rb") as source_file:
                digest = file_digest(source_file, "sha256").hexdigest()
            if digest != line.partition("=")[2].strip().lower():
                parser.error("Source PDF does not match the plan's SHA-256")
    pdfmetrics.registerFont(TTFont("ReadingNote", str(args.font), subfontIndex=0))
    source = PdfReader(args.source)
    writer = PdfWriter(clone_from=source)
    expected = Counter()
    page_marks = {}
    new_ids = set()
    seen = set()
    with pdfplumber.open(args.source) as doc:
        layouts = {}
        for line in plan:
            if not line.strip() or line.startswith("#"):
                continue
            fields = line.split("|", 3)
            page_number, kind, anchor = fields[:3]
            page_number = int(page_number)
            content = fields[3] if len(fields) == 4 else ""
            if not 1 <= page_number <= len(doc.pages) or kind not in COLORS:
                raise ValueError(f"Invalid page or annotation type: {line}")
            if (page_number, kind, anchor) in seen:
                raise ValueError(f"Duplicate annotation anchor: {line}")
            seen.add((page_number, kind, anchor))
            page = doc.pages[page_number - 1]
            if page_number not in layouts:
                original = source.pages[page_number - 1]
                if original.rotation or original.cropbox != original.mediabox or original.mediabox.lower_left != (0, 0):
                    raise ValueError(f"Page {page_number} requires rotated or cropped coordinate handling")
                layouts[page_number] = extract_lines(page, args.split)
            try:
                mark = annotation(writer, kind, page, layouts[page_number], anchor, content,
                                  page_marks.setdefault(page_number, []), args.note_width,
                                  args.align_inline_latin)
            except (IndexError, ValueError) as error:
                raise ValueError(f"Page {page_number}, {kind}, {anchor}: {error}") from error
            writer.add_annotation(page_number - 1, mark)
            new_ids.add(str(mark["/NM"]))
            expected[page_number, str(mark["/Subtype"])] += 1
    if not new_ids:
        parser.error("The plan contains no annotations")
    with NamedTemporaryFile(dir=args.output.parent, suffix=".pdf", delete=False) as temp:
        draft = Path(temp.name)
    try:
        writer.write(draft)
        reopened = PdfReader(draft)
        actual = Counter()
        cue_pages = {}
        if len(reopened.pages) != len(source.pages):
            raise ValueError("Page count changed")
        for n, page in enumerate(reopened.pages, 1):
            marks = page_annotations(page)
            if existing_signature(mark for mark in marks if str(mark.get("/NM")) not in new_ids) != \
               existing_signature(page_annotations(source.pages[n - 1])):
                raise ValueError(f"Existing annotations changed on page {n}")
            for mark in marks:
                if str(mark.get("/NM")) in new_ids:
                    if not mark["/AP"]["/N"].get_data():
                        raise ValueError(f"Missing appearance on page {n}")
                    actual[n, str(mark["/Subtype"])] += 1
                    if mark["/Subtype"] in {"/Text", "/FreeText"}:
                        for code in re.findall(r"\[(\d+)\]", str(mark.get("/Contents", ""))):
                            cue_pages.setdefault(code, set()).add(n)
        if actual != expected:
            raise ValueError(f"Annotation mismatch: {actual} != {expected}")
        if args.talk:
            print(f"Checked {check_talk_cues(args.talk, cue_pages)} talk cues against PDF notes")
        draft.replace(args.output)
    finally:
        draft.unlink(missing_ok=True)
    print(f"{len(source.pages)} pages, {sum(expected.values())} editable annotations")
    for key, count in sorted(expected.items()):
        print(*key, count)


if __name__ == "__main__":
    main()
