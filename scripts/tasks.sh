#!/bin/sh
# Update the dashboard TASKS & PROGRESS tracker (tasks.json) and publish immediately.
# The page polls tasks.json every ~20s and renders a per-track work tracker + progress bar,
# plus a mini per-task progress bar on IN-PROGRESS cards only.
#
# Each task: { "title": str, "status": "done"|"in_progress"|"pending", "track": str,
#              "note"?: str, "progress"?: 0-100, "started_at"?: iso8601 }
#
#   "progress" (optional) is a hand-seeded ESTIMATE (0-100) shown as-is on the mini bar.
#   "started_at" is auto-stamped the first time a task's status becomes "in_progress" (if not
#   already present / not overridden by an explicit --progress); the page then AUTO-DERIVES the
#   mini-bar % from elapsed-time vs. the same per-task duration assumption the ETA panel already
#   uses, so the bar advances on its own with no further edits. An explicit "progress" always wins.
#
#   set (replace the whole plan):
#     sh scripts/tasks.sh --set '[{"title":"Carve /me/","status":"in_progress","track":"backend"}, ...]'
#   set from a file:
#     sh scripts/tasks.sh --file plan.json
#   add one task (optionally seed a progress estimate):
#     sh scripts/tasks.sh --add "Wire signup API" --track backend --status pending --note "self-serve"
#     sh scripts/tasks.sh --add "Backfill X" --track backend --status in_progress --progress 25
#   flip a task to done (matched by title substring, case-insensitive):
#     sh scripts/tasks.sh --complete "signup"
#   set a task's status:
#     sh scripts/tasks.sh --status "signup" in_progress
#   set/replace a task's progress estimate (matched by title substring):
#     sh scripts/tasks.sh --set-progress "signup" 60
#   multi-field edit in one commit (matched by title substring; only given --to-* fields change):
#     sh scripts/tasks.sh --edit "signup" --to-status in_progress --to-note "..." --to-progress 40
here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "$here/.." && pwd)"
cd "$repo" || exit 1

# NON-INTERACTIVE + BOUNDED git: never block on a prompt; fail fast + cap each call (see heartbeat.sh).
export GIT_TERMINAL_PROMPT=0
export GIT_SSH_COMMAND="ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10"
# git-safe + BOUNDED: strip leaked GIT_* pointer env AND cap each git call via `timeout` on the REAL
# git binary so a stalled push can't hang. (`timeout command git` fails — `command` is a builtin.)
_gitbin="$(command -v git)"
git() { ( unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_PREFIX; timeout 40 "$_gitbin" "$@" ); }

MODE=""; PAYLOAD=""; TITLE=""; TRACK="backend"; STATUS="pending"; NOTE=""; MATCH=""; NEWSTATUS=""
PROGRESS=""; PROGRESSVAL=""
EDIT_STATUS=""; EDIT_NOTE=""; EDIT_PROGRESS=""; EDIT_TRACK=""
while [ $# -gt 0 ]; do
  case "$1" in
    --set)          MODE="set";      PAYLOAD="$2"; shift 2 ;;
    --file)         MODE="set";      PAYLOAD="$(cat "$2")"; shift 2 ;;
    --add)          MODE="add";      TITLE="$2"; shift 2 ;;
    --complete)     MODE="complete"; MATCH="$2"; shift 2 ;;
    --status)
      # context-aware: `--add ... --status X` is a modifier (one value); a bare/standalone
      # `--status MATCH NEWSTATUS` (mode not already "add") is the rename-by-title mode.
      if [ "$MODE" = "add" ]; then STATUS="$2"; shift 2
      else MODE="status"; MATCH="$2"; NEWSTATUS="$3"; shift 3; fi ;;
    --set-progress) MODE="progress"; MATCH="$2"; PROGRESSVAL="$3"; shift 3 ;;
    --edit)         MODE="edit";     MATCH="$2"; shift 2 ;;
    --track)        TRACK="$2"; shift 2 ;;
    --note)         NOTE="$2"; shift 2 ;;
    --progress)     PROGRESS="$2"; shift 2 ;;
    --to-status)    EDIT_STATUS="$2"; shift 2 ;;
    --to-note)      EDIT_NOTE="$2"; shift 2 ;;
    --to-progress)  EDIT_PROGRESS="$2"; shift 2 ;;
    --to-track)     EDIT_TRACK="$2"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; shift ;;
  esac
done
[ -n "$MODE" ] || { echo "ERROR: need one of --set/--file/--add/--complete/--status/--set-progress/--edit" >&2; exit 2; }

MODE="$MODE" PAYLOAD="$PAYLOAD" TITLE="$TITLE" TRACK="$TRACK" STATUS="$STATUS" \
NOTE="$NOTE" MATCH="$MATCH" NEWSTATUS="$NEWSTATUS" PROGRESS="$PROGRESS" PROGRESSVAL="$PROGRESSVAL" \
EDIT_STATUS="$EDIT_STATUS" EDIT_NOTE="$EDIT_NOTE" EDIT_PROGRESS="$EDIT_PROGRESS" EDIT_TRACK="$EDIT_TRACK" \
python - "$repo/tasks.json" <<'PY'
import os, sys, json, datetime
path = sys.argv[1]
mode = os.environ["MODE"]
VALID = {"done", "in_progress", "pending"}

def load():
    try:
        d = json.load(open(path, encoding="utf-8"))
        return d if isinstance(d, list) else d.get("tasks", [])
    except Exception:
        return []

def as_pct(raw):
    try:
        v = round(float(raw))
    except (TypeError, ValueError):
        return None
    return max(0, min(100, v))

# `touched` tracks exactly which task objects this invocation actually modified/added — the
# started_at auto-stamp below is scoped to ONLY these, never to every in_progress task in the
# file. Otherwise the FIRST run after adding this feature would retroactively stamp "now" onto
# every pre-existing in_progress task (falsely making long-running lanes read as freshly-started
# on the elapsed-time bar) instead of leaving them on the honest flat-estimate fallback until
# they're next explicitly touched.
touched = []

if mode == "set":
    try:
        tasks = json.loads(os.environ["PAYLOAD"])
    except Exception as e:
        sys.stderr.write("ERROR: --set payload is not valid JSON: %s\n" % e); sys.exit(2)
    if not isinstance(tasks, list):
        sys.stderr.write("ERROR: --set payload must be a JSON array\n"); sys.exit(2)
    touched = list(tasks)   # explicit full-replace — treat the whole payload as freshly asserted
else:
    tasks = load()
    if mode == "add":
        title = os.environ["TITLE"].strip()
        if not title:
            sys.stderr.write("ERROR: --add needs a title\n"); sys.exit(2)
        st = os.environ.get("STATUS", "pending")
        t = {"title": title, "status": st if st in VALID else "pending",
             "track": os.environ.get("TRACK", "backend")}
        if os.environ.get("NOTE"): t["note"] = os.environ["NOTE"]
        p = as_pct(os.environ.get("PROGRESS"))
        if p is not None: t["progress"] = p
        tasks.append(t)
        touched.append(t)
    elif mode in ("complete", "status"):
        m = os.environ["MATCH"].lower()
        new = "done" if mode == "complete" else os.environ.get("NEWSTATUS", "")
        if new not in VALID:
            sys.stderr.write("ERROR: status must be done|in_progress|pending\n"); sys.exit(2)
        hit = 0
        for t in tasks:
            if m in str(t.get("title", "")).lower():
                t["status"] = new; hit += 1; touched.append(t)
        if not hit:
            sys.stderr.write("WARN: no task title matched '%s'\n" % os.environ["MATCH"])
    elif mode == "progress":
        m = os.environ["MATCH"].lower()
        p = as_pct(os.environ.get("PROGRESSVAL"))
        if p is None:
            sys.stderr.write("ERROR: --set-progress needs a numeric 0-100 value\n"); sys.exit(2)
        hit = 0
        for t in tasks:
            if m in str(t.get("title", "")).lower():
                t["progress"] = p; hit += 1; touched.append(t)
        if not hit:
            sys.stderr.write("WARN: no task title matched '%s'\n" % os.environ["MATCH"])
    elif mode == "edit":
        m = os.environ["MATCH"].lower()
        es, en, ep, et = (os.environ.get(k) or "" for k in
                          ("EDIT_STATUS", "EDIT_NOTE", "EDIT_PROGRESS", "EDIT_TRACK"))
        if es and es not in VALID:
            sys.stderr.write("ERROR: --to-status must be done|in_progress|pending\n"); sys.exit(2)
        hit = 0
        for t in tasks:
            if m in str(t.get("title", "")).lower():
                if es: t["status"] = es
                if en: t["note"] = en
                if et: t["track"] = et
                if ep:
                    pv = as_pct(ep)
                    if pv is None:
                        sys.stderr.write("ERROR: --to-progress must be numeric 0-100\n"); sys.exit(2)
                    t["progress"] = pv
                hit += 1; touched.append(t)
        if not hit:
            sys.stderr.write("WARN: no task title matched '%s'\n" % os.environ["MATCH"])

# auto-stamp started_at ONLY on tasks this invocation touched, the first time they become
# in_progress (enables the elapsed-time auto-derived mini progress bar going forward); never
# overwrites an existing stamp. Untouched pre-existing in_progress tasks are left alone so they
# fall back to the flat-estimate bar instead of a falsely-reset "just started" clock.
now_iso = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
for t in touched:
    if t.get("status") == "in_progress" and not t.get("started_at"):
        t["started_at"] = now_iso

# normalise + validate statuses
for t in tasks:
    if t.get("status") not in VALID:
        t["status"] = "pending"
with open(path, "w", encoding="utf-8") as f:
    json.dump(tasks, f, indent=2); f.write("\n")
done = sum(1 for t in tasks if t.get("status") == "done")
ip   = sum(1 for t in tasks if t.get("status") == "in_progress")
print("tasks: %d total · %d done · %d in progress" % (len(tasks), done, ip))
PY
[ $? -eq 0 ] || exit 1

# bump version.json so already-open pages reload and pick up the tracker immediately
ver=$(date +%s)
printf '{"v":%s,"attention":""}\n' "$ver" > version.json

git add tasks.json version.json
git commit -q -m "tasks: update work tracker

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>" || { echo "nothing to commit"; exit 0; }
git pull --rebase -q 2>/dev/null
git push -q
echo "published tasks (version $ver)"
