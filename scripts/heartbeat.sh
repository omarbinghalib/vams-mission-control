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
# Explicit PATH so this can run under a NON-login shell (`sh -c`, not `sh -lc`): the login shell's
# profile-sourcing costs ~5s and, under heavy CPU load, starves for far longer before it even reaches
# this script. A non-login shell starts fast; we just need the MSYS/Git tools on PATH (python comes
# from the inherited Windows PATH).
export PATH="/mingw64/bin:/usr/bin:/bin:$PATH"

here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "$here/.." && pwd)"
cd "$repo" || exit 0

# SINGLE-WRITER LOCK — the scheduled task launches us FIRE-AND-FORGET (wscript -> sh.Run …,0,False),
# so a new heartbeat starts every minute even if the previous is still going. Under heavy fleet load
# a run can take 20-100s, so without this guard they PILE UP and collide on the git index / stats.json
# / push (the '25 lingering sh.exe' + never-lands-a-push symptom). mkdir is atomic: only one runner
# wins the lock; the rest exit immediately. A stale lock (crashed run, >5 min old) is reclaimed.
LOCK="$repo/.heartbeat.lock"
if ! mkdir "$LOCK" 2>/dev/null; then
  if [ -n "$(find "$LOCK" -maxdepth 0 -mmin +3 2>/dev/null)" ]; then
    rmdir "$LOCK" 2>/dev/null                       # stale (>3min): a crashed/killed run — reclaim
    mkdir "$LOCK" 2>/dev/null || { echo "heartbeat: lock race, skip $(date -u +%H:%MZ)"; exit 0; }
  else
    echo "heartbeat: another run holds the lock, skip $(date -u +%H:%MZ)"; exit 0
  fi
fi
trap 'rmdir "$LOCK" 2>/dev/null' EXIT INT TERM

# NON-INTERACTIVE git: a scheduled (non-interactive) run must NEVER block on a prompt.
# The freeze root-cause was a heartbeat instance hung on an interactive git/SSH prompt while the
# task's 72h execution limit + IgnoreNew policy silently rejected every later trigger. Force git to
# fail fast instead of prompting; the hard time backstop is the task's ExecutionTimeLimit (PT2M).
export GIT_TERMINAL_PROMPT=0
export GIT_SSH_COMMAND="ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10"
# git-safe + BOUNDED: strip any leaked GIT_* pointer env (a leaked GIT_DIR once fired commits into the
# code repo — the 99-commit incident) AND cap every git call with `timeout` on the REAL git binary so
# a `git push` that stalls under load/network can't hang forever holding the lock. (NB: `timeout
# command git` does NOT work — `command` is a shell builtin timeout can't exec — hence $_gitbin.)
_gitbin="$(command -v git)"
git() { ( unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_PREFIX; timeout 25 "$_gitbin" "$@" ); }

# STEP-TIMING LOG (gitignored) — so a detached/scheduled run leaves a trace of exactly which step is
# slow/stuck (invisible otherwise, since wscript runs it hidden with no console).
LOG="$repo/.heartbeat.log"
[ -f "$LOG" ] && { tail -n 200 "$LOG" > "$LOG.t" 2>/dev/null && mv "$LOG.t" "$LOG" 2>/dev/null; }
log(){ printf '%s pid=%s %s\n' "$(date -u +%H:%M:%S)" "$$" "$1" >> "$LOG"; }

log "START"
# hard-bound gen_stats too (belt & suspenders under the PT3M task limit): its probes are individually
# bounded, but cap the whole thing so it can never hold the lock indefinitely.
if timeout 150 python "$here/gen_stats.py" --heartbeat >> "$LOG" 2>&1; then
  log "gen_stats OK (push warranted)"
  git add stats.json; log "git add rc=$?"
  if ! git diff --cached --quiet stats.json; then
    git commit -q -m "Heartbeat: refresh worker liveness

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"; log "git commit rc=$?"
    git push -q; prc=$?; log "git push rc=$prc"
    [ "$prc" -eq 0 ] && echo "heartbeat pushed $(date -u +%H:%MZ)"
  else
    log "no stats diff (nothing to commit)"
  fi
else
  log "gen_stats skip/killed rc=$?"
  echo "heartbeat skip $(date -u +%H:%MZ)"
fi
log "END"
exit 0
