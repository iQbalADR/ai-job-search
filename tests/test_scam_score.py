"""Tests for tools/scam_score.py — the legitimacy / scam red-flag scorer."""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from tools.scam_score import score_text, main


def categories(result):
    return {f["category"] for f in result["flags"]}


class ScoreTextTests(unittest.TestCase):
    def test_clean_posting_is_likely_legit(self):
        r = score_text("Senior iOS Engineer at Acme. Build banking features in Swift. "
                       "Apply via our careers page.")
        self.assertEqual(r["legit_score"], 100)
        self.assertEqual(r["band"], "Likely legit")
        self.assertEqual(r["flags"], [])

    def test_off_platform_contact_flagged(self):
        r = score_text("Great role — contact me on Telegram to start.")
        self.assertIn("off_platform_contact", categories(r))
        self.assertLess(r["legit_score"], 100)

    def test_upfront_fee_flagged(self):
        self.assertIn("upfront_fees", categories(score_text("Pay a small registration fee to begin.")))

    def test_overpayment_check_flagged(self):
        r = score_text("We will send you a cashier's check; deposit the check and wire back the rest.")
        self.assertIn("overpayment_or_check", categories(r))

    def test_suspicious_payment_flagged(self):
        self.assertIn("suspicious_payment", categories(score_text("We pay in gift cards weekly.")))
        self.assertIn("suspicious_payment", categories(score_text("Payment via Western Union.")))

    def test_personal_financial_info_flagged(self):
        self.assertIn("personal_financial_info",
                      categories(score_text("Send your bank account details and SSN to onboard.")))

    def test_unrealistic_pay_flagged(self):
        self.assertIn("unrealistic_pay",
                      categories(score_text("Earn $700 per day, no experience needed!")))

    def test_pressure_flagged(self):
        self.assertIn("pressure", categories(score_text("Urgent hiring — start immediately!")))

    def test_one_flag_per_category(self):
        # Two off-platform mentions still yield a single off_platform_contact flag.
        r = score_text("Contact me on WhatsApp or Telegram.")
        offs = [f for f in r["flags"] if f["category"] == "off_platform_contact"]
        self.assertEqual(len(offs), 1)

    def test_case_insensitive(self):
        self.assertIn("off_platform_contact", categories(score_text("CONTACT ME ON TELEGRAM")))

    def test_bands_by_threshold(self):
        # Single -40 category (upfront_fees) -> 60 -> Caution.
        self.assertEqual(score_text("registration fee required")["band"], "Caution")
        # Off-platform (30) + upfront fee (40) -> 30 -> High risk.
        self.assertEqual(
            score_text("contact me on telegram and pay a registration fee")["band"], "High risk")

    def test_score_never_negative(self):
        text = ("telegram, registration fee, gift cards, send your bank account details, "
                "cashier's check deposit and wire back, earn $900 per day no experience, "
                "start immediately, data entry $500")
        self.assertEqual(score_text(text)["legit_score"], 0)


class MainTests(unittest.TestCase):
    def _run(self, argv):
        out = io.StringIO()
        with redirect_stdout(out):
            rc = main(argv)
        return rc, out.getvalue()

    def test_text_json_output(self):
        rc, out = self._run(["--text", "contact me on telegram"])
        self.assertEqual(rc, 0)
        data = json.loads(out)
        # off_platform_contact is -30 -> 70 -> "Likely legit" (band is >= 70).
        self.assertEqual(data["legit_score"], 70)
        self.assertEqual(data["band"], "Likely legit")

    def test_plain_output_lists_flags(self):
        rc, out = self._run(["--text", "pay a registration fee via gift cards", "--format", "plain"])
        self.assertEqual(rc, 0)
        self.assertIn("Legitimacy:", out)
        self.assertIn("registration fee", out.lower())

    def test_file_input(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "posting.txt"
            p.write_text("Clean posting, apply on our site.", encoding="utf-8")
            rc, out = self._run(["--file", str(p)])
            self.assertEqual(rc, 0)
            self.assertEqual(json.loads(out)["band"], "Likely legit")


if __name__ == "__main__":
    unittest.main()
