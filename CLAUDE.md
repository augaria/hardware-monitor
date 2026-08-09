# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

Push-based hardware monitoring. Agents on each monitored machine collect metrics with `psutil` and POST them to a central Flask server every `--interval` seconds (default 60). The server keeps the latest report per machine in memory, serves a dashboard, and grades threshold breaches into notifications.

Two independently deployed components. **A change under `central_server/` only requires rebuilding and redeploying the web image — agents are unaffected** unless `agent/` itself changed (agents are updated with `sudo bash install.sh --update` per machine).

## Key files

| File | Purpose |
|---|---|
| [central_server/main.py](central_server/main.py) | Flask app: `POST /report`, `GET /api/status`, dashboard, offline-watcher thread |
| [central_server/alerter.py](central_server/alerter.py) | Threshold state machine + notifier dispatch |
| [agent/hardware_monitor_agent/main.py](agent/hardware_monitor_agent/main.py) | Metric collection (psutil, pynvml, mdadm/LVM/ZFS parsing) |
| [scripts/install.sh](scripts/install.sh) | Agent installer (conda env + systemd unit); `--update` re-pulls, preserves config |
| [docker-compose.sample.yml](docker-compose.sample.yml) | Full env-var reference with comments |

No test suite. `./docker-build.sh [-t TAG] [-r REGISTRY] [-p] [--no-cache] [--refresh-deps]` builds the server image; `--refresh-deps` re-fetches the `notifier` git dependency without busting other layers.

## Alerting (`alerter.py`)

Crossing a threshold once is **not** an alert — temperatures and memory are point samples, so a backup or compile job would otherwise page you. Every condition (`machine:metric`, `machine:disk_temps:name`, `machine:offline`) runs the same state machine:

- **debounce** — must stay over the threshold continuously for `ALERT_FOR_MINUTES`; a reporting gap longer than `OFFLINE_TIMEOUT` restarts the timer, since without samples in between there is no evidence the value stayed high
- **repeat** — re-announced every `ALERT_REPEAT_HOURS` (deliberately *not* the debounce interval; conflating the two is what made a disk 1°C over its threshold email every 10 minutes)
- **hysteresis** — resolves only after falling to `ALERT_CLEAR_RATIO × threshold` for `ALERT_FOR_MINUTES`, then sends a recovery notice; the gap stops a metric on the line from alternating between firing and resolving

⚠️ **Never notify at `Level.INFO`.** `notifier` channels use `min_level=WARNING` and drop anything below it **silently** — the gate returns success, nothing is logged. Recovery and "back online" notices use `WARNING` for exactly this reason.

State persists to `ALERT_STATE_PATH` (default `/data/alert_state.json`), so recreating the container does not re-alert everything still hot — needs a writable volume at `/data`.

An unusable `NOTIFIER_CHANNELS` (bad JSON, unknown channel type, construction failure, notifier not installed) **aborts startup** via `_fatal()`. Unset or `[]` is the supported way to run log-only. This is deliberate: the previous behaviour left a server that looked healthy but could never deliver an alert.

## Thresholds

Server-wide defaults come from `ALERT_*` env vars; an agent may ship a `thresholds.conf` whose values are merged **per metric** in `_resolve_thresholds()`, so an agent overriding only `cpu_temp` keeps server defaults for everything else. Unset/empty disables that metric entirely.
