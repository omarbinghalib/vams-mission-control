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

# optional (all degrade gracefully if absent) — kept in local-config so this public repo
# carries no host-specific ports / docker paths.
DOCKER_CFG    = _CFG.get("docker", {}) or {}
DOCKER_BIN    = DOCKER_CFG.get("bin")               # e.g. C:\Program Files\Docker\Docker\resources\bin
DOCKER_FILTER = DOCKER_CFG.get("name_filter", "")   # e.g. vams_   (empty = all containers)
HEALTH_CHECKS = _CFG.get("health_checks", []) or [] # [{name,method,url,expect:[...],body?}]
REPOS         = _CFG.get("repos", []) or []         # [{track,path,branch}] for velocity + commit feed

LIVE_MIN       = 10   # task-output/transcript mtime younger than this (min) = live
STALE_MIN      = 6    # heartbeat (active fleet): republish if published stats older than this
IDLE_PULSE_MIN = 15   # heartbeat (idle fleet): low-frequency pulse so freshness never freezes

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

def run_safe(args, timeout=15, cwd=None, extra_path=None):
    """Central choke point for EVERY external call (git / docker). Never raises;
    returns (ok, output). Bounded by timeout; missing binary / non-zero exit => ok=False."""
    try:
        env = None
        if extra_path:
            env = dict(os.environ)
            env["PATH"] = env.get("PATH", "") + os.pathsep + extra_path
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout, cwd=cwd, env=env)
        return (r.returncode == 0), (r.stdout if r.returncode == 0 else (r.stderr or "").strip())
    except Exception as e:
        return False, str(e)

def branch_head(repo, branch, grep=None):
    """Latest commit (subject, iso) on branch, optionally matching grep; None on failure."""
    if not os.path.isdir(repo): return None
    base = ["git", "-C", repo, "log", "-1", "--format=%s%x1f%cI"]
    for args in ([base+["--grep",grep,branch]] if grep else []) + [base+[branch]]:
        ok, out = run_safe(args)
        if ok and out.strip():
            subj, ci = out.strip().split("\x1f", 1)
            return subj, ci
    return None

# ---------- docker container status (degrades gracefully) ----------
def _uptime(status):
    # "Up 2 hours (healthy)" -> "2 hours"   |  "Exited (0) 4 minutes ago" -> ""
    if status.startswith("Up "):
        return status[3:].split(" (")[0].strip()
    return ""

def _ports(portstr):
    hosts = sorted(set(re.findall(r":(\d+)->", portstr or "")))
    return ", ".join(hosts)

def docker_status():
    args = ["docker", "ps", "-a", "--format", "{{json .}}"]
    if DOCKER_FILTER:
        args += ["--filter", "name=" + DOCKER_FILTER]
    ok, out = run_safe(args, timeout=20, extra_path=DOCKER_BIN)
    if not ok:
        return {"available": False, "error": (out or "docker unavailable")[:200],
                "containers": [], "summary": {}}
    conts = []
    for line in out.splitlines():
        line = line.strip()
        if not line: continue
        try: d = json.loads(line)
        except Exception: continue
        status = d.get("Status", "")
        state  = (d.get("State", "") or "").lower()
        health = (d.get("HealthStatus", "") or "none").lower() or "none"
        up = (state == "running") or status.startswith("Up")
        conts.append({"name": d.get("Names", ""), "state": state or ("running" if up else "exited"),
                      "status": status, "health": health, "uptime": _uptime(status),
                      "ports": _ports(d.get("Ports", "")), "up": up})
    conts.sort(key=lambda c: (not c["up"], c["health"] != "unhealthy", c["name"]))
    up_n     = sum(1 for c in conts if c["up"])
    healthy  = sum(1 for c in conts if c["health"] == "healthy")
    problem  = sum(1 for c in conts if (not c["up"]) or c["health"] == "unhealthy")
    return {"available": True, "containers": conts,
            "summary": {"running": up_n, "total": len(conts),
                        "healthy": healthy, "problem": problem}}

# ---------- stack health (config-driven HTTP smoke; degrades gracefully) ----------
def _http_probe(method, url, body, timeout):
    import urllib.request, urllib.error
    data = None; headers = {}
    if body is not None:
        data = json.dumps(body).encode(); headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method.upper(), headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.getcode(), None
    except urllib.error.HTTPError as e:
        return e.code, None                      # reached the service; it answered with a status
    except Exception as e:
        return None, str(e)[:120]                # unreachable / timeout

def stack_health():
    out = []
    for c in HEALTH_CHECKS:
        url = c.get("url")
        if not url: continue
        expect = c.get("expect", [200])
        code, err = _http_probe(c.get("method", "GET"), url, c.get("body"), c.get("timeout", 6))
        out.append({"name": c.get("name", "check"), "method": c.get("method", "GET").upper(),
                    "url": url, "status": code, "expect": expect,
                    "ok": (code in expect), "reachable": (code is not None), "error": err})
    return out

# ---------- commit velocity + recent-commits feed (real git signals) ----------
def commit_activity():
    tracks, feed = [], []
    for r in REPOS:
        path, br, track = r.get("path"), r.get("branch"), r.get("track", "")
        if not path or not os.path.isdir(path):
            tracks.append({"track": track, "branch": br, "available": False}); continue
        def count(since):
            ok, o = run_safe(["git", "-C", path, "rev-list", "--count", "--since", since, br])
            o = (o or "").strip()
            return int(o) if ok and o.isdigit() else None
        ok, o = run_safe(["git", "-C", path, "log", "-6", "--format=%h%x1f%cI%x1f%s", br])
        if ok:
            for line in o.splitlines():
                p = line.split("\x1f")
                if len(p) == 3:
                    feed.append({"hash": p[0], "at": p[1], "subject": p[2], "track": track})
        tracks.append({"track": track, "branch": br, "available": True,
                       "commits_24h": count("24 hours ago"), "commits_7d": count("7 days ago")})
    feed.sort(key=lambda x: x["at"], reverse=True)
    return {"tracks": tracks, "recent": feed[:8]}

# ---------- delivery progress + dual-unit ETA (per-track % is a manual estimate) ----------
PROGRESS_CFG = {
  "tracks": [
    {"track": "backend",  "label": "Backend",  "pct": 100, "remaining_hours": [0, 2], "remaining_sessions": [0, 1], "weight": 1},
    {"track": "frontend", "label": "Frontend", "pct": 90,  "remaining_hours": [3, 6], "remaining_sessions": [1, 2], "weight": 1},
  ],
  "basis": ("Per-track % and remaining hours/sessions are ESTIMATES from the resume docs "
            "(milestone completion), not measurements. Overall % is equal-weighted across the "
            "two tracks. The real, measured signal is commit velocity (below)."),
}
def progress_block():
    tr = PROGRESS_CFG["tracks"]; wsum = sum(t["weight"] for t in tr) or 1
    overall = round(sum(t["pct"] * t["weight"] for t in tr) / wsum)
    rh = [sum(t["remaining_hours"][0] for t in tr), sum(t["remaining_hours"][1] for t in tr)]
    rs = [sum(t["remaining_sessions"][0] for t in tr), sum(t["remaining_sessions"][1] for t in tr)]
    return {"overall_pct": overall, "remaining_hours": rh, "remaining_sessions": rs,
            "tracks": tr, "basis": PROGRESS_CFG["basis"], "estimate": True}

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

# ---------- dynamic fleet discovery ----------
# The roster is AUTO-DETECTED from the commander's subagents dir so new/relaunched workers are
# picked up automatically. Status is an HONEST 4-state disambiguation (no collapsing to "idle"):
#   live    — task-output .jsonl mtime < LIVE_MIN
#   idle    — alive subagent, but quiet (mtime >= LIVE_MIN, no stop flag)
#   stopped — harness cancelled it: meta.json has "stoppedByUser": true
#   done    — completed & retired mission (fixed historical list below)
ROLE_MAP = {
  "backend":         {"branch": "microservices-backend",  "repo": VAMS_BACKEND},
  "frontend":        {"branch": "microservices-frontend", "repo": VAMS_FRONTEND},
  "mission-control": {"branch": "microservices-frontend", "repo": VAMS_FRONTEND, "grep": "mission-control"},
}
def role_from_desc(desc):
    d = (desc or "").lower()
    if "backend"  in d: return "backend"
    if "frontend" in d: return "frontend"
    if "mission"  in d or "dashboard" in d: return "mission-control"
    if "provision" in d: return "provisioning"
    if "environment" in d: return "environment"
    return "worker"

def first_user_text(jsonl):
    try:
        for line in open(jsonl, encoding="utf-8", errors="ignore"):
            if '"user"' not in line: continue
            d = json.loads(line)
            if d.get("type") != "user": continue
            c = (d.get("message") or {}).get("content")
            if isinstance(c, list):
                c = " ".join(x.get("text", "") for x in c if isinstance(x, dict))
            if c and "caller" not in str(c)[:24]:
                return str(c)
    except Exception:
        pass
    return ""

def objective_of(jsonl, fallback):
    """A short focus line for an auto-detected worker: the first task-prompt line that reads
    like an objective, else the first non-boilerplate sentence, else the meta description."""
    txt = first_user_text(jsonl)
    for line in txt.splitlines():
        s = line.strip(" -*#\t")
        if any(s.lower().startswith(k) for k in ("mission", "objective", "goal", "your task", "task:", "focus")):
            return s[:180]
    for sent in re.split(r"(?<=[.\n])", txt):
        s = sent.strip()
        if s and not s.lower().startswith("you are") and len(s) > 20:
            return s[:180]
    return fallback

# completed & retired workers — historical, not in the live subagents dir
DONE_WORKERS = [
  dict(name="Provisioning worker", role="provisioning", agent_id="a90719d7d38e41e26", short_id="a90719d7",
       last_active="2026-07-10T06:21:47Z", log_bytes=488835,
       focus="COMPLETE - django-tenants acme org + JWT user + module/role grants shipped; real GET /me/ + roles now live (platform blockers unblocked)."),
  dict(name="Environment worker", role="environment", agent_id="a9bea1c0e2cc2bc25", short_id="a9bea1c0",
       last_active="2026-07-10T05:33:12Z", log_bytes=218452,
       focus="COMPLETE - JDK 17 + Android SDK toolchain installed; Android + iOS native builds done."),
]

def _status_for(age, stopped):
    if stopped: return "stopped"
    if age is None: return "idle"
    return "live" if age < LIVE_MIN else "idle"

def discover_workers():
    workers = []
    # commander — transcript-based; live while ANY subagent is
    la = commander_activity()
    c_live = age_of(la) is not None and age_of(la) < LIVE_MIN
    workers.append(dict(name="Commander", role="commander", branch=None,
        focus="Orchestrating the live fleet, integration, and dashboard publishing.",
        agent_id="a49f5cc3 (root)", short_id="a49f5cc3",
        head_commit=None, head_commit_at=None, head_ago_min=None,
        out_last_active=la, out_age_min=age_of(la), log_bytes=None,
        signal="coordinating (orchestrator)", status=("live" if c_live else "idle"), live=c_live))

    # dynamic subagents from the commander's subagents dir
    for meta_path in sorted(glob.glob(os.path.join(SUBDIR, "agent-*.meta.json"))):
        try: meta = json.load(open(meta_path, encoding="utf-8"))
        except Exception: meta = {}
        agent_id = os.path.basename(meta_path)[len("agent-"):-len(".meta.json")]
        desc     = meta.get("description", "Worker")
        stopped  = bool(meta.get("stoppedByUser"))
        role     = role_from_desc(desc)
        jsonl    = os.path.join(SUBDIR, "agent-" + agent_id + ".jsonl")
        la2 = lb = None
        if os.path.exists(jsonl):
            la2 = iso(datetime.datetime.fromtimestamp(os.path.getmtime(jsonl), datetime.timezone.utc))
            lb  = os.path.getsize(jsonl)
        age    = age_of(la2)
        status = _status_for(age, stopped)
        rm     = ROLE_MAP.get(role, {})
        branch = rm.get("branch")
        rec = dict(name=desc, role=role, branch=branch,
                   focus=(objective_of(jsonl, desc) if os.path.exists(jsonl) else desc),
                   agent_id=agent_id, short_id=agent_id[:8],
                   head_commit=None, head_commit_at=None, head_ago_min=None,
                   out_last_active=la2, out_age_min=age, log_bytes=lb,
                   signal=("stopped by user (harness-cancelled)" if stopped else "task-output mtime + git commit"),
                   status=status, live=(status == "live"))
        if branch and rm.get("repo") and status in ("live", "idle"):
            h = branch_head(rm["repo"], branch, rm.get("grep"))
            if h:
                rec["head_commit"], ci = h[0], h[1]
                rec["head_commit_at"] = ci
                rec["head_ago_min"]   = age_of(iso(parse(ci)))
        workers.append(rec)

    # fixed done workers
    for w in DONE_WORKERS:
        workers.append(dict(name=w["name"], role=w["role"], branch=None, focus=w["focus"],
            agent_id=w["agent_id"], short_id=w["short_id"],
            head_commit=None, head_commit_at=None, head_ago_min=None,
            out_last_active=w["last_active"], out_age_min=age_of(w["last_active"]),
            log_bytes=w["log_bytes"], signal="mission complete", status="done", live=False))

    rank = {"live": 0, "idle": 1, "stopped": 2, "done": 3}
    workers.sort(key=lambda w: (0 if w["role"] == "commander" else 1, rank.get(w["status"], 9), w["name"]))
    return workers

# ---------- build sessions (transcripts + the discovered subagents) ----------
def build_sessions(workers):
    tx, subs = [], []
    for sid, group, label, o, cr, cc, n, la in BASELINE:
        f = find_transcript(sid)
        if f:
            o, cr, cc, n = token_usage(f)
            la = iso(datetime.datetime.fromtimestamp(os.path.getmtime(f), datetime.timezone.utc))
        if sid == "a49f5cc3":                       # commander: live while any subagent is
            la = commander_activity() or la
        live = age_of(la) is not None and age_of(la) < LIVE_MIN
        s = dict(id=sid, group=group, group_label=label,
                 output_tokens=o, cache_read_tokens=cr, cache_creation_tokens=cc, msgs=n,
                 last_active=la, age_min=age_of(la), live=live,
                 status=("live" if live else "idle"), subagent=False)
        if sid == "a49f5cc3":
            s["full_id"] = CMD_SID
        tx.append(s)

    for w in workers:
        if w["role"] == "commander":
            continue
        subs.append(dict(id=w["short_id"], full_id=w["agent_id"], group=w["role"],
                         group_label=w["name"], output_tokens=None, cache_read_tokens=None,
                         cache_creation_tokens=None, msgs=None, last_active=w["out_last_active"],
                         age_min=w["out_age_min"], live=(w["status"] == "live"),
                         status=w["status"], subagent=True, log_bytes=w["log_bytes"]))
    return tx, subs

# ---------- publisher freshness (is the heartbeat scheduled task actually running?) ----------
def publisher_status():
    ok, out = run_safe(["powershell", "-NoProfile", "-Command",
        "(Get-ScheduledTask -TaskName 'VAMS-MissionControl-Heartbeat' -ErrorAction SilentlyContinue).State"],
        timeout=20)
    state = (out or "").strip() if ok else ""
    return {"task": "VAMS-MissionControl-Heartbeat", "state": (state or "unknown"),
            "paused": (state.lower() == "disabled"), "checked_at": iso(now)}

def build_stats():
    workers = discover_workers()
    tx, subs = build_sessions(workers)
    sessions = tx + subs
    out_t = sum(s["output_tokens"] or 0 for s in tx)
    cr_t  = sum(s["cache_read_tokens"] or 0 for s in tx)
    cc_t  = sum(s["cache_creation_tokens"] or 0 for s in tx)
    msg_t = sum(s["msgs"] or 0 for s in tx)
    totals = dict(output_tokens=out_t, cache_read_tokens=cr_t, cache_creation_tokens=cc_t,
                  msgs=msg_t, sessions=len(sessions),
                  live_sessions=sum(1 for s in sessions if s["live"]),
                  transcript_sessions=len(tx), subagent_sessions=len(subs),
                  workers=len(workers), live_workers=sum(1 for w in workers if w["status"] == "live"),
                  stopped_workers=sum(1 for w in workers if w["status"] == "stopped"))
    acts = [w["out_last_active"] for w in workers if w.get("out_last_active")]
    last_activity = max(acts) if acts else None   # iso Z strings sort chronologically
    return dict(updated=iso(now), heartbeat_at=iso(now), last_activity=last_activity,
                generated_by="mission-control (ad796e2c)", schema=5,
                totals=totals, workers=workers, sessions=sessions, total_tokens=out_t,
                progress=progress_block(), docker=docker_status(),
                health=stack_health(), commits=commit_activity(),
                publisher=publisher_status())

def write_stats(stats):
    with open(STATS_PATH, "w", encoding="utf-8") as fh:
        json.dump(stats, fh, indent=2); fh.write("\n")

def sync_html(stats):
    if not os.path.exists(HTML_PATH): return
    html = open(HTML_PATH, encoding="utf-8").read()
    baked = json.dumps(stats, indent=2)
    # function replacement: baked JSON may contain \uXXXX escapes which re would otherwise
    # try to interpret as regex escapes in a string replacement.
    html = re.sub(r"/\*__STATS__\*/.*?/\*__/STATS__\*/",
                  lambda _m: "/*__STATS__*/" + baked + "/*__/STATS__*/", html, flags=re.S)
    open(HTML_PATH, "w", encoding="utf-8").write(html)

def live_key(stats):
    return sorted(s["id"] for s in stats["sessions"] if s["live"]), \
           sorted(w["short_id"] for w in stats["workers"] if w["live"])

def heartbeat_decision(old, new):
    """NEVER-GO-STALE push policy. Push when the live-set changed, or when the published stats
    are older than the applicable threshold: STALE_MIN with an active fleet, IDLE_PULSE_MIN when
    the fleet is fully idle (a low-frequency pulse so freshness never freezes — bounded, no spam).
    Returns (push:bool, any_live:bool, age:float, threshold:int, changed:bool)."""
    any_live = new["totals"]["live_workers"] > 0
    age = age_of(old.get("updated")) if old else None
    age = 1e9 if age is None else age
    changed = (live_key(old) != live_key(new)) if old else True
    threshold = STALE_MIN if any_live else IDLE_PULSE_MIN
    return (changed or age >= threshold), any_live, age, threshold, changed

def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    stats = build_stats()
    if mode == "--heartbeat":
        # NEVER-GO-STALE: push whenever there is live activity OR the live-set changed; and even
        # when the fleet is fully idle, emit a low-frequency IDLE PULSE (<= once / IDLE_PULSE_MIN)
        # so `updated`/`heartbeat_at` never freeze and the page never falsely reads as dead.
        # Idle pulses are throttled to at most ~4/hour, so no return of the junk-commit spam.
        try:
            old = json.load(open(STATS_PATH, encoding="utf-8"))
        except Exception:
            old = None
        do_push, any_live, age, threshold, changed = heartbeat_decision(old, stats)
        if do_push:
            write_stats(stats)
            print("PUSH %s updated=%s live_workers=%d age=%.1f/%d%s"
                  % ("active" if any_live else "idle-pulse", stats["updated"],
                     stats["totals"]["live_workers"], age, threshold,
                     " (live-set-changed)" if changed else ""))
            sys.exit(0)
        print("SKIP any_live=%s age=%.1f/%d changed=%s" % (any_live, age, threshold, changed))
        sys.exit(3)

    write_stats(stats)
    if mode == "--build":
        sync_html(stats)
    print("wrote %s | updated=%s live_workers=%d live_sessions=%d sessions=%d"
          % (STATS_PATH, stats["updated"], stats["totals"]["live_workers"],
             stats["totals"]["live_sessions"], stats["totals"]["sessions"]))

if __name__ == "__main__":
    main()
