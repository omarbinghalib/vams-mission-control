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
STALE_MIN      = 2    # heartbeat (active fleet): republish if published stats older than this
                      # (short so an active fleet refreshes ~every 1-2 min and the page feels live;
                      #  a live worker whose details changed still pushes on the very next cycle)
IDLE_PULSE_MIN = 15   # heartbeat (idle fleet): low-frequency pulse so freshness never freezes
                      # (anti-spam: a fully idle fleet still pushes at most ~4x/hour)
RECENT_MIN     = 120  # roster/sessions: "current" = live or active within this window (2h)
# Under heavy fleet load (100% CPU / near-zero free RAM, dozens of concurrent workers) the EXPENSIVE
# external probes (docker CLI ~9s, the PowerShell scheduled-task check ~14s, HTTP health) can starve
# the heartbeat and blow the task's 2-min ExecutionTimeLimit — the run gets killed before it writes,
# so stats.json freezes. These TTLs let those slow probes be RE-USED (carried forward) from the last
# stats.json instead of paid every single cycle, so the cheap live core (roster/tokens/progress from
# local file reads) always republishes within seconds.
DOCKER_TTL_MIN = 5    # re-probe docker at most this often; else carry forward the last good result
PUB_TTL_MIN    = 10   # re-check the scheduled-task publisher state at most this often

now = datetime.datetime.now(datetime.timezone.utc)

def _load_prev():
    """Best-effort load of the previously-published stats.json — the source for carry-forward of the
    slow probes. Never raises (atomic writes guarantee it's whole, but be defensive anyway)."""
    try:
        return json.load(open(STATS_PATH, encoding="utf-8"))
    except Exception:
        return None

# ---------- helpers ----------
def parse(ts):
    """Parse an ISO-8601 timestamp defensively — NEVER raises. Python 3.9's
    datetime.fromisoformat rejects a trailing 'Z' (and any malformed value), which would crash the
    whole heartbeat and leave stats.json frozen; so we normalise 'Z'->'+00:00', coerce naive
    datetimes to UTC, and swallow anything unparseable (e.g. a truncated value from a run that was
    killed mid-write) by returning None. Callers already treat None as 'unknown'."""
    if not ts: return None
    try:
        d = datetime.datetime.fromisoformat(str(ts).strip().replace("Z", "+00:00"))
        return d if d.tzinfo is not None else d.replace(tzinfo=datetime.timezone.utc)
    except Exception:
        return None

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

def docker_status(prev=None):
    pd = (prev or {}).get("docker") if isinstance(prev, dict) else None
    # CARRY-FORWARD CACHE: the docker CLI is sluggish (~9s, worse under load). Re-probe at most once
    # per DOCKER_TTL_MIN; otherwise reuse the last good block (with its own checked_at so staleness
    # stays honest). This is what keeps the heartbeat fast enough to finish inside the 2-min limit.
    if pd and pd.get("available") and (age_of(pd.get("checked_at")) or 1e9) < DOCKER_TTL_MIN:
        pd = dict(pd); pd["carried"] = True
        return pd
    args = ["docker", "ps", "-a", "--format", "{{json .}}"]
    if DOCKER_FILTER:
        args += ["--filter", "name=" + DOCKER_FILTER]
    # Bounded single probe (no doubling retry — under load a retry only compounds the starvation).
    # Still `-a` so stopped containers show red. On failure, reuse the last good block if we have one
    # rather than flapping to a false "unavailable" on a transient timeout.
    ok, out = run_safe(args, timeout=25, extra_path=DOCKER_BIN)
    if not ok:
        if pd and pd.get("available"):
            pd = dict(pd); pd["carried"] = True
            return pd
        return {"available": False, "error": (out or "docker unavailable")[:200],
                "containers": [], "summary": {}, "checked_at": iso(now)}
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
                        "healthy": healthy, "problem": problem}, "checked_at": iso(now)}

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

def stack_health(prev=None):
    prev_by = {}
    if isinstance(prev, dict):
        for h in (prev.get("health") or []):
            prev_by[h.get("url")] = h
    out = []
    for c in HEALTH_CHECKS:
        url = c.get("url")
        if not url: continue
        expect = c.get("expect", [200])
        # Tight 3s timeout so two checks cost <=6s even under load. If a probe is unreachable but we
        # had a reachable reading last cycle, carry that forward (marked stale) instead of flapping.
        code, err = _http_probe(c.get("method", "GET"), url, c.get("body"), c.get("timeout", 3))
        if code is None and prev_by.get(url, {}).get("reachable"):
            h = dict(prev_by[url]); h["carried"] = True
            out.append(h); continue
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

# ---------- delivery progress + dual-unit ETA — DERIVED from tasks.json ----------
# Overall % and per-track % come straight from the task tracker: done=100%, in_progress=50%,
# pending=0%. So whenever the commander edits tasks (tasks.sh), the progress + ETA AUTO-RESYNC
# with zero code change and can never lag behind scope. ETA hours/sessions scale off the same
# remaining-work ratio. Everything here stays honestly labelled an ESTIMATE — the real, measured
# signal is commit velocity (below).
TASKS_PATH     = os.path.join(REPO, "tasks.json")
HOURS_PER_UNIT = (1.5, 3.0)   # remaining hours   per remaining task-unit (pending=1, in_progress=0.5)
SESS_PER_UNIT  = (0.4, 0.8)   # remaining sessions per remaining task-unit
TRACK_LABELS   = {"backend": "Backend", "frontend": "Frontend", "mission-control": "Mission-control",
                  "audit": "Capstones", "deploy": "Deployment"}
TRACK_ORDER    = ["backend", "frontend", "deploy", "audit", "mission-control"]
ARCHIVE_TRACKS = {"archive"}   # shipped prior-session work — shown as a compact note, NOT counted
                               # in the live delivery % / ETA (which must reflect CURRENT work only)

def _load_tasks():
    """CURRENT, active task set only — archived (shipped prior-session) rows are excluded so the
    delivery-progress and ETA derive from the work actually in flight, never re-counting old 100%s."""
    try:
        t = json.load(open(TASKS_PATH, encoding="utf-8"))
        t = t if isinstance(t, list) else []
        return [x for x in t if x.get("track") not in ARCHIVE_TRACKS]
    except Exception:
        return []

def _task_weights(tasks):
    """(completed_weight, total, remaining_units) with done=1, in_progress=0.5, pending=0."""
    done   = sum(1 for t in tasks if t.get("status") == "done")
    inprog = sum(1 for t in tasks if t.get("status") == "in_progress")
    total  = len(tasks)
    return (done + 0.5 * inprog), total, ((total - done) - 0.5 * inprog)

def _hrs(u):  return [round(u * HOURS_PER_UNIT[0]), round(u * HOURS_PER_UNIT[1])]
def _sess(u): return [round(u * SESS_PER_UNIT[0]),  round(u * SESS_PER_UNIT[1])]

def progress_block():
    tasks = _load_tasks()
    if not tasks:
        return {"overall_pct": 0, "remaining_hours": [0, 0], "remaining_sessions": [0, 0],
                "tracks": [], "estimate": True,
                "basis": "tasks.json unavailable — progress cannot be derived."}
    cw, total, rem = _task_weights(tasks)
    overall = round(cw / total * 100) if total else 0
    # per-track, grouped straight from the tracker
    groups = {}
    for t in tasks:
        groups.setdefault(t.get("track", "other"), []).append(t)
    tracks_out = []
    for tr in TRACK_ORDER + [k for k in groups if k not in TRACK_ORDER]:
        items = groups.get(tr)
        if not items:
            continue
        cwt, tot, remt = _task_weights(items)
        tracks_out.append({"track": tr, "label": TRACK_LABELS.get(tr, tr.title()),
                           "pct": round(cwt / tot * 100) if tot else 0,
                           "remaining_hours": _hrs(remt), "remaining_sessions": _sess(remt),
                           "weight": 1})
    return {"overall_pct": overall, "remaining_hours": _hrs(rem), "remaining_sessions": _sess(rem),
            "tracks": tracks_out, "estimate": True,
            "basis": ("Overall % and per-track % are DERIVED from the task tracker "
                      "(done=100%, in-progress=50%, pending=0%) — they auto-resync whenever tasks "
                      "are updated, so the number can't lag scope. Remaining hours/sessions scale "
                      "off the same remaining-work ratio. All ESTIMATES; the measured signal is "
                      "commit velocity (below).")}

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
# The ACTIVE roster (build_stats) is then reduced to the CURRENT fleet only — the commander plus
# any worker live or active within RECENT_MIN. Everything older is collapsed into a single
# `history` summary count (see build_stats) instead of being carried as dozens of dead rows.
# There is NO hardcoded worker list: what shows is exactly what is (or was just) running now.
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
        # Per-worker tokens + turns: RE-READ from the worker's OWN transcript every cycle so the
        # numbers track live activity instead of showing n/a. Bounded to the CURRENT fleet (live or
        # recently active) to keep each heartbeat fast and off the collapsed long tail.
        out_tok = cr_tok = cc_tok = msgs = None
        current = (status == "live") or (status == "idle" and age is not None and age < RECENT_MIN)
        if os.path.exists(jsonl) and current:
            out_tok, cr_tok, cc_tok, msgs = token_usage(jsonl)
        rec = dict(name=desc, role=role, branch=branch,
                   focus=(objective_of(jsonl, desc) if os.path.exists(jsonl) else desc),
                   agent_id=agent_id, short_id=agent_id[:8],
                   head_commit=None, head_commit_at=None, head_ago_min=None,
                   out_last_active=la2, out_age_min=age, log_bytes=lb,
                   output_tokens=out_tok, cache_read_tokens=cr_tok,
                   cache_creation_tokens=cc_tok, msgs=msgs,
                   signal=("stopped by user (harness-cancelled)" if stopped else "task-output mtime + git commit"),
                   status=status, live=(status == "live"))
        if branch and rm.get("repo") and status in ("live", "idle"):
            h = branch_head(rm["repo"], branch, rm.get("grep"))
            if h:
                rec["head_commit"], ci = h[0], h[1]
                rec["head_commit_at"] = ci
                rec["head_ago_min"]   = age_of(iso(parse(ci)))
        workers.append(rec)

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
                         group_label=w["name"], output_tokens=w.get("output_tokens"),
                         cache_read_tokens=w.get("cache_read_tokens"),
                         cache_creation_tokens=w.get("cache_creation_tokens"), msgs=w.get("msgs"),
                         last_active=w["out_last_active"],
                         age_min=w["out_age_min"], live=(w["status"] == "live"),
                         status=w["status"], subagent=True, log_bytes=w["log_bytes"]))
    return tx, subs

# ---------- publisher freshness (is the heartbeat scheduled task actually running?) ----------
def publisher_status(prev=None):
    pp = (prev or {}).get("publisher") if isinstance(prev, dict) else None
    # Spawning PowerShell is the single most expensive probe (~14s under memory pressure), and the
    # scheduled-task state changes rarely — so re-check at most once per PUB_TTL_MIN and otherwise
    # carry the last reading forward. This alone removes the biggest chunk of the per-cycle cost.
    if pp and (age_of(pp.get("checked_at")) or 1e9) < PUB_TTL_MIN:
        pp = dict(pp); pp["carried"] = True
        return pp
    ok, out = run_safe(["powershell", "-NoProfile", "-Command",
        "(Get-ScheduledTask -TaskName 'VAMS-MissionControl-Heartbeat' -ErrorAction SilentlyContinue).State"],
        timeout=8)
    if not ok and pp:                     # probe failed under load — keep the last known state
        pp = dict(pp); pp["carried"] = True
        return pp
    state = (out or "").strip() if ok else ""
    return {"task": "VAMS-MissionControl-Heartbeat", "state": (state or "unknown"),
            "paused": (state.lower() == "disabled"), "checked_at": iso(now)}

def build_stats(prev=None):
    if prev is None:
        prev = _load_prev()   # source for carry-forward of the slow probes
    all_workers = discover_workers()
    # RECENCY: the ACTIVE roster is the CURRENT fleet ONLY — the commander plus any worker that is
    # live or was active within RECENT_MIN. Everything older (a prior session's finished workers)
    # is DROPPED from the roster and collapsed into a single `history` count, so the page never
    # shows 3-day-old retired workers as the current fleet. Liveness stays the honest 4-state model
    # for the workers that remain (live / idle / stopped).
    def _recent(w):
        if w["role"] == "commander":
            return True
        if w["status"] == "live":
            return True
        age = w.get("out_age_min")
        return age is not None and age < RECENT_MIN
    current = [w for w in all_workers if _recent(w)]
    history = [w for w in all_workers if not _recent(w)]
    for w in current:
        w["recent"] = True

    workers = current
    tx, subs = build_sessions(workers)   # sessions table carries ONLY the current subagents
    sessions = tx + subs
    for s in sessions:
        s["recent"] = True

    # collapsed history summary (count + freshest timestamp + status breakdown) — the retired
    # prior-session fleet, kept as one honest number instead of dozens of dead rows.
    hist_by = {}
    for w in history:
        k = w.get("status", "idle")
        hist_by[k] = hist_by.get(k, 0) + 1
    hist_acts = [w["out_last_active"] for w in history if w.get("out_last_active")]
    history_summary = dict(
        workers=len(history), by_status=hist_by,
        last_active=(max(hist_acts) if hist_acts else None),
        note=("Prior-session fleet — productization, full local deployment, and the parity + "
              "knowledge capstones (all shipped). Retired; not part of the current redesign work."))

    out_t = sum(s["output_tokens"] or 0 for s in tx)
    cr_t  = sum(s["cache_read_tokens"] or 0 for s in tx)
    cc_t  = sum(s["cache_creation_tokens"] or 0 for s in tx)
    msg_t = sum(s["msgs"] or 0 for s in tx)
    totals = dict(output_tokens=out_t, cache_read_tokens=cr_t, cache_creation_tokens=cc_t,
                  msgs=msg_t, sessions=len(sessions),
                  live_sessions=sum(1 for s in sessions if s["live"]),
                  transcript_sessions=len(tx), subagent_sessions=len(subs),
                  workers=len(workers), live_workers=sum(1 for w in workers if w["status"] == "live"),
                  stopped_workers=sum(1 for w in workers if w["status"] == "stopped"),
                  current_workers=len(workers), history_workers=len(history),
                  recent_window_min=RECENT_MIN)
    acts = [w["out_last_active"] for w in workers if w.get("out_last_active")]
    last_activity = max(acts) if acts else None   # iso Z strings sort chronologically
    return dict(updated=iso(now), heartbeat_at=iso(now), last_activity=last_activity,
                generated_by="mission-control (ad796e2c)", schema=5,
                totals=totals, workers=workers, sessions=sessions, total_tokens=out_t,
                history=history_summary,
                progress=progress_block(), docker=docker_status(prev),
                health=stack_health(prev), commits=commit_activity(),
                publisher=publisher_status(prev))

def write_stats(stats):
    """ATOMIC write: serialise to a temp file in the same dir, fsync, then os.replace() onto the
    final path. os.replace is atomic on Windows + POSIX, so a reader (or the next heartbeat) can
    only ever see the OLD or the fully-NEW file — never a half-written/corrupt one. This matters
    because the scheduled task's 2-min ExecutionTimeLimit can kill a slow run mid-write; without
    the atomic swap that would leave a truncated stats.json that then crashes the next parse."""
    tmp = STATS_PATH + "." + str(os.getpid()) + ".tmp"   # pid-unique so concurrent runs never clash
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(stats, fh, indent=2); fh.write("\n")
        fh.flush(); os.fsync(fh.fileno())
    # os.replace is atomic, but on WINDOWS it raises PermissionError if another process momentarily
    # holds the target open (a reader, git, or an overlapping heartbeat run). Retry briefly to ride
    # out the transient lock; as a last resort under sustained contention, overwrite in place so the
    # dashboard still refreshes rather than freezing.
    for _ in range(10):
        try:
            os.replace(tmp, STATS_PATH); return
        except PermissionError:
            time.sleep(0.2)
    try:
        with open(STATS_PATH, "w", encoding="utf-8") as fh:
            json.dump(stats, fh, indent=2); fh.write("\n")
    finally:
        try: os.remove(tmp)
        except OSError: pass

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

def live_sig(stats):
    """Mutable DETAILS of the live fleet — the fields that move as a live worker actually works:
    its task-output timestamp, log size, tokens/turns, status, and latest commit. Lets the
    heartbeat republish WITHIN a cycle whenever a live worker's details change, so the page never
    looks frozen between the coarse staleness windows — without no-op spam when the fleet is quiet."""
    sig = []
    for w in sorted(stats["workers"], key=lambda w: w.get("short_id") or ""):
        if w.get("status") == "live" or w.get("live"):
            sig.append((w.get("short_id"), w.get("status"), w.get("out_last_active"),
                        w.get("log_bytes"), w.get("head_commit"),
                        w.get("output_tokens"), w.get("msgs")))
    return sig

def heartbeat_decision(old, new):
    """NEVER-GO-STALE push policy. Push when: the live-SET changed; OR (while any worker is live)
    the live fleet's DETAILS changed this cycle — tokens/turns/last-active/commit — so live workers
    refresh on the normal ~3-min heartbeat cadence instead of lagging up to STALE_MIN; OR the
    published stats are older than the applicable threshold (STALE_MIN active / IDLE_PULSE_MIN idle,
    a low-frequency pulse so freshness never freezes). Bounded — no push when nothing changed.
    Returns (push:bool, any_live:bool, age:float, threshold:int, changed:bool)."""
    any_live = new["totals"]["live_workers"] > 0
    age = age_of(old.get("updated")) if old else None
    age = 1e9 if age is None else age
    set_changed = (live_key(old) != live_key(new)) if old else True
    sig_changed = (live_sig(old) != live_sig(new)) if old else True
    threshold = STALE_MIN if any_live else IDLE_PULSE_MIN
    changed = set_changed or (any_live and sig_changed)
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
