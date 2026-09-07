"""Tests for tools/mark_applied.py — resolving jobs and recording applications.

Covers identifier resolution (key / URL / substring / ambiguous / missing), the
tracker header handling and match-then-append behavior, and the end-to-end main()
that mirrors status onto seen_jobs.json and never duplicates or downgrades a
tracker row.
"""

import csv
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from datetime import date
from pathlib import Path

from tools.mark_applied import (
    APPLIED_STATUS,
    TRACKER_HEADER,
    existing_row,
    main,
    read_tracker,
    resolve,
    tracker_row_for,
)


def seen_dict():
    return {
        "https://a.example/job/1": {
            "title": "Senior iOS Engineer", "company": "Acme",
            "url": "https://a.example/job/1", "status": "new", "fit": "high",
            "deadline": "2026-09-20",
        },
        "co_title_key": {
            "title": "Backend Engineer", "company": "Globex",
            "url": "https://globex.example/be", "status": "ranked",
            "rank_verdict": "Strong Fit",
        },
    }


class ResolveTests(unittest.TestCase):
    def setUp(self):
        self.seen = seen_dict()

    def test_exact_key(self):
        self.assertEqual(resolve("co_title_key", self.seen), ("co_title_key", []))

    def test_exact_url(self):
        self.assertEqual(resolve("https://a.example/job/1", self.seen),
                         ("https://a.example/job/1", []))

    def test_unique_substring_of_title(self):
        self.assertEqual(resolve("Backend", self.seen)[0], "co_title_key")

    def test_unique_substring_of_url(self):
        self.assertEqual(resolve("globex.example/be", self.seen)[0], "co_title_key")

    def test_not_found(self):
        key, candidates = resolve("nope", self.seen)
        self.assertIsNone(key)
        self.assertEqual(candidates, [])

    def test_ambiguous_substring(self):
        seen = {
            "k1": {"title": "iOS Engineer", "company": "A", "url": "https://x/1"},
            "k2": {"title": "iOS Developer", "company": "B", "url": "https://x/2"},
        }
        key, candidates = resolve("ios", seen)
        self.assertIsNone(key)
        self.assertEqual(len(candidates), 2)


class TrackerHeaderTests(unittest.TestCase):
    def test_missing_file_yields_canonical_header(self):
        with tempfile.TemporaryDirectory() as d:
            header, rows = read_tracker(Path(d) / "nope.csv")
        self.assertEqual(header, TRACKER_HEADER)
        self.assertEqual(rows, [])

    def test_legacy_header_gains_deadline(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "t.csv"
            path.write_text("date,company,role,status\n2026-01-01,Acme,Eng,applied\n",
                            encoding="utf-8")
            header, rows = read_tracker(path)
        self.assertIn("deadline", header)
        self.assertEqual(rows[0]["company"], "Acme")
        self.assertEqual(rows[0]["deadline"], "")  # legacy row reads as empty deadline

    def test_existing_row_matches_case_insensitively(self):
        rows = [{"company": "Acme Corp", "role": "iOS Engineer", "status": "drafted"}]
        self.assertIsNotNone(existing_row(rows, "acme corp", "IOS ENGINEER"))
        self.assertIsNone(existing_row(rows, "Acme Corp", "Backend"))


class TrackerRowTests(unittest.TestCase):
    def test_row_uses_canonical_fields_and_status(self):
        rec = {"title": "Data Eng", "company": "Acme", "url": "https://x/1",
               "rank_verdict": "Strong Fit", "deadline": "2026-10-01"}
        args = type("A", (), {"date": "2026-09-03", "channel": "linkedin", "note": "ref by A"})()
        row = tracker_row_for(rec, args)
        self.assertEqual(row["status"], APPLIED_STATUS)
        self.assertEqual(row["role"], "Data Eng")
        self.assertEqual(row["source"], "https://x/1")
        self.assertEqual(row["fit_rating"], "Strong Fit")
        self.assertEqual(row["deadline"], "2026-10-01")
        self.assertIn("ref by A", row["notes"])


class MainTests(unittest.TestCase):
    def _setup(self, d: Path):
        seen_path = d / "seen.json"
        seen_path.write_text(json.dumps({"seen": seen_dict()}), encoding="utf-8")
        return seen_path, d / "tracker.csv"

    def _run(self, argv):
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            rc = main(argv)
        return rc, out.getvalue()

    def test_mark_by_url_writes_tracker_and_mirrors_seen(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            seen_path, tracker = self._setup(d)
            rc, _ = self._run(["https://a.example/job/1", "--input", str(seen_path),
                               "--tracker", str(tracker)])
            self.assertEqual(rc, 0)
            data = json.loads(seen_path.read_text())
            entry = data["seen"]["https://a.example/job/1"]
            self.assertEqual(entry["status"], "applied")
            self.assertEqual(entry["applied_date"], date.today().isoformat())
            rows = list(csv.DictReader(tracker.read_text().splitlines()))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["status"], "applied")
            self.assertEqual(rows[0]["company"], "Acme")

    def test_seen_only_skips_tracker(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            seen_path, tracker = self._setup(d)
            rc, _ = self._run(["co_title_key", "--input", str(seen_path),
                               "--tracker", str(tracker), "--seen-only"])
            self.assertEqual(rc, 0)
            self.assertFalse(tracker.exists())
            data = json.loads(seen_path.read_text())
            self.assertEqual(data["seen"]["co_title_key"]["status"], "applied")

    def test_not_found_is_atomic_no_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            seen_path, tracker = self._setup(d)
            before = seen_path.read_text()
            rc, _ = self._run(["does-not-exist", "--input", str(seen_path),
                               "--tracker", str(tracker)])
            self.assertEqual(rc, 1)
            self.assertFalse(tracker.exists())
            self.assertEqual(seen_path.read_text(), before)  # unchanged

    def test_does_not_duplicate_existing_open_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            seen_path, tracker = self._setup(d)
            tracker.write_text(
                ",".join(TRACKER_HEADER) + "\n"
                + "2026-08-01,Acme,,Senior iOS Engineer,,direct,drafted,,,,,,https://a.example/job/1,\n",
                encoding="utf-8",
            )
            rc, out = self._run(["https://a.example/job/1", "--input", str(seen_path),
                                 "--tracker", str(tracker)])
            self.assertEqual(rc, 0)
            rows = list(csv.DictReader(tracker.read_text().splitlines()))
            self.assertEqual(len(rows), 1)  # not duplicated
            self.assertEqual(rows[0]["status"], "drafted")  # left for /outcome to advance
            self.assertIn("already in tracker", out)

    def test_does_not_reopen_final_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            seen_path, tracker = self._setup(d)
            tracker.write_text(
                ",".join(TRACKER_HEADER) + "\n"
                + "2026-08-01,Acme,,Senior iOS Engineer,,direct,rejected,,,,,,https://a.example/job/1,\n",
                encoding="utf-8",
            )
            rc, out = self._run(["https://a.example/job/1", "--input", str(seen_path),
                                 "--tracker", str(tracker)])
            self.assertEqual(rc, 0)
            rows = list(csv.DictReader(tracker.read_text().splitlines()))
            self.assertEqual(rows[0]["status"], "rejected")  # not reopened
            self.assertIn("already closed", out)

    def test_dry_run_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            seen_path, tracker = self._setup(d)
            before = seen_path.read_text()
            rc, out = self._run(["https://a.example/job/1", "--input", str(seen_path),
                                 "--tracker", str(tracker), "--dry-run"])
            self.assertEqual(rc, 0)
            self.assertIn("Dry run", out)
            self.assertFalse(tracker.exists())
            self.assertEqual(seen_path.read_text(), before)

    def test_multiple_ids_and_dedup(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            seen_path, tracker = self._setup(d)
            # same job twice (url + substring) collapses to one action
            rc, _ = self._run(["https://a.example/job/1", "Senior iOS",
                               "--input", str(seen_path), "--tracker", str(tracker)])
            self.assertEqual(rc, 0)
            rows = list(csv.DictReader(tracker.read_text().splitlines()))
            self.assertEqual(len(rows), 1)


if __name__ == "__main__":
    unittest.main()
