#!/usr/bin/env python3
"""Mark scraped/ranked jobs as applied.

Records an application without running the full /apply workflow - for a job you
applied to directly on the employer's site, or one you just want to log. It does
two things:

  1. Appends an `applied` row to job_search_tracker.csv (the application record
     /scrape and /rank already exclude from future runs, and /html-report,
     /outcome, and /gmail-sync read). A job already in the tracker is left to
     /outcome to advance - this tool never downgrades or duplicates a row.
  2. Mirrors the status on the job's seen_jobs.json entry (status: applied), so
     the scrape state and the exported files agree.

Jobs are named by URL, by their seen_jobs.json key, or by a unique substring of
the URL or title (so "mark #3 as applied" resolves to that row's URL upstream).

Usage:
    python3 tools/mark_applied.py https://example.com/job/123
    python3 tools/mark_applied.py <url> <url> --channel linkedin --note "referred by A"
    python3 tools/mark_applied.py <url> --seen-only     # skip the tracker row
    python3 tools/mark_applied.py <url> --dry-run        # show what would change

Resolution is atomic: if any identifier is not found or is ambiguous, nothing is
written. The application date defaults to today; pass --date YYYY-MM-DD to override.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SEEN = ROOT / "job_scraper" / "seen_jobs.json"
DEFAULT_TRACKER = ROOT / "job_search_tracker.csv"

# The tracker CSV header, matching /outcome Step 1's canonical definition.
TRACKER_HEADER = [
    "date", "company", "sector", "role", "role_type", "channel", "status",
    "contact_person", "fit_rating", "notes", "cv_file", "cover_letter_file",
    "source", "deadline",
]

# Final tracker statuses (canonical + legacy space spellings), per /outcome's
# "Tracker status vocabulary". A row in a final state is never reopened here.
FINAL_STATUSES = {
    "hired", "rejected", "no_response", "offer_declined", "withdrawn",
    "no response", "offer declined",
}

APPLIED_STATUS = "applied"


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Mark scraped/ranked jobs as applied.")
    p.add_argument("identifiers", nargs="+",
                   help="Job URL(s), seen_jobs.json key(s), or a unique title/URL substring.")
    p.add_argument("--input", type=Path, default=DEFAULT_SEEN,
                   help=f"seen_jobs.json path (default: {DEFAULT_SEEN.relative_to(ROOT)}).")
    p.add_argument("--tracker", type=Path, default=DEFAULT_TRACKER,
                   help=f"tracker CSV path (default: {DEFAULT_TRACKER.relative_to(ROOT)}).")
    p.add_argument("--channel", default="direct",
                   help="Application channel for the tracker row (default: direct).")
    p.add_argument("--date", default=date.today().isoformat(),
                   help="Application date (YYYY-MM-DD, default: today).")
    p.add_argument("--note", default="",
                   help="Optional note appended to the tracker row's notes column.")
    p.add_argument("--seen-only", action="store_true",
                   help="Only update seen_jobs.json; do not write a tracker row.")
    p.add_argument("--dry-run", action="store_true",
                   help="Report what would change without writing any file.")
    return p.parse_args(argv)


def load_seen(path: Path) -> dict[str, Any]:
    if not path.exists():
        sys.exit(f"seen_jobs not found: {path} — run /scrape first, or pass --input")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        sys.exit(f"could not parse {path}: {exc}")
    if not isinstance(data, dict) or not isinstance(data.get("seen"), dict):
        sys.exit(f"{path} has no 'seen' object — nothing to mark")
    return data


def resolve(identifier: str, seen: dict[str, Any]) -> tuple[str | None, list[str]]:
    """Resolve one identifier to a single seen_jobs key. Returns (key, candidates):
    key is None when there is no unique match, and candidates lists the ambiguous
    keys (empty when nothing matched). Tiers: exact key, exact URL, unique substring."""
    if identifier in seen:
        return identifier, []
    url_hits = [k for k, r in seen.items()
                if isinstance(r, dict) and r.get("url") == identifier]
    if len(url_hits) == 1:
        return url_hits[0], []
    if len(url_hits) > 1:
        return None, url_hits
    needle = identifier.lower()
    sub_hits = [
        k for k, r in seen.items()
        if isinstance(r, dict)
        and needle in f"{r.get('url', '')} {r.get('title', '')}".lower()
    ]
    if len(sub_hits) == 1:
        return sub_hits[0], []
    return None, sub_hits


def read_tracker(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    """Return (header, rows). A missing file yields the canonical header and no
    rows; a legacy header missing 'deadline' gains it (per /outcome Step 1)."""
    if not path.exists():
        return list(TRACKER_HEADER), []
    with path.open(encoding="utf-8", newline="") as fh:
        reader = csv.reader(fh)
        try:
            header = next(reader)
        except StopIteration:
            return list(TRACKER_HEADER), []
        if "deadline" not in header:
            header = header + ["deadline"]
        rows = [dict(zip(header, r + [""] * (len(header) - len(r)))) for r in reader]
    return header, rows


def existing_row(rows: list[dict[str, str]], company: str, role: str) -> dict[str, str] | None:
    """A tracker row already matching company (and role) case-insensitively, if any."""
    c, r = company.strip().lower(), role.strip().lower()
    for row in rows:
        if row.get("company", "").strip().lower() == c and row.get("role", "").strip().lower() == r:
            return row
    return None


def tracker_row_for(rec: dict[str, Any], args: argparse.Namespace) -> dict[str, str]:
    note = f"marked applied {args.date}"
    if args.note:
        note = f"{note}; {args.note}"
    return {
        "date": args.date,
        "company": str(rec.get("company") or ""),
        "sector": "",
        "role": str(rec.get("title") or ""),
        "role_type": "",
        "channel": args.channel,
        "status": APPLIED_STATUS,
        "contact_person": "",
        "fit_rating": str(rec.get("rank_verdict") or rec.get("fit") or ""),
        "notes": note,
        "cv_file": "",
        "cover_letter_file": "",
        "source": str(rec.get("url") or ""),
        "deadline": str(rec.get("deadline") or ""),
    }


def write_tracker(path: Path, header: list[str], rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=header, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({col: row.get(col, "") for col in header})


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    data = load_seen(args.input)
    seen = data["seen"]

    # Resolve every identifier first; refuse to write anything if any is bad.
    resolved: dict[str, str] = {}  # identifier -> key
    errors: list[str] = []
    for ident in args.identifiers:
        key, candidates = resolve(ident, seen)
        if key is None:
            if candidates:
                errors.append(f"{ident!r} is ambiguous — matches {len(candidates)} jobs: "
                              + ", ".join(candidates[:5]) + ("…" if len(candidates) > 5 else ""))
            else:
                errors.append(f"{ident!r} matched no job in {args.input.name}")
        else:
            resolved[ident] = key
    if errors:
        for e in errors:
            print(f"error: {e}", file=sys.stderr)
        return 1

    keys = list(dict.fromkeys(resolved.values()))  # de-dup, preserve order
    header, rows = read_tracker(args.tracker)
    applied_lines: list[str] = []
    tracker_changed = False

    for key in keys:
        rec = seen[key]
        rec["status"] = APPLIED_STATUS
        rec["applied_date"] = args.date
        company = str(rec.get("company") or "")
        role = str(rec.get("title") or "")
        line = f"  {role or '(untitled)'} — {company or '(unknown company)'}"

        if args.seen_only:
            applied_lines.append(line + "  [seen_jobs only]")
            continue

        match = existing_row(rows, company, role)
        if match is None:
            rows.append(tracker_row_for(rec, args))
            tracker_changed = True
            applied_lines.append(line + "  [tracker row added]")
        else:
            status = match.get("status", "").strip().lower()
            if status in FINAL_STATUSES:
                applied_lines.append(line + f"  [tracker row already closed as '{status}', left as-is]")
            elif status == APPLIED_STATUS:
                applied_lines.append(line + "  [already applied in tracker, unchanged]")
            else:
                applied_lines.append(
                    line + f"  [already in tracker as '{status}', left for /outcome to advance]")

    if args.dry_run:
        print("Dry run — no files written. Would mark applied:")
        print("\n".join(applied_lines))
        return 0

    args.input.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    if tracker_changed:
        args.tracker.parent.mkdir(parents=True, exist_ok=True)
        write_tracker(args.tracker, header, rows)

    print(f"Marked {len(keys)} job(s) as applied:")
    print("\n".join(applied_lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
