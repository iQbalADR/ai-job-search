#!/usr/bin/env python3
"""Export scraped/ranked jobs from seen_jobs.json to a readable HTML page and CSV.

/scrape and /rank present a short table in the terminal, but a full run can hold
far more jobs than fit on screen. This tool writes the complete list to files that
are easy to scan: a self-contained HTML page with a sortable, filterable table and
clickable links, and a CSV that opens in Excel or Google Sheets.

Dependency-free (Python standard library only) and self-contained: the HTML embeds
its own CSS and JavaScript, so it opens offline in any browser.

Usage:
    python3 tools/export_jobs.py                       # export everything
    python3 tools/export_jobs.py --status new          # only newly scraped jobs
    python3 tools/export_jobs.py --status ranked --sort score
    python3 tools/export_jobs.py --top 50              # cap the file to the best 50
    python3 tools/export_jobs.py --formats html        # HTML only
    python3 tools/export_jobs.py --basename job-ranking --title "Job Ranking"

By default it reads job_scraper/seen_jobs.json and writes reports/job-matches.html
and reports/job-matches.csv (the reports/ folder is git-ignored). The written files
always contain the full filtered list; --top only caps the file when you ask it to.
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_INPUT = ROOT / "job_scraper" / "seen_jobs.json"
DEFAULT_OUTDIR = ROOT / "reports"

# Columns shown in both the HTML table and the CSV, in order. Each is
# (header, key-in-record). `_derived` keys are computed in record_row().
COLUMNS: list[tuple[str, str]] = [
    ("Score", "rank_score"),
    ("Verdict", "rank_verdict"),
    ("Fit", "fit"),
    ("Status", "status"),
    ("Title", "title"),
    ("Company", "company"),
    ("Location", "location"),
    ("Type", "employment_type"),
    ("Posted", "posted_date"),
    ("Deadline", "deadline"),
    ("Source", "_source"),
    ("URL", "url"),
]

# Sort strategies: name -> (key function, reverse). Missing values sort last.
FIT_ORDER = {"high": 3, "medium": 2, "low": 1}


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Export seen_jobs.json to a readable HTML page and CSV.",
    )
    p.add_argument(
        "--input", type=Path, default=DEFAULT_INPUT,
        help=f"Path to seen_jobs.json (default: {DEFAULT_INPUT.relative_to(ROOT)})",
    )
    p.add_argument(
        "--out-dir", type=Path, default=DEFAULT_OUTDIR,
        help=f"Directory to write into (default: {DEFAULT_OUTDIR.relative_to(ROOT)})",
    )
    p.add_argument(
        "--basename", default="job-matches",
        help="Base filename for the output files (default: job-matches)",
    )
    p.add_argument(
        "--title", default="Job Matches",
        help="Heading shown at the top of the HTML page (default: Job Matches)",
    )
    p.add_argument(
        "--status", default="all",
        help="Comma-separated statuses to include (new, ranked, skipped, expired) "
        "or 'all' (default: all).",
    )
    p.add_argument(
        "--sort", default="score", choices=["score", "fit", "date", "deadline", "company"],
        help="Sort order (default: score, which falls back to fit for unranked jobs).",
    )
    p.add_argument(
        "--top", default="all",
        help="Cap the output to the best N jobs, or 'all' (default: all).",
    )
    p.add_argument(
        "--formats", default="html,csv",
        help="Comma-separated output formats: html, csv (default: html,csv).",
    )
    return p.parse_args(argv)


def load_jobs(path: Path) -> list[dict[str, Any]]:
    """Read seen_jobs.json and return a list of job records with their keys attached."""
    if not path.exists():
        sys.exit(f"input not found: {path} — run /scrape first, or pass --input")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        sys.exit(f"could not parse {path}: {exc}")
    seen = data.get("seen", {}) if isinstance(data, dict) else {}
    jobs = []
    for key, rec in seen.items():
        if not isinstance(rec, dict):
            continue
        row = dict(rec)
        row.setdefault("_key", key)
        jobs.append(row)
    return jobs


def source_label(rec: dict[str, Any]) -> str:
    """A short 'where this came from' label: the portal, or the extra source name."""
    portal = rec.get("portal") or rec.get("source_name")
    source = rec.get("source")
    if portal and source and source != "cli":
        return f"{portal} ({source})"
    return portal or source or "—"


def sort_key(strategy: str):
    def score_key(r: dict[str, Any]) -> tuple:
        score = r.get("rank_score")
        score = score if isinstance(score, (int, float)) else -1
        fit = FIT_ORDER.get(str(r.get("fit", "")).lower(), 0)
        return (score, fit)

    def fit_key(r: dict[str, Any]) -> tuple:
        return (FIT_ORDER.get(str(r.get("fit", "")).lower(), 0),)

    def date_key(r: dict[str, Any]) -> tuple:
        return (str(r.get("posted_date") or r.get("first_seen") or ""),)

    def deadline_key(r: dict[str, Any]) -> tuple:
        # Jobs with a deadline first (soonest first); undated jobs sort last.
        d = str(r.get("deadline") or "")
        return (d == "", d)

    def company_key(r: dict[str, Any]) -> tuple:
        return (str(r.get("company") or "").lower(),)

    strategies = {
        "score": (score_key, True),
        "fit": (fit_key, True),
        "date": (date_key, True),
        "deadline": (deadline_key, False),
        "company": (company_key, False),
    }
    return strategies[strategy]


def filter_jobs(jobs: list[dict[str, Any]], statuses: str) -> list[dict[str, Any]]:
    if statuses.strip().lower() == "all":
        return jobs
    wanted = {s.strip().lower() for s in statuses.split(",") if s.strip()}
    return [j for j in jobs if str(j.get("status", "")).lower() in wanted]


def cap_top(jobs: list[dict[str, Any]], top: str) -> list[dict[str, Any]]:
    if top.strip().lower() == "all":
        return jobs
    try:
        n = int(top)
    except ValueError:
        sys.exit(f"--top must be a number or 'all', got {top!r}")
    return jobs[: max(0, n)]


def cell_value(rec: dict[str, Any], key: str) -> str:
    if key == "_source":
        return source_label(rec)
    val = rec.get(key)
    if val is None or val == "":
        return ""
    return str(val)


def write_csv(jobs: list[dict[str, Any]], path: Path) -> None:
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow([header for header, _ in COLUMNS])
        for rec in jobs:
            writer.writerow([cell_value(rec, key) for _, key in COLUMNS])


def badge_class(rec: dict[str, Any]) -> str:
    """CSS class for the row's fit/score, used to tint the fit/verdict cell."""
    score = rec.get("rank_score")
    if isinstance(score, (int, float)):
        if score >= 75:
            return "b-high"
        if score >= 60:
            return "b-medium"
        if score >= 30:
            return "b-low"
        return "b-none"
    fit = str(rec.get("fit", "")).lower()
    return {"high": "b-high", "medium": "b-medium", "low": "b-low"}.get(fit, "")


def render_html(jobs: list[dict[str, Any]], title: str, source: Path) -> str:
    generated = datetime.now().strftime("%Y-%m-%d %H:%M")
    header_cells = "".join(
        f'<th data-key="{html.escape(key)}">{html.escape(header)}</th>'
        for header, key in COLUMNS
        if key != "url"
    )

    body_rows = []
    for i, rec in enumerate(jobs, 1):
        cells = [f'<td class="num">{i}</td>']
        for header, key in COLUMNS:
            if key == "url":
                continue
            raw = cell_value(rec, key)
            if key in ("rank_score", "rank_verdict", "fit"):
                cls = badge_class(rec)
                text = html.escape(raw) if raw else "—"
                cells.append(f'<td><span class="badge {cls}">{text}</span></td>')
            elif key == "title":
                url = cell_value(rec, "url")
                text = html.escape(raw) or "(untitled)"
                if url:
                    href = html.escape(url, quote=True)
                    cells.append(
                        f'<td class="title"><a href="{href}" target="_blank" rel="noopener">{text}</a></td>'
                    )
                else:
                    cells.append(f'<td class="title">{text}</td>')
            else:
                cells.append(f"<td>{html.escape(raw) or '—'}</td>")
        # A dedicated Open link column so the URL is always one click away.
        url = cell_value(rec, "url")
        if url:
            href = html.escape(url, quote=True)
            cells.append(f'<td><a class="open" href="{href}" target="_blank" rel="noopener">open ↗</a></td>')
        else:
            cells.append("<td>—</td>")

        strengths = rec.get("strengths") or []
        gaps = rec.get("gaps") or []
        detail = ""
        if strengths or gaps:
            parts = []
            if strengths:
                items = "".join(f"<li>{html.escape(str(s))}</li>" for s in strengths)
                parts.append(f'<div class="strengths"><b>Strengths</b><ul>{items}</ul></div>')
            if gaps:
                items = "".join(f"<li>{html.escape(str(g))}</li>" for g in gaps)
                parts.append(f'<div class="gaps"><b>Gaps</b><ul>{items}</ul></div>')
            colspan = len(COLUMNS) + 1  # +1 for the row-number column
            detail = (
                f'<tr class="detail"><td colspan="{colspan}"><div class="detail-wrap">'
                + "".join(parts)
                + "</div></td></tr>"
            )
        body_rows.append(f'<tr class="job">{"".join(cells)}</tr>{detail}')

    rows_html = "\n".join(body_rows) or (
        f'<tr><td colspan="{len(COLUMNS) + 1}" class="empty">No jobs to show.</td></tr>'
    )

    try:
        source_rel = source.resolve().relative_to(ROOT)
    except ValueError:
        source_rel = source

    return _HTML_TEMPLATE.format(
        title=html.escape(title),
        generated=generated,
        count=len(jobs),
        source=html.escape(str(source_rel)),
        header_cells=header_cells + "<th>Link</th>",
        rows=rows_html,
    )


_HTML_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  :root {{ color-scheme: light dark; }}
  * {{ box-sizing: border-box; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
         margin: 0; padding: 1.5rem; background: #f6f7f9; color: #1c1e21; }}
  h1 {{ font-size: 1.4rem; margin: 0 0 .25rem; }}
  .meta {{ color: #666; font-size: .85rem; margin-bottom: 1rem; }}
  .controls {{ display: flex; gap: .75rem; flex-wrap: wrap; margin-bottom: .75rem; }}
  .controls input, .controls select {{ padding: .45rem .6rem; border: 1px solid #ccc;
         border-radius: 6px; font-size: .9rem; background: #fff; color: inherit; }}
  #q {{ flex: 1 1 240px; }}
  .table-wrap {{ overflow-x: auto; background: #fff; border-radius: 8px;
         box-shadow: 0 1px 3px rgba(0,0,0,.08); }}
  table {{ border-collapse: collapse; width: 100%; font-size: .88rem; }}
  th, td {{ padding: .55rem .7rem; text-align: left; border-bottom: 1px solid #eee;
         white-space: nowrap; }}
  th {{ position: sticky; top: 0; background: #fafafa; cursor: pointer; user-select: none;
         font-weight: 600; }}
  th:hover {{ background: #f0f0f0; }}
  td.title {{ white-space: normal; min-width: 260px; }}
  td.num {{ color: #999; }}
  a {{ color: #1a56db; text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
  a.open {{ font-size: .8rem; }}
  .badge {{ display: inline-block; padding: .1rem .45rem; border-radius: 999px;
         font-size: .8rem; font-weight: 600; }}
  .b-high {{ background: #d5f5e3; color: #1e7a46; }}
  .b-medium {{ background: #fdf2d0; color: #8a6d00; }}
  .b-low {{ background: #fde2e1; color: #a23b36; }}
  .b-none {{ background: #eee; color: #666; }}
  tr.detail td {{ background: #fbfbfd; white-space: normal; padding-top: 0; }}
  .detail-wrap {{ display: flex; gap: 2rem; flex-wrap: wrap; font-size: .84rem; }}
  .detail-wrap ul {{ margin: .2rem 0 .4rem 1rem; padding: 0; }}
  .strengths b {{ color: #1e7a46; }}
  .gaps b {{ color: #a23b36; }}
  .empty {{ text-align: center; color: #888; padding: 2rem; }}
  @media (prefers-color-scheme: dark) {{
    body {{ background: #18191a; color: #e4e6eb; }}
    .table-wrap {{ background: #242526; box-shadow: none; }}
    table td, table th {{ border-color: #3a3b3c; }}
    th {{ background: #2a2b2c; }} th:hover {{ background: #323334; }}
    .controls input, .controls select {{ background: #242526; border-color: #444; }}
    tr.detail td {{ background: #1f2021; }}
    a {{ color: #6ea8fe; }}
  }}
</style>
</head>
<body>
  <h1>{title}</h1>
  <div class="meta">{count} jobs · generated {generated} · source: {source}</div>
  <div class="controls">
    <input id="q" type="search" placeholder="Filter by any text (title, company, location, type…)">
    <select id="statusFilter"><option value="">All statuses</option></select>
  </div>
  <div class="table-wrap">
    <table id="jobs">
      <thead><tr><th>#</th>{header_cells}</tr></thead>
      <tbody>
{rows}
      </tbody>
    </table>
  </div>
<script>
  // Client-side filter + column sort. The detail rows (strengths/gaps) move with
  // their job row so sorting and filtering keep them paired.
  const table = document.getElementById("jobs");
  const tbody = table.tBodies[0];
  const q = document.getElementById("q");
  const statusSel = document.getElementById("statusFilter");

  function jobRows() {{
    const out = [];
    let cur = null;
    for (const tr of tbody.rows) {{
      if (tr.classList.contains("job")) {{ cur = {{ job: tr, detail: null }}; out.push(cur); }}
      else if (tr.classList.contains("detail") && cur) {{ cur.detail = tr; }}
    }}
    return out;
  }}

  // Populate the status dropdown from the Status column.
  (function initStatuses() {{
    const idx = [...table.tHead.rows[0].cells].findIndex(c => c.textContent.trim() === "Status");
    if (idx < 0) return;
    const seen = new Set();
    for (const {{ job }} of jobRows()) {{
      const v = job.cells[idx] ? job.cells[idx].textContent.trim() : "";
      if (v && v !== "—") seen.add(v);
    }}
    [...seen].sort().forEach(v => {{
      const o = document.createElement("option"); o.value = v.toLowerCase(); o.textContent = v;
      statusSel.appendChild(o);
    }});
  }})();

  function applyFilter() {{
    const term = q.value.toLowerCase();
    const status = statusSel.value;
    const statusIdx = [...table.tHead.rows[0].cells].findIndex(c => c.textContent.trim() === "Status");
    for (const {{ job, detail }} of jobRows()) {{
      const text = job.textContent.toLowerCase();
      const rowStatus = statusIdx >= 0 && job.cells[statusIdx]
        ? job.cells[statusIdx].textContent.trim().toLowerCase() : "";
      const show = text.includes(term) && (!status || rowStatus === status);
      job.style.display = show ? "" : "none";
      if (detail) detail.style.display = show ? "" : "none";
    }}
  }}
  q.addEventListener("input", applyFilter);
  statusSel.addEventListener("change", applyFilter);

  let sortState = {{ col: -1, asc: true }};
  table.tHead.rows[0].addEventListener("click", (e) => {{
    const th = e.target.closest("th");
    if (!th) return;
    const col = th.cellIndex;
    sortState.asc = sortState.col === col ? !sortState.asc : true;
    sortState.col = col;
    const pairs = jobRows();
    pairs.sort((a, b) => {{
      const av = (a.job.cells[col] ? a.job.cells[col].textContent : "").trim();
      const bv = (b.job.cells[col] ? b.job.cells[col].textContent : "").trim();
      const an = parseFloat(av), bn = parseFloat(bv);
      let cmp;
      if (!isNaN(an) && !isNaN(bn)) cmp = an - bn;
      else cmp = av.localeCompare(bv, undefined, {{ numeric: true }});
      return sortState.asc ? cmp : -cmp;
    }});
    for (const {{ job, detail }} of pairs) {{
      tbody.appendChild(job);
      if (detail) tbody.appendChild(detail);
    }}
  }});
</script>
</body>
</html>
"""


def main(argv: list[str]) -> int:
    args = parse_args(argv)

    formats = {f.strip().lower() for f in args.formats.split(",") if f.strip()}
    unknown = formats - {"html", "csv"}
    if unknown:
        sys.exit(f"unknown format(s): {', '.join(sorted(unknown))} — supported: html, csv")

    jobs = load_jobs(args.input)
    jobs = filter_jobs(jobs, args.status)
    key_fn, reverse = sort_key(args.sort)
    jobs.sort(key=key_fn, reverse=reverse)
    jobs = cap_top(jobs, args.top)

    args.out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    if "csv" in formats:
        csv_path = args.out_dir / f"{args.basename}.csv"
        write_csv(jobs, csv_path)
        written.append(csv_path)
    if "html" in formats:
        html_path = args.out_dir / f"{args.basename}.html"
        html_path.write_text(render_html(jobs, args.title, args.input), encoding="utf-8")
        written.append(html_path)

    print(f"Exported {len(jobs)} job(s):")
    for path in written:
        try:
            print(f"  {path.resolve().relative_to(ROOT)}")
        except ValueError:
            print(f"  {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
