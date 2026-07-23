#!/usr/bin/env python3
"""
sysres_gen.py -- standalone, OFF the heartbeat critical path.

Probes host CPU/mem (PowerShell CIM) and aggregate `docker stats --no-stream`, EACH individually
hard-timeout-bounded, and atomically writes sysres.json next to this script's repo root. Runs from
its OWN ~5-min OS scheduled task (VAMS-MissionControl-Sysres, see scripts/sysres.sh +
scripts/sysres_hidden.vbs) -- completely decoupled from gen_stats.py's heartbeat cycle.

WHY SEPARATE: the 2026-07-23 incident showed `docker stats` alone can take 2+ minutes under heavy
fleet load -- more than heartbeat.sh's whole 150s hard cap. Re-probing it every heartbeat tick (even
budget-guarded) risks reproducing that exact stall. Moving it to its own coarser-cadence, independently
timed-out process means the freshness heartbeat can NEVER be blocked by this probe again, at the cost
of a coarser (~5 min) sysres refresh -- an acceptable tradeoff for a "nice to have" panel.

gen_stats.py itself is intentionally left UNTOUCHED by this file: it has no `system_resources` key and
never will unless a future change explicitly adds one back inside its own budget-guarded phases.

Degrades honestly: on ANY probe failure this still writes a snapshot with "available": false (or with
whichever half succeeded) and an "error"/"*_error" string -- never raises, never leaves a stale file
from a half-written state (atomic os.replace), and never fakes a number. Always exits 0.
"""
import datetime
import json
import os
import re
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
OUT = os.path.join(REPO, "sysres.json")

HOST_TIMEOUT_SEC = 20
DOCKER_TIMEOUT_SEC = 25
CARRY_MAX_MIN = 20     # don't carry-forward a reading older than this (same TTL pattern gen_stats.py
                       # uses for its own docker/publisher probes) -- an honest "unavailable" beats an
                       # indefinitely-recycled stale number that would otherwise never look stale
                       # (the top-level checked_at refreshes every cycle regardless of carry-forward).


def iso(dt):
    return dt.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(ts):
    if not ts:
        return None
    try:
        d = datetime.datetime.fromisoformat(str(ts).strip().replace("Z", "+00:00"))
        return d if d.tzinfo is not None else d.replace(tzinfo=datetime.timezone.utc)
    except Exception:
        return None


def run_safe(args, timeout):
    """Never raises; returns (ok, output)."""
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return (r.returncode == 0), (r.stdout if r.returncode == 0 else (r.stderr or "").strip())
    except Exception as e:
        return False, str(e)


def host_stats():
    """Host-wide CPU% (avg across logical processors) + memory via PowerShell CIM. Hard-bounded."""
    ps = (
        "$ErrorActionPreference='Stop';"
        "$os=Get-CimInstance Win32_OperatingSystem;"
        "$cpu=(Get-CimInstance Win32_Processor | Measure-Object -Property LoadPercentage -Average).Average;"
        "$total=[math]::Round($os.TotalVisibleMemorySize/1024);"
        "$free=[math]::Round($os.FreePhysicalMemory/1024);"
        "$used=$total-$free;"
        "$pct=if($total -gt 0){[math]::Round(($used/$total)*100,1)}else{$null};"
        "$o=[ordered]@{cpu_pct=$cpu;mem_pct=$pct;mem_used_mb=$used;mem_total_mb=$total};"
        "$o | ConvertTo-Json -Compress"
    )
    ok, out = run_safe(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                        timeout=HOST_TIMEOUT_SEC)
    if not ok or not out.strip():
        return None, (out or "powershell host probe failed/timed out")[:200]
    try:
        d = json.loads(out.strip())
        return {"cpu_pct": d.get("cpu_pct"), "mem_pct": d.get("mem_pct"),
                "mem_used_mb": d.get("mem_used_mb"), "mem_total_mb": d.get("mem_total_mb")}, None
    except Exception as e:
        return None, ("parse error: " + str(e))[:200]


def docker_stats():
    """Aggregate (sum across containers) CPU% + memory via `docker stats --no-stream`. Hard-bounded,
    single attempt (no retry -- under load a retry only compounds the exact starvation that caused
    the 2026-07-23 incident)."""
    ok, out = run_safe(["docker", "stats", "--no-stream", "--format", "{{json .}}"],
                        timeout=DOCKER_TIMEOUT_SEC)
    if not ok:
        return None, (out or "docker stats unavailable/timed out")[:200]
    total_cpu = 0.0
    total_mem_mb = 0.0
    n = 0
    unit_mb = {"b": 1 / 1048576.0, "kib": 1 / 1024.0, "mib": 1.0, "gib": 1024.0, "tib": 1024.0 * 1024}
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        n += 1
        cpu = (d.get("CPUPerc") or "0%").strip().rstrip("%")
        try:
            total_cpu += float(cpu)
        except Exception:
            pass
        mem = d.get("MemUsage") or ""              # e.g. "123.4MiB / 2GiB"
        m = re.match(r"([\d.]+)\s*([KMGT]?i?B)", mem, re.IGNORECASE)
        if m:
            val = float(m.group(1))
            unit = m.group(2).lower()
            total_mem_mb += val * unit_mb.get(unit, 1.0)
    return {"containers": n, "total_cpu_pct": round(total_cpu, 1),
            "total_mem_mb": round(total_mem_mb, 1)}, None


def _load_prev():
    """Best-effort load of the previously-written sysres.json -- the source for carry-forward when
    a probe fails/times out this cycle (same resilience pattern gen_stats.py already uses for its
    own docker/publisher/health probes: reuse the last GOOD reading, marked carried, rather than
    showing a blank card every time this one probe has a rough cycle under load)."""
    try:
        return json.load(open(OUT, encoding="utf-8"))
    except Exception:
        return None


def _carry(key, fresh, prev, now):
    """Return (value, carried, as_of) for one sub-block: use the fresh reading if the probe
    succeeded this cycle; otherwise fall back to the previous GOOD reading IF it's not older than
    CARRY_MAX_MIN (else drop it -- honest 'unavailable' beats a stale number recycled forever)."""
    if fresh is not None:
        return fresh, False, iso(now)
    if isinstance(prev, dict) and prev.get(key):
        prev_as_of = parse_iso(prev.get(key + "_as_of") or prev.get("checked_at"))
        if prev_as_of is not None:
            age_min = (now - prev_as_of).total_seconds() / 60
            if age_min <= CARRY_MAX_MIN:
                return prev[key], True, iso(prev_as_of)
    return None, False, None


def main():
    now = datetime.datetime.now(datetime.timezone.utc)
    prev = _load_prev()
    fresh_host, herr = host_stats()
    fresh_docker, derr = docker_stats()

    host, host_carried, host_as_of = _carry("host", fresh_host, prev, now)
    docker, docker_carried, docker_as_of = _carry("docker", fresh_docker, prev, now)

    available = bool(host) or bool(docker)
    out = {
        "available": available,
        "checked_at": iso(now),
        "host": host or {},
        "docker": docker or {},
        "host_carried": host_carried,
        "docker_carried": docker_carried,
        "host_as_of": host_as_of,
        "docker_as_of": docker_as_of,
    }
    if herr:
        out["host_error"] = herr
    if derr:
        out["docker_error"] = derr
    if not available:
        out["error"] = herr or derr or "both probes failed"

    tmp = OUT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=1)
    os.replace(tmp, OUT)          # atomic swap -- a concurrent reader never sees a half-written file
    print("sysres_gen OK available={} host_err={} docker_err={} host_carried={} docker_carried={}"
          .format(available, herr, derr, host_carried, docker_carried))


if __name__ == "__main__":
    main()
