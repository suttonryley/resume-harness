"""Convert a resume's extracted text (.txt) into an editable Word document (.docx).

    python -m harness.docx_export runs/x/resume_v1.txt [--out runs/x/resume_v1.docx]
        [--page-size letter|a4]
    python -m harness.docx_export runs/x            # the run's best-scoring version

The input is the text `render.extract_text` writes next to each PDF: one line per
bullet, heading or entry line. Wrapped summary and skills lines are joined back
into paragraphs. The text is copied as-is: nothing is added, dropped or reworded,
so the .docx says exactly what the graded resume says. Prints a JSON summary on
stdout. Exit code 0 on success, 2 on unreadable or unrecognized input.

Layout rules, read off the classic template's text:
  - Lines before the first section heading are the masthead: the first is the
    name, a line with '|' or '@' is the contact line, anything else is the headline.
  - Section headings are the template's fixed <h2> titles (SECTIONS).
  - Lines starting with '•' are bullets. A line after a bullet that doesn't end in
    a date continues that bullet.
  - A line ending in a date or date range is an entry title (role, project,
    degree); the date goes on a right-aligned tab stop. The line after an entry
    title is its subtitle (company and location, school).
  - In Skills, 'Label: items' starts a group; other lines continue the group.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from docx import Document
from docx.enum.text import WD_TAB_ALIGNMENT
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor

from harness.render import MARGIN_IN, PAGE_SIZES_IN

SECTIONS = ("Summary", "Experience", "Projects", "Skills", "Education", "Certifications",
            "Security Clearance")
JOINED_SECTIONS = ("Summary",)          # wrapped lines are one paragraph
BULLET = "•"
_MONTH = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
_POINT = rf"(?:{_MONTH} )?\d{{4}}|Present"
DATE_TAIL = re.compile(rf"\s+((?:{_POINT})(?:\s*[–-]\s*(?:{_POINT}))?)$")
SKILL_GROUP = re.compile(r"^[^:•]{1,40}: ")

FONT = "Calibri"
INK = RGBColor(0x1F, 0x1F, 0x1F)
ACCENT = RGBColor(0x33, 0x33, 0x33)


def parse(text: str) -> dict:
    """Split extracted resume text into masthead lines and per-section blocks."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        raise ValueError("input is empty")
    first = next((i for i, ln in enumerate(lines) if ln in SECTIONS), None)
    if first is None:
        raise ValueError(f"no section headings found (expected one of {', '.join(SECTIONS)})")
    head = lines[:first]
    if not head:
        raise ValueError("no name line before the first section heading")
    contact = [ln for ln in head[1:] if "|" in ln or "@" in ln]
    masthead = {"name": head[0],
                "headline": [ln for ln in head[1:] if ln not in contact],
                "contact": contact}

    sections: list[dict] = []
    for ln in lines[first:]:
        if ln in SECTIONS:
            sections.append({"title": ln, "blocks": []})
            continue
        blocks = sections[-1]["blocks"]
        prev = blocks[-1] if blocks else None
        title = sections[-1]["title"]
        if title in JOINED_SECTIONS:
            if prev:
                prev["text"] += " " + ln
            else:
                blocks.append({"kind": "para", "text": ln})
        elif title == "Skills":
            if prev and not SKILL_GROUP.match(ln):
                prev["text"] += " " + ln
            else:
                blocks.append({"kind": "skill", "text": ln})
        elif ln.startswith(BULLET):
            blocks.append({"kind": "bullet", "text": ln[1:].strip()})
        elif (m := DATE_TAIL.search(ln)):
            blocks.append({"kind": "entry", "text": ln[:m.start()], "date": m.group(1)})
        elif prev and prev["kind"] == "bullet":
            prev["text"] += " " + ln
        elif prev and prev["kind"] == "entry":
            blocks.append({"kind": "subtitle", "text": ln})
        else:
            blocks.append({"kind": "para", "text": ln})
    return {"masthead": masthead, "sections": sections}


def _run(par, text: str, *, size: float = 10.5, bold: bool = False, italic: bool = False,
         color: RGBColor = INK):
    r = par.add_run(text)
    r.font.name = FONT
    r._element.get_or_add_rPr().get_or_add_rFonts().set(qn("w:eastAsia"), FONT)
    r.font.size = Pt(size)
    r.font.bold = bold
    r.font.italic = italic
    r.font.color.rgb = color
    return r


def _spacing(par, before: float = 0, after: float = 0):
    pf = par.paragraph_format
    pf.space_before = Pt(before)
    pf.space_after = Pt(after)
    pf.line_spacing = 1.05


def _bottom_border(par):
    p_pr = par._element.get_or_add_pPr()
    borders = OxmlElement("w:pBdr")
    bottom = OxmlElement("w:bottom")
    for k, v in {"w:val": "single", "w:sz": "6", "w:space": "1", "w:color": "808080"}.items():
        bottom.set(qn(k), v)
    borders.append(bottom)
    p_pr.append(borders)


def build(parsed: dict, page_size: str = "letter") -> Document:
    doc = Document()
    width_in, height_in = PAGE_SIZES_IN[page_size]
    sec = doc.sections[0]
    sec.page_width, sec.page_height = Inches(width_in), Inches(height_in)
    for side in ("left_margin", "right_margin", "top_margin", "bottom_margin"):
        setattr(sec, side, Inches(MARGIN_IN))
    right_tab = Inches(width_in - 2 * MARGIN_IN)

    normal = doc.styles["Normal"]
    normal.font.name = FONT
    normal.font.size = Pt(10.5)

    m = parsed["masthead"]
    p = doc.add_paragraph()
    _spacing(p, after=1)
    _run(p, m["name"], size=20, bold=True)
    for line in m["headline"]:
        p = doc.add_paragraph()
        _spacing(p, after=1)
        _run(p, line, size=12, color=ACCENT)
    for line in m["contact"]:
        p = doc.add_paragraph()
        _spacing(p, after=4)
        _run(p, line, size=10, color=ACCENT)

    for section in parsed["sections"]:
        p = doc.add_paragraph()
        _spacing(p, before=8, after=3)
        _run(p, section["title"].upper(), size=11, bold=True)
        _bottom_border(p)
        for b in section["blocks"]:
            kind = b["kind"]
            if kind == "bullet":
                p = doc.add_paragraph(style="List Bullet")
                _spacing(p, after=1.5)
                _run(p, b["text"])
            elif kind == "entry":
                p = doc.add_paragraph()
                _spacing(p, before=4)
                p.paragraph_format.tab_stops.add_tab_stop(right_tab, WD_TAB_ALIGNMENT.RIGHT)
                _run(p, b["text"], bold=True)
                _run(p, "\t" + b["date"])
            elif kind == "subtitle":
                p = doc.add_paragraph()
                _spacing(p, after=1.5)
                _run(p, b["text"], italic=True, color=ACCENT)
            elif kind == "skill":
                p = doc.add_paragraph()
                _spacing(p, after=1.5)
                label, _, items = b["text"].partition(": ")
                _run(p, label + ": ", bold=True)
                _run(p, items)
            else:
                p = doc.add_paragraph()
                _spacing(p, after=2)
                _run(p, b["text"])

    props = doc.core_properties
    props.title = f"{m['name']} – Resume"
    props.author = m["name"]
    return doc


def best_txt(run_dir: Path) -> Path:
    """The extracted text of a run's highest-scoring version (same pick as the report)."""
    state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
    graded = [h for h in state.get("history", []) if h.get("score") is not None]
    if not graded:
        raise ValueError(f"{run_dir} has no graded versions yet")
    best = max(graded, key=lambda h: h["score"])
    return run_dir / f"resume_{best['version']}.txt"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("resume_txt", type=Path,
                    help="A resume .txt, or a run directory (converts its best-scoring version)")
    ap.add_argument("--out", type=Path, help="Output .docx path (default: next to the input .txt)")
    ap.add_argument("--page-size", choices=sorted(PAGE_SIZES_IN), default="letter")
    args = ap.parse_args()

    try:
        src = best_txt(args.resume_txt) if args.resume_txt.is_dir() else args.resume_txt
        out = args.out or src.with_suffix(".docx")
        parsed = parse(src.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError, KeyError, json.JSONDecodeError) as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        return 2
    out.parent.mkdir(parents=True, exist_ok=True)
    build(parsed, args.page_size).save(out)
    counts: dict[str, int] = {}
    for s in parsed["sections"]:
        for b in s["blocks"]:
            counts[b["kind"]] = counts.get(b["kind"], 0) + 1
    print(json.dumps({"ok": True, "source": str(src), "out": str(out),
                      "sections": [s["title"] for s in parsed["sections"]],
                      "blocks": counts}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
