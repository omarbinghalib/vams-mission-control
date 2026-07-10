# VAMS Mission Control

Single home for the VAMS productization mission-control dashboard — **source, tooling, and
published output live in this one repo** (`omarbinghalib/vams-mission-control`). It deploys to
GitHub Pages via the built-in `pages-build-deployment` Actions workflow.

Live: **https://omarbinghalib.github.io/vams-mission-control/**

## Layout

```
/                       repo root (GitHub Pages serves index.html from here)
├── index.html          PUBLISHED dashboard (a build-time copy of mission-control.html)
├── mission-control.html SOURCE dashboard (edit this) — self-contained; polls stats.json +
│                        version.json itself, so index.html is just a verbatim copy
├── stats.json          LIVE stats the page polls every 45s (schema 5)
├── version.json        {"v":<epoch>,"attention":"..."} — bump triggers a client reload +
│                        drives the red attention banner
├── .nojekyll           serve files verbatim (no Jekyll)
├── scripts/
│   ├── gen_stats.py     regenerates stats.json from REAL local liveness (see below)
│   ├── local-config.example.json  template for machine paths (copy -> local-config.json)
│   ├── build-pages.sh   FULL publish: gen stats + sync source → index.html + bump version + push
│   ├── push-stats.sh    STATS-ONLY publish (no version bump / no reload)
│   └── heartbeat.sh     single-shot freshness tick for a Windows scheduled task
└── README.md
```

`scripts/local-config.json` (gitignored) holds this machine's paths — the Claude-transcript
directory, the commander session id, and the two git checkouts. It is deliberately kept out
of this public repo; copy `scripts/local-config.example.json` and fill it in on the host that
runs the generator.

## How liveness is computed (`scripts/gen_stats.py`)

Honest signals only — never hand-stamped. The **fleet is auto-detected**: one card per
`subagents/agent-*.jsonl`, named from its `meta.json` description, so new/relaunched workers
appear automatically. Status is an honest **4-state** disambiguation (no collapsing to "idle"):

- **live** — task-output `.jsonl` mtime `< 10 min`.
- **idle** — alive subagent, just quiet (mtime `>= 10 min`, no stop flag).
- **stopped** — harness-cancelled: the worker's `meta.json` has `"stoppedByUser": true`.
- **done** — completed & retired mission (fixed historical list: provisioning, environment).
- **Commander**: live while ANY subagent is (its root transcript goes quiet while a worker
  holds the turn), i.e. freshest mtime across the whole session tree.
- **Transcript sessions**: token totals + mtime from the `~/.claude/projects` `.jsonl`
  transcripts; numbers for transcripts no longer on disk fall back to a baked baseline.

**Publisher freshness**: the generator reads the heartbeat scheduled-task state
(`Get-ScheduledTask`) and emits `publisher.paused`. When the task is disabled the page shows a
visible **"publisher paused — page frozen at &lt;time&gt;"** banner, so a stale page is
obviously stale rather than silently misleading.

The two machine-absolute path groups (`~/.claude/projects` transcript dirs and the
`VAMS-frontend` / `VAMS-backend` git checkouts), plus the optional docker path / gateway ports
/ watched repos, live in the gitignored `scripts/local-config.json`; everything else resolves
relative to this repo.

## Panels

Beyond the roster + session/token tables: **Time to go-live** (overall % + remaining hours &
worker-sessions — a labelled estimate, with real commit velocity as the measured cadence);
**Active workers** (live-only); **Live stack** (`docker ps` container grid + gateway HTTP
smoke checks, degrading to "unavailable" if unreachable); **Recent commits** feed across both
service branches. All non-liveness numbers are real signals; every estimate is labelled.
Every external call (git / docker / powershell) goes through the `run_safe()` choke point.

## Publish

```sh
# full publish (source or logic changed): regenerates stats, rebuilds index.html, bumps version
sh scripts/build-pages.sh "commit subject"

# stats-only refresh (no reload): regenerate then push
python scripts/gen_stats.py && sh scripts/push-stats.sh
```

Both commit + push to `origin/main`; Actions deploys within ~1 min.

## Continuous freshness — heartbeat

`scripts/heartbeat.sh` is **single-shot**: regenerate stats from live liveness, and push
**only when warranted** (live workers present AND published stats older than 6 min, OR the
live worker/session set changed). This bounds pushes and avoids commit-spam. Register it as a
Windows Scheduled Task running every ~3 min:

```
Program:   C:\Program Files\Git\bin\sh.exe
Arguments: -lc "<posix-path-to-this-repo>/scripts/heartbeat.sh"
```

Use the Git-Bash (POSIX) form of the checkout path, e.g. `/c/Users/<you>/.../pages`. Adjust
the Git install path if different. The script self-locates its repo, so no other config is
needed beyond `scripts/local-config.json`.

## History

Consolidated 2026-07-10. The dashboard source + scripts previously lived in the
`VAMS-frontend` worktree under `docs/session-handoff/mission-control/`; that copy is now a
frozen backup (commit `f4c78e4` on `microservices-frontend`) and this repo is canonical.
