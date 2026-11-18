"""Check that a built resume stays inside the work history and the data rules.

Run on every resume the builder writes, before rendering. Any error sends the
resume back to the builder with the error list; a resume that never passes is
not graded. The v0 resume is the user's own and is not checked.

Checks (see CLAUDE.md "Data rules"):
  - header, roles, projects, education, certifications match the record
  - every bullet has source_ids that resolve to achievements of that role/project;
    the summary's source_ids resolve to summaries or achievements
  - every number in a bullet or the summary occurs in its sources
  - skills-section items are skill names or aliases from the record
  - nothing from constraints.never_claim or any achievement's private_terms
  - in-progress / proposed work isn't worded as finished
  - the clearance line is exactly the approved wording, only when allowed, and
    clearance terms appear nowhere else
  - no skill-specific year counts; total-experience years don't exceed the record
  - approximate metrics keep a qualifier; non-shareable metrics never appear

    python -m harness.grounding resume_v1.json --job job.json [--work-history data/work_history.json]
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path

from harness.evidence import Index, allowed_clearance, build_index, professional_years
from harness.job import JobContext, count_occurrences
from harness.models import Resume
from harness.work_history import WorkHistory

NUMBER = re.compile(r"(?<![A-Za-z0-9.])\d[\d,]*(?:\.\d+)?")
YEARS_CLAIM = re.compile(r"(\d+(?:\.\d+)?)\s*\+?\s*(?:years?|yrs?)\b", re.I)
COMPLETION_WORDS = re.compile(
    r"\b(replaced|completed|finished|delivered|launched|shipped|eliminated|retired|decommissioned|"
    r"migrated|consolidated|achieved|rolled out|cut over|went live)\b", re.I)
PROGRESS_MARKERS = re.compile(
    r"\b(leading|lead|leads|driving|drives|ongoing|in progress|in-progress|currently|underway|initiated|"
    r"initiating|proposing|proposed|designing|building|planning|piloting|discovery|developing|working|"
    r"heading|toward|towards)\b", re.I)
CLEARANCE_TERMS = ["clearance", "ts/sci", "top secret", "polygraph"]
QUALIFIERS = re.compile(r"(~|\babout|\bapproximately|\broughly|\baround|\bnearly|\bunder|\bover|"
                        r"\bless than|\bup to|\bmore than)\s*$", re.I)


def _numbers(text: str) -> set[str]:
    return {n.replace(",", "").rstrip(".") for n in NUMBER.findall(text)}


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def _text_fields(resume: Resume) -> list[tuple[str, str]]:
    """(location, text) for everything printed except the clearance line."""
    out: list[tuple[str, str]] = []
    h = resume.header
    out += [("header", x) for x in [h.name, h.headline or "", h.contact.location or ""] if x]
    if resume.summary:
        out.append(("summary", resume.summary.text))
    for i, r in enumerate(resume.experience):
        out += [(f"experience[{i}]", r.title), (f"experience[{i}]", r.company)]
        out += [(f"experience[{i}].bullets[{j}]", b.text) for j, b in enumerate(r.bullets)]
    for i, p in enumerate(resume.projects):
        out.append((f"projects[{i}]", p.name))
        out += [(f"projects[{i}].bullets[{j}]", b.text) for j, b in enumerate(p.bullets)]
    for g in resume.skills:
        out += [("skills", g.group), *[("skills", x) for x in g.items]]
    for e in resume.education:
        out += [("education", x) for x in [e.degree, e.field or "", e.institution, *e.details] if x]
    for c in resume.certifications:
        out += [("certifications", x) for x in [c.name, c.issuer or ""] if x]
    return out


def _check_bullet(where: str, text: str, source_ids: list[str], owner: str, idx: Index,
                  errors: list[str]) -> None:
    if not source_ids:
        errors.append(f"{where}: bullet has no source_ids")
        return
    sources = []
    for sid in source_ids:
        info = idx.achievements.get(sid)
        if info is None:
            errors.append(f"{where}: source id {sid} is not an achievement in work_history.json")
        elif info.holder_id != owner:
            errors.append(f"{where}: {sid} belongs to {info.holder_id}, not {owner}")
        else:
            sources.append(info)
    if not sources:
        return
    _check_numbers(where, text, " | ".join(s.source_text for s in sources), errors)
    _check_technologies(where, text, sources, idx, errors)
    unfinished = [s.id for s in sources if s.achievement.status != "complete"]
    if unfinished:
        done = COMPLETION_WORDS.findall(text)
        if done:
            errors.append(f"{where}: sources {', '.join(unfinished)} are in progress or proposed, but the bullet "
                          f"says {', '.join(sorted(set(w.lower() for w in done)))!s}; word it as ongoing")
        elif not PROGRESS_MARKERS.search(text):
            errors.append(f"{where}: sources {', '.join(unfinished)} are in progress or proposed; the bullet "
                          "must say so (e.g. 'Leading', 'Driving', 'in progress')")
    for s in sources:
        for m in s.achievement.metrics:
            if m.approximate and m.value.startswith("~"):
                core = _norm(m.value[1:].split("→")[0])
                for hit in re.finditer(re.escape(core), _norm(text)):
                    if not QUALIFIERS.search(_norm(text)[:hit.start()]):
                        errors.append(f"{where}: '{core}' is an approximate figure ({s.id}); keep '~' or 'about'")


def _check_technologies(where: str, text: str, sources: list, idx: Index, errors: list[str]) -> None:
    """A skill named in a bullet must be one of its sources' skills or appear in their text,
    so a tool used elsewhere in the role (or at another job) can't be attached here."""
    own = {sid for s in sources for sid in s.achievement.skills}
    source_text = " | ".join(f"{s.achievement.summary} | {s.details_text}" for s in sources)
    for sid, names in idx.skill_terms.items():
        if sid in own:
            continue
        hit = [n for n in names if count_occurrences(text, [n])]
        if hit and not any(count_occurrences(source_text, [n]) for n in names):
            errors.append(f"{where}: names {hit[0]!r}, which isn't in its source achievements "
                          f"({', '.join(s.id for s in sources)})")


def _check_numbers(where: str, text: str, source_text: str, errors: list[str]) -> None:
    allowed = _numbers(source_text)
    years_spans = [m.span(1) for m in YEARS_CLAIM.finditer(text)]
    for m in NUMBER.finditer(text):
        if any(a <= m.start() < b for a, b in years_spans):
            continue  # years are checked separately
        n = m.group(0).replace(",", "").rstrip(".")
        if n not in allowed:
            errors.append(f"{where}: number {m.group(0)!r} does not appear in its sources")


def check(resume: Resume, wh: WorkHistory, job: JobContext) -> list[str]:
    errors: list[str] = []
    idx = build_index(wh)
    prof = wh.profile

    # Header
    h = resume.header
    if h.name != prof.name:
        errors.append(f"header.name {h.name!r} must be {prof.name!r}")
    if h.contact.email.lower() != prof.contact.email.lower():
        errors.append("header.contact.email must match the work history profile")
    if h.contact.phone and h.contact.phone != prof.contact.phone:
        errors.append("header.contact.phone must match the work history profile")
    titles = set(prof.headlines) | {t for r in wh.roles for t in [r.title, *r.alt_titles]}
    if h.headline and h.headline not in titles:
        errors.append(f"header.headline {h.headline!r} must be a profile headline or a title you've held")
    known_links = {l.url.rstrip("/") for l in prof.contact.links}
    for l in h.contact.links:
        if l.url.rstrip("/") not in known_links:
            errors.append(f"header link {l.url} is not in the work history profile")

    # Summary
    if resume.summary:
        if not resume.summary.source_ids:
            errors.append("summary: needs source_ids (sum_ or ach_ ids it draws on)")
        sums = {s.id: s.text for s in prof.summaries}
        src = []
        for sid in resume.summary.source_ids:
            if sid in sums:
                src.append(sums[sid])
            elif sid in idx.achievements:
                src.append(idx.achievements[sid].source_text)
            else:
                errors.append(f"summary: source id {sid} is not a summary or achievement")
        if src:
            _check_numbers("summary", resume.summary.text, " | ".join(src), errors)

    # Roles
    roles = {r.id: r for r in wh.roles}
    for i, r in enumerate(resume.experience):
        where = f"experience[{i}]"
        wr = roles.get(r.source_role_id or "")
        if wr is None:
            errors.append(f"{where}: source_role_id {r.source_role_id!r} is not a role in work_history.json")
            continue
        if r.title != wr.title and r.title not in wr.alt_titles:
            errors.append(f"{where}: title {r.title!r} must be {wr.title!r} or one of its alt_titles")
        if not any(count_occurrences(r.company, [n]) for n in [wr.company, *wr.company_aliases]):
            errors.append(f"{where}: company {r.company!r} doesn't name {wr.company!r}")
        if (r.start, r.end) != (wr.start, wr.end):
            errors.append(f"{where}: dates {r.start}..{r.end} must be {wr.start}..{wr.end}")
        if r.location and wr.location and r.location != wr.location:
            errors.append(f"{where}: location {r.location!r} must be {wr.location!r} or omitted")
        for j, b in enumerate(r.bullets):
            _check_bullet(f"{where}.bullets[{j}]", b.text, b.source_ids, wr.id, idx, errors)

    # Projects
    projects = {p.id: p for p in wh.projects}
    for i, p in enumerate(resume.projects):
        where = f"projects[{i}]"
        wp = projects.get(p.source_project_id or "")
        if wp is None:
            errors.append(f"{where}: source_project_id {p.source_project_id!r} is not a project in work_history.json")
            continue
        if p.name != wp.name:
            errors.append(f"{where}: name {p.name!r} must be {wp.name!r}")
        if p.url and p.url != wp.url:
            errors.append(f"{where}: url must be {wp.url!r} or omitted")
        for j, b in enumerate(p.bullets):
            _check_bullet(f"{where}.bullets[{j}]", b.text, b.source_ids, wp.id, idx, errors)

    # Skills section
    names = {_norm(n) for terms in idx.skill_terms.values() for n in terms}
    for g in resume.skills:
        for item in g.items:
            if _norm(item) not in names:
                errors.append(f"skills: {item!r} is not a skill name or alias in work_history.json")

    # Education and certifications
    for e in resume.education:
        if not any(e.institution == we.institution and e.degree == we.degree and e.field in (None, we.field)
                   for we in wh.education):
            errors.append(f"education: {e.degree} at {e.institution} doesn't match the work history")
    for c in resume.certifications:
        if not any(c.name == wc.name for wc in wh.certifications):
            errors.append(f"certifications: {c.name!r} is not in the work history")

    # Whole-document text rules
    fields = _text_fields(resume)
    private = {t for info in idx.achievements.values() for t in info.achievement.private_terms}
    hidden = [m.value for info in idx.achievements.values() for m in info.achievement.metrics if not m.shareable]
    years_cap = math.floor(professional_years(wh))
    skill_names = [n for terms in idx.skill_terms.values() for n in terms]
    for where, text in fields:
        for term in wh.constraints.never_claim:
            if count_occurrences(text, [term]):
                errors.append(f"{where}: mentions {term!r}, which is on never_claim")
        for term in private:
            if count_occurrences(text, [term]):
                errors.append(f"{where}: contains a private term from an achievement (confidential); remove it")
        for value in hidden:
            if _norm(value) in _norm(text):
                errors.append(f"{where}: prints a non-shareable metric ({value!r})")
        for term in CLEARANCE_TERMS:
            if count_occurrences(text, [term]):
                errors.append(f"{where}: mentions {term!r}; clearance may only appear as Resume.clearance")
        for m in YEARS_CLAIM.finditer(text):
            if float(m.group(1)) > years_cap:
                errors.append(f"{where}: claims {m.group(0)!r} but the record has {years_cap}+ years in total")
            window = " ".join(text[m.end():].split()[:6])
            hit = [n for n in skill_names if count_occurrences(window, [n])]
            if hit:
                errors.append(f"{where}: {m.group(0)!r} next to {hit[0]!r} reads as a skill-specific year count; "
                              "derived skill years are never printed")

    # Clearance
    allowed = allowed_clearance(wh, job.requests_clearance)
    if resume.clearance is not None:
        if allowed is None:
            errors.append("clearance: this job does not request a clearance; leave Resume.clearance null")
        elif resume.clearance != allowed:
            errors.append(f"clearance: must be exactly {allowed!r}")

    # Target
    if resume.target and job.source.raw_text_sha256 and resume.target.job_sha != job.source.raw_text_sha256:
        errors.append("target.job_sha must equal job.json source.raw_text_sha256")

    return list(dict.fromkeys(errors))


def main() -> int:
    ap = argparse.ArgumentParser(description="Grounding checks for a built resume.")
    ap.add_argument("resume_json", type=Path)
    ap.add_argument("--job", type=Path, required=True)
    ap.add_argument("--work-history", type=Path, default=Path("data/work_history.json"))
    args = ap.parse_args()
    resume = Resume.model_validate_json(args.resume_json.read_text(encoding="utf-8"))
    job = JobContext.model_validate_json(args.job.read_text(encoding="utf-8"))
    wh = WorkHistory.model_validate_json(args.work_history.read_text(encoding="utf-8"))
    errors = check(resume, wh, job)
    print(json.dumps({"ok": not errors, "errors": errors}, indent=2, ensure_ascii=False))
    return 0 if not errors else 1


if __name__ == "__main__":
    sys.exit(main())
