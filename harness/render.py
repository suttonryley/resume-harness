"""Render a Resume JSON to PDF and report how well it fits the page budget.

    python -m harness.render runs/x/resume_v1.json --out runs/x/resume_v1.pdf
        [--template classic] [--max-pages 2] [--page-size letter|a4]

Writes <out>.pdf and <out>.txt (text extracted from the PDF, which is what the
grader reads), then prints a JSON page-fit report on stdout. Exit code 0 when
the resume fits in --max-pages, 3 when it overflows, 2 on invalid input.
"""
from __future__ import annotations

import argparse
import base64
import json
import re
import math
import sys
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined
from playwright.sync_api import sync_playwright
from pydantic import ValidationError
from pypdf import PdfReader

from harness.models import Resume

TEMPLATES = Path(__file__).parent / "templates"
CSS_PX_PER_IN = 96
PAGE_SIZES_IN = {"letter": (8.5, 11.0), "a4": (8.2677, 11.6929)}
MARGIN_IN = 0.6
MONTHS = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()


def ym(value: str) -> str:
    """'2022-03' -> 'Mar 2022'; 'present' -> 'Present'; '2019' stays."""
    if value == "present":
        return "Present"
    if len(value) == 7 and value[4] == "-":
        return f"{MONTHS[int(value[5:]) - 1]} {value[:4]}"
    return value


def bare_url(url: str) -> str:
    for prefix in ("https://", "http://", "www."):
        if url.startswith(prefix):
            url = url[len(prefix):]
    return url.rstrip("/")


FONT_MIME = {".woff2": "font/woff2", ".woff": "font/woff", ".ttf": "font/ttf", ".otf": "font/otf"}


def inline_fonts(css: str) -> str:
    """Embed url("fonts/...") references as data URIs so the HTML is self-contained.

    Pages loaded with set_content can't read file:// URLs, so without this
    Chromium silently falls back to system fonts.
    """
    def sub(m: re.Match) -> str:
        path = TEMPLATES / m.group(1)
        data = base64.b64encode(path.read_bytes()).decode()
        return f'url("data:{FONT_MIME[path.suffix]};base64,{data}")'
    return re.sub(r'url\("(fonts/[^"]+)"\)', sub, css)


def render_html(resume: Resume, template: str, page_size: str) -> str:
    env = Environment(loader=FileSystemLoader(TEMPLATES), autoescape=True, undefined=StrictUndefined)
    env.filters["ym"] = ym
    env.filters["bare_url"] = bare_url
    css = inline_fonts((TEMPLATES / f"{template}.css").read_text(encoding="utf-8"))
    return env.get_template(f"{template}.html.j2").render(
        r=resume,
        css=css,
        page_size="letter" if page_size == "letter" else "A4",
        margin=f"{MARGIN_IN}in",
    )


LIGATURES = {"ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi", "ﬄ": "ffl"}


def normalize_extracted(raw: str) -> str:
    """Turn pypdf layout-mode text into one line per bullet or heading.

    Layout mode keeps reading order and bullet glyphs but pads columns with spaces
    and indents wrapped lines. Indented lines are joined to the line above (with no
    space after a trailing hyphen: the template never hyphenates, so a line-end '-'
    is a real hyphen), runs of spaces collapse, and ligature glyphs are expanded so
    the grader's evidence quotes match ordinary text.
    """
    for lig, plain in LIGATURES.items():
        raw = raw.replace(lig, plain)
    lines: list[str] = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        text = re.sub(r"\s{2,}", " ", line.strip())
        if line[:1].isspace() and lines and not text.startswith("•"):
            lines[-1] += text if lines[-1].endswith("-") else " " + text
        else:
            lines.append(text)
    return "\n".join(lines) + "\n"


def extract_text(pdf: Path) -> str:
    """Plain reading-order text, roughly what an ATS parser sees.

    One extractor everywhere (pypdf layout mode) so v0 and built resumes, on any
    machine, are graded on identical footing. Xpdf's pdftotext (bundled with Git for
    Windows) and pypdf's default mode both scramble bullet order on this template.
    """
    pages = [page.extract_text(extraction_mode="layout") or "" for page in PdfReader(str(pdf)).pages]
    return normalize_extracted("\n".join(pages))


def render(resume: Resume, out_pdf: Path, template: str = "classic",
           page_size: str = "letter", max_pages: int = 2) -> dict:
    html = render_html(resume, template, page_size)
    w_in, h_in = PAGE_SIZES_IN[page_size]
    content_w_px = (w_in - 2 * MARGIN_IN) * CSS_PX_PER_IN
    content_h_px = (h_in - 2 * MARGIN_IN) * CSS_PX_PER_IN

    out_pdf.parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": math.ceil(content_w_px), "height": 1000})
        page.emulate_media(media="print")
        page.set_content(html, wait_until="networkidle")
        missing = page.evaluate("""async () => {
            await Promise.allSettled([...document.fonts].map(f => f.load()));
            return [...document.fonts].filter(f => f.status !== 'loaded')
                .map(f => `${f.family} ${f.weight} ${f.style}`);
        }""")
        if missing:
            raise RuntimeError(f"Template fonts failed to load: {missing}")
        metrics = page.evaluate("""() => {
            const cs = getComputedStyle(document.body);
            return {height: document.body.getBoundingClientRect().height,
                    line: parseFloat(cs.lineHeight)};
        }""")
        page.pdf(path=str(out_pdf), prefer_css_page_size=True, print_background=True)
        browser.close()

    reader = PdfReader(str(out_pdf))
    pages = len(reader.pages)
    text = extract_text(out_pdf)
    out_pdf.with_suffix(".txt").write_text(text, encoding="utf-8")

    budget_px = content_h_px * max_pages
    line_px = metrics["line"]
    overflow_px = metrics["height"] - budget_px
    fits = pages <= max_pages
    # The height estimate ignores page breaks: bullets never split, headings stay
    # with their first bullet and the last two bullets stay together, so content
    # near a boundary moves down a little and can push the PDF over budget while
    # the raw height still fits. When the PDF overflows, count the lines
    # that actually landed on the extra pages instead.
    spilled = 0
    if not fits:
        extra = "\n".join(p.extract_text(extraction_mode="layout") or "" for p in reader.pages[max_pages:])
        spilled = sum(1 for line in extra.splitlines() if line.strip())
    return {
        "pdf": str(out_pdf),
        "txt": str(out_pdf.with_suffix(".txt")),
        "pages": pages,
        "max_pages": max_pages,
        "fits": fits,
        "fill_ratio": round(metrics["height"] / budget_px, 3),
        "overflow_lines": max(spilled, math.ceil(overflow_px / line_px)) if not fits else 0,
        "spare_lines": max(0, math.floor(-overflow_px / line_px)) if fits else 0,
        # True when the content would fit by height but page breaks push it over.
        "break_overflow": not fits and overflow_px <= 0,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("resume_json", type=Path)
    ap.add_argument("--out", type=Path, required=True, help="Output PDF path")
    ap.add_argument("--template", default="classic")
    ap.add_argument("--max-pages", type=int, default=2)
    ap.add_argument("--page-size", choices=sorted(PAGE_SIZES_IN), default="letter")
    args = ap.parse_args()

    try:
        resume = Resume.model_validate_json(args.resume_json.read_text(encoding="utf-8"))
    except ValidationError as e:
        print(json.dumps({"error": "invalid_resume", "details": json.loads(e.json())}, indent=2))
        return 2

    report = render(resume, args.out, args.template, args.page_size, args.max_pages)
    print(json.dumps(report, indent=2))
    return 0 if report["fits"] else 3


if __name__ == "__main__":
    sys.exit(main())
