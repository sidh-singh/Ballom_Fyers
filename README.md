# Ballom — Server Health Dashboard

A small standalone app (its own `health_check` branch) that watches every server in
[`ip_list.txt`](ip_list.txt) and shows a live + historical health dashboard. Completely
separate from the 12 fyers trading branches — its own port (**9000**) and its own data dir.

## What it does
- Polls every server **every 10 s** (all servers concurrently; tune `CHECK_INTERVAL_SEC`) and shows **UP / DOWN**, latency, uptime %, and **when each
  server last changed** — so a **shutdown is detected and recorded with a date-time**.
- **Beautiful dashboard:** status donut + stat tiles, a per-server **uptime timeline**
  (green = up, red = down) for the chosen day, one card per server, and a **downtime log**.
- **Auto light / dark theme by IST time** — light during the day (06:00–18:00), dark at night.
  (Tune the window via `DAY_START_HOUR` / `DAY_END_HOUR` in dashboard.py.)
- **Notification dots:** each server card shows a 🔔 badge when it had a recent status change
  you haven't seen yet (e.g. a server that **shut down and restarted quickly** while you were
  away). Click the badge to read what happened — it's then marked read and the dot clears
  (persisted in `acks.json`, so it stays cleared across refreshes until the next change).
- **Date dropdown** to browse the **last 1 week** (older data is auto-deleted). It **defaults to
  today** and re-anchors to today on every page load / refresh — a tab left open overnight never
  opens on a stale day, and it rolls over to the new day at midnight — while an explicitly chosen
  past day stays put until you reload.
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
Provider: Veeble

Name: FYERS
IP: 88.150.138.80
OS: Debian
Provider: Veeble
```
- **`OS:`** sets the type explicitly and picks the **brand logo** shown on the card:
  `Windows`, `Debian`, `Ubuntu`, `Mac`, or `Linux` (generic). Windows probes RDP (3389);
  everything else probes SSH (22). Add more distros by dropping an SVG in `assets/icons/`.
- `OS:` is **optional**: if you omit it, it's inferred from the name (names starting with `MT` → Windows, anything else → Linux).
- **`Provider:`** is the **VPS / cloud host** (free text, e.g. `Veeble`). It's shown in small
  text on each server card (`☁ Veeble`), next to the IP and OS logo. It's **optional** — omit
  it and the card just leaves it blank. All 11 current servers are on **Veeble**.
- The old `(IP) - NAME` one-line style is still accepted (OS inferred from the name).

## How a server is checked (no credentials needed)
Each server is probed two ways every sweep: an ICMP **ping** (reachability) and a TCP connect to
its **service port** (RDP 3389 for Windows, SSH 22 for Linux — proves the guest OS is actually
serving). The **dashboard shows both probes separately** on each card (`ping ●` / `port ●`), so you
can always see *why* a box reads up. The ping counts only on a **genuine echo reply** (verified by
its TTL), not a zero exit code — on Windows `ping` exits 0 even for a "destination host unreachable"
router reply, which would otherwise read a DOWN box as UP.

**What counts as UP** is `UP_POLICY` (in `constants.py`, or the `UP_POLICY` env var):
- `any` *(default)* — UP if **either** probe answers. Tolerant of a firewalled RDP/SSH.
- `port` — the **service port must answer**; ICMP is ignored. Use this when something upstream (the
  host / hypervisor) answers ping even while the guest is rebooting, which would otherwise hide the
  reboot behind a false "ping UP". Requires the port to be reachable from the monitor.
- `ping` — ICMP only.

*(Deeper checks — a specific service, CPU/RAM — would need login credentials; `health.py` is the one
place to extend.)*

**Flap-proofing.** Each check **retries the probe** up to `PROBE_ATTEMPTS` times before concluding
DOWN (so a single dropped ping on a healthy box doesn't flap), and the monitor records a change
after `CONFIRM_SWEEPS` consecutive agreeing sweeps. `CONFIRM_SWEEPS` is **1** by default — a change
shows on the first sweep that sees it (~10–17 s at the 10 s interval), so a real restart is never
missed; the retry alone absorbs single-packet blips. Raise it to 2–3 only if flapping ever returns.

**If a real reboot is NOT showing as DOWN:** watch the card's `ping ●` / `port ●` dots — or run
`python probe_debug.py` on the **monitor host** — while you reboot the box. If **ping stays green
while port goes red**, the host/hypervisor is answering ICMP for a down guest: set `UP_POLICY=port`
(works as long as `port ●` is green when the box is healthy, i.e. the port is reachable from the
monitor). If the port is never reachable either, network probing alone can't see the reboot — that
needs a small agent on the box.

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
| `health.py` | per-server ping + service-port check (retries, TTL-verified ping, `UP_POLICY`) |
| `monitor.py` | background loop → `current.json` + per-day history (1-week retention) |
| `probe_debug.py` | run on the monitor host to watch raw ping/port per server live (diagnose detection) |
| `dashboard.py` | the dashboard (port 9000) |
| `constants.py` | ports, intervals, retention, data dir |
| `*.bat` / `*.sh` | Windows / Linux launchers |

Data (status history) lives **outside** the repo: `C:/Ballom_HealthCheck` (Windows) or
`~/ballom-healthcheck` (Linux).
