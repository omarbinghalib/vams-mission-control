#!/bin/sh
# STATS-ONLY publish (no version.json bump -> no full page reload; the page polls
# stats.json directly every 45s). Commits + pushes whatever stats.json currently holds.
# Regenerate it first with:  python scripts/gen_stats.py
#
# Usage:  sh scripts/push-stats.sh
here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "$here/.." && pwd)"
cd "$repo" || exit 1

# NON-INTERACTIVE + BOUNDED git: never block on a prompt; fail fast + cap each call (see heartbeat.sh).
export GIT_TERMINAL_PROMPT=0
export GIT_SSH_COMMAND="ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10"
# git-safe: strip any leaked GIT_* pointer env so every git call operates on THIS repo only
# (hard-won: a leaked GIT_DIR once fired commits into the code repo — the 99-commit incident).
_tmo=""; command -v timeout >/dev/null 2>&1 && _tmo="timeout 30"
git() { ( unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_PREFIX; $_tmo command git "$@" ); }

git add stats.json
if git diff --cached --quiet stats.json; then
  echo "no stats change"; exit 0
fi
git commit -q -m "Update worker stats

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
git push -q
echo "stats pushed"
