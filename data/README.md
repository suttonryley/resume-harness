# data/

Your personal files live here. Everything in this folder except this README is
gitignored, because it holds contact details and anything else you record about your
career.

| File | What it is |
|---|---|
| `work_history.json` | The master record the builder draws on (`WorkHistory` model). The builder can only select and rephrase what is in here. |
| `resume_current.json` | Your current resume (`Resume` model), graded as v0. A `.pdf` or `.txt` resume can be passed to `/resume-run` instead. |

To start, copy the examples and replace their contents with your own:

```bash
cp examples/sample_work_history.json data/work_history.json
cp examples/sample_resume.json data/resume_current.json
python -m harness.work_history data/work_history.json   # validate + list what's missing
```
