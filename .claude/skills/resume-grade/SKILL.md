---
name: resume-grade
description: Re-grade the latest resume version of a finished run without rebuilding, to check grader consistency or refresh a grade. Takes a run directory.
argument-hint: <run-dir>
---

# /resume-grade

Same mechanics as `/resume-run` (read `.claude/skills/resume-run/SKILL.md` for the
interpreter and `agent` handling).

1. `PY -m harness.run regrade <run_dir>`. This archives the latest grade as
   `grade_<v>.prevN.json` and resets that one step.
2. Loop on `PY -m harness.run next <run_dir>` until `action` is `done` or `failed`. Only the
   resume-grader runs. Nothing is rebuilt.
3. Report the new score next to the earlier score(s) for the same version (the response's
   `history`; re-grades are marked). A spread of more than about 0.5 points between grades of
   the same text means the grader isn't stable enough to trust the 9/10 threshold. Say so if
   you see it.
