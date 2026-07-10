#!/bin/sh
# Raise / clear the "COMMANDER NEEDS YOUR ATTENTION" red alert on the live dashboard.
# The page polls attention.json every ~15s and turns the whole screen red + fires a browser
# notification when active. This writes the structured payload and PUBLISHES immediately
# (commit + push; Actions deploys in ~1 min) so the alert appears within a refresh.
#
#   raise:  sh scripts/attention.sh --message "Decision: ship X or Y?" \
#                                    --option "A: ship X now" --option "B: wait for Y"
#   clear:  sh scripts/attention.sh --clear
#
# Payload shape (attention.json): { active, message, options[], raised_at }
here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "$here/.." && pwd)"
cd "$repo" || exit 1

clear=0; msg=""
: > "$here/.attn_opts.tmp"
while [ $# -gt 0 ]; do
  case "$1" in
    --clear)   clear=1; shift ;;
    --message) msg="$2"; shift 2 ;;
    --option)  printf '%s\n' "$2" >> "$here/.attn_opts.tmp"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; shift ;;
  esac
done

CLEAR="$clear" MSG="$msg" OPTS_FILE="$here/.attn_opts.tmp" python - "$repo/attention.json" <<'PY'
import os, sys, json, datetime
path = sys.argv[1]
if os.environ.get("CLEAR") == "1":
    obj = {"active": False, "message": "", "options": [], "raised_at": None}
else:
    opts = []
    try:
        with open(os.environ["OPTS_FILE"], encoding="utf-8") as f:
            opts = [ln.rstrip("\n") for ln in f if ln.strip()]
    except Exception:
        pass
    msg = (os.environ.get("MSG") or "").strip()
    if not msg:
        sys.stderr.write("ERROR: --message is required to raise an attention\n"); sys.exit(2)
    obj = {"active": True, "message": msg, "options": opts,
           "raised_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
with open(path, "w", encoding="utf-8") as f:
    json.dump(obj, f, indent=2); f.write("\n")
print("attention active=%s | %s | %d option(s)" % (obj["active"], obj.get("message","")[:70], len(obj["options"])))
PY
rc=$?
rm -f "$here/.attn_opts.tmp"
[ $rc -eq 0 ] || exit $rc

# bump version.json so already-open pages reload and pick up the alert immediately
ver=$(date +%s)
printf '{"v":%s,"attention":""}\n' "$ver" > version.json

git add attention.json version.json
git commit -q -m "attention: $([ "$clear" = 1 ] && echo cleared || echo raised)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>" || { echo "nothing to commit"; exit 0; }
git pull --rebase -q 2>/dev/null
git push -q
echo "published attention (version $ver)"
