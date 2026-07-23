#!/usr/bin/env python3
"""
sysres_gen.py -- standalone, OFF the heartbeat critical path.

Probes host CPU/mem/disk/network/uptime/temperature (ONE combined PowerShell CIM call, each
sub-section independently try/catch'd inside the PS script so one bad sensor can't blank the rest)
plus per-container + aggregate `docker stats`. Both probes are individually hard-timeout-bounded.
Atomically writes sysres.json next to this script's repo root. Runs from its OWN ~5-min OS
scheduled task (VAMS-MissionControl-Sysres, see scripts/sysres.sh + scripts/sysres_hidden.vbs) --
completely decoupled from gen_stats.py's heartbeat cycle.

WHY SEPARATE: the 2026-07-23 incident showed `docker stats` alone can take 2+ minutes under heavy
fleet load -- more than heartbeat.sh's whole 150s hard cap. Re-probing it every heartbeat tick (even
budget-guarded) risks reproducing that exact stall. Moving it to its own coarser-cadence, independently
timed-out process means the freshness heartbeat can NEVER be blocked by this probe again, at the cost
of a coarser (~5 min) sysres refresh -- an acceptable tradeoff for a "nice to have" panel.

gen_stats.py itself is intentionally left UNTOUCHED by this file: it has no `system_resources` key and
never will unless a future change explicitly adds one back inside its own budget-guarded phases.

Degrades honestly: on ANY probe/sensor failure this still writes a snapshot with the failed section
either carried-forward from the last good reading (capped at CARRY_MAX_MIN) or explicitly absent with
an "*_error" string -- NEVER fabricated. Temperature in particular: this host has no exposed ACPI
thermal zone (confirmed: MSAcpi_ThermalZoneTemperature raises "Not supported"), so temp_c is reported
as unavailable rather than a made-up number -- if a future host DOES expose one, it will just start
reporting real values with no code change needed. Always exits 0, never leaves a half-written file
(atomic os.replace).
"""
import datetime
import json
import os
import re
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
OUT = os.path.join(REPO, "sysres.json")

HOST_TIMEOUT_SEC = 40     # one combined CIM call (cpu/mem/disk/net/uptime/temp) -- measured ~20-30s
DOCKER_TIMEOUT_SEC = 30
CARRY_MAX_MIN = 20     # don't carry-forward a reading older than this (same TTL pattern gen_stats.py
                       # uses for its own docker/publisher probes) -- an honest "unavailable" beats an
                       # indefinitely-recycled stale number that would otherwise never look stale
                       # (the top-level checked_at refreshes every cycle regardless of carry-forward).

# ONE combined CIM probe (host cpu/mem/uptime/process-count, disks, network rate, thermal) instead of
# several separate powershell spawns -- fewer subprocess starts is cheaper under the exact heavy-load
# conditions this design exists to survive. Each section is independently try/catch'd so one bad
# sensor (e.g. no ACPI thermal zone) degrades ONLY that section, not the whole probe.
_HOST_PS = r"""
$ErrorActionPreference='Stop'
$result = [ordered]@{}
try {
  $os=Get-CimInstance Win32_OperatingSystem
  $procsAll=Get-CimInstance Win32_Processor
  $cpu=($procsAll | Measure-Object -Property LoadPercentage -Average).Average
  $cores=@($procsAll | ForEach-Object { $_.LoadPercentage })
  $total=[math]::Round($os.TotalVisibleMemorySize/1024)
  $free=[math]::Round($os.FreePhysicalMemory/1024)
  $used=$total-$free
  $pct=if($total -gt 0){[math]::Round(($used/$total)*100,1)}else{$null}
  $uptimeSec=[math]::Round(((Get-Date) - $os.LastBootUpTime).TotalSeconds)
  $procCount=(Get-Process).Count
  $result.host = [ordered]@{cpu_pct=$cpu; cores=$cores; mem_pct=$pct; mem_used_mb=$used; mem_total_mb=$total; uptime_sec=$uptimeSec; process_count=$procCount}
} catch { $result.host_error = $_.Exception.Message }
try {
  $disks = @(Get-CimInstance Win32_LogicalDisk -Filter "DriveType=3" | ForEach-Object {
    $sz=[math]::Round($_.Size/1GB,1); $fr=[math]::Round($_.FreeSpace/1GB,1)
    [ordered]@{drive=$_.DeviceID; used_gb=[math]::Round($sz-$fr,1); total_gb=$sz; pct=if($sz -gt 0){[math]::Round((($sz-$fr)/$sz)*100,1)}else{$null}}
  })
  $result.disks = $disks
} catch { $result.disks_error = $_.Exception.Message }
try {
  $nics = Get-CimInstance Win32_PerfFormattedData_Tcpip_NetworkInterface | Where-Object { $_.Name -notmatch 'Loopback|isatap|Teredo' }
  $rx = ($nics | Measure-Object -Property BytesReceivedPersec -Sum).Sum
  $tx = ($nics | Measure-Object -Property BytesSentPersec -Sum).Sum
  $result.network = [ordered]@{rx_bytes_per_sec=$rx; tx_bytes_per_sec=$tx}
} catch { $result.network_error = $_.Exception.Message }
try {
  $zones = Get-CimInstance -Namespace root/wmi -ClassName MSAcpi_ThermalZoneTemperature -ErrorAction Stop
  $result.temp_c = @($zones | ForEach-Object { [math]::Round(($_.CurrentTemperature/10) - 273.15, 1) })
} catch { $result.temp_error = $_.Exception.Message }
$result | ConvertTo-Json -Compress -Depth 5
"""


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


def host_probe():
    """Returns a dict with whichever of host/disks/network/temp_c succeeded this cycle, plus
    *_error strings for whichever didn't. None (probe-level failure) only if the WHOLE PowerShell
    call itself failed/timed out (a section-level failure inside a successful call still returns a
    dict, just with that section's *_error set instead of its data)."""
    ok, out = run_safe(["powershell", "-NoProfile", "-NonInteractive", "-Command", _HOST_PS],
                        timeout=HOST_TIMEOUT_SEC)
    if not ok or not out.strip():
        return None, (out or "powershell host probe failed/timed out")[:300]
    try:
        return json.loads(out.strip()), None
    except Exception as e:
        return None, ("parse error: " + str(e))[:300]


def docker_stats():
    """Per-container + aggregate (sum across containers) CPU%/mem via `docker stats --no-stream`.
    Hard-bounded, single attempt (no retry -- under load a retry only compounds the exact starvation
    that caused the 2026-07-23 incident)."""
    ok, out = run_safe(["docker", "stats", "--no-stream", "--format", "{{json .}}"],
                        timeout=DOCKER_TIMEOUT_SEC)
    if not ok:
        return None, (out or "docker stats unavailable/timed out")[:200]
    total_cpu = 0.0
    total_mem_mb = 0.0
    containers = []
    unit_mb = {"b": 1 / 1048576.0, "kib": 1 / 1024.0, "mib": 1.0, "gib": 1024.0, "tib": 1024.0 * 1024}

    def to_mb(s):
        # .strip() matters: MemUsage splits as "123.4MiB " / " 7.617GiB" (space around the "/"),
        # and re.match anchors at position 0 -- an un-stripped leading space on the limit half
        # silently matched nothing and always returned 0.0 (found + fixed while verifying real
        # output: every container's mem_limit_mb came back 0.0 despite a real "X GiB" limit).
        m = re.match(r"([\d.]+)\s*([KMGT]?i?B)", (s or "").strip(), re.IGNORECASE)
        if not m:
            return 0.0
        return float(m.group(1)) * unit_mb.get(m.group(2).lower(), 1.0)

    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        cpu = (d.get("CPUPerc") or "0%").strip().rstrip("%")
        try:
            cpu_v = float(cpu)
        except Exception:
            cpu_v = 0.0
        total_cpu += cpu_v
        mem_parts = (d.get("MemUsage") or "").split("/")   # "123.4MiB / 2GiB"
        mem_used_mb = to_mb(mem_parts[0]) if len(mem_parts) > 0 else 0.0
        mem_limit_mb = to_mb(mem_parts[1]) if len(mem_parts) > 1 else 0.0
        total_mem_mb += mem_used_mb
        mem_pct_raw = (d.get("MemPerc") or "0%").strip().rstrip("%")
        try:
            mem_pct = float(mem_pct_raw)
        except Exception:
            mem_pct = None
        containers.append({
            "name": d.get("Name") or d.get("Container") or "?",
            "cpu_pct": round(cpu_v, 1),
            "mem_used_mb": round(mem_used_mb, 1),
            "mem_limit_mb": round(mem_limit_mb, 1),
            "mem_pct": mem_pct,
        })
    containers.sort(key=lambda c: c["cpu_pct"], reverse=True)
    return {"containers": len(containers), "total_cpu_pct": round(total_cpu, 1),
            "total_mem_mb": round(total_mem_mb, 1), "list": containers}, None


def _load_prev():
    """Best-effort load of the previously-written sysres.json -- the source for carry-forward when
    a probe/section fails this cycle (same resilience pattern gen_stats.py already uses for its own
    docker/publisher/health probes: reuse the last GOOD reading, marked carried, rather than showing
    a blank card every time this one probe has a rough cycle under load)."""
    try:
        return json.load(open(OUT, encoding="utf-8"))
    except Exception:
        return None


def _carry(key, fresh, prev, now):
    """Return (value, carried, as_of) for one sub-block: use the fresh reading if it's present this
    cycle; otherwise fall back to the previous GOOD reading IF it's not older than CARRY_MAX_MIN
    (else drop it -- honest 'unavailable' beats a stale number recycled forever)."""
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

    host_blob, host_probe_err = host_probe()
    fresh_docker, derr = docker_stats()

    # host_blob (if the PS call itself succeeded) carries per-section data/errors independently.
    host_blob = host_blob or {}
    fresh_host = host_blob.get("host")
    fresh_disks = host_blob.get("disks")
    fresh_network = host_blob.get("network")
    fresh_temp = host_blob.get("temp_c")
    herr = host_probe_err or host_blob.get("host_error")
    disks_err = host_probe_err or host_blob.get("disks_error")
    network_err = host_probe_err or host_blob.get("network_error")
    temp_err = host_probe_err or host_blob.get("temp_error")

    host, host_carried, host_as_of = _carry("host", fresh_host, prev, now)
    disks, disks_carried, disks_as_of = _carry("disks", fresh_disks, prev, now)
    network, network_carried, network_as_of = _carry("network", fresh_network, prev, now)
    temp_c, temp_carried, temp_as_of = _carry("temp_c", fresh_temp, prev, now)
    docker, docker_carried, docker_as_of = _carry("docker", fresh_docker, prev, now)

    available = bool(host) or bool(docker)
    out = {
        "available": available,
        "checked_at": iso(now),
        "host": host or {},
        "disks": disks or [],
        "network": network or {},
        "temp_c": temp_c,                 # list of zone readings in Celsius, or None if unavailable
        "temp_available": temp_c is not None and len(temp_c) > 0,
        "docker": docker or {},
        "host_carried": host_carried,
        "disks_carried": disks_carried,
        "network_carried": network_carried,
        "temp_carried": temp_carried,
        "docker_carried": docker_carried,
        "host_as_of": host_as_of,
        "disks_as_of": disks_as_of,
        "network_as_of": network_as_of,
        "temp_as_of": temp_as_of,
        "docker_as_of": docker_as_of,
    }
    if herr:
        out["host_error"] = herr
    if disks_err:
        out["disks_error"] = disks_err
    if network_err:
        out["network_error"] = network_err
    if temp_err:
        out["temp_error"] = temp_err
    if derr:
        out["docker_error"] = derr
    if not available:
        out["error"] = herr or derr or "both probes failed"

    tmp = OUT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=1)
    os.replace(tmp, OUT)          # atomic swap -- a concurrent reader never sees a half-written file
    print("sysres_gen OK available={} host_err={} docker_err={} temp_err={} host_carried={} docker_carried={}"
          .format(available, herr, derr, temp_err, host_carried, docker_carried))


if __name__ == "__main__":
    main()
