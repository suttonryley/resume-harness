# Resume harness

A Claude Code harness that tailors a resume to a job posting:

1. Take a job URL, fetch the posting, parse it into `JobContext` JSON.
2. Grade the current resume against it (score out of 10).
3. Score ≥ 9 → report what is still missing.
4. Score < 9 → build a new resume from `data/work_history.json`, render it, grade it.
5. Still < 9 → report remaining weaknesses, split into presentation gaps (fixable by
   wording) and experience gaps (not in the work history at all).

Full design and rationale: `docs/DESIGN.md`. Read it before changing a component.

## Status

Implemented end to end. Use `/resume-run <url | pasted posting> [resume-file]`.

- `harness/run.py`: the orchestrator. A resumable state machine over a run directory.
  `next` does every deterministic step itself and returns only when it needs a subagent,
  a pasted posting, or is finished. It owns every retry cap and branch (3 attempts per
  LLM step, 2 rebuilds). The skills in `.claude/skills/` just loop on it.
- `harness/fetch_job.py`: headless Chromium fetch, with block detection (error status,
  login redirect, captcha text, under 1,200 characters). A blocked fetch falls back to
  asking for a paste.
- `harness/job.py`: `JobContext`. `finalize()` fills the text hash and keyword counts from
  `job_raw.txt`; the orchestrator injects `source` from `job_meta.json`.
- `harness/grade.py`: `GradeReport`, plus the evidence-substring check. List requirements
  (`Requirement.items` + `match: all|any|most`) are graded item by item; code combines them
  (`requirement_value`, `effective`).
- `harness/stability.py`: compares repeated grades of one resume (score spread, flips).
- `harness/score.py`: score and decision. Keyword hits are checked by code, not the
  grader.
- `harness/gaps.py`: presentation vs. experience split. A gap counts as presentation only
  when the work history has evidence the resume doesn't show yet.
- `harness/rank.py`: relevance ranking for the builder.
- `harness/grounding.py`: data-rule checks on built resumes.
- `harness/evidence.py`: shared term matching over the work history, including the
  `EQUIVALENTS` table (LLM = large language model, Claude = Anthropic, ...).
- Subagents in `.claude/agents/`: `job-parser`, `resume-grader`, `resume-builder` (Read
  and Write only).
- Skills: `/resume-run`, `/job-ingest` (fetch and parse only), `/resume-grade <run-dir>`
  (re-grade the latest version without rebuilding), `/resume-docx <txt | run-dir>`
  (Word copy of the graded text).
- `harness/docx_export.py`: extracted resume text to .docx, wording copied verbatim. Company
  and location stay on one line because the .txt doesn't separate them.
- Already built before this: `models.py` (`Resume`, now with `clearance`),
  `work_history.py`, `render.py` and the classic template.
- Personal data lives in `data/` (gitignored except `data/README.md`):
  `data/work_history.json` and `data/resume_current.*` (v0). Fictional examples of both
  are in `examples/`.

Tested end to end on a real posting, with the LLM steps done by hand and every retry
path exercised (`runs/2026-11-12_disney-experiences-senior-software-engineer/`, which is
gitignored). Not yet tested: the real subagents. Their files load in a new Claude Code
session.

Open: check grader stability (run `/resume-grade` a few times on one run) before
trusting the 9/10 threshold, and choose models for the parser and grader
(`docs/DESIGN.md`).

## Commands

```bash
pip install -r requirements.txt && python -m playwright install chromium
python -m harness.run init [--url URL] [--resume data/resume_current.json]
python -m harness.run next <run_dir> [--stop-after parse|grade]
python -m harness.run regrade <run_dir>
python -m harness.render <resume.json> --out <file.pdf> [--max-pages 2] [--page-size letter|a4]
python -m harness.docx_export <resume.txt | run_dir> [--out <file.docx>]  # editable Word copy
python -m harness.fetch_job <url> --out-dir <dir>          # exit 4 = blocked
python -m harness.job <job.json> [--raw <job_raw.txt>]      # validate (+ finalize)
python -m harness.grade <grade.json> --job <job.json> --resume-text <resume.txt>
python -m harness.score <grade.json> --job <job.json> --resume-text <resume.txt>
python -m harness.stability <grade.json>... --job <job.json> --resume-text <resume.txt>
python -m harness.gaps <grade.json> --job <job.json> --resume-text <resume.txt>
python -m harness.rank --job <job.json>
python -m harness.grounding <resume.json> --job <job.json>
python -m harness.work_history data/work_history.json      # validate + audit gaps
# Schemas (regenerate after changing a model):
python -m harness.models > schemas/resume.schema.json
python -m harness.work_history --schema > schemas/work_history.schema.json
python -m harness.job --schema > schemas/job.schema.json
python -m harness.grade --schema > schemas/grade.schema.json
```

`render.py` exits 0 when the resume fits, 3 when it overflows, 2 on invalid input.
On Windows the interpreter is `.venv/Scripts/python`.

## Architecture rules

- Python owns anything that must be exact: fetching, validation, scoring, branching,
  gap classification, grounding checks, rendering. Claude owns judgment: parsing,
  grading, writing. Thresholds and iteration caps live in code, never in prompts.
- Pydantic models are the single source of truth for every schema. Regenerate
  `schemas/*.json` after changing a model.
- LLM steps run as subagents in `.claude/agents/` with minimal tools. Orchestration
  lives in skills under `.claude/skills/<name>/SKILL.md` (the recommended format;
  `.claude/commands/` is legacy).
- The grader never sees `work_history.json`, only resume text and `job.json`. Every
  piece of evidence it cites must be a literal substring of the resume text, or the
  grade is rejected.
- The grader reads text extracted from the rendered PDF, never the JSON, so v0 and
  built resumes are graded on the same footing.
- The score is computed in `score.py` from per-requirement statuses and weights. The
  LLM never outputs the number; `GradeReport` rejects unknown fields like `score`.
  Keyword presence is checked by code, not judged by the grader.
- Exact values are filled by code, never by an LLM: the posting's hash, keyword counts
  and `source`.
- Text extraction is pypdf layout mode plus normalization (`render.extract_text`) on
  every machine. Don't reintroduce `pdftotext`: Git for Windows ships Xpdf's version,
  which scrambles bullets.

## Data rules: do not break these

- The builder may only select and rephrase achievements in `work_history.json`. Every
  bullet carries `source_ids` that must resolve to real achievement ids.
- Never print anything in `constraints.never_claim`.
- Never print any phrase in an achievement's `private_terms` (confidential names,
  codenames, systems). `grounding.py` must scan output for these.
- Achievements with `status: in_progress` or `proposed` must not be worded as finished.
- Clearance: print only `clearances[].public_wording`, verbatim, and only when the job
  posting asks for a clearance (`include_policy: only_if_job_requests`). Never mention
  it in bullets or derive wording from other fields.
- Skill `years` with `years_basis: derived` are ceilings (time in roles where the skill
  appears). Use them for requirement checks; never print them on a resume.
- Metrics with `approximate: true` may be printed with "~" or "about"; metrics with
  `shareable: false` never get printed.
- Achievement `details` may contain interview-only stories (e.g. mentoring anecdotes
  about colleagues). Bullets stay general and never describe a colleague's weaknesses.
- When editing `work_history.json` programmatically: clear `skills[].evidence_ids`,
  apply changes, validate, then run `sync_evidence()` and `derive_skill_stats()`.
  Achievements are the source of truth for evidence links.
- Don't infer technologies the user didn't state (e.g. don't attach a language to an
  achievement because the role used it elsewhere). Ask instead.

## Conventions

- IDs: `role_<slug>`, `proj_<slug>`, `ach_<slug>_<nn>`, `sk_<slug>`, `edu_<slug>`,
  `cert_<slug>`, `sum_<slug>`, `clr_<slug>`. Enforced by the model.
- Dates: `YYYY-MM`, or `present` for ongoing end dates.
- Per-run artifacts go in `runs/<YYYY-MM-DD>_<company>-<role>/` (gitignored).
- `data/` contains personal information and is gitignored. Never commit it, and never
  copy its contents into tracked files (docs, prompts, examples, tests).
