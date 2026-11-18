"""Score a validated grade and decide what the run does next.

The grader only labels requirements; every number here is computed. Thresholds
and caps live in this module so prompts never carry them.

    score = 1 + 9 * weighted mean of the components present:
      required   weight-averaged credit of required requirements (met 1, partial 0.5, missing 0;
                 item-graded requirements combine their items by match rule, see grade.requirement_value)
      preferred  same for preferred requirements
      keywords   share of posting keyword occurrences that the resume text contains
      seniority  grader's seniority_fit: match 1, above 0.8, below 0.4

    python -m harness.score grade.json --job job.json --resume-text resume_v0.txt [--rebuilds-done 0]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from harness.grade import GradeReport, effective
from harness.job import JobContext, count_occurrences

THRESHOLD = 9.0
MAX_REBUILDS = 2

SENIORITY_VALUE = {"match": 1.0, "above": 0.8, "below": 0.4}
COMPONENT_WEIGHTS = {"required": 0.65, "preferred": 0.15, "keywords": 0.10, "seniority": 0.10}

Decision = Literal["done", "rebuild", "report_gaps"]


class KeywordHit(BaseModel):
    term: str
    count_in_posting: int
    found: bool


class ScoreResult(BaseModel):
    resume_version: str
    score: float
    subscores: dict[str, float | None]
    keyword_hits: list[KeywordHit]
    threshold: float = THRESHOLD


def keyword_hits(job: JobContext, resume_text: str) -> list[KeywordHit]:
    return [KeywordHit(term=k.term, count_in_posting=k.count or 1,
                       found=count_occurrences(resume_text, [k.term, *k.synonyms]) > 0)
            for k in job.keywords]


def _weighted(job: JobContext, grade: GradeReport, kind: str) -> float | None:
    value = {rid: v for rid, (_, v) in effective(grade, job).items()}
    reqs = [r for r in job.requirements if r.type == kind]
    total = sum(r.weight for r in reqs)
    if not total:
        return None
    return sum(r.weight * value[r.id] for r in reqs) / total


def score(grade: GradeReport, job: JobContext, resume_text: str) -> ScoreResult:
    hits = keyword_hits(job, resume_text)
    kw_total = sum(h.count_in_posting for h in hits)
    subs: dict[str, float | None] = {
        "required": _weighted(job, grade, "required"),
        "preferred": _weighted(job, grade, "preferred"),
        "keywords": (sum(h.count_in_posting for h in hits if h.found) / kw_total) if kw_total else None,
        "seniority": SENIORITY_VALUE[grade.seniority_fit],
    }
    present = {k: v for k, v in subs.items() if v is not None}
    weight = sum(COMPONENT_WEIGHTS[k] for k in present)
    raw = sum(COMPONENT_WEIGHTS[k] * v for k, v in present.items()) / weight
    return ScoreResult(resume_version=grade.resume_version, score=round(1 + 9 * raw, 2),
                       subscores={k: (round(v, 3) if v is not None else None) for k, v in subs.items()},
                       keyword_hits=hits)


def decide(result: ScoreResult, rebuilds_done: int) -> Decision:
    if result.score >= THRESHOLD:
        return "done"
    if rebuilds_done < MAX_REBUILDS:
        return "rebuild"
    return "report_gaps"


def main() -> int:
    ap = argparse.ArgumentParser(description="Score a grade and print the decision.")
    ap.add_argument("grade_json", type=Path)
    ap.add_argument("--job", type=Path, required=True)
    ap.add_argument("--resume-text", type=Path, required=True)
    ap.add_argument("--rebuilds-done", type=int, default=0)
    args = ap.parse_args()
    grade = GradeReport.model_validate_json(args.grade_json.read_text(encoding="utf-8"))
    job = JobContext.model_validate_json(args.job.read_text(encoding="utf-8"))
    result = score(grade, job, args.resume_text.read_text(encoding="utf-8"))
    print(json.dumps({**result.model_dump(), "decision": decide(result, args.rebuilds_done)}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
