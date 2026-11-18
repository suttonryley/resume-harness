"""Compare several grades of the same resume text and job: how stable is the grader?

Reports the score spread and every requirement (or item) whose status differs
between grades. A score spread above ~0.5 means the rubric or prompt needs
tightening before the 9/10 threshold can be trusted (docs/DESIGN.md).

    python -m harness.stability runs/x/grade_v2.json runs/x/grade_v2.prev1.json ... \
        --job runs/x/job.json --resume-text runs/x/resume_v2.txt
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from harness.grade import GradeReport, effective, validate_grade
from harness.job import JobContext
from harness.score import score


def compare(grades: list[GradeReport], job: JobContext, resume_text: str) -> dict:
    scores = [score(g, job, resume_text).score for g in grades]
    eff = [effective(g, job) for g in grades]
    unstable = []
    for req in job.requirements:
        statuses = [e[req.id][0] for e in eff]
        row = {"req_id": req.id, "weight": req.weight, "type": req.type, "statuses": statuses,
               "values": [round(e[req.id][1], 3) for e in eff]}
        if req.items:
            per_item = {}
            for g in grades:
                res = next(r for r in g.requirement_results if r.req_id == req.id)
                for i in res.item_results:
                    per_item.setdefault(" ".join(i.item.split()).lower(), []).append(i.status)
            flips = {k: v for k, v in per_item.items() if len(set(v)) > 1}
            if flips:
                row["item_flips"] = flips
        if len(set(row["values"])) > 1 or "item_flips" in row:
            unstable.append(row)
    return {"samples": len(grades), "scores": scores, "spread": round(max(scores) - min(scores), 2),
            "requirements": len(job.requirements), "unstable": unstable,
            "seniority_fit": [g.seniority_fit for g in grades]}


def main() -> int:
    ap = argparse.ArgumentParser(description="Compare repeated grades of one resume against one job.")
    ap.add_argument("grades", type=Path, nargs="+")
    ap.add_argument("--job", type=Path, required=True)
    ap.add_argument("--resume-text", type=Path, required=True)
    args = ap.parse_args()
    job = JobContext.model_validate_json(args.job.read_text(encoding="utf-8"))
    text = args.resume_text.read_text(encoding="utf-8")
    grades = []
    for p in args.grades:
        g = GradeReport.model_validate_json(p.read_text(encoding="utf-8"))
        errors = validate_grade(g, job, text)
        if errors:
            print(f"{p}: invalid grade:\n  " + "\n  ".join(errors), file=sys.stderr)
            return 2
        grades.append(g)
    print(json.dumps(compare(grades, job, text), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
