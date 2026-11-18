"""Split unmet requirements into presentation gaps and experience gaps.

The grader can't see the work history, so it can't tell "you have this but the
resume doesn't show it" from "you don't have this". This module can: for every
partial or missing requirement it looks for the requirement's normalized_terms in
skill names and aliases, achievement text, education and certifications.

  presentation  evidence exists in work_history.json; better selection or wording fixes it
  experience    no evidence (or the term is on constraints.never_claim, or the
                years_min exceeds total professional experience)

A requirement whose matching evidence is all already on the resume is an
experience gap too: no rewording can improve it. For an item-graded requirement
only the items that aren't met are looked up, so a framework the resume already
shows doesn't make the gap look fixable.

    python -m harness.gaps grade.json --job job.json --resume-text resume_v1.txt [--work-history ...]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from harness.evidence import Index, build_index, equivalents, professional_years
from harness.grade import GradeReport, effective
from harness.job import JobContext, Requirement, count_occurrences
from harness.work_history import WorkHistory

MAX_EVIDENCE_IDS = 6


def _spelling_key(term: str) -> str:
    """'node.js', 'nodejs' and 'Node JS' share a key; so do 'event-driven' and 'event driven'."""
    return re.sub(r"[^a-z0-9+#]", "", term.lower())


class Gap(BaseModel):
    req_id: str
    req_text: str
    type: Literal["required", "preferred"]
    weight: float
    status: Literal["partial", "missing"]
    unmet_items: list[str] = Field(default_factory=list, description="Item-graded requirements: items not met")
    gap_type: Literal["presentation", "experience"]
    matched_terms: list[str] = Field(default_factory=list)
    unmatched_terms: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list, description="sk_/ach_/edu_/cert_/clr_ ids")
    suggestion: str


def _classify(req: Requirement, status: str, idx: Index, years: float, resume_text: str,
              unmet_items: list[str] | None = None) -> Gap:
    wh = idx.wh
    base = dict(req_id=req.id, req_text=req.text, type=req.type, weight=req.weight, status=status,
                unmet_items=unmet_items or [])
    terms = req.normalized_terms
    if unmet_items:
        terms = list(dict.fromkeys(t for i in req.items if i.name in unmet_items for t in i.terms))

    banned = [t for t in wh.constraints.never_claim if count_occurrences(" | ".join(terms), [t]) > 0]
    if banned:
        return Gap(**base, gap_type="experience", unmatched_terms=terms,
                   suggestion=f"{', '.join(banned)} is on never_claim; don't add it. Address it in a cover "
                   "letter or interview if at all.")

    if req.category == "clearance":
        printable = [c for c in wh.clearances if c.public_wording and c.include_policy != "never"]
        if printable:
            return Gap(**base, gap_type="presentation", evidence_ids=[c.id for c in printable],
                       suggestion="Print the approved clearance line (Resume.clearance) verbatim.")
        return Gap(**base, gap_type="experience", unmatched_terms=terms,
                   suggestion="No printable clearance in the work history.")

    if req.category == "experience" and req.years_min and years < req.years_min:
        return Gap(**base, gap_type="experience",
                   suggestion=f"Asks for {req.years_min:g}+ years; work history has about {years:.1f} years "
                   "of professional experience.")

    matched: list[str] = []
    ids: list[str] = []
    for sid, hit in idx.skills_matching(terms).items():
        ids.append(sid)
        matched += hit
    in_progress_only = True
    for aid, info in idx.achievements.items():
        hit = idx.matches(info.surface_text, terms) or idx.matches(info.details_text, terms)
        if hit:
            ids.append(aid)
            matched += hit
            in_progress_only &= info.achievement.status != "complete"
    for e in wh.education:
        hit = idx.matches(" | ".join(x for x in [e.degree, e.field or "", e.institution, *e.details] if x), terms)
        if hit:
            ids.append(e.id)
            matched += hit
            in_progress_only = False
    for c in wh.certifications:
        hit = idx.matches(" | ".join(x for x in [c.name, c.issuer or ""] if x), terms)
        if hit:
            ids.append(c.id)
            matched += hit
            in_progress_only = False

    matched = list(dict.fromkeys(matched))
    keys = {_spelling_key(t) for t in matched}
    unmatched = [t for t in terms if t not in matched and _spelling_key(t) not in keys]
    ids = list(dict.fromkeys(ids))
    if not matched:
        return Gap(**base, gap_type="experience", unmatched_terms=unmatched,
                   suggestion="No evidence in the work history for any of: " + ", ".join(terms) + ".")

    # Only evidence the resume doesn't already show can be fixed by wording.
    collapsed = _spelling_key(resume_text)
    shown_keys = {_spelling_key(t) for t in matched if count_occurrences(resume_text, equivalents(t))
                  or (len(_spelling_key(t)) >= 5 and _spelling_key(t) in collapsed)}
    fixable = [t for t in matched if _spelling_key(t) not in shown_keys]
    if not fixable:
        rest = f" No work-history match for: {', '.join(unmatched)}." if unmatched else ""
        return Gap(**base, gap_type="experience", matched_terms=matched, unmatched_terms=unmatched,
                   suggestion=f"The resume already shows everything the work history supports here "
                              f"({', '.join(matched)}).{rest}")

    # Evidence for what the resume doesn't show yet comes first: that's what the builder needs.
    def supports_fixable(i: str) -> bool:
        if i in idx.achievements:
            info = idx.achievements[i]
            return bool(idx.matches(info.surface_text, fixable) or idx.matches(info.details_text, fixable))
        if i in idx.skill_terms:
            return bool(idx.matches(" | ".join(idx.skill_terms[i]), fixable))
        return True
    ids.sort(key=lambda i: not supports_fixable(i))
    ach_ids = [i for i in ids if i.startswith("ach_")]
    other_ids = [i for i in ids if not i.startswith("ach_")]
    shown = (ach_ids[:MAX_EVIDENCE_IDS - min(len(other_ids), 2)] + other_ids)[:MAX_EVIDENCE_IDS]
    msg = f"Work history shows {', '.join(fixable)}, which the resume doesn't"
    if len(fixable) < len(matched):
        msg += f" (it already shows {', '.join(t for t in matched if t not in fixable)})"
    msg += f" ({', '.join(shown)})" if shown else ""
    if req.category in ("education", "certification"):
        msg += "; include it in the Education/Certifications section."
    else:
        msg += "; make it explicit in a bullet or the skills section."
    if ach_ids and in_progress_only:
        msg += " The supporting work is in progress, so word it as ongoing."
    if unmatched:
        msg += f" No work-history match for: {', '.join(unmatched)}."
    return Gap(**base, gap_type="presentation", matched_terms=matched, unmatched_terms=unmatched,
               evidence_ids=shown, suggestion=msg)


def classify(grade: GradeReport, job: JobContext, wh: WorkHistory, resume_text: str) -> list[Gap]:
    idx = build_index(wh)
    years = professional_years(wh)
    reqs = {r.id: r for r in job.requirements}
    status = effective(grade, job)
    gaps = []
    for r in grade.requirement_results:
        if r.req_id not in reqs or status[r.req_id][0] == "met":
            continue
        req = reqs[r.req_id]
        unmet = None
        if req.items:
            graded = {" ".join(i.item.split()).lower(): i.status for i in r.item_results}
            unmet = [i.name for i in req.items if graded.get(" ".join(i.name.split()).lower()) != "met"]
        gaps.append(_classify(req, status[r.req_id][0], idx, years, resume_text, unmet))
    # Biggest point losses first: required before preferred, then by weight, missing before partial.
    return sorted(gaps, key=lambda g: (g.type != "required", -g.weight, g.status != "missing"))


def main() -> int:
    ap = argparse.ArgumentParser(description="Classify unmet requirements as presentation or experience gaps.")
    ap.add_argument("grade_json", type=Path)
    ap.add_argument("--job", type=Path, required=True)
    ap.add_argument("--resume-text", type=Path, required=True, help="the graded resume text")
    ap.add_argument("--work-history", type=Path, default=Path("data/work_history.json"))
    args = ap.parse_args()
    grade = GradeReport.model_validate_json(args.grade_json.read_text(encoding="utf-8"))
    job = JobContext.model_validate_json(args.job.read_text(encoding="utf-8"))
    wh = WorkHistory.model_validate_json(args.work_history.read_text(encoding="utf-8"))
    text = args.resume_text.read_text(encoding="utf-8")
    print(json.dumps([g.model_dump() for g in classify(grade, job, wh, text)], indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
