#!/usr/bin/env python3
"""Export scraped/ranked jobs from seen_jobs.json to a readable HTML page and CSV.

/scrape and /rank present a short table in the terminal, but a full run can hold
far more jobs than fit on screen. This tool writes the complete list to files that
are easy to scan: a self-contained HTML page with a sortable, filterable table and
clickable links, and a CSV that opens in Excel or Google Sheets.

Dependency-free (Python standard library only) and self-contained: the HTML embeds
its own CSS and JavaScript, so it opens offline in any browser.

Usage:
    python3 tools/export_jobs.py                       # export everything live
    python3 tools/export_jobs.py --status new          # only newly scraped jobs
    python3 tools/export_jobs.py --status ranked --sort score
    python3 tools/export_jobs.py --max-age-days 14     # drop postings older than 14 days
    python3 tools/export_jobs.py --include-expired     # keep dead postings too
    python3 tools/export_jobs.py --group-by employment-type --target-types freelance,part-time
    python3 tools/export_jobs.py --top 50              # cap the file to the best 50
    python3 tools/export_jobs.py --formats html        # HTML only
    python3 tools/export_jobs.py --basename job-ranking --title "Job Ranking"

By default it reads job_scraper/seen_jobs.json and writes reports/job-matches.html
and reports/job-matches.csv (the reports/ folder is git-ignored). Jobs the pipeline
marked expired/dead are dropped unless --include-expired; pass --max-age-days to also
drop stale postings by date. Near-duplicate postings (the same role cross-posted on
several boards or under a company alias) are collapsed into one row unless --no-dedupe.
The written files contain the full filtered list; --top only caps the file when asked.
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import re
import sys
from datetime import date, datetime, timedelta
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
        "--include-expired", action="store_true",
        help="Include jobs marked expired/dead. By default they are dropped, since a "
        "closed posting is noise (pass --status expired to list only those).",
    )
    p.add_argument(
        "--max-age-days", type=int, default=None,
        help="Drop jobs whose posting date (or first-seen date) is older than N days. "
        "Omit for no age filter. Jobs with no known date are kept (never guessed).",
    )
    p.add_argument(
        "--no-dedupe", action="store_true",
        help="Keep near-duplicate postings (same role cross-posted on several boards "
        "or under a company alias). By default they are collapsed into one row.",
    )
    p.add_argument(
        "--group-by", default="none", choices=["none", "employment-type"],
        help="Split the output into separate lists. 'employment-type' groups jobs into "
        "Freelance / Part-time / Full-time / … sections (HTML) and orders the CSV by group.",
    )
    p.add_argument(
        "--target-types", default="",
        help="Comma-separated employment types to list first when grouping "
        "(e.g. freelance,part-time) — typically your configured employment_types.",
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
    """A short 'where this came from' label: the portal, or the extra source name.
    When this row absorbed cross-posted duplicates, the other sources are noted."""
    portal = rec.get("portal") or rec.get("source_name")
    source = rec.get("source")
    if portal and source and source != "cli":
        label = f"{portal} ({source})"
    else:
        label = portal or source or "—"
    others = rec.get("_dupe_sources")
    if others:
        label = f"{label} +{len(others)} ({', '.join(others)})"
    return label


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


def wanted_statuses(statuses: str) -> set[str] | None:
    """The requested status set, or None for 'all'."""
    if statuses.strip().lower() == "all":
        return None
    return {s.strip().lower() for s in statuses.split(",") if s.strip()}


def filter_jobs(jobs: list[dict[str, Any]], statuses: str) -> list[dict[str, Any]]:
    wanted = wanted_statuses(statuses)
    if wanted is None:
        return jobs
    return [j for j in jobs if str(j.get("status", "")).lower() in wanted]


def drop_expired(jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Remove jobs the pipeline has marked dead - a closed posting is noise."""
    return [j for j in jobs if str(j.get("status", "")).lower() != "expired"]


# Company-name tokens that carry no identity - legal-entity suffixes and a few
# generic corporate words. Dropped before comparing so "Kotak Mahindra Bank" and
# "Kotak" (or "IDC Technologies Pte Ltd" and "IDC Technologies") can match.
COMPANY_STOPWORDS = frozenset({
    "ltd", "limited", "inc", "incorporated", "llc", "plc", "corp", "corporation",
    "co", "company", "gmbh", "ag", "sa", "srl", "bv", "nv", "oy", "ab", "aps",
    "as", "pte", "pty", "llp", "lp", "kk", "group", "holdings", "holding", "the",
})


def _tokens(text: str) -> list[str]:
    """Lowercase alphanumeric tokens; punctuation and separators become breaks."""
    return re.findall(r"[a-z0-9]+", text.lower())


def company_tokens(name: Any) -> frozenset[str]:
    """Identity tokens of a company name, with legal/generic suffixes removed.
    Empty when nothing meaningful remains (then it never matches another)."""
    if not name:
        return frozenset()
    return frozenset(t for t in _tokens(str(name)) if t not in COMPANY_STOPWORDS)


def normalize_title(title: Any) -> str:
    """A title reduced for comparison: parentheticals dropped, punctuation folded
    to single spaces, lowercased. Conservative - it does not reorder words - so it
    merges 'Senior iOS Dev' with 'Senior iOS Dev (Remote)' but not unrelated roles."""
    if not title:
        return ""
    text = re.sub(r"\([^)]*\)", " ", str(title))
    return " ".join(_tokens(text))


def company_related(a: frozenset[str], b: frozenset[str]) -> bool:
    """True when two company token sets plausibly name the same employer: equal,
    or one is a subset of the other ('kotak' ⊆ 'kotak mahindra'). Empty sets never
    match - absence of a name is not evidence of sameness."""
    if not a or not b:
        return False
    return a == b or a <= b or b <= a


def _completeness(rec: dict[str, Any]) -> int:
    """How many of the display fields are filled - used to pick the record to keep
    from a duplicate cluster when scores tie."""
    return sum(1 for _, key in COLUMNS if key not in ("_source",) and rec.get(key))


def dedupe(jobs: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """Collapse near-duplicate postings (same normalized title + related company,
    e.g. the same job cross-posted on two boards or under a company alias) into one
    row, preserving first-appearance order. The kept row is the highest-scored /
    most-complete of the cluster; the others' sources are recorded on it for a note.
    Returns (deduped, merged_count)."""
    n = len(jobs)
    keys = [(normalize_title(j.get("title")), company_tokens(j.get("company"))) for j in jobs]

    # Union-find over records that share a normalized title and a related company.
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        parent[find(i)] = find(j)

    by_title: dict[str, list[int]] = {}
    for i, (tkey, _) in enumerate(keys):
        if tkey:  # an empty title carries no identity - never merge on it
            by_title.setdefault(tkey, []).append(i)
    for idxs in by_title.values():
        for a in range(len(idxs)):
            for b in range(a + 1, len(idxs)):
                if company_related(keys[idxs[a]][1], keys[idxs[b]][1]):
                    union(idxs[a], idxs[b])

    clusters: dict[int, list[int]] = {}
    for i in range(n):
        clusters.setdefault(find(i), []).append(i)

    def score(rec: dict[str, Any]) -> float:
        s = rec.get("rank_score")
        return s if isinstance(s, (int, float)) else -1

    out: list[dict[str, Any]] = []
    merged = 0
    emitted: set[int] = set()
    for i in range(n):  # iterate in original order so output is stable
        root = find(i)
        if root in emitted:
            continue
        emitted.add(root)
        members = clusters[root]
        if len(members) == 1:
            out.append(jobs[i])
            continue
        # Keep the best member (highest score, then most complete, then earliest).
        best = max(members, key=lambda m: (score(jobs[m]), _completeness(jobs[m]), -m))
        kept = dict(jobs[best])
        others = [m for m in members if m != best]
        kept["_dupe_count"] = len(others)
        kept["_dupe_sources"] = sorted(
            {source_label(jobs[m]) for m in others} - {source_label(jobs[best])}
        )
        out.append(kept)
        merged += len(others)
    return out, merged


def freshness_date(rec: dict[str, Any]) -> date | None:
    """The date to age a job by: its posting date, else when it was first seen.
    Returns None when neither parses as YYYY-MM-DD (never guessed)."""
    for key in ("posted_date", "first_seen"):
        raw = rec.get(key)
        if not raw:
            continue
        try:
            return date.fromisoformat(str(raw)[:10])
        except ValueError:
            continue
    return None


def filter_by_age(jobs: list[dict[str, Any]], max_age_days: int) -> list[dict[str, Any]]:
    """Drop jobs older than max_age_days. A job with no parseable date is kept -
    absence of a date is not evidence the posting is stale."""
    cutoff = date.today() - timedelta(days=max_age_days)
    kept = []
    for j in jobs:
        d = freshness_date(j)
        if d is None or d >= cutoff:
            kept.append(j)
    return kept


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
    if key == "employment_type":
        # Show the canonical label ("Part-time"), matching the group headings,
        # rather than a portal's raw spelling ("part_time").
        return _canon_employment(rec.get(key)) or ""
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


# Employment-type spellings (portal-native and shared-vocabulary) folded to one
# display label per group, so freelance/part-time roles list apart from full-time.
EMPLOYMENT_CANON = {
    "full-time": "Full-time", "full_time": "Full-time", "fulltime": "Full-time",
    "permanent": "Full-time", "perm": "Full-time",
    "part-time": "Part-time", "part_time": "Part-time", "parttime": "Part-time",
    "contract": "Contract", "contractor": "Contract",
    "freelance": "Freelance", "freelancer": "Freelance",
    "temporary": "Temporary", "temp": "Temporary",
    "internship": "Internship", "intern": "Internship",
}
UNSPECIFIED_GROUP = "Unspecified"
# Non-target groups fall back to this order; Unspecified always sorts last.
DEFAULT_GROUP_ORDER = [
    "Freelance", "Part-time", "Contract", "Temporary", "Internship", "Full-time",
    UNSPECIFIED_GROUP,
]


def _canon_employment(value: Any) -> str | None:
    raw = str(value or "").strip().lower()
    if not raw:
        return None
    return EMPLOYMENT_CANON.get(raw, raw.title())


def employment_group(rec: dict[str, Any]) -> str:
    """The employment-type group label for a job, or 'Unspecified' when unknown."""
    return _canon_employment(rec.get("employment_type")) or UNSPECIFIED_GROUP


def ordered_groups(
    jobs: list[dict[str, Any]], target_types: list[str] | None
) -> list[tuple[str, list[dict[str, Any]]]]:
    """Partition jobs by employment-type group, ordered: the configured target
    types first (in the order given), then the default order, then any leftover
    labels alphabetically, with 'Unspecified' always last. Empty groups drop out."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for j in jobs:
        groups.setdefault(employment_group(j), []).append(j)

    order: list[str] = []
    for t in target_types or []:
        lab = _canon_employment(t)
        if lab and lab in groups and lab not in order and lab != UNSPECIFIED_GROUP:
            order.append(lab)
    for lab in DEFAULT_GROUP_ORDER:
        if lab in groups and lab not in order and lab != UNSPECIFIED_GROUP:
            order.append(lab)
    for lab in sorted(groups):
        if lab not in order and lab != UNSPECIFIED_GROUP:
            order.append(lab)
    if UNSPECIFIED_GROUP in groups:
        order.append(UNSPECIFIED_GROUP)
    return [(lab, groups[lab]) for lab in order]


def _header_cells() -> str:
    cells = "".join(
        f'<th data-key="{html.escape(key)}">{html.escape(header)}</th>'
        for header, key in COLUMNS
        if key != "url"
    )
    return cells + "<th>Link</th>"


def _job_row_html(rec: dict[str, Any], index: int) -> str:
    cells = [f'<td class="num">{index}</td>']
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
    url = cell_value(rec, "url")
    if url:
        href = html.escape(url, quote=True)
        cells.append(f'<td><a class="open" href="{href}" target="_blank" rel="noopener">open ↗</a></td>')
    else:
        cells.append("<td>—</td>")

    detail = ""
    strengths = rec.get("strengths") or []
    gaps = rec.get("gaps") or []
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
            + "".join(parts) + "</div></td></tr>"
        )
    return f'<tr class="job">{"".join(cells)}</tr>{detail}'


def _table_html(jobs: list[dict[str, Any]], start_index: int, table_id: str | None = None) -> tuple[str, int]:
    """A full <table> for a job list, numbering rows from start_index. Returns
    (html, next_index) so grouped sections keep a single running numbering."""
    idx = start_index
    rows = []
    for rec in jobs:
        rows.append(_job_row_html(rec, idx))
        idx += 1
    rows_html = "\n".join(rows) or (
        f'<tr><td colspan="{len(COLUMNS) + 1}" class="empty">No jobs to show.</td></tr>'
    )
    id_attr = f' id="{table_id}"' if table_id else ""
    table = (
        f'<table class="jobs"{id_attr}><thead><tr><th>#</th>{_header_cells()}</tr></thead>'
        f'<tbody>\n{rows_html}\n</tbody></table>'
    )
    return table, idx


def render_html(
    jobs: list[dict[str, Any]],
    title: str,
    source: Path,
    group_by: str | None = None,
    target_types: list[str] | None = None,
) -> str:
    generated = datetime.now().strftime("%Y-%m-%d %H:%M")

    if group_by == "employment-type":
        sections = []
        idx = 1
        for label, gjobs in ordered_groups(jobs, target_types):
            table, idx = _table_html(gjobs, idx)
            sections.append(
                f'<section class="group-section">'
                f'<h2 class="group">{html.escape(label)} <span class="gcount">({len(gjobs)})</span></h2>'
                f'<div class="table-wrap">{table}</div></section>'
            )
        content = "\n".join(sections) or '<p class="empty">No jobs to show.</p>'
    else:
        table, _ = _table_html(jobs, 1, table_id="jobs")
        content = f'<div class="table-wrap">{table}</div>'

    try:
        source_rel = source.resolve().relative_to(ROOT)
    except ValueError:
        source_rel = source

    return _HTML_TEMPLATE.format(
        title=html.escape(title),
        generated=generated,
        count=len(jobs),
        source=html.escape(str(source_rel)),
        content=content,
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
  h2.group {{ font-size: 1.05rem; margin: 1.4rem 0 .45rem; }}
  h2.group .gcount {{ color: #888; font-weight: 400; font-size: .85rem; }}
  .group-section {{ margin-bottom: 1rem; }}
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
{content}
<script>
  // Client-side filter + per-table column sort, across one or more tables (the
  // page may group jobs by employment type into separate sections). Detail rows
  // (strengths/gaps) move with their job row so sort/filter keep them paired.
  const tables = Array.from(document.querySelectorAll("table.jobs"));
  const q = document.getElementById("q");
  const statusSel = document.getElementById("statusFilter");

  function rowsOf(tbody) {{
    const out = [];
    let cur = null;
    for (const tr of tbody.rows) {{
      if (tr.classList.contains("job")) {{ cur = {{ job: tr, detail: null }}; out.push(cur); }}
      else if (tr.classList.contains("detail") && cur) {{ cur.detail = tr; }}
    }}
    return out;
  }}
  function statusIndex(t) {{
    return Array.from(t.tHead.rows[0].cells).findIndex(c => c.textContent.trim() === "Status");
  }}

  // Populate the status dropdown from every table's Status column.
  (function initStatuses() {{
    const seen = new Set();
    for (const t of tables) {{
      const idx = statusIndex(t);
      if (idx < 0) continue;
      for (const {{ job }} of rowsOf(t.tBodies[0])) {{
        const v = job.cells[idx] ? job.cells[idx].textContent.trim() : "";
        if (v && v !== "—") seen.add(v);
      }}
    }}
    [...seen].sort().forEach(v => {{
      const o = document.createElement("option"); o.value = v.toLowerCase(); o.textContent = v;
      statusSel.appendChild(o);
    }});
  }})();

  function applyFilter() {{
    const term = q.value.toLowerCase();
    const status = statusSel.value;
    for (const t of tables) {{
      const idx = statusIndex(t);
      let visible = 0;
      for (const {{ job, detail }} of rowsOf(t.tBodies[0])) {{
        const text = job.textContent.toLowerCase();
        const rowStatus = idx >= 0 && job.cells[idx] ? job.cells[idx].textContent.trim().toLowerCase() : "";
        const show = text.includes(term) && (!status || rowStatus === status);
        job.style.display = show ? "" : "none";
        if (detail) detail.style.display = show ? "" : "none";
        if (show) visible++;
      }}
      const section = t.closest(".group-section");
      if (section) section.style.display = visible ? "" : "none";
    }}
  }}
  q.addEventListener("input", applyFilter);
  statusSel.addEventListener("change", applyFilter);

  for (const t of tables) {{
    const state = {{ col: -1, asc: true }};
    t.tHead.rows[0].addEventListener("click", (e) => {{
      const th = e.target.closest("th");
      if (!th) return;
      const col = th.cellIndex;
      state.asc = state.col === col ? !state.asc : true;
      state.col = col;
      const tbody = t.tBodies[0];
      const pairs = rowsOf(tbody);
      pairs.sort((a, b) => {{
        const av = (a.job.cells[col] ? a.job.cells[col].textContent : "").trim();
        const bv = (b.job.cells[col] ? b.job.cells[col].textContent : "").trim();
        const an = parseFloat(av), bn = parseFloat(bv);
        let cmp;
        if (!isNaN(an) && !isNaN(bn)) cmp = an - bn;
        else cmp = av.localeCompare(bv, undefined, {{ numeric: true }});
        return state.asc ? cmp : -cmp;
      }});
      for (const {{ job, detail }} of pairs) {{
        tbody.appendChild(job);
        if (detail) tbody.appendChild(detail);
      }}
    }});
  }}
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
    total = len(jobs)
    jobs = filter_jobs(jobs, args.status)

    # Drop dead postings unless the caller opted in, or explicitly asked for only
    # expired ones (--status expired). A closed job is noise in a matches export.
    wanted = wanted_statuses(args.status)
    asked_only_expired = wanted is not None and wanted == {"expired"}
    dropped_expired = 0
    if not args.include_expired and not asked_only_expired:
        before = len(jobs)
        jobs = drop_expired(jobs)
        dropped_expired = before - len(jobs)

    # Drop stale postings past the freshness window (by posting date, else
    # first-seen). Undated jobs are kept - absence of a date is not staleness.
    dropped_stale = 0
    if args.max_age_days is not None:
        before = len(jobs)
        jobs = filter_by_age(jobs, args.max_age_days)
        dropped_stale = before - len(jobs)

    # Collapse near-duplicate postings (same role on several boards / company alias).
    merged_dupes = 0
    if not args.no_dedupe:
        jobs, merged_dupes = dedupe(jobs)

    key_fn, reverse = sort_key(args.sort)
    jobs.sort(key=key_fn, reverse=reverse)
    jobs = cap_top(jobs, args.top)

    group_by = None if args.group_by == "none" else args.group_by
    target_types = [t.strip() for t in args.target_types.split(",") if t.strip()]
    # When grouping, lay the CSV out group-by-group too (same order the HTML
    # sections use), so both files present freelance/part-time apart from full-time.
    if group_by == "employment-type":
        jobs = [rec for _, grp in ordered_groups(jobs, target_types) for rec in grp]

    args.out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    if "csv" in formats:
        csv_path = args.out_dir / f"{args.basename}.csv"
        write_csv(jobs, csv_path)
        written.append(csv_path)
    if "html" in formats:
        html_path = args.out_dir / f"{args.basename}.html"
        html_path.write_text(
            render_html(jobs, args.title, args.input, group_by, target_types),
            encoding="utf-8",
        )
        written.append(html_path)

    notes = []
    if dropped_expired:
        notes.append(f"dropped {dropped_expired} expired")
    if dropped_stale:
        notes.append(f"dropped {dropped_stale} older than {args.max_age_days}d")
    if merged_dupes:
        notes.append(f"merged {merged_dupes} duplicate(s)")
    suffix = f" (from {total}; {'; '.join(notes)})" if notes else ""
    print(f"Exported {len(jobs)} job(s){suffix}:")
    for path in written:
        try:
            print(f"  {path.resolve().relative_to(ROOT)}")
        except ValueError:
            print(f"  {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
