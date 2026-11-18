# resume-harness

A [Claude Code](https://claude.com/claude-code) harness that tailors your resume to a job
posting and tells you honestly what's still missing.

Give it a job URL (or pasted posting text). It:

1. Fetches the posting and parses it into structured requirements.
2. Grades your current resume against it, requirement by requirement, with quoted
   evidence, and computes a score out of 10.
3. Scores 9 or higher: reports what's still missing.
4. Scores under 9: builds a new resume from your work history, renders it to PDF and
   grades it again (up to 2 rebuilds).
5. Still under 9: reports the remaining gaps, split into **presentation gaps** (you have
   the experience but the resume doesn't show it) and **experience gaps** (it isn't in
   your work history at all).

The builder can only select and rephrase achievements you recorded. It can't invent any.

## How it works

Python owns everything that has to be exact. Claude owns judgment.

| Step | Done by |
|---|---|
| Fetch the posting, detect login walls and captchas | `harness/fetch_job.py` (Playwright) |
| Parse the posting into `JobContext` | `job-parser` subagent |
| Grade each requirement as met / partial / missing, with evidence | `resume-grader` subagent |
| Check that every evidence quote occurs in the resume text | `harness/grade.py` |
| Compute the score, check keywords, decide whether to rebuild | `harness/score.py` |
| Rank achievements by relevance to the posting | `harness/rank.py` |
| Write a tailored resume from the work history | `resume-builder` subagent |
| Check the built resume against the data rules | `harness/grounding.py` |
| Render to PDF, check page fit, extract text | `harness/render.py` |
| Split gaps into presentation vs. experience | `harness/gaps.py` |
| Orchestrate all of the above, own every retry cap and branch | `harness/run.py` |

Design choices that keep the output trustworthy:

- **The LLM never outputs the score.** The grader labels each requirement and
  `score.py` computes a weighted score, so a fixed 9/10 threshold means the same thing
  from run to run.
- **The grader never sees your work history.** It reads only the text extracted from
  the rendered PDF (roughly what an applicant tracking system sees), and every piece of
  evidence it cites must be a literal substring of that text or the grade is rejected.
- **Built resumes are grounded.** Every bullet carries `source_ids` back to real
  achievements. `grounding.py` rejects invented numbers, in-progress work worded as
  finished, skills attached to the wrong job, and anything you marked as never to be
  printed (`never_claim`, per-achievement `private_terms`, non-shareable metrics).
- **Clearances are opt-in.** A clearance is printed only with your exact approved
  wording, and only when the posting asks for one.
- **Runs are resumable.** Each run is a directory under `runs/`, and its files are the
  state, so an interrupted run picks up where it stopped.

The full design and its rationale are in [`docs/DESIGN.md`](docs/DESIGN.md).
[`CLAUDE.md`](CLAUDE.md) holds the rules Claude Code follows when working in this repo.

## Requirements

- Python 3.9+
- [Claude Code](https://claude.com/claude-code) (runs the subagents and skills)
- Chromium for Playwright (installed below)

## Setup

```bash
git clone <this repo> && cd resume-harness
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m playwright install chromium
```

On Python 3.9, if pip tries to build greenlet from source, use
`pip install --only-binary :all: -r requirements.txt`.

### Add your data

Your personal files go in `data/`, which is gitignored. Start from the fictional examples:

```bash
cp examples/sample_work_history.json data/work_history.json
cp examples/sample_resume.json data/resume_current.json
```

Then replace their contents with your own:

- **`data/work_history.json`** is the master record of everything the builder may use:
  roles, projects, achievements (with details, metrics and skills), skills, education,
  certifications, clearances, and constraints. The richer the `details`, the better the
  tailoring. See `schemas/work_history.schema.json` for every field. Validate it and list
  what's still missing with:

  ```bash
  python -m harness.work_history data/work_history.json
  ```

- **`data/resume_current.json`** is your current resume, graded as v0. You can also pass
  a `.pdf` or `.txt` resume to `/resume-run` instead.

An easy way to fill in the work history is to ask Claude Code to interview you about each
role and write the answers into `data/work_history.json`.

## Usage

Open Claude Code in the repo and run:

```
/resume-run https://jobs.example.com/senior-backend-engineer
```

or paste the posting text instead of a URL. Optionally pass a resume file as a second
argument. When the posting is behind a login or captcha, the harness asks you to paste it.

Each run writes to `runs/<YYYY-MM-DD>_<company>-<role>/`: the posting, every resume
version as JSON, PDF and text, every grade and score, and `report.md` with the score
history and the gap split.

Other skills:

| Skill | What it does |
|---|---|
| `/job-ingest <url \| text>` | Fetch and parse a posting only. For checking how a posting was read. |
| `/resume-grade <run-dir>` | Re-grade a run's latest version without rebuilding. Run it a few times to check grader consistency. |
| `/resume-docx <resume.txt \| run-dir>` | Export an editable Word copy of a graded resume. |

### Command line

The deterministic steps run on their own too:

```bash
python -m harness.render examples/sample_resume.json --out out/sample.pdf   # exit 3 = overflows the page budget
python -m harness.rank --job examples/sample_job.json --work-history examples/sample_work_history.json
python -m harness.grounding examples/sample_resume.json --job examples/sample_job.json \
    --work-history examples/sample_work_history.json
python -m harness.fetch_job <url> --out-dir out/job                         # exit 4 = blocked
python -m harness.docx_export examples/sample_resume.txt --out out/sample.docx
```

See [`CLAUDE.md`](CLAUDE.md#commands) for the full list, including the orchestrator
(`python -m harness.run init | next | regrade`).

## Repository layout

```
.claude/agents/      job-parser, resume-grader, resume-builder subagents (Read and Write only)
.claude/skills/      /resume-run, /job-ingest, /resume-grade, /resume-docx
harness/             Python: orchestrator, fetching, models, scoring, grounding, rendering
harness/templates/   "classic" ATS-friendly HTML/CSS template and bundled fonts
schemas/             JSON Schemas generated from the Pydantic models
examples/            Fictional work history, resume, job and rendered output
data/                Your personal files (gitignored)
runs/                Per-run artifacts (gitignored)
docs/DESIGN.md       Design and rationale
```

Pydantic models are the single source of truth for every schema. After changing a model,
regenerate `schemas/` with the commands in `CLAUDE.md`.

## Privacy

Your work history, resumes and run artifacts stay local: `data/` and `runs/` are
gitignored. Job postings are fetched with a headless browser on your machine. The posting,
your resume text and (for the builder only) your work history are sent to Claude through
Claude Code, like any other file it reads.

## Fonts

The template bundles Source Sans 3 and Source Serif 4, licensed under the SIL Open Font
License (`harness/templates/fonts/OFL-LICENSE.md`).
