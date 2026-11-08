"""
servers.py — read ip_list.txt into a list of servers.

Tolerant parser so you can add servers in EITHER style and they are auto-detected
on the dashboard's next refresh (no code change):

    Name: MT12
    IP: 1.2.3.4
    OS: Windows          # optional — omit it and the OS is inferred from the name

    (1.2.3.4) - MT12     # legacy one-line style, still accepted

OS resolution: an explicit "OS: Windows|Linux" wins; otherwise a name starting with
"MT" (case-insensitive) is WINDOWS and anything else (e.g. FYERS) is LINUX.
"""
from __future__ import annotations

import re
from pathlib import Path

from constants import IP_LIST_FILE

_IPV4 = r"\d{1,3}(?:\.\d{1,3}){3}"
_NAME_RE = re.compile(r"Name:\s*([^\r\n]+)", re.IGNORECASE)
_IP_RE = re.compile(r"IP:\s*(" + _IPV4 + r")", re.IGNORECASE)
_OS_RE = re.compile(r"OS:\s*([A-Za-z][A-Za-z0-9 _-]*)", re.IGNORECASE)
_LINE_RE = re.compile(r"\(?\s*(" + _IPV4 + r")\s*\)?\s*[-–:]\s*([^\r\n]+)")


def _norm_os(value: str) -> str | None:
    """Normalise the OS: keep the specific distro so the dashboard can show its logo.
    -> 'windows' / 'debian' / 'ubuntu' / 'mac' / 'linux' (generic); unknown -> None."""
    v = (value or "").strip().lower()
    if not v:
        return None
    if "win" in v:
        return "windows"
    if "debian" in v:
        return "debian"
    if "ubuntu" in v:
        return "ubuntu"
    if any(k in v for k in ("mac", "osx", "apple", "darwin")):
        return "mac"
    if any(k in v for k in ("linux", "unix", "nix", "centos", "fedora", "redhat",
                            "rhel", "arch", "mint", "suse")):
        return "linux"
    return None


def os_of(name: str) -> str:
    """Fallback OS when none is specified: MT-prefixed names are Windows, else Linux."""
    return "windows" if name.strip().upper().startswith("MT") else "linux"


def _valid_ip(ip: str) -> bool:
    return all(o.isdigit() and 0 <= int(o) <= 255 for o in ip.split("."))


def _add(out: list, seen: set, name: str, ip: str, os_type: str | None) -> None:
    name = (name or "").strip()
    ip = (ip or "").strip()
    if not name or not ip or ip in seen or not _valid_ip(ip):
        return
    seen.add(ip)
    out.append({"name": name, "ip": ip, "os_type": os_type or os_of(name)})


def load_servers(path: Path = IP_LIST_FILE) -> list[dict]:
    """Return [{name, ip, os_type}, ...] in file order, de-duplicated by IP."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except Exception:
        return []
    # drop comment lines (# ...) so they never parse as entries
    text = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))

    out: list[dict] = []
    seen: set[str] = set()

    # 1) block format — split at each "Name:" and read Name / IP / (optional) OS in that block
    for block in re.split(r"(?=Name:)", text, flags=re.IGNORECASE):
        nm, ipm = _NAME_RE.search(block), _IP_RE.search(block)
        if nm and ipm:
            osm = _OS_RE.search(block)
            _add(out, seen, nm.group(1), ipm.group(1),
                 _norm_os(osm.group(1)) if osm else None)

    # 2) legacy one-line "(IP) - NAME"
    for ip, name in _LINE_RE.findall(text):
        _add(out, seen, name, ip, None)

    return out


if __name__ == "__main__":
    for s in load_servers():
        print(f"{s['name']:<8} {s['ip']:<16} {s['os_type']}")
