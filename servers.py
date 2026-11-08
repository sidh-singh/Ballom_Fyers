"""
servers.py — read ip_list.txt into a list of servers.

Tolerant parser so you can add servers in EITHER style and they are auto-detected
on the dashboard's next refresh (no code change):

    Name: MT12
    IP: 1.2.3.4

    (1.2.3.4) - MT12          # legacy one-line style, still accepted

OS rule: a name starting with "MT" (case-insensitive) is a WINDOWS server; anything
else (e.g. FYERS) is a LINUX server — matches the owner's convention.
"""
from __future__ import annotations

import re
from pathlib import Path

from constants import IP_LIST_FILE

_IP = r"(\d{1,3}(?:\.\d{1,3}){3})"
_BLOCK_RE = re.compile(r"Name:\s*([^\r\n]+?)\s*[\r\n]+\s*IP:\s*" + _IP, re.IGNORECASE)
_LINE_RE = re.compile(r"\(?\s*" + _IP + r"\s*\)?\s*[-–:]\s*([^\r\n]+)")


def os_of(name: str) -> str:
    """'windows' for MT-prefixed names, else 'linux' (owner's convention)."""
    return "windows" if name.strip().upper().startswith("MT") else "linux"


def _add(out: list, seen: set, name: str, ip: str) -> None:
    name = (name or "").strip()
    ip = (ip or "").strip()
    if not name or not ip or ip in seen:
        return
    # reject obviously bad octets
    if any(not o.isdigit() or int(o) > 255 for o in ip.split(".")):
        return
    seen.add(ip)
    out.append({"name": name, "ip": ip, "os_type": os_of(name)})


def load_servers(path: Path = IP_LIST_FILE) -> list[dict]:
    """Return [{name, ip, os_type}, ...] in file order, de-duplicated by IP."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except Exception:
        return []
    # strip comments (# ...) so they never parse as entries
    text = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))

    out: list[dict] = []
    seen: set[str] = set()
    # 1) block format: Name: ... / IP: ...
    for name, ip in _BLOCK_RE.findall(text):
        _add(out, seen, name, ip)
    # 2) legacy one-line "(IP) - NAME"
    for ip, name in _LINE_RE.findall(text):
        _add(out, seen, name, ip)
    return out


if __name__ == "__main__":
    for s in load_servers():
        print(f"{s['name']:<8} {s['ip']:<16} {s['os_type']}")
