#!/bin/sh
# SINGLE-SHOT sysres collector for a Windows Scheduled Task (run every ~5 min). Writes sysres.json
# (host CPU/mem + aggregate `docker stats`) and pushes it -- COMPLETELY SEPARATE from heartbeat.sh /
# gen_stats.py. See sysres_gen.py's header for why this MUST stay off the freshness critical path
# (the 2026-07-23 incident: `docker stats` alone measured 2+ min under heavy fleet load, more than
# heartbeat.sh's whole 150s budget). Always exits 0 so the scheduler never records a failure.
export PATH="/mingw64/bin:/usr/bin:/bin:$PATH"

here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "$here/.." && pwd)"
cd "$repo" || exit 0

# SINGLE-WRITER LOCK -- own lock file, separate from heartbeat's .heartbeat.lock, so the two
# schedules never contend for the same mkdir but also never overlap with THEMSELVES (a slow ~25s
# run piling up on a fast 5-min cadence would be unusual, but stay defensive regardless).
LOCK="$repo/.sysres.lock"
if ! mkdir "$LOCK" 2>/dev/null; then
  if [ -n "$(find "$LOCK" -maxdepth 0 -mmin +4 2>/dev/null)" ]; then
    rmdir "$LOCK" 2>/dev/null                       # stale (>4min): a crashed/killed run -- reclaim
    mkdir "$LOCK" 2>/dev/null || { echo "sysres: lock race, skip $(date -u +%H:%MZ)"; exit 0; }
  else
    echo "sysres: another run holds the lock, skip $(date -u +%H:%MZ)"; exit 0
  fi
fi
trap 'rmdir "$LOCK" 2>/dev/null' EXIT INT TERM

# NON-INTERACTIVE git + git-safe + BOUNDED, same hardening as heartbeat.sh (a scheduled/non-
# interactive run must never block on a prompt; a leaked GIT_* pointer must never redirect a
# commit into the wrong repo; every git call is capped so a stall can't hold the lock forever).
export GIT_TERMINAL_PROMPT=0
export GIT_SSH_COMMAND="ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10"
_gitbin="$(command -v git)"
git() { ( unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_PREFIX; timeout 25 "$_gitbin" "$@" ); }

LOG="$repo/.sysres.log"
[ -f "$LOG" ] && { tail -n 200 "$LOG" > "$LOG.t" 2>/dev/null && mv "$LOG.t" "$LOG" 2>/dev/null; }
log(){ printf '%s pid=%s %s\n' "$(date -u +%H:%M:%S)" "$$" "$1" >> "$LOG"; }

log "START"
# Hard-bound the whole probe (belt & suspenders): sysres_gen.py's own two subprocess calls are each
# individually timeout-bounded (20s host + 25s docker = worst case ~45s), this outer cap just makes
# sure the WHOLE script (interpreter start, imports, etc.) can never hold the lock indefinitely.
if timeout 60 python "$here/sysres_gen.py" >> "$LOG" 2>&1; then
  log "sysres_gen OK"
  # PATHSPEC-LIMITED add+commit -- this script owns ONLY sysres.json. Same golden-rule reasoning as
  # heartbeat.sh: a plain `git commit` with no pathspec would commit everything staged in the shared
  # index (tasks.sh / heartbeat.sh may have their own files staged at the same moment).
  git add sysres.json; log "git add rc=$?"
  if ! git diff --cached --quiet -- sysres.json; then
    git commit -q -m "Sysres: refresh host/docker resource snapshot

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>" -- sysres.json; log "git commit rc=$?"
    git push -q; prc=$?; log "git push rc=$prc"
    [ "$prc" -eq 0 ] && echo "sysres pushed $(date -u +%H:%MZ)"
  else
    log "no sysres diff (nothing to commit)"
  fi
else
  log "sysres_gen skip/killed rc=$?"
  echo "sysres skip $(date -u +%H:%MZ)"
fi
log "END"
exit 0
