# /job-reset-pref - Reset job-search preferences and recheck the cache

Re-ask your `/scrape` and `/rank` preferences — employment type (freelance / part-time
/ full-time / …), location scope (a country or city, remote, or global), remoteness,
recency, and how results are shown — write them to `job-search.config.yaml` (or the
private local override), then recheck the already-scraped jobs against the new
preferences and regenerate the result files. This changes *what you look for and how it
is presented*; it does not touch your profile (that is `/setup`) or delete anything.

Follow these steps in order.

---

## Step 0: Parse input

`$ARGUMENTS` is optional and just seeds the answers — e.g. `/job-reset-pref freelance`
pre-selects freelance, `/job-reset-pref indonesia` pre-fills the location. With no
argument, ask everything interactively.

---

## Step 1: Load current preferences

Read the active config: prefer `job-search.config.local.yaml` (repo root) if it exists,
otherwise `job-search.config.yaml`. Treat every key as optional. Show the user the
current values you found for the fields below (or "unset / default") so they can see
what they are changing from.

---

## Step 2: Ask the preferences

Use **AskUserQuestion**, defaulting each question to the current value from Step 1.
Keep it to a few questions:

1. **Employment types** (multi-select): `freelance`, `part-time`, `contract`,
   `full-time`, `temporary`, `internship`, or *All types*. This is `search.employment_types`
   (empty list = all).
2. **Location scope**: a specific place (e.g. "Indonesia only" → `Jakarta, Indonesia`;
   "Global / anywhere"; "Remote"). Capture one or more entries for `search.locations`.
   Offer the current locations and a "Remote" and "Global" option; let the user type a
   place. "Global / anywhere" means no location narrowing — record it as `Remote` plus a
   note that location is judged loosely in Step 4, rather than inventing a fake place string.
3. **Workplace type** (multi-select): `remote`, `hybrid`, `onsite`, or *Any*. This is
   `search.workplace_types` (empty = any).
4. **Recency window**: how many days back to consider a posting fresh — `search.posted_within_days`
   (e.g. 7 / 14 / 30).
5. **How many to show**: `output.show` — `top10`, `top50`, `all`, or a number.
6. **Off-type roles** (only ask when employment types were chosen in Q1): `output.employment_filter` —
   *Keep all, grouped* (`group`, default: freelance/part-time in their own sections, full-time and
   untyped roles still listed under their own headings) or *Only my types* (`strict`: drop full-time,
   off-type, and roles whose type can't be determined). This is the one-tap freelance/part-time-only
   switch — note that `strict` can hide legitimate roles that simply don't state a type.

---

## Step 3: Write the preferences

Preferences are personal, so **prefer the git-ignored `job-search.config.local.yaml`**:
if it exists, write there. If it does not exist, ask: "Save to a private
`job-search.config.local.yaml` (not pushed) or edit the tracked `job-search.config.yaml`?"
— default to creating the local override. When creating it, you may copy the tracked
file as a starting point so its comments carry over.

Write only the `search.*`, `output.show`, and `output.employment_filter` keys the user set,
preserving all other keys and comments. Then read the file back and show the effective new
values as a short confirmation. Never write personal data other than these preference values.

---

## Step 4: Recheck the cache against the new preferences

This is a **non-destructive** re-evaluation — it never deletes `seen_jobs.json` entries
(they are the deduplication memory) or rewrites their `status`. It re-derives the view.

1. Read `job_scraper/seen_jobs.json`. If it is missing or empty, say there is nothing
   cached to recheck and skip to Step 5.
2. Compare each cached entry to the **new** preferences and tally, by reason, how many
   now fall out:
   - **Location** — judge the entry's stored `location` against the new scope (e.g.
     "Indonesia only" excludes "Berlin, Germany"; "Global" excludes nothing). This is a
     judgment call, so reason about the place string; do not invent a location the entry
     does not carry.
   - **Employment type** — compare the entry's `employment_type` (when present) to the
     new `search.employment_types`. An entry with no stored type is *unconfirmed*, not a
     mismatch — count it separately as "type unknown", never as a definite drop.
   - **Freshness** — `posted_date` older than the new `search.posted_within_days` is stale.
   - **Liveness** is out of scope here (it needs a re-fetch); note that `/rank` re-checks
     every posting's liveness.
3. Report a short summary: how many cached jobs still match, and the fall-out counts per
   reason. Do not change any entry's status. If the user explicitly asks to prune the
   cache, you may set clearly out-of-scope entries to `status: "skipped"` (still kept for
   dedup) — only with their confirmation, and never for "type unknown" entries.
4. Regenerate the result files with the new preferences:

   ```bash
   python3 tools/export_jobs.py --status new,ranked --top all \
     --max-age-days <posted_within_days> \
     --group-by employment-type --target-types "<employment_types, comma-joined>" \
     --basename job-matches --title "Job Matches (prefs updated <YYYY-MM-DD>)"
   ```

   - **If `output.employment_filter` is `strict`** (the Q6 "Only my types" choice), also pass
     `--employment-types "<the configured types, comma-joined>"` so the file shows only those
     types. The exporter infers a role's type from its title first, so this drops full-time,
     off-type, and genuinely untyped roles — tell the user how many that removed (the
     exporter reports "dropped N off-type", and the recheck tally above shows the same) so an
     over-aggressive filter is visible. For the default `group`, omit
     `--employment-types` and keep every role in its section.
   - Drop the `--group-by`/`--target-types` flags when the user chose *All types*.
   - Always pass `--top all` — the files are never capped (that is the terminal's job).
   - Respect `output.formats`/`output.directory` from the config as `/scrape` does.
   - If `python3` is unavailable, fall back to `python`; if neither is present, skip the
     export with a note.

---

## Step 5: Offer to refresh

The recheck only re-examines what is already cached. To act on the new preferences, offer:

- **`/scrape`** — fetch fresh postings under the new employment-type / location / recency
  filters (the portal CLIs apply them natively where they can).
- **`/rank`** — re-score and liveness-check, which also retires any now-dead cached jobs.

Ask which they want and hand off.

---

## Important rules

1. **Non-destructive by default.** Never delete `seen_jobs.json` entries or rewrite their
   `status` without explicit confirmation; the cache is the dedup memory. Regenerate the
   view, do not mutate the record.
2. **Preferences are personal.** Prefer the git-ignored `job-search.config.local.yaml` so
   locations and preferences are never pushed. See `job-search.config.yaml`'s header.
3. **Never invent data.** An unknown employment type is unconfirmed, not a mismatch; an
   entry carries only the location it actually has. Do not guess to make a filter tidy.
4. **This is preferences, not profile.** Roles, skills, and the fit framework stay with
   `/setup --section search`; deal-breakers stay in your profile. `/job-reset-pref` only
   edits the `search.*` and `output.*` knobs and rechecks the cache.
