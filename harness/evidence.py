"""Shared lookups over the work history: term matching, achievement text, tenure.

Used by rank.py (what to put on the page), gaps.py (is a missing requirement a
wording problem or an experience problem) and grounding.py (did the builder stay
inside the record). Matching is whole-word and case-insensitive (see
job.count_occurrences), so a requirement term like "c" can't match "Clojure".
"""
from __future__ import annotations

from dataclasses import dataclass, field

from harness.job import count_occurrences
from harness.work_history import Achievement, WorkHistory, _months


# Terms that mean the same thing for matching purposes. Keep groups to true
# equivalents (a product and its maker, an abbreviation and its expansion), never
# merely related technologies: a match here counts as evidence.
EQUIVALENTS: list[set[str]] = [
    {"llm", "llms", "large language model", "large language models", "language model", "language models"},
    {"anthropic", "anthropic models", "claude", "claude code"},
    {"openai", "openai models", "gpt", "chatgpt"},
    {"gemini", "google gemini", "google models"},
    {"agentic", "agentic systems", "agentic workflows", "ai agents", "ai agent", "agents"},
    {"web development", "web applications", "web application", "web application development",
     "full-stack web development", "web apps"},
    {"new relic", "newrelic"},
    {"cloudwatch", "amazon cloudwatch", "aws cloudwatch"},
    {"datadog", "data dog"},
]
_EQUIV_INDEX = {t: g for g in EQUIVALENTS for t in g}


def equivalents(term: str) -> list[str]:
    """The term plus everything in its equivalence group (lowercased)."""
    t = " ".join(term.lower().split())
    return sorted(_EQUIV_INDEX.get(t, {t}) | {t})


@dataclass
class AchievementInfo:
    id: str
    holder_id: str
    holder_name: str
    achievement: Achievement
    skill_text: str  # skill names + aliases
    surface_text: str  # summary, tags, skill text: what a bullet would naturally say
    details_text: str
    source_text: str  # everything a bullet may draw facts from (for grounding)


@dataclass
class Index:
    wh: WorkHistory
    achievements: dict[str, AchievementInfo] = field(default_factory=dict)
    skill_terms: dict[str, list[str]] = field(default_factory=dict)  # sk id -> [name, *aliases]

    def matches(self, text: str, terms: list[str]) -> list[str]:
        """The terms that occur in text, directly or through an equivalent (see EQUIVALENTS)."""
        return [t for t in terms if count_occurrences(text, equivalents(t)) > 0]

    def skills_matching(self, terms: list[str]) -> dict[str, list[str]]:
        out = {}
        for sid, names in self.skill_terms.items():
            hit = self.matches(" | ".join(names), terms)
            if hit:
                out[sid] = hit
        return out


def build_index(wh: WorkHistory) -> Index:
    idx = Index(wh=wh)
    for s in wh.skills:
        idx.skill_terms[s.id] = [s.name, *s.aliases]
    holders = [(r.id, f"{r.title}, {r.company}", r.achievements) for r in wh.roles]
    holders += [(p.id, p.name, p.achievements) for p in wh.projects]
    for holder_id, holder_name, achs in holders:
        for a in achs:
            skill_text = " | ".join(n for sid in a.skills for n in idx.skill_terms.get(sid, []))
            tags = " | ".join(t.replace("_", " ") for t in a.tags)
            metrics = " | ".join(f"{m.value} {m.description}" for m in a.metrics if m.shareable)
            surface = " | ".join(x for x in [a.summary, tags, skill_text] if x)
            idx.achievements[a.id] = AchievementInfo(
                id=a.id, holder_id=holder_id, holder_name=holder_name, achievement=a,
                skill_text=skill_text, surface_text=surface, details_text=a.details or "",
                source_text=" | ".join(x for x in [a.summary, a.details or "", metrics, skill_text] if x))
    return idx


def allowed_clearance(wh: WorkHistory, requests_clearance: bool) -> str | None:
    """The only clearance text a resume may print for this job, or None."""
    for c in wh.clearances:
        if c.public_wording and (c.include_policy == "always" or
                                 (c.include_policy == "only_if_job_requests" and requests_clearance)):
            return c.public_wording
    return None


def professional_years(wh: WorkHistory) -> float:
    """Distinct months across all roles (overlaps counted once), in years."""
    months: set[tuple[int, int]] = set()
    for r in wh.roles:
        months |= _months(r.start, r.end)
    return len(months) / 12
