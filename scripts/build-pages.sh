#!/bin/sh
# FULL publish — entirely within this repo (source + published output share one tree).
#   regenerate stats.json + sync the baked <STATS> block  ->  copy source to index.html
#   ->  bump version.json (triggers client reload)  ->  commit + push.
# GitHub Actions (pages-build-deployment) then deploys the site.
#
# Usage:  sh scripts/build-pages.sh ["commit subject"]
here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "$here/.." && pwd)"
cd "$repo" || exit 1

# git-safe: strip any leaked GIT_* pointer env so every git call operates on THIS repo only
# (hard-won: a leaked GIT_DIR once fired commits into the code repo — the 99-commit incident).
git() { ( unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_PREFIX; command git "$@" ); }

python "$here/gen_stats.py" --build || exit 1

ver=$(date +%s)
att=""
[ -f "$repo/attention.txt" ] && att=$(head -c 300 "$repo/attention.txt" | tr -d '"' | tr '\n' ' ')
printf '{"v":%s,"attention":"%s"}\n' "$ver" "$att" > version.json
cp mission-control.html index.html

git add index.html mission-control.html stats.json version.json
git commit -q -m "${1:-Refresh dashboard}

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
git push -q
echo "pushed version $ver"
