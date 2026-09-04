"""Tests for tools/export_jobs.py — filtering, freshness, fuzzy dedup, and output.

Covers the status/expired/age filters, the company/title normalization and the
near-duplicate collapsing they feed, and that the CSV/HTML writers emit the
expected content. Age tests build dates relative to today so they never rot.
"""

import csv
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import date, timedelta
from pathlib import Path

from tools.export_jobs import (
    company_related,
    company_tokens,
    normalize_title,
    dedupe,
    drop_expired,
    employment_group,
    filter_by_age,
    filter_by_employment,
    filter_jobs,
    freshness_date,
    cap_top,
    cell_value,
    ordered_groups,
    source_label,
    wanted_statuses,
    write_csv,
    render_html,
    main,
)


def job(**overrides):
    base = {
        "title": "Backend Engineer",
        "company": "Acme",
        "url": "https://example.com/j",
        "status": "new",
        "fit": "medium",
    }
    base.update(overrides)
    return base


def iso(days_ago: int) -> str:
    return (date.today() - timedelta(days=days_ago)).isoformat()


class StatusFilterTests(unittest.TestCase):
    def test_all_returns_none_sentinel(self):
        self.assertIsNone(wanted_statuses("all"))
        self.assertIsNone(wanted_statuses("  ALL "))

    def test_explicit_set_is_lowercased(self):
        self.assertEqual(wanted_statuses("New, Ranked"), {"new", "ranked"})

    def test_filter_jobs_keeps_only_requested_statuses(self):
        jobs = [job(status="new"), job(status="ranked"), job(status="expired")]
        self.assertEqual(len(filter_jobs(jobs, "new")), 1)
        self.assertEqual(len(filter_jobs(jobs, "new,ranked")), 2)
        self.assertEqual(len(filter_jobs(jobs, "all")), 3)


class ExpiredFilterTests(unittest.TestCase):
    def test_drop_expired_removes_only_expired(self):
        jobs = [job(status="new"), job(status="expired"), job(status="ranked")]
        kept = drop_expired(jobs)
        self.assertEqual([j["status"] for j in kept], ["new", "ranked"])

    def test_drop_expired_is_case_insensitive(self):
        self.assertEqual(drop_expired([job(status="Expired")]), [])


class FreshnessTests(unittest.TestCase):
    def test_uses_posted_date_first(self):
        self.assertEqual(freshness_date(job(posted_date="2026-01-02", first_seen="2026-05-05")),
                         date(2026, 1, 2))

    def test_falls_back_to_first_seen(self):
        self.assertEqual(freshness_date(job(first_seen="2026-05-05")), date(2026, 5, 5))

    def test_tolerates_full_iso_timestamp(self):
        self.assertEqual(freshness_date(job(posted_date="2026-01-02T09:00:00Z")),
                         date(2026, 1, 2))

    def test_unparseable_or_missing_is_none(self):
        self.assertIsNone(freshness_date(job(posted_date="ASAP")))
        self.assertIsNone(freshness_date(job()))

    def test_filter_by_age_drops_stale_keeps_recent_and_undated(self):
        jobs = [
            job(title="recent", posted_date=iso(3)),
            job(title="stale", posted_date=iso(40)),
            job(title="undated"),  # no date -> kept, never guessed
        ]
        kept = {j["title"] for j in filter_by_age(jobs, 14)}
        self.assertEqual(kept, {"recent", "undated"})

    def test_filter_by_age_boundary_is_inclusive(self):
        jobs = [job(title="edge", posted_date=iso(14))]
        self.assertEqual(len(filter_by_age(jobs, 14)), 1)


class CapTopTests(unittest.TestCase):
    def test_all_is_no_cap(self):
        jobs = [job(), job(), job()]
        self.assertEqual(len(cap_top(jobs, "all")), 3)

    def test_numeric_cap(self):
        jobs = [job(), job(), job()]
        self.assertEqual(len(cap_top(jobs, "2")), 2)

    def test_bad_value_exits(self):
        with self.assertRaises(SystemExit):
            cap_top([job()], "lots")


class CompanyNormalizationTests(unittest.TestCase):
    def test_strips_legal_and_generic_suffixes(self):
        self.assertEqual(company_tokens("Acme Pte Ltd"), frozenset({"acme"}))
        self.assertEqual(company_tokens("IDC Technologies (Singapore) PTE. LTD."),
                         frozenset({"idc", "technologies", "singapore"}))

    def test_alias_is_a_token_subset(self):
        self.assertEqual(company_tokens("Kotak Mahindra Bank"),
                         frozenset({"kotak", "mahindra", "bank"}))
        self.assertEqual(company_tokens("Kotak"), frozenset({"kotak"}))

    def test_empty_when_only_suffixes_or_blank(self):
        self.assertEqual(company_tokens("Ltd"), frozenset())
        self.assertEqual(company_tokens(""), frozenset())
        self.assertEqual(company_tokens(None), frozenset())


class CompanyRelatedTests(unittest.TestCase):
    def test_subset_is_related(self):
        self.assertTrue(company_related(frozenset({"kotak"}),
                                        frozenset({"kotak", "mahindra", "bank"})))

    def test_equal_is_related(self):
        self.assertTrue(company_related(frozenset({"acme"}), frozenset({"acme"})))

    def test_disjoint_is_not_related(self):
        self.assertFalse(company_related(frozenset({"globex"}), frozenset({"initech"})))

    def test_empty_never_related(self):
        self.assertFalse(company_related(frozenset(), frozenset({"acme"})))


class TitleNormalizationTests(unittest.TestCase):
    def test_drops_parentheticals_and_folds_punctuation(self):
        self.assertEqual(normalize_title("Senior iOS Developer (Remote)"),
                         "senior ios developer")
        self.assertEqual(normalize_title("Frontend Engineer (Mobile, React Native) - spht"),
                         "frontend engineer spht")

    def test_empty_title(self):
        self.assertEqual(normalize_title(""), "")
        self.assertEqual(normalize_title(None), "")


class DedupeTests(unittest.TestCase):
    def test_merges_company_alias_same_title(self):
        jobs = [
            job(title="PM Digital Banking Kotak 811", company="Kotak Mahindra Bank", url="https://a"),
            job(title="PM Digital Banking Kotak 811", company="Kotak", url="https://b",
                source_name="WeWorkRemotely", source="rss"),
        ]
        out, merged = dedupe(jobs)
        self.assertEqual(len(out), 1)
        self.assertEqual(merged, 1)
        self.assertEqual(out[0]["_dupe_count"], 1)

    def test_keeps_highest_scored_member(self):
        jobs = [
            job(title="Senior iOS Developer", company="Acme Pte Ltd", url="https://a", rank_score=70),
            job(title="Senior iOS Developer (Remote)", company="Acme", url="https://b", rank_score=82),
        ]
        out, merged = dedupe(jobs)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["url"], "https://b")  # the 82-point record

    def test_does_not_merge_different_companies(self):
        jobs = [
            job(title="Backend Engineer", company="Globex", url="https://a"),
            job(title="Backend Engineer", company="Initech", url="https://b"),
        ]
        out, merged = dedupe(jobs)
        self.assertEqual(len(out), 2)
        self.assertEqual(merged, 0)

    def test_does_not_merge_different_titles_same_company(self):
        jobs = [
            job(title="Backend Engineer", company="Acme", url="https://a"),
            job(title="Frontend Engineer", company="Acme", url="https://b"),
        ]
        out, _ = dedupe(jobs)
        self.assertEqual(len(out), 2)

    def test_empty_title_never_merges(self):
        jobs = [job(title="", company="Acme", url="https://a"),
                job(title="", company="Acme", url="https://b")]
        out, merged = dedupe(jobs)
        self.assertEqual(len(out), 2)
        self.assertEqual(merged, 0)

    def test_preserves_first_appearance_order(self):
        jobs = [
            job(title="Zeta Role", company="Zco", url="https://z"),
            job(title="Alpha Role", company="Aco", url="https://a"),
        ]
        out, _ = dedupe(jobs)
        self.assertEqual([j["title"] for j in out], ["Zeta Role", "Alpha Role"])


class EmploymentGroupTests(unittest.TestCase):
    def test_canonicalizes_portal_and_vocab_spellings(self):
        self.assertEqual(employment_group(job(employment_type="full_time")), "Full-time")
        self.assertEqual(employment_group(job(employment_type="part-time")), "Part-time")
        self.assertEqual(employment_group(job(employment_type="freelance")), "Freelance")
        self.assertEqual(employment_group(job(employment_type="contract")), "Contract")

    def test_missing_type_is_unspecified(self):
        self.assertEqual(employment_group(job()), "Unspecified")
        self.assertEqual(employment_group(job(employment_type="")), "Unspecified")

    def test_unknown_type_is_title_cased(self):
        self.assertEqual(employment_group(job(employment_type="seasonal")), "Seasonal")

    def test_type_cell_shows_canonical_label(self):
        self.assertEqual(cell_value(job(employment_type="part_time"), "employment_type"), "Part-time")
        self.assertEqual(cell_value(job(), "employment_type"), "")


class EmploymentFilterTests(unittest.TestCase):
    def test_keeps_only_requested_types(self):
        jobs = [
            job(title="fl", employment_type="freelance"),
            job(title="pt", employment_type="part_time"),
            job(title="ft", employment_type="full_time"),
        ]
        kept = {j["title"] for j in filter_by_employment(jobs, ["freelance", "part-time"])}
        self.assertEqual(kept, {"fl", "pt"})

    def test_drops_unspecified_when_filtering(self):
        jobs = [job(title="fl", employment_type="freelance"), job(title="unk")]
        kept = {j["title"] for j in filter_by_employment(jobs, ["freelance"])}
        self.assertEqual(kept, {"fl"})

    def test_empty_filter_keeps_all(self):
        jobs = [job(employment_type="freelance"), job()]
        self.assertEqual(len(filter_by_employment(jobs, [])), 2)


class OrderedGroupsTests(unittest.TestCase):
    def _labels(self, groups):
        return [label for label, _ in groups]

    def test_target_types_come_first_unspecified_last(self):
        jobs = [
            job(title="ft", employment_type="full_time"),
            job(title="fl", employment_type="freelance"),
            job(title="pt", employment_type="part-time"),
            job(title="none"),
        ]
        groups = ordered_groups(jobs, ["freelance", "part-time"])
        self.assertEqual(self._labels(groups), ["Freelance", "Part-time", "Full-time", "Unspecified"])

    def test_default_order_without_targets(self):
        jobs = [
            job(title="ft", employment_type="full_time"),
            job(title="ct", employment_type="contract"),
            job(title="fl", employment_type="freelance"),
        ]
        # DEFAULT_GROUP_ORDER: Freelance, Part-time, Contract, …, Full-time
        self.assertEqual(self._labels(ordered_groups(jobs, [])),
                         ["Freelance", "Contract", "Full-time"])

    def test_empty_groups_are_omitted(self):
        jobs = [job(employment_type="freelance"), job(employment_type="freelance")]
        groups = ordered_groups(jobs, ["freelance", "part-time"])
        self.assertEqual(self._labels(groups), ["Freelance"])
        self.assertEqual(len(groups[0][1]), 2)


class GroupedHtmlTests(unittest.TestCase):
    def test_grouped_html_has_a_section_per_type(self):
        jobs = [
            job(title="Freelance Role", employment_type="freelance", url="https://x/1"),
            job(title="Full Role", employment_type="full_time", url="https://x/2"),
        ]
        out = render_html(jobs, "Matches", Path("x.json"),
                          group_by="employment-type", target_types=["freelance"])
        self.assertEqual(out.count('class="group-section"'), 2)
        self.assertIn(">Freelance <", out)
        self.assertIn(">Full-time <", out)
        # Freelance section precedes Full-time (target first)
        self.assertLess(out.index(">Freelance <"), out.index(">Full-time <"))

    def test_ungrouped_html_is_a_single_table(self):
        jobs = [job(url="https://x/1")]
        out = render_html(jobs, "Matches", Path("x.json"))
        self.assertIn('id="jobs"', out)
        # No structural group sections (the CSS rule is always present, so match
        # the element attribute, not the bare class name).
        self.assertNotIn('class="group-section"', out)


class SourceLabelTests(unittest.TestCase):
    def test_portal_with_non_cli_source(self):
        self.assertEqual(source_label(job(source_name="WWR", source="rss")), "WWR (rss)")

    def test_portal_cli_is_bare(self):
        self.assertEqual(source_label(job(portal="linkedin-search", source="cli")),
                         "linkedin-search")

    def test_absorbed_duplicate_sources_are_noted(self):
        rec = job(portal="linkedin-search", source="cli", _dupe_sources=["freehire-search"])
        self.assertIn("+1", source_label(rec))
        self.assertIn("freehire-search", source_label(rec))


class WriteCsvTests(unittest.TestCase):
    def test_writes_header_and_row_values(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "out.csv"
            write_csv([job(title="Data Eng", company="Acme & Co", rank_score=82,
                           url="https://x", posted_date="2026-08-30")], path)
            rows = list(csv.reader(path.read_text(encoding="utf-8").splitlines()))
        self.assertEqual(rows[0][0], "Score")
        self.assertIn("Data Eng", rows[1])
        self.assertIn("Acme & Co", rows[1])  # raw text, no HTML escaping in CSV


class RenderHtmlTests(unittest.TestCase):
    def test_contains_title_link_and_escapes_html(self):
        html_out = render_html(
            [job(title="Data Eng", company="Acme & Co", url="https://x/1")],
            "Job Matches",
            Path("job_scraper/seen_jobs.json"),
        )
        self.assertIn("Job Matches", html_out)
        self.assertIn('href="https://x/1"', html_out)
        self.assertIn("Acme &amp; Co", html_out)  # ampersand escaped

    def test_empty_list_renders_without_error(self):
        html_out = render_html([], "Empty", Path("x.json"))
        self.assertIn("No jobs to show", html_out)


class MainEndToEndTests(unittest.TestCase):
    def _write_seen(self, d: Path, seen: dict) -> Path:
        path = d / "seen.json"
        path.write_text(json.dumps({"seen": seen}), encoding="utf-8")
        return path

    def test_default_run_drops_expired_and_dedupes(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            seen = {
                "a": job(title="Role A", company="Acme", url="https://a", posted_date=iso(2)),
                "b": job(title="Role A", company="Acme Ltd", url="https://b", posted_date=iso(2)),
                "c": job(title="Dead", company="Zco", url="https://c", status="expired",
                         posted_date=iso(2)),
            }
            inp = self._write_seen(d, seen)
            out_dir = d / "reports"
            with redirect_stdout(io.StringIO()):
                rc = main(["--input", str(inp), "--out-dir", str(out_dir), "--formats", "csv"])
            self.assertEqual(rc, 0)
            rows = list(csv.reader((out_dir / "job-matches.csv").read_text().splitlines()))
            # header + 1 (a/b merged, c expired dropped)
            self.assertEqual(len(rows), 2)

    def test_max_age_filters_and_no_dedupe_keeps_all(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            seen = {
                "a": job(title="Role A", company="Acme", url="https://a", posted_date=iso(2)),
                "b": job(title="Role A", company="Acme Ltd", url="https://b", posted_date=iso(2)),
                "old": job(title="Old", company="Zco", url="https://o", posted_date=iso(40)),
            }
            inp = self._write_seen(d, seen)
            out_dir = d / "reports"
            with redirect_stdout(io.StringIO()):
                rc = main(["--input", str(inp), "--out-dir", str(out_dir), "--formats", "csv",
                           "--max-age-days", "14", "--no-dedupe"])
            self.assertEqual(rc, 0)
            rows = list(csv.reader((out_dir / "job-matches.csv").read_text().splitlines()))
            # header + a + b (old dropped by age; no dedupe so a and b both kept)
            self.assertEqual(len(rows), 3)

    def test_group_by_orders_csv_by_type(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            seen = {
                "a": job(title="FT", company="A", url="https://a", employment_type="full_time", posted_date=iso(1)),
                "b": job(title="FL", company="B", url="https://b", employment_type="freelance", posted_date=iso(1)),
                "c": job(title="PT", company="C", url="https://c", employment_type="part_time", posted_date=iso(1)),
            }
            inp = self._write_seen(d, seen)
            out_dir = d / "reports"
            with redirect_stdout(io.StringIO()):
                rc = main(["--input", str(inp), "--out-dir", str(out_dir), "--formats", "csv",
                           "--group-by", "employment-type", "--target-types", "freelance,part-time"])
            self.assertEqual(rc, 0)
            rows = list(csv.DictReader((out_dir / "job-matches.csv").read_text().splitlines()))
            self.assertEqual([r["Type"] for r in rows], ["Freelance", "Part-time", "Full-time"])

    def test_missing_input_exits(self):
        with self.assertRaises(SystemExit):
            main(["--input", "/nonexistent/seen.json"])

    def test_unknown_format_exits(self):
        with tempfile.TemporaryDirectory() as tmp:
            inp = self._write_seen(Path(tmp), {"a": job()})
            with self.assertRaises(SystemExit):
                main(["--input", str(inp), "--formats", "pdf"])


if __name__ == "__main__":
    unittest.main()
