#!/bin/sh
# Update the dashboard TASKS & PROGRESS tracker (tasks.json) and publish immediately.
# The page polls tasks.json every ~20s and renders a per-track work tracker + progress bar.
#
# Each task: { "title": str, "status": "done"|"in_progress"|"pending", "track": str, "note"?: str }
#
#   set (replace the whole plan):
#     sh scripts/tasks.sh --set '[{"title":"Carve /me/","status":"in_progress","track":"backend"}, ...]'
#   set from a file:
#     sh scripts/tasks.sh --file plan.json
#   add one task:
#     sh scripts/tasks.sh --add "Wire signup API" --track backend --status pending --note "self-serve"
#   flip a task to done (matched by title substring, case-insensitive):
#     sh scripts/tasks.sh --complete "signup"
#   set a task's status:
#     sh scripts/tasks.sh --status "signup" in_progress
here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "$here/.." && pwd)"
cd "$repo" || exit 1

# git-safe: strip any leaked GIT_* pointer env so every git call operates on THIS repo only
# (hard-won: a leaked GIT_DIR once fired commits into the code repo — the 99-commit incident).
git() { ( unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_PREFIX; command git "$@" ); }

MODE=""; PAYLOAD=""; TITLE=""; TRACK="backend"; STATUS="pending"; NOTE=""; MATCH=""; NEWSTATUS=""
while [ $# -gt 0 ]; do
  case "$1" in
    --set)      MODE="set";      PAYLOAD="$2"; shift 2 ;;
    --file)     MODE="set";      PAYLOAD="$(cat "$2")"; shift 2 ;;
    --add)      MODE="add";      TITLE="$2"; shift 2 ;;
    --complete) MODE="complete"; MATCH="$2"; shift 2 ;;
    --status)   MODE="status";   MATCH="$2"; NEWSTATUS="$3"; shift 3 ;;
    --track)    TRACK="$2"; shift 2 ;;
    --note)     NOTE="$2"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; shift ;;
  esac
done
[ -n "$MODE" ] || { echo "ERROR: need one of --set/--file/--add/--complete/--status" >&2; exit 2; }

MODE="$MODE" PAYLOAD="$PAYLOAD" TITLE="$TITLE" TRACK="$TRACK" STATUS="$STATUS" \
NOTE="$NOTE" MATCH="$MATCH" NEWSTATUS="$NEWSTATUS" \
python - "$repo/tasks.json" <<'PY'
import os, sys, json
path = sys.argv[1]
mode = os.environ["MODE"]
VALID = {"done", "in_progress", "pending"}

def load():
    try:
        d = json.load(open(path, encoding="utf-8"))
        return d if isinstance(d, list) else d.get("tasks", [])
    except Exception:
        return []

if mode == "set":
    try:
        tasks = json.loads(os.environ["PAYLOAD"])
    except Exception as e:
        sys.stderr.write("ERROR: --set payload is not valid JSON: %s\n" % e); sys.exit(2)
    if not isinstance(tasks, list):
        sys.stderr.write("ERROR: --set payload must be a JSON array\n"); sys.exit(2)
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
        tasks.append(t)
    elif mode in ("complete", "status"):
        m = os.environ["MATCH"].lower()
        new = "done" if mode == "complete" else os.environ.get("NEWSTATUS", "")
        if new not in VALID:
            sys.stderr.write("ERROR: status must be done|in_progress|pending\n"); sys.exit(2)
        hit = 0
        for t in tasks:
            if m in str(t.get("title", "")).lower():
                t["status"] = new; hit += 1
        if not hit:
            sys.stderr.write("WARN: no task title matched '%s'\n" % os.environ["MATCH"])

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
