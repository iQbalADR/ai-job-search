#!/usr/bin/env python3
"""Score a job/freelance posting's legitimacy from common scam red flags.

Freelance and project marketplaces (Upwork, Freelancer, and the like) carry a lot
of scam postings. This scans a posting's text for well-known red-flag patterns -
off-platform contact, upfront fees, overpayment/check scams, odd payment methods,
requests for personal/financial details, unrealistic pay, and high-pressure or
vague copy - and returns a legitimacy score (0-100, higher = more legitimate), a
band, and the flags it matched.

This is a HEURISTIC signal, not a verdict. A legitimate posting can trip a flag
(a crypto company naturally says "cryptocurrency"; a real role can be "urgent"),
and a clever scam can trip none. Use it to decide where to look closely, and let
the reader's judgment (and, in /scrape and /rank, the model's reading of the full
context) refine it. It never names a company as a scammer.

Usage:
    python3 tools/scam_score.py --text "we pay via gift cards, contact on telegram"
    python3 tools/scam_score.py --file posting.txt
    cat posting.txt | python3 tools/scam_score.py            # read from stdin
    python3 tools/scam_score.py --file posting.txt --format plain

Default output is JSON: {legit_score, band, flags:[{category, reason, matched}]}.
"""

from __future__ import annotations

import argparse
import json
import re
import sys

# Each red-flag category: a weight (points subtracted from 100 if ANY of its
# patterns match, once per category so multiple hits don't over-penalize), a
# human reason, and the patterns. Patterns are matched case-insensitively.
RED_FLAGS: list[dict] = [
    {
        "category": "off_platform_contact",
        "weight": 30,
        "reason": "asks to move off-platform to a personal messenger",
        "patterns": [
            r"\bwhats\s?app\b", r"\btelegram\b", r"\bsignal app\b", r"\bskype\b",
            r"contact (?:me|us) (?:on|via|through|at)\b", r"\btext me\b",
            r"\bd\.?m\.? me\b", r"reach (?:me|out) on\b", r"message me on\b",
        ],
    },
    {
        "category": "upfront_fees",
        "weight": 40,
        "reason": "requires an upfront payment or fee to start",
        "patterns": [
            r"registration fee", r"training fee", r"processing fee",
            r"security deposit", r"application fee", r"activation fee",
            r"pay (?:a |an )?(?:fee|deposit)", r"onboarding fee",
            r"purchase (?:your own )?(?:equipment|starter kit|software) (?:first|upfront)",
        ],
    },
    {
        "category": "overpayment_or_check",
        "weight": 40,
        "reason": "overpayment / fake-check / reshipping pattern",
        "patterns": [
            r"send (?:you )?a (?:cashier'?s )?check", r"mail(?:ed)? (?:you )?a check",
            r"deposit (?:the )?check and (?:send|wire|transfer)", r"overpay",
            r"reship", r"package forwarding", r"receive and forward (?:packages|payments)",
        ],
    },
    {
        "category": "suspicious_payment",
        "weight": 35,
        "reason": "unusual or irreversible payment method",
        "patterns": [
            r"gift cards?", r"western union", r"moneygram", r"wire transfer only",
            r"\bbitcoin\b", r"\busdt\b", r"\bcrypto(?:currency)? (?:wallet|payment|only)\b",
            r"pay(?:ment)? in crypto",
        ],
    },
    {
        "category": "personal_financial_info",
        "weight": 30,
        "reason": "asks for bank/ID/financial details before hiring",
        "patterns": [
            r"bank account (?:details|number|info)", r"routing number",
            r"social security(?: number)?", r"\bssn\b",
            r"send (?:a )?(?:copy of )?your (?:id|passport|driver'?s license)",
            r"credit card (?:number|details)",
        ],
    },
    {
        "category": "unrealistic_pay",
        "weight": 20,
        "reason": "pay looks too good for the work / no experience needed",
        "patterns": [
            r"\$\s?\d{3,}\s?(?:/|per )?(?:day|hour)\b.*(?:no experience|simple|easy)",
            r"(?:earn|make)\s+\$\s?\d{3,}.*(?:per day|a day|weekly|per week)",
            r"guaranteed (?:income|salary|pay)", r"easy money", r"get rich",
            r"work (?:just )?1\s?-\s?2 hours.*\$\d{2,}",
            r"no experience (?:needed|required).*\$\s?\d{3,}",
        ],
    },
    {
        "category": "pressure",
        "weight": 10,
        "reason": "high-pressure urgency to act fast",
        "patterns": [
            r"start immediately", r"urgent(?:ly)? (?:hiring|needed)", r"limited slots",
            r"act now", r"apply (?:within|in) \d+ (?:hours|minutes)", r"hiring today only",
        ],
    },
    {
        "category": "vague_generic",
        "weight": 10,
        "reason": "vague scope with outsized pay (classic filler)",
        "patterns": [
            r"data entry.*\$\s?\d{3,}", r"copy\s?-?\s?paste.*\$\d{2,}",
            r"simple (?:online )?(?:task|job).*\$\d{2,}",
        ],
    },
]

BANDS = [(70, "Likely legit"), (40, "Caution"), (0, "High risk")]


def _match(pattern: str, text: str) -> str | None:
    m = re.search(pattern, text, re.IGNORECASE)
    return m.group(0).strip() if m else None


def score_text(text: str) -> dict:
    """Return {legit_score, band, flags} for a posting's text."""
    flags = []
    penalty = 0
    for rule in RED_FLAGS:
        for pattern in rule["patterns"]:
            matched = _match(pattern, text)
            if matched:
                penalty += rule["weight"]
                flags.append({
                    "category": rule["category"],
                    "reason": rule["reason"],
                    "matched": matched,
                })
                break  # one hit per category
    legit_score = max(0, 100 - penalty)
    band = next(name for threshold, name in BANDS if legit_score >= threshold)
    return {"legit_score": legit_score, "band": band, "flags": flags}


def read_input(args: argparse.Namespace) -> str:
    if args.text is not None:
        return args.text
    if args.file is not None:
        try:
            return args.file.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            sys.exit(f"could not read {args.file}: {exc}")
    if not sys.stdin.isatty():
        return sys.stdin.read()
    sys.exit("no posting text — pass --text, --file, or pipe text on stdin")


def parse_args(argv: list[str]) -> argparse.Namespace:
    from pathlib import Path
    p = argparse.ArgumentParser(description="Score a posting's legitimacy from scam red flags.")
    p.add_argument("--text", help="Posting text to score.")
    p.add_argument("--file", type=Path, help="Read posting text from this file.")
    p.add_argument("--format", default="json", choices=["json", "plain"],
                   help="Output format (default: json).")
    return p.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    text = read_input(args)
    result = score_text(text)
    if args.format == "plain":
        print(f"Legitimacy: {result['band']} ({result['legit_score']}/100)")
        if result["flags"]:
            print("Red flags:")
            for f in result["flags"]:
                print(f"  - {f['reason']} (matched: \"{f['matched']}\")")
        else:
            print("No common scam red flags matched (heuristic only).")
    else:
        print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
