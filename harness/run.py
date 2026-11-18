"""Run orchestrator: a resumable state machine over a run directory.

The /resume-run skill loops on `next`. Each call does every deterministic step it
can (fetch, validate, render, score, gaps, ranking, grounding) and returns only
when it needs something it can't do itself:

  {"action": "agent", "agent": <name>, "prompt": <text>}   run that subagent, then call next again
  {"action": "need_paste", "paste_path": ...}              ask the user for the posting text, save it there
  {"action": "done", "report": ...}                        finished; show the report
  {"action": "failed", "reason": ...}                      stopped; relay the reason
  {"action": "stopped", ...}                               reached --stop-after

Every response carries "run_dir", which changes once (after parsing, the
directory is renamed to <date>_<company>-<role>). All caps and thresholds live
here and in score.py.

    python -m harness.run init [--url URL] [--resume data/resume_current.json]
    python -m harness.run next <run_dir> [--stop-after parse|grade]
    python -m harness.run regrade <run_dir>      # grade the latest version again, no rebuild
    python -m harness.run status <run_dir>
"""
from __future__ import annotations

import argparse
import json
import re
import secrets
import shutil
import sys
from datetime import date, datetime, timezone
from pathlib import Path

from pydantic import ValidationError

from harness import fetch_job, gaps as gaps_mod, grounding, rank as rank_mod, score as score_mod
from harness.grade import GradeReport, validate_grade
from harness.job import JobContext, finalize
from harness.models import Resume
from harness.render import extract_text, render
from harness.work_history import WorkHistory

ROOT = Path(__file__).resolve().parent.parent
RUNS = ROOT / "runs"
WORK_HISTORY = ROOT / "data" / "work_history.json"
DEFAULT_RESUME = ROOT / "data" / "resume_current.json"

MAX_AGENT_ATTEMPTS = 3  # each LLM step: first try + 2 retries with the validation errors


# ---------------------------------------------------------------- state

def _load(run: Path) -> dict:
    return json.loads((run / "state.json").read_text(encoding="utf-8"))


def _save(run: Path, st: dict) -> None:
    (run / "state.json").write_text(json.dumps(st, indent=2) + "\n", encoding="utf-8")


def _write_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _rel(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return str(path)


def _slug(text: str, limit: int = 40) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:limit].rstrip("-") or "x"


def init(url: str | None, resume: Path) -> Path:
    if not resume.exists():
        raise SystemExit(f"resume file not found: {resume}")
    run = RUNS / f"{date.today().isoformat()}_pending-{secrets.token_hex(3)}"
    run.mkdir(parents=True)
    _save(run, {"url": url, "resume_source": str(resume.resolve()),
                "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "status": "running", "done": [], "attempts": {}, "errors": {}, "warnings": [],
                "current": "v0", "rebuilds": 0, "history": [], "regrade": None})
    return run


# ---------------------------------------------------------------- responses

def _resp(run: Path, action: str, **kw) -> dict:
    return {"action": action, "run_dir": _rel(run), **kw}


def _agent(run: Path, st: dict, key: str, agent: str, prompt: str) -> dict:
    st["attempts"][key] = st["attempts"].get(key, 0) + 1
    n = st["attempts"][key]
    errors = st["errors"].get(key)
    if errors:
        prompt += ("\n\nYour previous attempt was rejected by validation. Fix every problem below and "
                   "rewrite the whole output file:\n" + "\n".join(f"- {e}" for e in errors))
    _save(run, st)
    return _resp(run, "agent", agent=agent, attempt=n, max_attempts=MAX_AGENT_ATTEMPTS, prompt=prompt)


def _fail(run: Path, st: dict, reason: str) -> dict:
    st["status"] = "failed"
    st["failure"] = reason
    _save(run, st)
    return _resp(run, "failed", reason=reason)


def _exhausted(st: dict, key: str) -> bool:
    return st["attempts"].get(key, 0) >= MAX_AGENT_ATTEMPTS


def _error_lines(e: Exception) -> list[str]:
    """Readable one-line-per-problem errors for a retry prompt."""
    if isinstance(e, ValidationError):
        out = []
        for err in e.errors()[:30]:
            loc = ".".join(str(x) for x in err["loc"])
            msg = err["msg"].removeprefix("Value error, ")
            out.append(f"{loc}: {msg}" if loc else msg)
        return out
    return [str(e)]


# ---------------------------------------------------------------- steps

def _parse(run: Path, st: dict) -> tuple[Path, dict | None]:
    """Returns (run_dir, response or None when parsing is complete)."""
    raw_path, job_path = run / "job_raw.txt", run / "job.json"
    if job_path.exists():
        try:
            data = json.loads(job_path.read_text(encoding="utf-8"))
            meta = json.loads((run / "job_meta.json").read_text(encoding="utf-8"))
            data["source"] = {"url": meta["url"], "fetched_at": meta["fetched_at"]}  # code owns source
            job = finalize(JobContext.model_validate(data), raw_path.read_text(encoding="utf-8"))
        except (ValueError, ValidationError, json.JSONDecodeError) as e:  # ValidationError is a ValueError
            st["errors"]["parse"] = _error_lines(e)
        else:
            job_path.write_text(job.model_dump_json(indent=2) + "\n", encoding="utf-8")
            st["done"].append("parse")
            st["errors"].pop("parse", None)
            run = _rename(run, job)
            _save(run, st)
            return run, None
    if _exhausted(st, "parse"):
        return run, _fail(run, st, "The job posting could not be parsed into a valid job.json after "
                                   f"{MAX_AGENT_ATTEMPTS} attempts. Last errors: {st['errors'].get('parse')}")
    prompt = (f"Parse the job posting in {_rel(raw_path)} and write {_rel(job_path)}.")
    return run, _agent(run, st, "parse", "job-parser", prompt)


def _rename(run: Path, job: JobContext) -> Path:
    if not run.name.split("_", 1)[-1].startswith("pending-"):
        return run
    base = f"{run.name.split('_', 1)[0]}_{_slug(job.company.name, 30)}-{_slug(job.role.title, 40)}"
    target, n = RUNS / base, 2
    while target.exists():
        target, n = RUNS / f"{base}-{n}", n + 1
    run.rename(target)
    return target


def _v0(run: Path, st: dict, wh: WorkHistory) -> None:
    txt = run / "resume_v0.txt"
    if txt.exists():
        return
    src = Path(st["resume_source"])
    entry = {"version": "v0", "source": str(src)}
    if src.suffix == ".json":
        resume = Resume.model_validate_json(src.read_text(encoding="utf-8"))
        shutil.copy(src, run / "resume_v0.json")
        fit = render(resume, run / "resume_v0.pdf", max_pages=wh.constraints.max_pages)
        _write_json(run / "render_v0.json", fit)
        entry["pages"] = fit["pages"]
        if not fit["fits"]:
            st["warnings"].append(f"v0 runs to {fit['pages']} pages (budget {fit['max_pages']}).")
    elif src.suffix == ".pdf":
        shutil.copy(src, run / "resume_v0.pdf")
        txt.write_text(extract_text(src), encoding="utf-8")
    else:
        txt.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    st["v0"] = entry


def _grade(run: Path, st: dict, v: str, wh: WorkHistory) -> dict | None:
    key = f"grade_{v}"
    job = JobContext.model_validate_json((run / "job.json").read_text(encoding="utf-8"))
    txt_path, grade_path = run / f"resume_{v}.txt", run / f"grade_{v}.json"
    text = txt_path.read_text(encoding="utf-8")
    if grade_path.exists():
        try:
            grade = GradeReport.model_validate_json(grade_path.read_text(encoding="utf-8"))
            errors = validate_grade(grade, job, text, version=v)
        except ValidationError as e:
            errors = _error_lines(e)
        if not errors:
            result = score_mod.score(grade, job, text)
            decision = score_mod.decide(result, st["rebuilds"])
            gap_list = gaps_mod.classify(grade, job, wh, text)
            _write_json(run / f"score_{v}.json", {**result.model_dump(), "decision": decision,
                                                  "rebuilds_done": st["rebuilds"],
                                                  "gaps": [g.model_dump() for g in gap_list]})
            st["done"].append(key)
            st["errors"].pop(key, None)
            pages = json.loads((run / f"render_{v}.json").read_text())["pages"] if (run / f"render_{v}.json").exists() else None
            st["history"].append({"version": v, "score": result.score, "subscores": result.subscores,
                                  "decision": decision, "pages": pages,
                                  "regrade": st.get("regrade") == v})
            _save(run, st)
            return None
        st["errors"][key] = errors
    if _exhausted(st, key):
        return _fail(run, st, f"The grade for {v} failed validation {MAX_AGENT_ATTEMPTS} times. "
                              f"Last errors: {st['errors'].get(key)}")
    prompt = (f"Grade the resume text in {_rel(txt_path)} against the job in {_rel(run / 'job.json')}. "
              f"Write {_rel(grade_path)} with resume_version \"{v}\" and job_sha \"{job.source.raw_text_sha256}\". "
              "Read only those two files.")
    return _agent(run, st, key, "resume-grader", prompt)


def _build(run: Path, st: dict, v: str, wh: WorkHistory) -> dict | None:
    """Returns a response, or None when resume_{v} is built and rendered."""
    key = f"build_{v}"
    job = JobContext.model_validate_json((run / "job.json").read_text(encoding="utf-8"))
    ranking = run / "ranking.json"
    if not ranking.exists():
        _write_json(ranking, rank_mod.rank(wh, job))
    rjson, pdf = run / f"resume_{v}.json", run / f"resume_{v}.pdf"
    overflow_only = False
    if rjson.exists():
        errors: list[str] = []
        try:
            resume = Resume.model_validate_json(rjson.read_text(encoding="utf-8"))
        except ValidationError as e:
            errors = _error_lines(e)
        else:
            errors = grounding.check(resume, wh, job)
            _write_json(run / f"grounding_{v}.json", {"ok": not errors, "errors": errors})
            if not errors:
                fit = render(resume, pdf, max_pages=wh.constraints.max_pages)
                _write_json(run / f"render_{v}.json", fit)
                if not fit["fits"]:
                    overflow_only = True
                    msg = (f"The rendered resume runs to {fit['pages']} pages; the budget is {fit['max_pages']}. "
                           f"About {fit['overflow_lines']} lines landed past the last allowed page.")
                    if fit.get("break_overflow"):
                        msg += (" The content would fit by height, but each role is kept whole on one page, so a "
                                "role near the page boundary moved down entire. Cut enough from the roles before "
                                "it (or that role itself) to pull it back onto the page")
                    msg += (". Drop or shorten the lowest-ranked bullets first, and reword bullets that wrap a word "
                            "or two onto a new line.")
                    errors = [msg]
        if not errors:
            st["done"].append(key)
            st["errors"].pop(key, None)
            _save(run, st)
            return None
        st["errors"][key] = errors
        if _exhausted(st, key):
            if overflow_only:
                st["warnings"].append(f"{v} still overflows the page budget after {MAX_AGENT_ATTEMPTS} "
                                      "attempts; graded anyway.")
                st["done"].append(key)
                _save(run, st)
            else:
                _abandon_build(run, st, v)
            return None
    elif _exhausted(st, key):
        _abandon_build(run, st, v)
        return None

    prev = f"v{int(v[1:]) - 1}"
    lines = [
        f"Build resume {v} for the job in {_rel(run / 'job.json')} and write it to {_rel(rjson)}.",
        f"Inputs: work history {_rel(WORK_HISTORY)}; relevance ranking {_rel(ranking)}; "
        f"the previous version's text {_rel(run / f'resume_{prev}.txt')} and its gaps in "
        f"{_rel(run / f'score_{prev}.json')} (the \"gaps\" list; presentation gaps are what you can fix).",
        f"Set target.job_sha to \"{job.source.raw_text_sha256}\". Page budget: {wh.constraints.max_pages} pages.",
    ]
    fit_prev = run / f"render_{prev}.json"
    if fit_prev.exists():
        f = json.loads(fit_prev.read_text())
        lines.append(f"The previous version rendered to {f['pages']} page(s), fill ratio {f['fill_ratio']} "
                     f"({f['spare_lines']} spare lines).")
    return _agent(run, st, key, "resume-builder", "\n".join(lines))


def _abandon_build(run: Path, st: dict, v: str) -> None:
    st["warnings"].append(f"{v} could not be built within the data rules after {MAX_AGENT_ATTEMPTS} attempts "
                          f"and was not graded. Last errors: {st['errors'].get(f'build_{v}')}")
    st["abandoned"] = v
    _save(run, st)


# ---------------------------------------------------------------- report

def _report(run: Path, st: dict) -> dict:
    job = JobContext.model_validate_json((run / "job.json").read_text(encoding="utf-8"))
    graded = [h for h in st["history"] if not h.get("regrade")]
    best = max(graded, key=lambda h: h["score"])
    sc = json.loads((run / f"score_{best['version']}.json").read_text(encoding="utf-8"))
    pres = [g for g in sc["gaps"] if g["gap_type"] == "presentation"]
    exp = [g for g in sc["gaps"] if g["gap_type"] == "experience"]
    missing_kw = [k["term"] for k in sc["keyword_hits"] if not k["found"]]
    reached = best["score"] >= score_mod.THRESHOLD

    L = [f"# {job.role.title}, {job.company.name}", ""]
    L.append(f"Posting: {job.source.url or 'pasted text'}  ")
    L.append(f"Run: `{_rel(run)}`")
    L += ["", "## Result", ""]
    if best["version"] == "v0" and reached:
        L.append(f"Your current resume scores **{best['score']:.1f}/10**, at or above the {score_mod.THRESHOLD:g} "
                 "threshold, so no rebuild was needed. What's still missing is listed below.")
    elif reached:
        L.append(f"**{best['version']}** scores **{best['score']:.1f}/10** (threshold {score_mod.THRESHOLD:g}).")
    else:
        L.append(f"Best version: **{best['version']}** at **{best['score']:.1f}/10**, below the "
                 f"{score_mod.THRESHOLD:g} threshold after {st['rebuilds']} rebuild(s). The remaining gaps are "
                 "split below into what better wording can fix and what isn't in your work history.")
    pdf = run / f"resume_{best['version']}.pdf"
    if pdf.exists():
        L.append(f"\nResume: `{_rel(pdf)}`")
    L += ["", "## Score history", "", "| Version | Score | Required | Preferred | Keywords | Seniority | Pages |",
          "|---|---|---|---|---|---|---|"]
    fmt = lambda x: "n/a" if x is None else f"{x:.2f}"
    for h in st["history"]:
        s = h["subscores"]
        label = h["version"] + (" (re-grade)" if h.get("regrade") else "")
        L.append(f"| {label} | {h['score']:.2f} | {fmt(s['required'])} | {fmt(s['preferred'])} | "
                 f"{fmt(s['keywords'])} | {fmt(s['seniority'])} | {h.get('pages') or 'n/a'} |")

    def gap_lines(items):
        out = []
        for g in items:
            out.append(f"- **{g['req_id']}** ({g['type']}, weight {g['weight']:g}, {g['status']}): {g['req_text']}  ")
            if g.get("unmet_items"):
                out.append(f"  Not shown: {', '.join(g['unmet_items'])}.  ")
            out.append(f"  {g['suggestion']}")
        return out or ["- None."]

    L += ["", f"## Presentation gaps ({len(pres)}): fixable by wording", ""] + gap_lines(pres)
    L += ["", f"## Experience gaps ({len(exp)}): not in your work history", ""] + gap_lines(exp)
    L += ["", "## Posting keywords not on the resume", "", ", ".join(missing_kw) if missing_kw else "None."]
    notes = st["warnings"] + [f"Parser: {n}" for n in job.parse_notes]
    if notes:
        L += ["", "## Notes", ""] + [f"- {n}" for n in notes]
    (run / "report.md").write_text("\n".join(L) + "\n", encoding="utf-8")
    st["status"] = "done"
    st["regrade"] = None
    _save(run, st)
    return _resp(run, "done", report=_rel(run / "report.md"), best_version=best["version"],
                 best_score=best["score"], threshold=score_mod.THRESHOLD,
                 history=[{"version": h["version"], "score": h["score"], "regrade": h.get("regrade", False)}
                          for h in st["history"]],
                 presentation_gaps=len(pres), experience_gaps=len(exp))


# ---------------------------------------------------------------- next

def next_step(run: Path, stop_after: str | None = None) -> dict:
    st = _load(run)
    if st["status"] == "done":
        return _report(run, st)
    if st["status"] == "failed":
        return _resp(run, "failed", reason=st.get("failure"))
    wh = WorkHistory.model_validate_json(WORK_HISTORY.read_text(encoding="utf-8"))

    # 1. posting text
    raw = run / "job_raw.txt"
    if not raw.exists():
        if st["url"] and not st.get("fetch_failed"):
            result = fetch_job.fetch(st["url"])
            if result.ok:
                fetch_job.write_meta(run, result.text, st["url"], "fetched", final_url=result.final_url,
                                     status=result.status)
            else:
                st["fetch_failed"] = result.reason
                _save(run, st)
        if not raw.exists():
            why = f"Fetching {st['url']} failed: {st['fetch_failed']}. " if st.get("fetch_failed") else ""
            return _resp(run, "need_paste", paste_path=_rel(raw),
                         message=why + "Ask the user to paste the full job posting text, save it verbatim to "
                                       "paste_path, then call next again.")
    if not (run / "job_meta.json").exists():
        fetch_job.write_meta(run, None, st["url"], "pasted")

    # 2. parse
    if "parse" not in st["done"]:
        run, resp = _parse(run, st)
        if resp:
            return resp
    if stop_after == "parse":
        return _resp(run, "stopped", stage="parse", job=_rel(run / "job.json"))

    # 3. v0 text
    _v0(run, st, wh)
    _save(run, st)

    # 4. grade / decide / rebuild
    while True:
        v = st["current"]
        if st.get("abandoned") == v:
            return _report(run, st)
        if f"build_{v}" not in st["done"] and v != "v0":
            resp = _build(run, st, v, wh)
            if resp:
                return resp
            if st.get("abandoned") == v:
                return _report(run, st)
        if f"grade_{v}" not in st["done"]:
            resp = _grade(run, st, v, wh)
            if resp:
                return resp
            if stop_after == "grade" or st.get("regrade"):
                if st.get("regrade"):
                    return _report(run, st)
                return _resp(run, "stopped", stage="grade", score=st["history"][-1])
        decision = st["history"][-1]["decision"]
        if decision != "rebuild":
            return _report(run, st)
        st["rebuilds"] += 1
        st["current"] = f"v{int(v[1:]) + 1}"
        _save(run, st)


def regrade(run: Path) -> dict:
    st = _load(run)
    if st["status"] != "done" or not st["history"]:
        raise SystemExit("regrade needs a finished run")
    v = st["history"][-1]["version"]
    grade = run / f"grade_{v}.json"
    k = 1
    while (run / f"grade_{v}.prev{k}.json").exists():
        k += 1
    grade.rename(run / f"grade_{v}.prev{k}.json")
    if (run / f"score_{v}.json").exists():
        (run / f"score_{v}.json").rename(run / f"score_{v}.prev{k}.json")
    st["done"].remove(f"grade_{v}")
    st["attempts"].pop(f"grade_{v}", None)
    st["current"], st["regrade"], st["status"] = v, v, "running"
    _save(run, st)
    return _resp(run, "ready", message=f"grade_{v}.json archived; call next to re-grade {v}.")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("init")
    p.add_argument("--url")
    p.add_argument("--resume", type=Path, default=DEFAULT_RESUME)
    p = sub.add_parser("next")
    p.add_argument("run_dir", type=Path)
    p.add_argument("--stop-after", choices=["parse", "grade"])
    p = sub.add_parser("regrade")
    p.add_argument("run_dir", type=Path)
    p = sub.add_parser("status")
    p.add_argument("run_dir", type=Path)
    args = ap.parse_args()

    if args.cmd == "init":
        if args.url and not re.match(r"^https?://", args.url):
            raise SystemExit("--url must start with http:// or https://")
        out = {"run_dir": _rel(init(args.url, args.resume))}
    elif args.cmd == "next":
        out = next_step(args.run_dir.resolve(), args.stop_after)
    elif args.cmd == "regrade":
        out = regrade(args.run_dir.resolve())
    else:
        out = _load(args.run_dir.resolve())
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 1 if out.get("action") == "failed" else 0


if __name__ == "__main__":
    sys.exit(main())
