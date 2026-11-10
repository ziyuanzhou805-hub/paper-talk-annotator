"""Show PDF page geometry, existing annotations, and exact text anchors."""

import argparse
from collections import Counter
from hashlib import file_digest
from pathlib import Path

import pdfplumber
from pypdf import PdfReader

from annotate_pdf import extract_lines, page_annotations


def selected_pages(specification, count):
    if not specification:
        return range(1, count + 1)
    pages = set()
    for part in specification.split(","):
        first, _, last = part.partition("-")
        start, stop = int(first), int(last or first)
        if not 1 <= start <= stop <= count:
            raise ValueError(f"Page range outside the document: {part}")
        pages.update(range(start, stop + 1))
    return sorted(pages)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--pages", help="Physical page numbers such as 1-5,8")
    parser.add_argument("--column", choices=("all", "F", "LR", "L", "R"), default="all")
    parser.add_argument("--split", type=float, default=0.5)
    args = parser.parse_args()
    if not 0 < args.split < 1:
        parser.error("--split must be between 0 and 1")
    with args.source.open("rb") as stream:
        print(f"# source-sha256={file_digest(stream, 'sha256').hexdigest()}")
    reader = PdfReader(args.source)
    print(f"Physical page count: {len(reader.pages)}")
    with pdfplumber.open(args.source) as source:
        for number in selected_pages(args.pages, len(reader.pages)):
            original = reader.pages[number - 1]
            page = source.pages[number - 1]
            counts = Counter(str(mark.get("/Subtype")) for mark in page_annotations(original))
            print(f"\n=== PDF page {number} | {page.width:g} x {page.height:g} pt | "
                  f"rotation {original.rotation} | crop {original.cropbox} | marks {dict(counts)} ===")
            print(f"Extracted glyphs: {len(page.chars)}")
            columns = "LRF" if args.column == "all" else args.column
            for column, lines in extract_lines(page, args.split, columns).items():
                for index, row in enumerate(lines, 1):
                    print(f"{column}{index:02d} [{row['top']:.1f} {row['x0']:.1f}–{row['x1']:.1f}] {row['text']}")


if __name__ == "__main__":
    main()
