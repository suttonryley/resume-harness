# Resume harness: design

This document records the decisions made during the discovery phase and the parts
still to build. `CLAUDE.md` holds the short version and the rules.

## Pipeline

```
/resume-run <url> [resume-file]
  │
  ├─ fetch_job.py ............ URL → runs/<dir>/job_raw.txt + job_meta.json      (Python)
  │     blocked (login wall, captcha)? → ask the user to paste the posting
  ├─ job-parser .............. job_raw.txt → job.json (JobContext)               (subagent)
  │     validate; on failure retry ≤ 2 with the validation error
  ├─ render.py ............... v0 resume → resume_v0.pdf + resume_v0.txt         (Python)
  ├─ resume-grader ........... resume_v0.txt + job.json → grade_v0.json           (subagent)
  │     validate: every resume_evidence string must occur in resume_v0.txt
  ├─ score.py ................ grade → {score, decision}                         (Python)
  │     decision: done (≥ 9) | rebuild (< 9, iterations left) | report_gaps
  ├─ gaps.py ................. split missing reqs into presentation / experience  (Python)
  │
  ├─ if rebuild:
  │   ├─ rank.py ............. achievements ranked by relevance to job.json       (Python)
  │   ├─ resume-builder ...... work_history + job + ranking + prior gaps → resume_vN.json
  │   ├─ grounding.py ........ source_ids exist, no never_claim, no private_terms,
  │   │                        no in-progress work worded as done                 (Python)
  │   ├─ render.py ........... page fit; overflow → back to builder (≤ 2 tries)
  │   └─ grade → score → gaps (fresh grader context each time)
  │
  └─ report.md ............... score history, what's missing, gap split
```

Iteration cap: 2 rebuilds before falling through to the gap report.

## Components

| Piece | Kind | Tools | Notes |
|---|---|---|---|
| `/resume-run` | skill | Bash, Read, Write, Task | Thin loop; follows `score.py` decisions |
| `/job-ingest` | skill | same | Fetch + parse only, for debugging |
| `/resume-grade <run-dir>` | skill | same | Re-grade without rebuilding |
| `job-parser` | subagent | Read, Write | Extraction only, no judgment |
| `resume-grader` | subagent | Read, Write | Must not read `data/` |
| `resume-builder` | subagent | Read, Write | The only LLM step that reads work history |
| `fetch_job.py` | Python | | Playwright + text extraction; distinct exit code when blocked |
| `score.py`, `gaps.py`, `rank.py`, `grounding.py` | Python | | Deterministic |
| `render.py` | Python | | Resume JSON → PDF + extracted text + page fit |
| `run.py` | Python | | Orchestrator state machine; owns every cap and branch |

Subagent tool lists restrict which tools, not which files. Grader independence is
therefore enforced by the evidence-substring check, not by permissions.

## Schemas

All implemented as Pydantic models and exported to `schemas/`. The sketches below are the
original design; the notes under each heading say where the implementation differs.

### JobContext

Implemented in `harness/job.py`; `schemas/job.schema.json` is generated from it.
Additions over this sketch: unknown fields are rejected, `normalized_terms` is
lowercased and needs at least one term, every required weight must exceed every
preferred weight, and `requests_clearance` is derived when omitted. `source.url` is null
for pasted postings, and `location.remote` has an `unspecified` value (the default) so
the parser never guesses. `raw_text_sha256` and keyword `count`s are left out by the
parser and filled by `finalize()` from `job_raw.txt`, which also rejects any keyword
that doesn't occur in the posting (whole-word, case-insensitive, plurals allowed).

```json
{
  "schema_version": "1.0",
  "source": {"url": "string", "fetched_at": "ISO-8601", "raw_text_sha256": "string"},
  "role": {
    "title": "string", "normalized_title": "string",
    "seniority": "intern|junior|mid|senior|staff|principal|manager|director|exec",
    "department": "string|null",
    "employment_type": "full_time|part_time|contract|internship",
    "location": {"cities": ["string"], "remote": "onsite|hybrid|remote", "notes": "string|null"},
    "compensation": {"min": "number|null", "max": "number|null", "currency": "string|null", "period": "year|hour|null"}
  },
  "company": {"name": "string", "industry": "string|null", "stage_or_size": "string|null",
              "mission_summary": "string|null", "values": ["string"]},
  "requirements": [{
    "id": "req_01",
    "text": "near-verbatim from the posting",
    "type": "required|preferred",
    "category": "hard_skill|tool|experience|domain|education|certification|soft_skill|clearance|other",
    "normalized_terms": ["kubernetes", "k8s"],
    "years_min": "number|null",
    "weight": "0-1; required > preferred; repeated or emphasized > single mention"
  }],
  "responsibilities": [{"id": "resp_01", "text": "string", "related_requirement_ids": ["req_01"]}],
  "keywords": [{"term": "string", "count": "int", "synonyms": ["string"]}],
  "requests_clearance": "bool (true if any requirement has category clearance)",
  "parse_notes": ["ambiguities the parser flagged"]
}
```

### GradeReport

Implemented in `harness/grade.py`. It differs from this sketch: `grade_vN.json` holds only
what the grader writes (requirement results, seniority_fit, notes) and rejects unknown
fields, so a grader that adds `score` fails validation. Code writes `keyword_hits`,
`score`, `subscores`, `decision` and `gaps` to a separate `score_vN.json`. Keyword hits
moved from the grader to code because term presence is exact.

```json
{
  "schema_version": "1.0",
  "resume_version": "v0|v1|v2",
  "job_sha": "string",
  "requirement_results": [{
    "req_id": "req_01",
    "status": "met|partial|missing",
    "resume_evidence": ["literal substrings of the resume text"],
    "rationale": "string"
  }],
  "keyword_hits": [{"term": "string", "found": "bool"}],
  "seniority_fit": "below|match|above",
  "notes": "string",

  "score": "filled by score.py",
  "subscores": "filled by score.py: required, preferred, keywords, seniority",
  "gaps": "filled by gaps.py: [{req_id, gap_type: presentation|experience, suggestion}]"
}
```

In this original sketch, the grader filled everything above the blank line and Python
filled the rest.

**Item grading (added after the first three runs).** Every run plateaued around 8/10,
mostly because list requirements ("TypeScript, Dart, Swift, Python and OOP", seven
frameworks) were permanently `partial`: the old prompt read every "and" list as "all
required", and `partial` is worth 0.5 whether 1 of 7 or 6 of 7 items are shown. Now the
parser splits such requirements into `items` with a `match` rule (`all`, `any`, `most`),
the grader writes one `item_results` entry per item, and `grade.requirement_value()`
combines them: `all` = mean, `any` = max, `most` = shown / half, capped at 1. The
requirement's status (met/partial/missing) is derived from that value, and `gaps.py`
looks up only the unmet items. Requirements without items are graded as before.

`RequirementResult` field order is now `rationale`, evidence, `status`, so the grader
reasons before it labels. The prompt also anchors the cases that flipped between grades
(a practice named once, summary-only claims, close equivalents).
`python -m harness.stability <grade files> --job ... --resume-text ...` reports the score
spread and the requirements or items that disagree.

## Key decisions

**Computed score.** Asking an LLM for "8/10" drifts between runs, which makes a fixed
9/10 threshold meaningless. The grader marks each requirement met/partial/missing with
evidence; `score.py` computes a weighted score (met = 1, partial = 0.5, missing = 0)
mapped to 1–10. Check grader stability by grading the same input several times before
trusting the threshold.

**Gap split.** The grader can't see work history, so it can't tell "you have this but
the resume doesn't show it" from "you don't have this." `gaps.py` checks each missing
or partial requirement's `normalized_terms` against skill names, aliases, and
achievement text in `work_history.json`. Match with evidence → presentation gap.
No match → experience gap. The final report leads with this split.

**Relevance ranking instead of strength ratings.** The user chose not to rate
achievement strength. `rank.py` scores each achievement by matching its skills (names
and aliases) and tags against requirement `normalized_terms`, weighted by requirement
weight, with having metrics as a tiebreaker and in-progress status as a small penalty.
The builder fills the page from the top. `strength` remains as an optional manual
override in the model.

**Grade the PDF text.** Both v0 and built resumes are rendered and the grader reads
the extracted text, which approximates what an ATS parser sees and keeps scores
comparable. Extraction order puts the dates line between title and company for some
roles; tell the grader to expect that.

**Page-fit loop.** The builder can't predict layout. `render.py` reports
`fill_ratio`, `overflow_lines`, and `spare_lines`. Overflow → builder trims lowest-ranked
content first; it often only needs to reword bullets that wrap a single word onto a
new line. Spare lines → room for another relevant achievement.

**Template (classic).** Single column; real text only; plain section names; bullets
drawn as a literal "•" so they survive extraction; Source Sans 3 body, Source Serif 4
name only, embedded as data URIs (pages loaded with `set_content` can't read
`file://` fonts, and Chromium silently falls back). `render.py` fails loudly if any
template font doesn't load. Jinja autoescaping must not touch the CSS (`css | safe`).

## Implementation decisions (build phase)

**Orchestration is a state machine in Python.** `/resume-run` loops on `python -m
harness.run next <run_dir>`, which does every deterministic step and returns
`agent` / `need_paste` / `done` / `failed`. The run directory's files are the state, so a
run can resume after an interruption. Caps: 3 attempts per LLM step (the first plus 2
retries with the validation errors), 2 rebuilds. If a build still breaks a data rule
after 3 attempts, it is abandoned and not graded. If it only overflows the page budget,
it is graded with a warning.

**Score formula** (`score.py`): `1 + 9 ×` a weighted mean of required (0.65), preferred
(0.15), keywords (0.10) and seniority (0.10). Missing components are renormalized away.
Statuses are worth met 1, partial 0.5, missing 0; item-graded requirements combine their
items by match rule (see Item grading above). Seniority is worth match 1, above 0.8,
below 0.4. In the end-to-end test, one status flip on a 0.6-weight requirement moved the
score by 0.15.

**Gap split refinement.** A partial or missing requirement is a presentation gap only if
the work history has matching evidence that the resume text doesn't already show
(spelling variants such as "front end" and "frontend" count as shown). Otherwise it is an
experience gap. Without this, requirements like "TypeScript, Dart, Swift, Python" stayed
"presentation" forever even though Dart and Swift aren't in the record.

**Grounding heuristics** (`grounding.py`). Every number in a bullet must occur in that
bullet's source achievements. In-progress work must not use completion words (replaced,
completed, migrated…) and must carry a progress marker (leading, driving, in
progress…). Skills-section items must be exact skill names or aliases. Year claims are
capped at total professional years, and a year count next to a skill name is rejected.
Clearance terms may appear only in `Resume.clearance`, which the template renders as its
own section. Any skill named in a bullet must be one of its source achievements' skills
or appear in their text, so a tool used at one job can't be attached to another.

**Equivalent terms.** `evidence.EQUIVALENTS` groups terms that mean the same thing for
matching (LLM = large language model, Claude = Anthropic, web development = web
applications, and so on). Ranking and gap classification use it. The grader's prompt
lists the same equivalents. Keep the groups to true equivalents, because a match counts
as evidence. The keyword subscore stays literal (term plus the posting's synonyms),
because it approximates an ATS scan.

**Text extraction.** One extractor everywhere: pypdf layout mode, normalized to one line
per bullet, with ligatures expanded and wrapped lines joined. Xpdf's `pdftotext` (shipped
with Git for Windows) and pypdf's default mode both scrambled bullet order on the classic
template.

**Fetching.** Blocked means: HTTP ≥ 400, a redirect to a login page, captcha or wall text
on a short page, or under 1,200 characters. Job-board content containers (Workday,
Greenhouse, Lever and others) are preferred over the whole page body.

## Decisions made with the user

- Output format: PDF only (a Word copy can be exported afterwards with `/resume-docx`).
  Template: clean, ATS-friendly default ("classic").
- Skills the user can't back with work go on `constraints.never_claim`. A skill comes off
  that list only when an achievement evidences it.
- Skill proficiency ratings skipped; years and last-used are derived from role dates.
- Achievement strength ratings skipped in favor of relevance ranking.
- Unmeasurable impact is allowed: achievements carry `no_metrics: true` instead of
  invented numbers.
- A clearance is shown only when a posting asks, using the exact approved
  `public_wording`.
- `resume_current.json` stays the true v0, even when the work history interview turns up
  corrections to it. Corrections go into `work_history.json`, and built resumes pick them up.

## Open questions

- Grader stability: re-grade one run several times with `/resume-grade` before trusting
  the 9/10 threshold. A spread above ~0.5 means the rubric or prompt needs tightening.

- `job-parser` and `resume-grader` model choice (a cheaper model may be fine for parsing).
- Whether the grader should also see the raw posting text, to catch parser misses
  (more coverage, less consistency).
