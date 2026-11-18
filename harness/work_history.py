"""WorkHistory: the master record of everything the builder may draw on.

The builder can only select and rephrase what is in this file, so it is the
ceiling on every resume the harness produces. Achievements are the atomic
unit; skills point at the achievements that prove them.

ID conventions (enforced): role_<slug>, proj_<slug>, ach_<slug>_<nn>,
sk_<slug>, edu_<slug>, cert_<slug>, sum_<slug>.

    python -m harness.work_history data/work_history.json   # validate + audit
    python -m harness.work_history --schema                 # print JSON Schema
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from harness.models import YEAR_MONTH, Contact

ID_PATTERNS = {
    "role": re.compile(r"^role_[a-z0-9_]+$"),
    "proj": re.compile(r"^proj_[a-z0-9_]+$"),
    "ach": re.compile(r"^ach_[a-z0-9_]+_\d{2}$"),
    "sk": re.compile(r"^sk_[a-z0-9_]+$"),
    "edu": re.compile(r"^edu_[a-z0-9_]+$"),
    "cert": re.compile(r"^cert_[a-z0-9_]+$"),
    "sum": re.compile(r"^sum_[a-z0-9_]+$"),
    "clr": re.compile(r"^clr_[a-z0-9_]+$"),
}


def _check_id(kind: str, value: str) -> str:
    if not ID_PATTERNS[kind].match(value):
        raise ValueError(f"id {value!r} does not match {ID_PATTERNS[kind].pattern}")
    return value


def _check_ym(value: str, allow_present: bool = False) -> str:
    if allow_present and value == "present":
        return value
    if not YEAR_MONTH.match(value):
        raise ValueError(f"{value!r} must be YYYY-MM" + (" or 'present'" if allow_present else ""))
    return value


class Metric(BaseModel):
    value: str = Field(description="As it would be written: '15M+', '3,000+', '31%'")
    description: str = Field(description="What the number measures")
    approximate: bool = Field(False, description="True if estimated rather than measured")
    shareable: bool = Field(True, description="False if confidential; builder must not print it")


class Achievement(BaseModel):
    id: str
    summary: str = Field(description="One canonical sentence; the default bullet wording")
    details: str | None = Field(
        None, description="Free-form context: the problem, what you did, how, scale, outcome. "
        "Longer than any bullet; the builder mines this for tailored phrasings.")
    metrics: list[Metric] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list, description="sk_ ids used or demonstrated")
    tags: list[str] = Field(default_factory=list, description="Themes: leadership, migration, payments...")
    strength: int | None = Field(
        None, ge=1, le=5, description="Optional manual override. By default the builder ranks achievements "
        "by relevance to the target job, not by this rating")
    private_terms: list[str] = Field(
        default_factory=list, description="Words or phrases from details that must never appear in a resume; "
        "grounding checks scan output for them")
    no_metrics: bool = Field(False, description="True when the impact genuinely can't be measured; "
                             "the audit stops asking for numbers")
    status: Literal["complete", "in_progress", "proposed"] = Field(
        "complete", description="Builder must not describe in_progress or proposed work as finished")

    _id = field_validator("id")(lambda cls, v: _check_id("ach", v))


class Role(BaseModel):
    id: str
    company: str
    company_aliases: list[str] = Field(default_factory=list, description="e.g. 'Northwind' for 'Northwind Logistics'")
    title: str
    alt_titles: list[str] = Field(default_factory=list, description="Truthful alternates only")
    employment_type: Literal["full_time", "part_time", "contract", "internship"] = "full_time"
    location: str | None = None
    start: str
    end: str
    context: str | None = Field(None, description="Team, product, scale. For the builder, not printed")
    achievements: list[Achievement] = Field(default_factory=list)

    _id = field_validator("id")(lambda cls, v: _check_id("role", v))
    _start = field_validator("start")(lambda cls, v: _check_ym(v))
    _end = field_validator("end")(lambda cls, v: _check_ym(v, allow_present=True))


class Project(BaseModel):
    id: str
    name: str
    kind: Literal["open_source", "personal", "academic", "hackathon", "other"] = "other"
    url: str | None = None
    start: str | None = None
    end: str | None = None
    context: str | None = None
    achievements: list[Achievement] = Field(default_factory=list)

    _id = field_validator("id")(lambda cls, v: _check_id("proj", v))
    _start = field_validator("start")(lambda cls, v: v if v is None else _check_ym(v))
    _end = field_validator("end")(lambda cls, v: v if v is None else _check_ym(v, allow_present=True))


class Skill(BaseModel):
    id: str
    name: str
    aliases: list[str] = Field(default_factory=list)
    category: Literal["language", "framework", "library", "database", "cloud", "tool",
                      "platform", "practice", "domain", "soft"]
    proficiency: Literal["familiar", "working", "strong", "expert"] | None = None
    years: float | None = None
    last_used: str | None = Field(None, description="YYYY or 'present'")
    years_basis: Literal["derived", "self_reported"] | None = Field(
        None, description="derived = total time in the roles/projects that evidence this skill (an upper "
        "bound on hands-on time); self_reported = entered by hand and never overwritten")
    evidence_ids: list[str] = Field(default_factory=list,
                                    description="Achievement ids that demonstrate this skill")

    _id = field_validator("id")(lambda cls, v: _check_id("sk", v))


class SummaryVariant(BaseModel):
    id: str
    angle: str = Field(description="e.g. 'backend', 'platform', 'AI tooling'")
    text: str

    _id = field_validator("id")(lambda cls, v: _check_id("sum", v))


class Profile(BaseModel):
    name: str
    headlines: list[str] = Field(default_factory=list, description="Title lines under the name")
    contact: Contact
    summaries: list[SummaryVariant] = Field(default_factory=list)


class Education(BaseModel):
    id: str
    institution: str
    degree: str
    field: str | None = None
    start: str | None = None
    end: str | None = None
    details: list[str] = Field(default_factory=list, description="Honors, GPA, coursework")

    _id = field_validator("id")(lambda cls, v: _check_id("edu", v))


class Certification(BaseModel):
    id: str
    name: str
    issuer: str | None = None
    date: str | None = None
    expires: str | None = None

    _id = field_validator("id")(lambda cls, v: _check_id("cert", v))


class Clearance(BaseModel):
    id: str
    level: str = Field(description="Internal record, e.g. 'TS/SCI'")
    polygraph: str | None = Field(None, description="e.g. 'polygraph', 'CI polygraph', 'full-scope polygraph'")
    status: Literal["active", "current", "inactive", "expired"]
    held_through: str | None = Field(None, description="YYYY-MM the clearance was last active")
    public_wording: str | None = Field(
        None, description="Exact text approved for a resume. The builder may only print this string, "
        "and only when include_policy allows. None means it can never be printed.")
    include_policy: Literal["only_if_job_requests", "always", "never"] = "only_if_job_requests"

    _id = field_validator("id")(lambda cls, v: _check_id("clr", v))


class Constraints(BaseModel):
    never_claim: list[str] = Field(default_factory=list,
                                   description="Skills, titles or phrases never to put on a resume")
    always_include: list[str] = Field(default_factory=list, description="Ids that must appear")
    max_pages: int = 2
    preferred_section_order: list[str] = Field(
        default_factory=lambda: ["summary", "experience", "projects", "skills", "education", "certifications"])


class WorkHistory(BaseModel):
    schema_version: Literal["1.0"] = "1.0"
    profile: Profile
    roles: list[Role] = Field(default_factory=list)
    projects: list[Project] = Field(default_factory=list)
    skills: list[Skill] = Field(default_factory=list)
    education: list[Education] = Field(default_factory=list)
    certifications: list[Certification] = Field(default_factory=list)
    clearances: list[Clearance] = Field(default_factory=list)
    constraints: Constraints = Field(default_factory=Constraints)

    def achievements(self) -> dict[str, Achievement]:
        out = {}
        for holder in [*self.roles, *self.projects]:
            for a in holder.achievements:
                out[a.id] = a
        return out

    @model_validator(mode="after")
    def _cross_refs(self) -> WorkHistory:
        ids: list[str] = [r.id for r in self.roles] + [p.id for p in self.projects]
        ids += [s.id for s in self.skills] + [e.id for e in self.education]
        ids += [c.id for c in self.certifications] + [s.id for s in self.profile.summaries]
        ids += [c.id for c in self.clearances]
        ach = [a.id for h in [*self.roles, *self.projects] for a in h.achievements]
        dupes = {i for i in ids + ach if (ids + ach).count(i) > 1}
        if dupes:
            raise ValueError(f"duplicate ids: {sorted(dupes)}")

        skill_ids, ach_ids = {s.id for s in self.skills}, set(ach)
        errors = []
        for h in [*self.roles, *self.projects]:
            for a in h.achievements:
                errors += [f"{a.id} references unknown skill {s}" for s in a.skills if s not in skill_ids]
        for s in self.skills:
            errors += [f"{s.id} evidence {e} is not an achievement" for e in s.evidence_ids if e not in ach_ids]
        all_ids = set(ids + ach)
        errors += [f"always_include {i} is unknown" for i in self.constraints.always_include if i not in all_ids]
        if errors:
            raise ValueError("; ".join(errors))
        return self

    def sync_evidence(self) -> None:
        """Rebuild skill.evidence_ids from what achievements declare.

        Achievements are the source of truth, so removing a skill from an
        achievement also removes that achievement from the skill's evidence.
        """
        by_skill: dict[str, set[str]] = {s.id: set() for s in self.skills}
        for a_id, a in self.achievements().items():
            for s in a.skills:
                by_skill[s].add(a_id)
        for s in self.skills:
            s.evidence_ids = sorted(by_skill[s.id])


def _months(start: str, end: str) -> set[tuple[int, int]]:
    """Every (year, month) in [start, end]; 'present' means the current month."""
    from datetime import date
    today = date.today()
    y, m = map(int, start.split("-"))
    ey, em = (today.year, today.month) if end == "present" else map(int, end.split("-"))
    out = set()
    while (y, m) <= (ey, em):
        out.add((y, m)); m += 1
        if m > 12: y, m = y + 1, 1
    return out


def derive_skill_stats(wh: WorkHistory) -> None:
    """Fill years and last_used for skills from the dates of their evidence.

    Overlapping roles are counted once. Skills marked self_reported are left alone.
    """
    holder_of = {a.id: h for h in [*wh.roles, *wh.projects] for a in h.achievements}
    for skill in wh.skills:
        if skill.years_basis == "self_reported":
            continue
        months: set[tuple[int, int]] = set()
        ends = []
        for a_id in skill.evidence_ids:
            h = holder_of[a_id]
            if h.start and h.end:
                months |= _months(h.start, h.end)
                ends.append(h.end)
        if not months:
            continue
        skill.years = round(len(months) / 12, 1)
        skill.last_used = "present" if "present" in ends else max(ends)[:4]
        skill.years_basis = "derived"


def audit(wh: WorkHistory) -> dict[str, list[str]]:
    """What is still missing. This list drives the interview."""
    gaps: dict[str, list[str]] = {
        "profile": [], "roles": [], "achievements": [], "skills": [], "education_certs": [], "clearances": []}
    if not wh.profile.summaries:
        gaps["profile"].append("no summary variants")
    for r in wh.roles:
        if not r.location:
            gaps["roles"].append(f"{r.id}: no location")
        if not r.context:
            gaps["roles"].append(f"{r.id}: no context (team, product, scale)")
    for a_id, a in wh.achievements().items():
        missing = [n for n, ok in [("details", a.details), ("metrics", a.metrics or a.no_metrics),
                                   ("skills", a.skills)] if not ok]
        if missing:
            gaps["achievements"].append(f"{a_id}: {', '.join(missing)}")
    for s in wh.skills:
        missing = [n for n, ok in [("years", s.years), ("last_used", s.last_used),
                                   ("evidence", s.evidence_ids)] if not ok]
        if missing:
            gaps["skills"].append(f"{s.id}: {', '.join(missing)}")
    for c in wh.clearances:
        if c.include_policy != "never" and not c.public_wording:
            gaps.setdefault("clearances", []).append(f"{c.id}: no approved public_wording (can't be printed)")
    if not wh.education:
        gaps["education_certs"].append("no education entries")
    return {k: v for k, v in gaps.items() if v}


def main() -> int:
    if "--schema" in sys.argv:
        print(json.dumps(WorkHistory.model_json_schema(), indent=2))
        return 0
    path = Path(sys.argv[1])
    try:
        wh = WorkHistory.model_validate_json(path.read_text())
    except ValidationError as e:
        print(e)
        return 2
    gaps = audit(wh)
    print(json.dumps({"valid": True, "roles": len(wh.roles),
                      "achievements": len(wh.achievements()), "skills": len(wh.skills),
                      "gap_count": sum(map(len, gaps.values())), "gaps": gaps}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
