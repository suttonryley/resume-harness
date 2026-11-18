"""JobContext: a job posting parsed into structured requirements.

Written by the job-parser subagent from runs/<dir>/job_raw.txt; read by the grader,
score.py, gaps.py and rank.py. The parser only extracts. Validation here catches
the mistakes an LLM makes (bad ids, dangling references, inconsistent flags) so the
orchestrator can retry the parse with the error text.

Anything exact is filled by code, not the parser: `finalize()` computes
raw_text_sha256 and keyword counts from job_raw.txt. The parser leaves both out.

ID conventions (enforced): req_<nn>, resp_<nn>.

    python -m harness.job runs/x/job.json --raw runs/x/job_raw.txt   # finalize, validate, write back
    python -m harness.job runs/x/job.json                            # validate only
    python -m harness.job --schema                                   # print JSON Schema
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

REQ_ID = re.compile(r"^req_\d{2,}$")
RESP_ID = re.compile(r"^resp_\d{2,}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")

Seniority = Literal["intern", "junior", "mid", "senior", "staff", "principal", "manager", "director", "exec"]
RequirementCategory = Literal["hard_skill", "tool", "experience", "domain", "education", "certification",
                              "soft_skill", "clearance", "other"]


class _Strict(BaseModel):
    """Parser output must not carry fields the schema doesn't know (catches typos)."""
    model_config = ConfigDict(extra="forbid")


def _terms(values: list[str]) -> list[str]:
    """Lowercase, trim and de-duplicate, keeping first-seen order."""
    out: list[str] = []
    for v in values:
        t = " ".join(v.split()).lower()
        if t and t not in out:
            out.append(t)
    return out


class Source(_Strict):
    url: str | None = Field(description="Posting URL; null when the user pasted the text (fetch fallback)")
    fetched_at: datetime = Field(description="When the text was fetched or pasted")
    raw_text_sha256: str | None = Field(
        None, description="sha256 of job_raw.txt; GradeReport.job_sha points here. Filled by finalize(); "
        "the parser leaves it out")

    @field_validator("raw_text_sha256")
    @classmethod
    def _sha(cls, v: str | None) -> str | None:
        if v is None:
            return v
        v = v.lower()
        if not SHA256.match(v):
            raise ValueError("raw_text_sha256 must be 64 hex characters")
        return v


class Location(_Strict):
    cities: list[str] = Field(default_factory=list)
    remote: Literal["onsite", "hybrid", "remote", "unspecified"] = Field(
        "unspecified", description="'unspecified' when the posting doesn't say; never guess")
    notes: str | None = None


class Compensation(_Strict):
    min: float | None = Field(None, ge=0)
    max: float | None = Field(None, ge=0)
    currency: str | None = Field(None, description="ISO 4217, e.g. USD")
    period: Literal["year", "hour"] | None = None

    @model_validator(mode="after")
    def _range(self) -> Compensation:
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError(f"compensation min {self.min} is greater than max {self.max}")
        if (self.min is not None or self.max is not None) and self.period is None:
            raise ValueError("compensation has an amount but no period")
        return self


class JobRole(_Strict):
    title: str = Field(description="As written in the posting")
    normalized_title: str = Field(description="Plain title without level or team, e.g. 'software engineer'")
    seniority: Seniority
    department: str | None = None
    employment_type: Literal["full_time", "part_time", "contract", "internship"] = "full_time"
    location: Location
    compensation: Compensation = Field(default_factory=Compensation)


class Company(_Strict):
    name: str
    industry: str | None = None
    stage_or_size: str | None = None
    mission_summary: str | None = None
    values: list[str] = Field(default_factory=list)


MatchRule = Literal["all", "any", "most"]
MOST_MIN_ITEMS = 4


class RequirementItem(_Strict):
    """One listed thing inside a requirement: a language, a framework, a half of 'web and mobile'."""
    name: str = Field(description="As written in the posting, e.g. 'Dart', 'mobile', 'NoSQL'")
    terms: list[str] = Field(default_factory=list, description="Lowercase match terms for this item, incl. "
                             "spellings and abbreviations. Defaults to the lowercased name")

    _norm = field_validator("terms")(lambda cls, v: _terms(v))

    @model_validator(mode="after")
    def _default_terms(self) -> RequirementItem:
        if not self.terms:
            self.terms = _terms([self.name])
        return self


class Requirement(_Strict):
    id: str = Field(description="req_01, req_02, ...")
    text: str = Field(description="Near-verbatim from the posting")
    type: Literal["required", "preferred"]
    category: RequirementCategory
    normalized_terms: list[str] = Field(
        min_length=1, description="Lowercase match terms incl. common synonyms, e.g. ['kubernetes', 'k8s']. "
        "gaps.py and rank.py match work history against these")
    years_min: float | None = Field(None, ge=0)
    weight: float = Field(gt=0, le=1, description="Required outranks preferred; repeated or emphasized "
                          "requirements outrank single mentions")
    items: list[RequirementItem] = Field(
        default_factory=list, description="The separately checkable things a requirement lists (languages, "
        "frameworks, both halves of 'web and mobile'). The grader judges each; code combines them by `match`. "
        "Empty for a single skill or quality")
    match: MatchRule | None = Field(
        None, description="How items combine. all: every item is needed (score = share shown). any: one is "
        "enough ('AWS or GCP', 'such as'). most: a long stack list where a strong candidate shows about half "
        f"(score = shown / half, capped at 1; needs {MOST_MIN_ITEMS}+ items). Required when items is set")

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        if not REQ_ID.match(v):
            raise ValueError(f"requirement id {v!r} must look like req_01")
        return v

    _norm = field_validator("normalized_terms")(lambda cls, v: _terms(v))

    @model_validator(mode="after")
    def _items(self) -> Requirement:
        if not self.items:
            if self.match is not None:
                raise ValueError(f"{self.id}: match is set but there are no items")
            return self
        names = [" ".join(i.name.split()).lower() for i in self.items]
        dupes = sorted({n for n in names if names.count(n) > 1})
        if dupes:
            raise ValueError(f"{self.id}: duplicate item names: {', '.join(dupes)}")
        if len(self.items) < 2:
            raise ValueError(f"{self.id}: a single item is just the requirement; leave items empty")
        if self.match is None:
            raise ValueError(f"{self.id}: items are set but match is not (all, any or most)")
        if self.match == "most" and len(self.items) < MOST_MIN_ITEMS:
            raise ValueError(f"{self.id}: match 'most' needs {MOST_MIN_ITEMS}+ items; use all or any")
        # Item terms are match terms for the whole requirement too (rank.py and gaps.py use these).
        self.normalized_terms = _terms(self.normalized_terms + [t for i in self.items for t in i.terms])
        return self


class Responsibility(_Strict):
    id: str = Field(description="resp_01, resp_02, ...")
    text: str
    related_requirement_ids: list[str] = Field(default_factory=list)

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        if not RESP_ID.match(v):
            raise ValueError(f"responsibility id {v!r} must look like resp_01")
        return v


class Keyword(_Strict):
    term: str
    count: int | None = Field(
        None, ge=1, description="Occurrences of the term or any synonym in the posting. Filled by finalize(); "
        "the parser leaves it out")
    synonyms: list[str] = Field(default_factory=list)


class JobContext(_Strict):
    schema_version: Literal["1.0"] = "1.0"
    source: Source
    role: JobRole
    company: Company
    requirements: list[Requirement] = Field(min_length=1)
    responsibilities: list[Responsibility] = Field(default_factory=list)
    keywords: list[Keyword] = Field(default_factory=list)
    requests_clearance: bool = Field(
        description="True iff some requirement has category 'clearance'. Filled from the requirements "
        "when omitted; gates whether the resume may print the clearance line")
    parse_notes: list[str] = Field(default_factory=list, description="Ambiguities the parser flagged")

    @model_validator(mode="before")
    @classmethod
    def _fill_clearance(cls, data: object) -> object:
        if isinstance(data, dict) and "requests_clearance" not in data:
            reqs = data.get("requirements") or []
            data = {**data, "requests_clearance": any(
                isinstance(r, dict) and r.get("category") == "clearance" for r in reqs)}
        return data

    @model_validator(mode="after")
    def _consistency(self) -> JobContext:
        errors: list[str] = []

        req_ids = [r.id for r in self.requirements]
        resp_ids = [r.id for r in self.responsibilities]
        for kind, ids in (("requirement", req_ids), ("responsibility", resp_ids)):
            dupes = sorted({i for i in ids if ids.count(i) > 1})
            if dupes:
                errors.append(f"duplicate {kind} ids: {', '.join(dupes)}")

        known = set(req_ids)
        for resp in self.responsibilities:
            for rid in resp.related_requirement_ids:
                if rid not in known:
                    errors.append(f"{resp.id} references unknown requirement {rid}")

        has_clearance = any(r.category == "clearance" for r in self.requirements)
        if self.requests_clearance != has_clearance:
            errors.append(f"requests_clearance is {self.requests_clearance} but "
                          f"{'a' if has_clearance else 'no'} requirement has category 'clearance'")

        required = [r.weight for r in self.requirements if r.type == "required"]
        preferred = [r.weight for r in self.requirements if r.type == "preferred"]
        if required and preferred and min(required) <= max(preferred):
            errors.append(f"every required weight must exceed every preferred weight "
                          f"(lowest required {min(required)}, highest preferred {max(preferred)})")

        terms = [k.term.lower() for k in self.keywords]
        dupes = sorted({t for t in terms if terms.count(t) > 1})
        if dupes:
            errors.append(f"duplicate keywords: {', '.join(dupes)}")

        if errors:
            raise ValueError("; ".join(errors))
        return self


def count_occurrences(text: str, variants: list[str]) -> int:
    """Whole-word, case-insensitive matches of any variant (plural -s/-es allowed).

    Variants are tried longest first in one alternation, so overlapping variants
    ('API' and 'API design') count once per occurrence, not once each.
    """
    alts = sorted({" ".join(v.split()).lower() for v in variants if v.strip()}, key=len, reverse=True)
    if not alts:
        return 0
    body = "|".join(r"\s+".join(re.escape(w) for w in a.split(" ")) for a in alts)
    pattern = rf"(?<![a-z0-9])(?:{body})(?:e?s)?(?![a-z0-9])"
    return len(re.findall(pattern, text.lower()))


def finalize(job: JobContext, raw_text: str) -> JobContext:
    """Fill the exact fields from the raw posting text: hash and keyword counts.

    Raises ValueError if the parsed file names a different text (hash mismatch) or
    lists a keyword that never appears in the posting (a parser invention).
    """
    sha = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()
    if job.source.raw_text_sha256 not in (None, sha):
        raise ValueError(f"raw_text_sha256 {job.source.raw_text_sha256} does not match the raw text ({sha})")
    data = job.model_dump(mode="json")
    data["source"]["raw_text_sha256"] = sha
    missing = []
    for kw in data["keywords"]:
        n = count_occurrences(raw_text, [kw["term"], *kw["synonyms"]])
        if n == 0:
            missing.append(kw["term"])
        kw["count"] = n or None
    if missing:
        raise ValueError(f"keywords not found in the posting text: {', '.join(missing)}")
    return JobContext.model_validate(data)


def main() -> int:
    if "--schema" in sys.argv:
        print(json.dumps(JobContext.model_json_schema(), indent=2))
        return 0
    ap = argparse.ArgumentParser(description="Validate (and with --raw, finalize) a parsed job posting.")
    ap.add_argument("job_json", type=Path)
    ap.add_argument("--raw", type=Path, help="job_raw.txt: fill hash and keyword counts, write job_json back")
    args = ap.parse_args()
    try:
        job = JobContext.model_validate_json(args.job_json.read_text(encoding="utf-8"))
        if args.raw:
            job = finalize(job, args.raw.read_text(encoding="utf-8"))
            args.job_json.write_text(job.model_dump_json(indent=2) + "\n", encoding="utf-8")
    except (ValidationError, ValueError) as e:
        print(e)
        return 2
    unfilled = job.source.raw_text_sha256 is None or any(k.count is None for k in job.keywords)
    by_type = {t: sum(r.type == t for r in job.requirements) for t in ("required", "preferred")}
    print(json.dumps({"valid": True, "title": job.role.title, "company": job.company.name,
                      "seniority": job.role.seniority, "requirements": by_type,
                      "responsibilities": len(job.responsibilities), "keywords": len(job.keywords),
                      "requests_clearance": job.requests_clearance,
                      "finalized": not unfilled,
                      "parse_notes": job.parse_notes}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
