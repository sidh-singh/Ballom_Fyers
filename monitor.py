"""
monitor.py — background health-check loop (the "engine" for the dashboard).

Every CHECK_INTERVAL_SEC it probes every server in ip_list.txt (concurrently) and writes:
  • current.json        — the latest status of every server (what the dashboard shows live)
  • day_<YYYY-MM-DD>.json — that day's baseline + every status CHANGE (UP↔DOWN) with a timestamp

Keeps exactly RETENTION_DAYS (1 week) of day files. New servers added to ip_list.txt are
picked up automatically on the next cycle. Run it alongside the dashboard (see the launchers).
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
    _IST = ZoneInfo("Asia/Kolkata")
except Exception:   # pragma: no cover
    _IST = None

from constants import (
    CHECK_INTERVAL_SEC, RETENTION_DAYS, HEALTH_DIR, CURRENT_FILE,
)
from servers import load_servers
from health import check_server

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s | monitor | %(levelname)s | %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S")
log = logging.getLogger("monitor")


def _now() -> datetime:
    return datetime.now(_IST) if _IST else datetime.now()


def _ts() -> str:
    return _now().strftime("%Y-%m-%d %H:%M:%S")


def _today() -> str:
    return _now().strftime("%Y-%m-%d")


def _read_json(path: Path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(suffix=".json", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, default=str)
        os.replace(tmp, path)
    except Exception:
        try:
            Path(tmp).unlink()
        except Exception:
            pass
        raise


def _day_file(d: str) -> Path:
    return HEALTH_DIR / f"day_{d}.json"


def _prune() -> None:
    cutoff = (_now().date() - timedelta(days=RETENTION_DAYS)).isoformat()
    for f in HEALTH_DIR.glob("day_*.json"):
        if f.stem.replace("day_", "") < cutoff:
            try:
                f.unlink()
            except Exception:
                pass


def run_once(prev_status: dict, since_map: dict) -> None:
    servers = load_servers()
    if not servers:
        log.warning("ip_list.txt has no servers")
        return
    with ThreadPoolExecutor(max_workers=min(32, len(servers))) as ex:
        results = list(ex.map(check_server, servers))

    ts = _ts()
    today = _today()
    day = _read_json(_day_file(today), None)
    if not isinstance(day, dict):
        # first check of the day → baseline = the status each server is entering the day with
        day = {"date": today,
               "baseline": {r["name"]: (prev_status.get(r["name"]) or r["status"]) for r in results},
               "events": []}

    up = 0
    out_servers = []
    for r in results:
        name = r["name"]
        prev = prev_status.get(name)
        if prev is not None and prev != r["status"]:
            day["events"].append({"at": ts, "name": name, "ip": r["ip"],
                                  "os_type": r["os_type"], "from": prev, "to": r["status"]})
            since_map[name] = ts
            log.info("%s %s -> %s", name, prev, r["status"])
        since_map.setdefault(name, ts)
        day["baseline"].setdefault(name, r["status"])
        prev_status[name] = r["status"]
        r["checked_at"] = ts
        r["since"] = since_map[name]
        up += 1 if r["status"] == "UP" else 0
        out_servers.append(r)

    _write_json(_day_file(today), day)
    _write_json(CURRENT_FILE, {
        "generated_at": ts,
        "total": len(out_servers),
        "up": up,
        "down": len(out_servers) - up,
        "servers": out_servers,
    })
    _prune()


def main() -> None:
    HEALTH_DIR.mkdir(parents=True, exist_ok=True)
    prev = _read_json(CURRENT_FILE, {})
    prev_status = {s["name"]: s.get("status") for s in prev.get("servers", [])}
    since_map = {s["name"]: s.get("since", _ts()) for s in prev.get("servers", [])}
    log.info("health monitor started — %d server(s), every %ss, data=%s",
             len(load_servers()), CHECK_INTERVAL_SEC, HEALTH_DIR)
    while True:
        try:
            run_once(prev_status, since_map)
        except Exception as e:
            log.exception("check cycle failed: %s", e)
        time.sleep(CHECK_INTERVAL_SEC)


if __name__ == "__main__":
    main()
