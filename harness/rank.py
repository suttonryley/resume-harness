"""Rank achievements and skills by relevance to a job, for the resume-builder.

An achievement earns each requirement's weight when one of the requirement's
normalized_terms appears in its summary, tags or skills, and half the weight when
the term only appears in its details. Shareable metrics add a small tiebreaker;
in-progress or proposed work takes a small penalty. The builder fills the page
from the top of this list.

    python -m harness.rank --job job.json [--work-history data/work_history.json] > ranking.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from harness.evidence import allowed_clearance, build_index, professional_years
from harness.job import JobContext
from harness.work_history import WorkHistory

DETAILS_FACTOR = 0.5
METRIC_BONUS = 0.05
IN_PROGRESS_FACTOR = 0.85


def rank(wh: WorkHistory, job: JobContext) -> dict:
    idx = build_index(wh)
    achievements = []
    for aid, info in idx.achievements.items():
        a = info.achievement
        score, matched = 0.0, []
        for req in job.requirements:
            if idx.matches(info.surface_text, req.normalized_terms):
                score += req.weight
                matched.append(req.id)
            elif idx.matches(info.details_text, req.normalized_terms):
                score += req.weight * DETAILS_FACTOR
                matched.append(req.id)
        if any(m.shareable for m in a.metrics):
            score += METRIC_BONUS
        if a.status != "complete":
            score *= IN_PROGRESS_FACTOR
        achievements.append({"id": aid, "holder_id": info.holder_id, "holder": info.holder_name,
                             "score": round(score, 3), "status": a.status, "matched_requirements": matched,
                             "summary": a.summary})
    achievements.sort(key=lambda x: -x["score"])

    skills = []
    for req in job.requirements:
        for sid in idx.skills_matching(req.normalized_terms):
            skills.append((sid, req.id, req.weight))
    by_skill: dict[str, dict] = {}
    for sid, rid, w in skills:
        entry = by_skill.setdefault(sid, {"id": sid, "name": idx.skill_terms[sid][0], "score": 0.0,
                                          "matched_requirements": []})
        entry["score"] = round(entry["score"] + w, 3)
        entry["matched_requirements"].append(rid)

    return {
        "job_sha": job.source.raw_text_sha256,
        "achievements": achievements,
        "relevant_skills": sorted(by_skill.values(), key=lambda x: -x["score"]),
        "clearance_line": allowed_clearance(wh, job.requests_clearance),
        "professional_years": round(professional_years(wh), 1),
        "never_claim": wh.constraints.never_claim,
        "max_pages": wh.constraints.max_pages,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Rank achievements by relevance to a job.")
    ap.add_argument("--job", type=Path, required=True)
    ap.add_argument("--work-history", type=Path, default=Path("data/work_history.json"))
    args = ap.parse_args()
    job = JobContext.model_validate_json(args.job.read_text(encoding="utf-8"))
    wh = WorkHistory.model_validate_json(args.work_history.read_text(encoding="utf-8"))
    print(json.dumps(rank(wh, job), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
