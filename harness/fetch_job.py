"""Fetch a job posting's text with a headless browser.

    python -m harness.fetch_job <url> --out-dir runs/<dir>

Writes <out-dir>/job_raw.txt and job_meta.json and prints the meta as JSON.
Exit codes: 0 fetched, 4 blocked (login wall, captcha, error status or too little
text; ask the user to paste the posting instead), 2 bad input.

Postings pasted by the user skip this module: the orchestrator writes job_raw.txt
itself and records method "pasted" in job_meta.json (see write_meta).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

MIN_CHARS = 1200  # a real posting is longer; less usually means a wall or a shell page
NAV_TIMEOUT_MS = 30_000
SETTLE_TIMEOUT_MS = 8_000

LOGIN_URL = re.compile(r"(authwall|/login|/signin|/sign-in|/checkpoint|/uas/|accounts\.google)", re.I)
BLOCK_TEXT = re.compile(
    r"(verify (that )?you are (a )?human|are you a robot|captcha|access denied|unusual traffic|"
    r"enable javascript and cookies|sign in to (view|see|continue)|join now to see|"
    r"request (has been )?blocked|checking your browser)", re.I)

# Containers that usually hold the posting body on common boards; the longest
# one with enough text wins, otherwise the whole page body is used.
CONTENT_SELECTORS = [
    "[data-automation-id='jobPostingDescription']",  # Workday
    "#content .job-post", "#app_body", ".job__description",  # Greenhouse
    ".posting-page", ".section-wrapper.page-full-width",  # Lever
    ".jobs-description", ".description__text",  # LinkedIn (public view)
    "#jobDescriptionText",  # Indeed
    "main", "article", "[role=main]",
]


@dataclass
class FetchResult:
    ok: bool
    text: str
    final_url: str
    status: int | None
    reason: str | None = None


def tidy(text: str) -> str:
    """Trim trailing spaces and collapse runs of blank lines."""
    lines = [ln.rstrip() for ln in text.replace("\r\n", "\n").split("\n")]
    out = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return out + "\n"


def blocked_reason(status: int | None, final_url: str, text: str) -> str | None:
    if status is not None and status >= 400:
        return f"HTTP {status}"
    if LOGIN_URL.search(final_url):
        return f"redirected to a login page ({final_url})"
    if len(text) < MIN_CHARS:
        hit = BLOCK_TEXT.search(text)
        return f"page looks blocked ('{hit.group(0)}')" if hit else f"only {len(text)} characters of text"
    hit = BLOCK_TEXT.search(text[:2000])
    if hit and len(text) < 3 * MIN_CHARS:
        return f"page looks blocked ('{hit.group(0)}')"
    return None


def fetch(url: str) -> FetchResult:
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page()
            try:
                response = page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
            except PlaywrightError as e:
                return FetchResult(False, "", url, None, f"navigation failed: {str(e).splitlines()[0]}")
            try:  # job boards often render the body client-side
                page.wait_for_load_state("networkidle", timeout=SETTLE_TIMEOUT_MS)
            except PlaywrightError:
                pass
            status = response.status if response else None
            body = page.evaluate("() => document.body ? document.body.innerText : ''")
            best = ""
            for sel in CONTENT_SELECTORS:
                for el in page.query_selector_all(sel):
                    t = el.inner_text()
                    if len(t) > len(best):
                        best = t
            text = tidy(best if len(best) >= MIN_CHARS else body)
            final_url = page.url
        finally:
            browser.close()
    reason = blocked_reason(status, final_url, text)
    return FetchResult(reason is None, text, final_url, status, reason)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def write_meta(out_dir: Path, text: str | None, url: str | None, method: str, **extra) -> dict:
    """Write job_raw.txt and job_meta.json; returns the meta. text=None keeps the
    job_raw.txt already there (a posting the user pasted)."""
    raw = out_dir / "job_raw.txt"
    if text is not None:
        raw.write_text(text, encoding="utf-8")
    meta = {"url": url, "method": method, "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "raw_text_sha256": sha256_text(raw.read_text(encoding="utf-8")), **extra}
    (out_dir / "job_meta.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    return meta


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("url")
    ap.add_argument("--out-dir", type=Path, required=True)
    args = ap.parse_args()
    if not re.match(r"^https?://", args.url):
        print(json.dumps({"ok": False, "reason": "URL must start with http:// or https://"}))
        return 2
    args.out_dir.mkdir(parents=True, exist_ok=True)
    result = fetch(args.url)
    if not result.ok:
        print(json.dumps({"ok": False, "url": args.url, "final_url": result.final_url,
                          "status": result.status, "reason": result.reason}, indent=2))
        return 4
    meta = write_meta(args.out_dir, result.text, args.url, "fetched",
                      final_url=result.final_url, status=result.status)
    print(json.dumps({"ok": True, **meta, "chars": len(result.text)}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
