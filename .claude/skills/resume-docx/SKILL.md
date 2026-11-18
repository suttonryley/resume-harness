---
name: resume-docx
description: Convert a resume's extracted text (.txt) into an editable Word document (.docx). Takes a resume .txt, or a run directory to convert that run's best-scoring version.
argument-hint: <resume.txt | run-dir> [out.docx]
---

# /resume-docx

Use `.venv/Scripts/python` if it exists (Windows), otherwise `.venv/bin/python`, otherwise
`python`. Below, `PY` means that interpreter. Run from the repo root.

1. `PY -m harness.docx_export <resume.txt | run-dir> [--out <out.docx>]`
   - A run directory converts the version with the highest score.
   - Without `--out`, the .docx is written next to the .txt.
2. If `ok` is false, tell the user the `error` and stop. Don't edit the .txt to make it
   parse.
3. Tell the user the .docx path and which .txt it came from (`source`). Mention that the
   wording is copied as-is from the graded text, so it matches what was scored.

Never reword, add or drop content in the .docx. If the user wants wording changes, those
belong in the Resume JSON, followed by a re-render.
