"""Schemas for the resume harness.

Pydantic models are the single source of truth. Export JSON Schema with:
    python -m harness.models > schemas/resume.schema.json

This module holds the Resume model. WorkHistory lives in harness/work_history.py,
JobContext in harness/job.py and GradeReport in harness/grade.py.
"""
from __future__ import annotations

import json
import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator

YEAR_MONTH = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
SectionName = Literal["summary", "experience", "projects", "skills", "education", "certifications"]


class Link(BaseModel):
    label: str
    url: str


class Contact(BaseModel):
    email: str
    phone: str | None = None
    location: str | None = None
    links: list[Link] = Field(default_factory=list)


class Header(BaseModel):
    name: str
    headline: str | None = Field(None, description="One line under the name, e.g. the target title.")
    contact: Contact


class Bullet(BaseModel):
    text: str
    source_ids: list[str] = Field(
        default_factory=list,
        description="WorkHistory ids this bullet is derived from. Required for built resumes; "
        "empty only for a parsed existing resume (v0).",
    )


class Summary(BaseModel):
    text: str
    source_ids: list[str] = Field(default_factory=list)


class Role(BaseModel):
    source_role_id: str | None = None
    company: str
    title: str
    location: str | None = None
    start: str = Field(description="YYYY-MM")
    end: str = Field(description="YYYY-MM or 'present'")
    bullets: list[Bullet] = Field(default_factory=list)

    @field_validator("start")
    @classmethod
    def _start(cls, v: str) -> str:
        if not YEAR_MONTH.match(v):
            raise ValueError("start must be YYYY-MM")
        return v

    @field_validator("end")
    @classmethod
    def _end(cls, v: str) -> str:
        if v != "present" and not YEAR_MONTH.match(v):
            raise ValueError("end must be YYYY-MM or 'present'")
        return v


class Project(BaseModel):
    source_project_id: str | None = None
    name: str
    url: str | None = None
    dates: str | None = None
    bullets: list[Bullet] = Field(default_factory=list)


class SkillGroup(BaseModel):
    group: str = Field(description="e.g. 'Languages', 'Cloud & infrastructure'")
    items: list[str]


class Education(BaseModel):
    institution: str
    degree: str
    field: str | None = None
    end: str | None = Field(None, description="YYYY or YYYY-MM")
    details: list[str] = Field(default_factory=list)


class Certification(BaseModel):
    name: str
    issuer: str | None = None
    date: str | None = None


class Target(BaseModel):
    job_sha: str
    job_title: str
    company: str


class Resume(BaseModel):
    schema_version: Literal["1.0"] = "1.0"
    target: Target | None = None
    header: Header
    summary: Summary | None = None
    experience: list[Role] = Field(default_factory=list)
    projects: list[Project] = Field(default_factory=list)
    skills: list[SkillGroup] = Field(default_factory=list)
    education: list[Education] = Field(default_factory=list)
    certifications: list[Certification] = Field(default_factory=list)
    clearance: str | None = Field(
        None, description="Exactly a work-history clearances[].public_wording, and only when the job requests a "
        "clearance; grounding.py rejects anything else. Rendered as its own section after the others")
    section_order: list[SectionName] = Field(
        default_factory=lambda: ["summary", "experience", "projects", "skills", "education", "certifications"]
    )


if __name__ == "__main__":
    print(json.dumps(Resume.model_json_schema(), indent=2))
