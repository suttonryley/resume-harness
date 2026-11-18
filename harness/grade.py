"""GradeReport: the resume-grader subagent's judgment of a resume against a job.

The grader marks each requirement met / partial / missing and quotes evidence.
A requirement that lists items (job.Requirement.items) is graded item by item
instead, and code combines the item statuses by the requirement's `match` rule
(requirement_value). The grader never outputs a score: score.py computes that,
and keyword presence is checked by code (score.keyword_hits), not judged.
Unknown fields are rejected, so a grader that adds "score" fails validation.

Field order puts rationale and evidence before status: the grader writes the
JSON in that order, so it reasons and quotes before it labels.

validate_grade() enforces what the schema can't: every requirement (and every
item) graded exactly once, and every evidence quote is a literal substring of
the resume text the grader was given (whitespace, case, curly quotes and bullet
glyphs normalized).

    python -m harness.grade grade.json --job job.json --resume-text resume_v0.txt
    python -m harness.grade --schema
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from harness.job import JobContext, Requirement

MIN_EVIDENCE_CHARS = 4

Status = Literal["met", "partial", "missing"]
STATUS_VALUE: dict[str, float] = {"met": 1.0, "partial": 0.5, "missing": 0.0}

_EVIDENCE_DESC = ("Literal substrings of the resume text that support the status. Required for met and "
                  "partial; must be empty for missing")


class ItemResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    item: str = Field(description="The item's name, exactly as job.json lists it")
    resume_evidence: list[str] = Field(default_factory=list, description=_EVIDENCE_DESC)
    status: Status


class RequirementResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    req_id: str
    rationale: str = Field(description="One or two sentences, written first: what the text shows and what it lacks")
    item_results: list[ItemResult] = Field(
        default_factory=list, description="Only for requirements with items: one result per item. "
        "Then resume_evidence and status are left out")
    resume_evidence: list[str] = Field(default_factory=list, description=_EVIDENCE_DESC + ". Unused when "
                                       "the requirement has items")
    status: Status | None = Field(None, description="Required unless the requirement has items")


class GradeReport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["1.0"] = "1.0"
    resume_version: str = Field(description="v0, v1, v2 ...", pattern=r"^v\d+$")
    job_sha: str = Field(description="job.json source.raw_text_sha256")
    requirement_results: list[RequirementResult]
    seniority_fit: Literal["below", "match", "above"] = Field(
        description="How the resume's level compares with the role's seniority")
    notes: str = Field("", description="Anything a reader should know; not used in scoring")


def normalize(text: str) -> str:
    """Comparison form for evidence checks."""
    text = text.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    text = text.replace("–", "-").replace("—", "-").replace("•", " ")
    return re.sub(r"\s+", " ", text).strip().lower()


def _key(name: str) -> str:
    return " ".join(name.split()).lower()


def requirement_value(req: Requirement, res: RequirementResult) -> float:
    """0..1 credit for one requirement. Assumes the grade passed validate_grade."""
    if not req.items:
        return STATUS_VALUE[res.status]
    vals = [STATUS_VALUE[i.status] for i in res.item_results]
    if req.match == "any":
        return max(vals)
    if req.match == "most":
        return min(1.0, sum(vals) / math.ceil(len(vals) / 2))
    return sum(vals) / len(vals)


def value_status(value: float) -> Status:
    return "met" if value >= 1.0 else "missing" if value <= 0.0 else "partial"


def effective(grade: GradeReport, job: JobContext) -> dict[str, tuple[Status, float]]:
    """req_id -> (status, value), with item-graded requirements combined by their match rule."""
    reqs = {r.id: r for r in job.requirements}
    out = {}
    for res in grade.requirement_results:
        if res.req_id in reqs:
            v = requirement_value(reqs[res.req_id], res)
            out[res.req_id] = (value_status(v), v)
    return out


def _check_evidence(label: str, status: str, quotes: list[str], haystack: str, errors: list[str]) -> None:
    if status in ("met", "partial") and not quotes:
        errors.append(f"{label} is {status} but quotes no resume_evidence")
    if status == "missing" and quotes:
        errors.append(f"{label} is missing but quotes resume_evidence; use partial or drop the quotes")
    for quote in quotes:
        q = normalize(quote)
        if len(q) < MIN_EVIDENCE_CHARS:
            errors.append(f"{label}: evidence {quote!r} is too short to be meaningful")
        elif q not in haystack:
            errors.append(f"{label}: evidence {quote!r} is not a literal substring of the resume text")


def validate_grade(grade: GradeReport, job: JobContext, resume_text: str, version: str | None = None) -> list[str]:
    """Return a list of problems; empty means the grade is acceptable."""
    errors: list[str] = []
    if version and grade.resume_version != version:
        errors.append(f"resume_version is {grade.resume_version!r}, expected {version!r}")
    if job.source.raw_text_sha256 and grade.job_sha != job.source.raw_text_sha256:
        errors.append(f"job_sha {grade.job_sha!r} does not match job.json ({job.source.raw_text_sha256})")

    expected = [r.id for r in job.requirements]
    seen = [r.req_id for r in grade.requirement_results]
    missing = [i for i in expected if i not in seen]
    unknown = [i for i in seen if i not in expected]
    dupes = sorted({i for i in seen if seen.count(i) > 1})
    if missing:
        errors.append(f"requirements not graded: {', '.join(missing)}")
    if unknown:
        errors.append(f"unknown requirement ids: {', '.join(unknown)}")
    if dupes:
        errors.append(f"requirements graded more than once: {', '.join(dupes)}")

    haystack = normalize(resume_text)
    reqs = {r.id: r for r in job.requirements}
    for r in grade.requirement_results:
        req = reqs.get(r.req_id)
        if req is None:
            continue
        if not req.items:
            if r.item_results:
                errors.append(f"{r.req_id} has no items in job.json; drop item_results and set status")
            if r.status is None:
                errors.append(f"{r.req_id} needs a status (met, partial or missing)")
            else:
                _check_evidence(r.req_id, r.status, r.resume_evidence, haystack, errors)
            continue
        names = {_key(i.name): i.name for i in req.items}
        if r.status is not None or r.resume_evidence:
            errors.append(f"{r.req_id} is graded by item: leave out its status and resume_evidence and put "
                          "them in item_results")
        got = [_key(i.item) for i in r.item_results]
        absent = [names[k] for k in names if k not in got]
        extra = [i.item for i in r.item_results if _key(i.item) not in names]
        twice = sorted({i.item for i in r.item_results if got.count(_key(i.item)) > 1})
        if absent:
            errors.append(f"{r.req_id}: items not graded: {', '.join(absent)}")
        if extra:
            errors.append(f"{r.req_id}: unknown items {', '.join(extra)}; use exactly: {', '.join(names.values())}")
        if twice:
            errors.append(f"{r.req_id}: items graded more than once: {', '.join(twice)}")
        for i in r.item_results:
            _check_evidence(f"{r.req_id} item {i.item!r}", i.status, i.resume_evidence, haystack, errors)
    return errors


def main() -> int:
    if "--schema" in sys.argv:
        print(json.dumps(GradeReport.model_json_schema(), indent=2))
        return 0
    ap = argparse.ArgumentParser(description="Validate a GradeReport against its job and resume text.")
    ap.add_argument("grade_json", type=Path)
    ap.add_argument("--job", type=Path, required=True)
    ap.add_argument("--resume-text", type=Path, required=True)
    args = ap.parse_args()
    try:
        grade = GradeReport.model_validate_json(args.grade_json.read_text(encoding="utf-8"))
        job = JobContext.model_validate_json(args.job.read_text(encoding="utf-8"))
    except ValidationError as e:
        print(e)
        return 2
    errors = validate_grade(grade, job, args.resume_text.read_text(encoding="utf-8"))
    print(json.dumps({"valid": not errors, "errors": errors}, indent=2))
    return 0 if not errors else 2


if __name__ == "__main__":
    sys.exit(main())
