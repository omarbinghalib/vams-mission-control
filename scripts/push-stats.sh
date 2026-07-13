#!/bin/sh
# STATS-ONLY publish (no version.json bump -> no full page reload; the page polls
# stats.json directly every 45s). Commits + pushes whatever stats.json currently holds.
# Regenerate it first with:  python scripts/gen_stats.py
#
# Usage:  sh scripts/push-stats.sh
here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "$here/.." && pwd)"
cd "$repo" || exit 1

# git-safe: strip any leaked GIT_* pointer env so every git call operates on THIS repo only
# (hard-won: a leaked GIT_DIR once fired commits into the code repo — the 99-commit incident).
git() { ( unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_PREFIX; command git "$@" ); }

git add stats.json
if git diff --cached --quiet stats.json; then
  echo "no stats change"; exit 0
fi
git commit -q -m "Update worker stats

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
git push -q
echo "stats pushed"
