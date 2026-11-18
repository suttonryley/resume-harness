---
name: resume-run
description: Tailor the user's resume to a job posting. Fetches and parses the posting, grades the current resume, rebuilds from the work history if it scores under 9/10, and reports what's still missing, split into wording gaps and experience gaps. Use when the user gives a job URL or posting text and wants their resume graded or tailored.
argument-hint: <job-url | pasted posting text> [resume-file]
---

# /resume-run

You are a thin loop around `python -m harness.run`. Python decides everything: what step is
next, how many retries are allowed, the score, and whether to rebuild. You run the subagents
it asks for and relay the result. **Never** do any of these yourself:

- compute or estimate a score
- decide to rebuild or stop
- write or edit `job.json`, `grade_*.json` or `resume_*.json` (only the subagents write
  those, and only when asked)
- read or summarize `data/work_history.json` for the grader

## Python

Run commands from the repo root. Use `.venv/Scripts/python` if it exists (Windows),
otherwise `.venv/bin/python`, otherwise `python`. Below, `PY` means that interpreter.

## Steps

1. **Start a run.** Arguments: a job URL or pasted posting text, then optionally a resume
   file (`.json` Resume, `.pdf` or `.txt`; default `data/resume_current.json`).
   - With a URL: `PY -m harness.run init --url <url> [--resume <file>]`
   - With pasted text, or nothing: `PY -m harness.run init [--resume <file>]`

   Keep the `run_dir` it prints.
2. **Loop.** Run `PY -m harness.run next <run_dir>` and act on `action`. Always use the
   `run_dir` from the most recent response, because it changes once, after parsing.
   - `agent`: call the Agent tool with `subagent_type` set to the response's `agent` and
     `prompt` set to the response's `prompt`, verbatim. Add nothing. When the subagent
     finishes, call `next` again. Don't inspect or fix its output: `next` validates it and
     re-runs the subagent with the errors if needed.
   - `need_paste`: if the user already gave the posting text in this conversation, write it
     verbatim (no edits, no summarizing) to `paste_path` with the Write tool. Otherwise show
     the response's `message`, ask the user to paste the full posting, and write their text
     to `paste_path`. Then call `next`.
   - `done`: go to step 3.
   - `failed`: tell the user the `reason` and the `run_dir`, then stop.
3. **Report.** Read the `report` file named in the response. Tell the user:
   - the best version and its score against the threshold, and the score history in one
     line (for example v0 6.8 → v1 8.4 → v2 8.7)
   - the path to the best resume PDF
   - the presentation gaps (fixable by wording) and the experience gaps (not in the work
     history), keeping the report's split and order
   - any Notes (warnings or parser notes) that affect how to read the result

   Link to `report.md` for the details. Don't restate the whole report.

Progress updates during the loop: one short line per step ("Parsing the posting",
"Grading v0", "Building v1"), no more.
