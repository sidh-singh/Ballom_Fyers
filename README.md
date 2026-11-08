# Ballom — Server Health Dashboard

A small standalone app (its own `health_check` branch) that watches every server in
[`ip_list.txt`](ip_list.txt) and shows a live + historical health dashboard. Completely
separate from the 12 fyers trading branches — its own port (**9000**) and its own data dir.

## What it does
- Polls every server **every 10 s** (all servers concurrently; tune `CHECK_INTERVAL_SEC`) and shows **UP / DOWN**, latency, uptime %, and **when each
  server last changed** — so a **shutdown is detected and recorded with a date-time**.
- **Beautiful dark dashboard:** status donut + stat tiles, a per-server **uptime timeline**
  (green = up, red = down) for the chosen day, one card per server, and a **downtime log**.
- **Notification dots:** each server card shows a 🔔 badge when it had a recent status change
  you haven't seen yet (e.g. a server that **shut down and restarted quickly** while you were
  away). Click the badge to read what happened — it's then marked read and the dot clears
  (persisted in `acks.json`, so it stays cleared across refreshes until the next change).
- **Date dropdown** to browse the **last 1 week**; older data is auto-deleted.
- **Scales to many servers (100+):** the uptime timeline, the server-card grid, and the
  downtime log each scroll **inside their own panel** (adaptive — no scrollbar when there are
  only a few), so the page stays a manageable height instead of growing endlessly.
- **Auto-detects new servers:** add a server to `ip_list.txt` and it appears on the next
  refresh — no code change.

## The server list — `ip_list.txt`
```
Name: MT1
IP: 37.220.31.63
OS: Windows

Name: FYERS
IP: 88.150.138.80
OS: Linux
```
- **`OS:`** sets the type explicitly — **Windows** probes RDP (port 3389), **Linux** probes SSH (port 22).
- `OS:` is **optional**: if you omit it, it's inferred from the name (names starting with `MT` → Windows, anything else → Linux).
- The old `(IP) - NAME` one-line style is still accepted (OS inferred from the name).

## How a server is checked (no credentials needed)
Each server is **UP** if **either** an ICMP **ping** succeeds **or** its **service port**
(RDP 3389 for Windows, SSH 22 for Linux) accepts a TCP connection — enough to detect a
shutdown / unreachable box even if ICMP is firewalled. *(Deeper checks — a specific service
running, CPU/RAM — would need login credentials; `health.py` is the one place to extend.)*

## Run it — Windows
```bat
start_all.bat            REM starts the monitor (new window) + the dashboard
REM or separately:
start_monitor.bat        REM background poller (leave running)
start_dashboard.bat 9000 REM dashboard  ->  http://localhost:9000
```
(Needs `python` on PATH; dependencies install automatically on first run.)

## Run it — Linux (Debian / Ubuntu)
```bash
bash setup.sh            # one-time: venv + deps + background services
./start.sh               # start monitor + dashboard (background, survive reboot)
./stop.sh                # stop both
# foreground (quick test): ./start_monitor.sh   and   ./start_dashboard.sh
```
Dashboard → `http://<server-ip>:9000` (or SSH-tunnel `127.0.0.1:9000`). Logs:
`journalctl -u ballom-health -f`.

## Files
| File | Purpose |
|------|---------|
| `ip_list.txt` | the server list (edit this to add servers) |
| `servers.py` | tolerant parser + OS classification |
| `health.py` | per-server ping + service-port check |
| `monitor.py` | background loop → `current.json` + per-day history (1-week retention) |
| `dashboard.py` | the dashboard (port 9000) |
| `constants.py` | ports, intervals, retention, data dir |
| `*.bat` / `*.sh` | Windows / Linux launchers |

Data (status history) lives **outside** the repo: `C:/Ballom_HealthCheck` (Windows) or
`~/ballom-healthcheck` (Linux).
