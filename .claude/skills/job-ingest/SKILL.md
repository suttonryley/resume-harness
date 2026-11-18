---
name: job-ingest
description: Fetch and parse a job posting into job.json without grading or building a resume. For debugging the parser or inspecting how a posting was read.
argument-hint: <job-url | pasted posting text>
---

# /job-ingest

Same mechanics as `/resume-run` (read `.claude/skills/resume-run/SKILL.md` for the
interpreter, `agent` and `need_paste` handling), but stop once the posting is parsed:

1. `PY -m harness.run init [--url <url>]`
2. Loop on `PY -m harness.run next <run_dir> --stop-after parse` until `action` is `stopped`
   or `failed`.
3. On `stopped`, run `PY -m harness.job <job>` (the `job` path from the response) and show
   the user its summary:
   - title, company and seniority
   - required and preferred counts, and the highest-weighted requirements
   - whether a clearance is requested
   - every parse note

   Mention the run directory so they can continue with `/resume-run`. To continue, they can
   run `next` on the same directory without `--stop-after`.
