#!/usr/bin/env bash
# Keep your personalized profile/CV files from being committed or pushed on THIS
# clone, without untracking them (they must stay in the repo as templates for
# everyone else). It marks them with git's skip-worktree bit: git then ignores
# your local edits to these files. This is per-clone local state - it is never
# committed or pushed, so run it once after cloning.
#
#   tools/private-profile.sh on      # protect the personal files (skip-worktree)
#   tools/private-profile.sh off     # unprotect them (needed before an upstream pull)
#   tools/private-profile.sh status  # show which are currently protected
#
# CAVEAT: while a file is protected, `git pull` can refuse to merge an upstream
# change to it ("local changes would be overwritten"). Before updating from
# upstream, run `off`, pull, then `on` again. For a fully hands-off personal
# search, a PRIVATE repository (SETUP.md section 8) is cleaner than this bit.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# The files /setup fills with your personal data (SETUP.md "What gets populated").
FILES=(
  "CLAUDE.md"
  ".claude/skills/job-application-assistant/01-candidate-profile.md"
  ".claude/skills/job-application-assistant/02-behavioral-profile.md"
  ".claude/skills/job-application-assistant/04-job-evaluation.md"
  ".claude/skills/job-application-assistant/05-cv-templates.md"
  ".claude/skills/job-application-assistant/07-interview-prep.md"
  ".claude/skills/job-scraper/search-queries.md"
  "cv/main_example.tex"
)

# Collect the files that are actually tracked into the `tracked` array; warn about
# any that aren't. A plain read loop keeps this working on the bash 3.2 that ships
# with macOS (no `mapfile`).
tracked=()
collect_tracked() {
  tracked=()
  local f
  for f in "${FILES[@]}"; do
    if git ls-files --error-unmatch "$f" >/dev/null 2>&1; then
      tracked+=("$f")
    else
      echo "  (skipped, not tracked: $f)" >&2
    fi
  done
}

cmd="${1:-status}"
case "$cmd" in
  on)
    collect_tracked
    [ "${#tracked[@]}" -gt 0 ] && git update-index --skip-worktree "${tracked[@]}"
    echo "Protected ${#tracked[@]} file(s) from commit/push on this clone."
    echo "Edit them freely; git will ignore the changes. Run 'off' before an upstream pull."
    ;;
  off)
    collect_tracked
    [ "${#tracked[@]}" -gt 0 ] && git update-index --no-skip-worktree "${tracked[@]}"
    echo "Unprotected ${#tracked[@]} file(s). Their changes are visible to git again."
    ;;
  status)
    echo "Protected (skip-worktree) profile/CV files:"
    if git ls-files -v | grep -E '^S ' | grep -Ff <(printf '%s\n' "${FILES[@]}"); then
      :
    else
      echo "  (none - run 'tools/private-profile.sh on' to protect them)"
    fi
    ;;
  *)
    echo "usage: tools/private-profile.sh [on|off|status]" >&2
    exit 2
    ;;
esac
