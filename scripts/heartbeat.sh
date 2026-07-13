#!/bin/sh
# SINGLE-SHOT heartbeat for a Windows Scheduled Task (run every ~3 min).
# Regenerates stats.json from the REAL local liveness (new commander's subagent task-output
# dir, mtime<10min + branch-commit recency) and pushes ONLY when a refresh is warranted:
# there are live workers AND (published stats is stale >6min OR the live set changed).
# That keeps the public page fresh without the commit-spam that a blind every-tick push
# would cause. Always exits 0 so the scheduler never records a failure.
#
# Invoke (from Task Scheduler / manually), using the POSIX form of this repo's checkout path:
#   "C:\Program Files\Git\bin\sh.exe" -lc "/c/path/to/pages/scripts/heartbeat.sh"
here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "$here/.." && pwd)"
cd "$repo" || exit 0

# NON-INTERACTIVE git: a scheduled (non-interactive) run must NEVER block on a prompt.
# The freeze root-cause was a heartbeat instance hung on an interactive git/SSH prompt while the
# task's 72h execution limit + IgnoreNew policy silently rejected every later trigger. Force git to
# fail fast instead of prompting; the hard time backstop is the task's ExecutionTimeLimit (PT2M).
export GIT_TERMINAL_PROMPT=0
export GIT_SSH_COMMAND="ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10"
# git-safe: strip any leaked GIT_* pointer env so every git call operates on THIS repo only
# (hard-won: a leaked GIT_DIR once fired commits into the code repo — the 99-commit incident).
git() { ( unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_PREFIX; command git "$@" ); }

if python "$here/gen_stats.py" --heartbeat; then   # exit 0 = push warranted, stats.json rewritten
  git add stats.json
  if ! git diff --cached --quiet stats.json; then
    git commit -q -m "Heartbeat: refresh worker liveness

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
    git push -q && echo "heartbeat pushed $(date -u +%H:%MZ)"
  fi
else
  echo "heartbeat skip $(date -u +%H:%MZ)"
fi
exit 0
