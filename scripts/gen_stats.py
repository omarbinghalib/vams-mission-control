#!/usr/bin/env python
"""
VAMS Mission Control — stats.json generator (schema 5).

Single source of truth for dashboard liveness. Computes worker + session liveness from
the REAL local signals, exactly as the dashboard documents them:
  - subagent workers: task-output file mtime in the new commander's subagents dir
    (<10 min = live), combined with branch-commit recency;
  - transcript sessions: transcript .jsonl token totals + mtime.

Lives in the vams-mission-control repo (this is now the SINGLE home for all mission-control
assets). Writes <repo>/stats.json. Resolves the repo root from its own location, so the only
machine-absolute paths are the ~/.claude transcript dirs and the two worker git checkouts
(documented below) — those are inherently local to this machine.

Modes:
  python gen_stats.py            -> write stats.json (used by build-pages.sh)
  python gen_stats.py --build    -> write stats.json AND sync the baked <STATS> block in
                                    mission-control.html (first-paint fallback)
  python gen_stats.py --heartbeat-> write+exit 0 ONLY if a push is warranted (live workers
                                    and published stats is stale >STALE_MIN, or the live set
                                    changed); otherwise leave stats.json untouched, exit 3.
                                    Lets a scheduled heartbeat avoid commit-spam.
"""
import sys, os, re, json, glob, time, datetime, subprocess

# ---- repo-relative paths (portable) ----
SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO        = os.path.dirname(SCRIPTS_DIR)
STATS_PATH  = os.path.join(REPO, "stats.json")
HTML_PATH   = os.path.join(REPO, "mission-control.html")

# ---- machine-local signal sources — loaded from the gitignored scripts/local-config.json ----
# Keeps this machine's Claude-transcript layout, OS username, and checkout paths OUT of the
# public repo. Copy scripts/local-config.example.json -> scripts/local-config.json and fill in.
def _load_cfg():
    p = os.path.join(SCRIPTS_DIR, "local-config.json")
    if not os.path.exists(p):
        sys.stderr.write("ERROR: missing scripts/local-config.json — copy "
                         "scripts/local-config.example.json and fill in your machine paths.\n")
        sys.exit(2)
    return json.load(open(p, encoding="utf-8"))
_CFG            = _load_cfg()
PROJECTS_BASE   = _CFG["projects_base"]        # e.g. C:\Users\<you>\.claude\projects
CMD_PROJECT     = _CFG["commander_project"]    # new commander's project slug
CMD_SID         = _CFG["commander_session"]    # new commander root session id
SEARCH_PROJECTS = _CFG["search_projects"]      # project slugs to scan for transcripts
VAMS_FRONTEND   = _CFG["vams_frontend"]        # VAMS-frontend checkout (branch-commit recency)
VAMS_BACKEND    = _CFG["vams_backend"]          # VAMS-backend checkout
SUBDIR          = os.path.join(PROJECTS_BASE, CMD_PROJECT, CMD_SID, "subagents")
CMD_TRANSCRIPT  = os.path.join(PROJECTS_BASE, CMD_PROJECT, CMD_SID + ".jsonl")

LIVE_MIN  = 10   # task-output/transcript mtime younger than this (min) = live
STALE_MIN = 6    # heartbeat: republish if published stats older than this

now = datetime.datetime.now(datetime.timezone.utc)

# ---------- helpers ----------
def parse(ts):
    if not ts: return None
    return datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))

def age_of(ts):
    d = parse(ts)
    return None if d is None else max(0.0, round((now - d).total_seconds()/60, 1))

def iso(dt):
    return dt.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def find_transcript(sid):
    for proj in SEARCH_PROJECTS:
        for f in glob.glob(os.path.join(PROJECTS_BASE, proj, sid + "*.jsonl")):
            return f
    return None

def token_usage(path):
    out=cr=cc=n=0
    with open(path, encoding="utf-8", errors="ignore") as fh:
        for line in fh:
            if '"usage"' not in line: continue
            try: d = json.loads(line)
            except Exception: continue
            u = (d.get("message") or {}).get("usage")
            if u:
                out += u.get("output_tokens",0); cr += u.get("cache_read_input_tokens",0)
                cc  += u.get("cache_creation_input_tokens",0); n += 1
    return out, cr, cc, n

def subagent_file(short):
    fs = sorted(glob.glob(os.path.join(SUBDIR, "agent-" + short + "*.jsonl")))
    return fs[-1] if fs else None

def commander_activity():
    """Commander session is live while ANY of its subagents is — its own root transcript
    goes quiet whenever a worker holds the turn. Use the freshest mtime across the whole
    session tree (root transcript + every subagent task-output file)."""
    mtimes = []
    if os.path.exists(CMD_TRANSCRIPT): mtimes.append(os.path.getmtime(CMD_TRANSCRIPT))
    mtimes += [os.path.getmtime(f) for f in glob.glob(os.path.join(SUBDIR, "*.jsonl"))]
    if not mtimes: return None
    return iso(datetime.datetime.fromtimestamp(max(mtimes), datetime.timezone.utc))

def branch_head(repo, branch, grep=None):
    """Latest commit (subject, iso) on branch, optionally matching grep; None on failure."""
    if not os.path.isdir(repo): return None
    base = ["git", "-C", repo, "log", "-1", "--format=%s%x1f%cI"]
    for args in ([base+["--grep",grep,branch]] if grep else []) + [base+[branch]]:
        try:
            r = subprocess.run(args, capture_output=True, text=True, timeout=15)
            if r.returncode==0 and r.stdout.strip():
                subj, ci = r.stdout.strip().split("\x1f", 1)
                return subj, ci
        except Exception:
            pass
    return None

# ---------- transcript-session baseline (numbers preserved for sessions whose .jsonl is gone) ----------
# Files that still exist on this machine are recomputed live; the rest fall back to these.
BASELINE = [
  ("a49f5cc3","commander","Commander",        83240,   3960060,   530517, 68,  "2026-07-10T06:48:32Z"),
  ("aa30f33e","commander","Commander",         8202319, 3544789470,20330369,8727,"2026-07-09T07:44:28Z"),
  ("1f403dd5","backend",  "Backend",           3082893, 1446695660,20266145,3101,"2026-07-03T04:41:24Z"),
  ("479c5f63","commander","Commander",         1387040, 1017397236, 7338420,1829,"2026-07-07T06:31:20Z"),
  ("2eb0ee20","commander","Commander",          862161,  161687648, 6332775, 614,"2026-07-07T10:18:17Z"),
  ("b0a25ec9","commander","Commander",          742095,   73169827, 1783895, 402,"2026-07-10T06:30:41Z"),
  ("a3607e93","commander","Commander",          172195,   18572383,  319452, 219,"2026-07-09T05:55:07Z"),
  ("bca1b96d","frontend", "Frontend",             9908,     810404,  209179,  22,"2026-07-10T03:50:38Z"),
  ("3f777a8b","commander","Commander",            7758,     219554,       0,  24,"2026-07-07T07:17:26Z"),
  ("649d5b29","commander","Commander",            7177,     464892,   62450,  18,"2026-07-07T06:31:20Z"),
  ("070e42cb","backend",  "Backend",              1961,     207645,   34599,   9,"2026-07-02T16:56:07Z"),
  ("a7ec81c1","backend",  "Backend",                 0,          0,       0,   1,"2026-07-02T16:38:39Z"),
]

# ---------- worker roster (identity is static; liveness is computed) ----------
# kind: "transcript" (commander root), "subagent" (live worker), "done" (completed & retired)
ROSTER = [
  dict(name="Commander", role="commander", branch=None, kind="transcript",
       agent_id="a49f5cc3 (root)", short_id="a49f5cc3", transcript=CMD_TRANSCRIPT,
       signal="coordinating (orchestrator)",
       focus="Orchestrating the relaunched backend + frontend workers, integration, and dashboard publishing."),
  dict(name="Backend worker", role="backend", branch="microservices-backend", kind="subagent",
       agent_id="a7aa95fb33d1db66d", short_id="a7aa95fb", repo=VAMS_BACKEND,
       signal="task-output mtime + git commit",
       focus="Relaunched under the new commander. Real-data parity complete; continuing SO->MR + request-leave + line-item persistence saga."),
  dict(name="Frontend worker", role="frontend", branch="microservices-frontend", kind="subagent",
       agent_id="a84f4e234ffc22a31", short_id="a84f4e23", repo=VAMS_FRONTEND,
       signal="task-output mtime + git commit",
       focus="Relaunched under the new commander. Phase 5 Android + iOS done, /api/v1 aligned (220 green); authenticated E2E + reconciliation."),
  dict(name="Mission-control", role="mission-control", branch="microservices-frontend", kind="subagent",
       agent_id="ad796e2c1ded647f8", short_id="ad796e2c", repo=VAMS_FRONTEND, commit_grep="mission-control",
       signal="task-output mtime + git commit",
       focus="Dashboard accuracy - roster + liveness honesty - stats freshness - single-repo publish."),
  dict(name="Provisioning worker", role="provisioning", branch=None, kind="done",
       agent_id="a90719d7d38e41e26", short_id="a90719d7",
       last_active="2026-07-10T06:21:47Z", log_bytes=488835, signal="mission complete",
       focus="COMPLETE - django-tenants acme org + JWT user + module/role grants shipped; real GET /me/ + roles now live (platform blockers unblocked)."),
  dict(name="Environment worker", role="environment", branch=None, kind="done",
       agent_id="a9bea1c0e2cc2bc25", short_id="a9bea1c0",
       last_active="2026-07-10T05:33:12Z", log_bytes=218452, signal="mission complete",
       focus="COMPLETE - JDK 17 + Android SDK toolchain installed; Android + iOS native builds done."),
]

# ---------- build sessions ----------
def build_sessions():
    tx, subs = [], []
    for sid, group, label, o, cr, cc, n, la in BASELINE:
        f = find_transcript(sid)
        if f:
            o, cr, cc, n = token_usage(f)
            la = iso(datetime.datetime.fromtimestamp(os.path.getmtime(f), datetime.timezone.utc))
        if sid == "a49f5cc3":                       # commander: live while any subagent is
            la = commander_activity() or la
        s = dict(id=sid, group=group, group_label=label,
                 output_tokens=o, cache_read_tokens=cr, cache_creation_tokens=cc, msgs=n,
                 last_active=la, age_min=age_of(la),
                 live=(age_of(la) is not None and age_of(la) < LIVE_MIN), subagent=False)
        if sid == "a49f5cc3":
            s["full_id"] = CMD_SID
        tx.append(s)

    for w in ROSTER:
        if w["kind"] == "commander" and False:  # commander is a transcript session (a49f5cc3), already in tx
            pass
        if w["kind"] == "subagent":
            f = subagent_file(w["short_id"])
            la = iso(datetime.datetime.fromtimestamp(os.path.getmtime(f), datetime.timezone.utc)) if f else None
            lb = os.path.getsize(f) if f else None
            subs.append(dict(id=w["short_id"], full_id=w["agent_id"], group=w["role"],
                             group_label=w["name"], output_tokens=None, cache_read_tokens=None,
                             cache_creation_tokens=None, msgs=None, last_active=la, age_min=age_of(la),
                             live=(age_of(la) is not None and age_of(la) < LIVE_MIN),
                             subagent=True, log_bytes=lb))
        elif w["kind"] == "done":
            subs.append(dict(id=w["short_id"], full_id=w["agent_id"], group=w["role"],
                             group_label=w["name"], output_tokens=None, cache_read_tokens=None,
                             cache_creation_tokens=None, msgs=None, last_active=w["last_active"],
                             age_min=age_of(w["last_active"]), live=False, subagent=True,
                             log_bytes=w["log_bytes"]))
    return tx, subs

# ---------- build workers roster ----------
def build_workers():
    out = []
    for w in ROSTER:
        rec = dict(name=w["name"], role=w["role"], branch=w["branch"], focus=w["focus"],
                   agent_id=w["agent_id"], short_id=w["short_id"],
                   head_commit=None, head_commit_at=None, head_ago_min=None,
                   out_last_active=None, out_age_min=None, log_bytes=None,
                   status="idle", live=False, signal=w["signal"])
        if w["kind"] == "transcript":               # commander: live while any subagent is
            la = commander_activity()
            if la:
                rec["out_last_active"] = la; rec["out_age_min"] = age_of(la)
                rec["live"] = age_of(la) < LIVE_MIN
        elif w["kind"] == "subagent":
            f = subagent_file(w["short_id"])
            if f:
                la = iso(datetime.datetime.fromtimestamp(os.path.getmtime(f), datetime.timezone.utc))
                rec["out_last_active"] = la; rec["out_age_min"] = age_of(la)
                rec["log_bytes"] = os.path.getsize(f)
                rec["live"] = age_of(la) < LIVE_MIN
            if w["branch"]:
                h = branch_head(w["repo"], w["branch"], w.get("commit_grep"))
                if h:
                    rec["head_commit"], ci = h[0], h[1]
                    rec["head_commit_at"] = ci
                    rec["head_ago_min"] = age_of(iso(parse(ci)))
        elif w["kind"] == "done":
            rec["out_last_active"] = w["last_active"]; rec["out_age_min"] = age_of(w["last_active"])
            rec["log_bytes"] = w["log_bytes"]
        rec["status"] = "live" if rec["live"] else ("done" if w["kind"]=="done" else "idle")
        out.append(rec)
    return out

def build_stats():
    tx, subs = build_sessions()
    sessions = tx + subs
    workers = build_workers()
    out_t = sum(s["output_tokens"] or 0 for s in tx)
    cr_t  = sum(s["cache_read_tokens"] or 0 for s in tx)
    cc_t  = sum(s["cache_creation_tokens"] or 0 for s in tx)
    msg_t = sum(s["msgs"] or 0 for s in tx)
    totals = dict(output_tokens=out_t, cache_read_tokens=cr_t, cache_creation_tokens=cc_t,
                  msgs=msg_t, sessions=len(sessions),
                  live_sessions=sum(1 for s in sessions if s["live"]),
                  transcript_sessions=len(tx), subagent_sessions=len(subs),
                  workers=len(workers), live_workers=sum(1 for w in workers if w["live"]))
    return dict(updated=iso(now), generated_by="mission-control (ad796e2c)", schema=5,
                totals=totals, workers=workers, sessions=sessions, total_tokens=out_t)

def write_stats(stats):
    with open(STATS_PATH, "w", encoding="utf-8") as fh:
        json.dump(stats, fh, indent=2); fh.write("\n")

def sync_html(stats):
    if not os.path.exists(HTML_PATH): return
    html = open(HTML_PATH, encoding="utf-8").read()
    baked = json.dumps(stats, indent=2)
    html = re.sub(r"/\*__STATS__\*/.*?/\*__/STATS__\*/",
                  "/*__STATS__*/" + baked + "/*__/STATS__*/", html, flags=re.S)
    open(HTML_PATH, "w", encoding="utf-8").write(html)

def live_key(stats):
    return sorted(s["id"] for s in stats["sessions"] if s["live"]), \
           sorted(w["short_id"] for w in stats["workers"] if w["live"])

def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    stats = build_stats()
    if mode == "--heartbeat":
        try:
            old = json.load(open(STATS_PATH, encoding="utf-8"))
        except Exception:
            old = None
        any_live = stats["totals"]["live_workers"] > 0
        stale = True
        changed = True
        if old:
            stale = (age_of(old.get("updated")) or 1e9) >= STALE_MIN
            changed = live_key(old) != live_key(stats)
        if any_live and (stale or changed):
            write_stats(stats)
            print("PUSH updated=%s live_workers=%d live_sessions=%d%s"
                  % (stats["updated"], stats["totals"]["live_workers"],
                     stats["totals"]["live_sessions"], " (live-set-changed)" if changed else ""))
            sys.exit(0)
        print("SKIP any_live=%s stale=%s changed=%s" % (any_live, stale, changed))
        sys.exit(3)

    write_stats(stats)
    if mode == "--build":
        sync_html(stats)
    print("wrote %s | updated=%s live_workers=%d live_sessions=%d sessions=%d"
          % (STATS_PATH, stats["updated"], stats["totals"]["live_workers"],
             stats["totals"]["live_sessions"], stats["totals"]["sessions"]))

if __name__ == "__main__":
    main()
