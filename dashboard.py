"""
dashboard.py — Ballom server health dashboard (standalone, read-only).

Shows every server from ip_list.txt: live UP/DOWN, latency, uptime, when each last
changed, a per-server uptime TIMELINE for the chosen day, and a downtime event log.
A date dropdown browses the last week. New servers in ip_list.txt appear automatically.

Run:  python dashboard.py [port]      (default 9000 — separate from the fyers 8050–8056)
Data comes from the monitor (monitor.py), which must be running alongside.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta

import dash
from dash import dcc, html
from dash.dependencies import Input, Output
import plotly.graph_objects as go

try:
    from zoneinfo import ZoneInfo
    _IST = ZoneInfo("Asia/Kolkata")
except Exception:                      # pragma: no cover
    _IST = None

from constants import (
    APP_NAME, DASHBOARD_PORT, DASHBOARD_REFRESH_MS, RETENTION_DAYS,
    HEALTH_DIR, CURRENT_FILE,
)
from servers import load_servers

C = {
    "bg": "#0a0e17", "card": "#141a28", "card2": "#1a2133", "border": "#232c42",
    "text": "#e6e9f2", "dim": "#93a0bd", "muted": "#5b6680",
    "green": "#22c55e", "red": "#ef4444", "amber": "#f59e0b", "grey": "#6b7280",
    "blue": "#60a5fa", "teal": "#2dd4bf", "grid": "rgba(140,150,180,0.08)",
}


# ── helpers ───────────────────────────────────────────────────────────────────
def _now():
    return datetime.now(_IST) if _IST else datetime.now()


def _today_iso():
    return _now().strftime("%Y-%m-%d")


def _read(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _parse(ts):
    """Parse 'YYYY-MM-DD HH:MM:SS' (naive) to datetime."""
    try:
        return datetime.strptime(ts[:19], "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def _recent_dates(n=RETENTION_DAYS):
    t = _now().date()
    opts = []
    for i in range(n):
        d = t - timedelta(days=i)
        label = "Today" if i == 0 else ("Yesterday" if i == 1 else d.strftime("%a %d %b"))
        opts.append({"label": label, "value": d.isoformat()})
    return opts


def _day_data(date_iso):
    return _read(HEALTH_DIR / f"day_{date_iso}.json", {}) or {}


def _segments(name, baseline, events, day_start, day_end):
    """Build [(start_dt, end_dt, status)] for one server across [day_start, day_end]."""
    evs = sorted([e for e in events if e.get("name") == name and _parse(e.get("at", ""))],
                 key=lambda e: e["at"])
    status = baseline.get(name) or (evs[0]["from"] if evs else "UP")
    segs, cur = [], day_start
    for e in evs:
        at = _parse(e["at"])
        if at is None or at <= cur:
            status = e["to"]
            continue
        at = min(at, day_end)
        segs.append((cur, at, status))
        cur, status = at, e["to"]
    if cur < day_end:
        segs.append((cur, day_end, status))
    return segs


def _uptime_pct(segs):
    total = sum((e - s).total_seconds() for s, e, _ in segs) or 1
    up = sum((e - s).total_seconds() for s, e, st in segs if st == "UP")
    return round(100.0 * up / total, 2)


def _secs(dt, day_start):
    return max(0.0, (dt - day_start).total_seconds())


# ── pieces ────────────────────────────────────────────────────────────────────
def _card(children, pad="14px 16px", extra=None):
    st = {"background": C["card"], "border": f"1px solid {C['border']}",
          "borderRadius": "12px", "padding": pad}
    if extra:
        st.update(extra)
    return html.Div(children, style=st)


def _tile(label, value, color):
    return _card([
        html.Div(label, style={"color": C["dim"], "fontSize": "11px", "letterSpacing": "0.6px",
                               "textTransform": "uppercase"}),
        html.Div(value, style={"color": color, "fontSize": "26px", "fontWeight": "800",
                               "marginTop": "4px"}),
    ], extra={"flex": "1", "minWidth": "120px"})


def _os_badge(os_type):
    win = os_type == "windows"
    return html.Span("🪟 Windows" if win else "🐧 Linux",
                     style={"color": C["blue"] if win else C["teal"], "fontSize": "11px",
                            "fontWeight": "700"})


def summary_tiles(servers, uptimes, view, historical):
    total = len(servers)
    up = sum(1 for s in servers if s.get("status") == "UP")
    down = total - up
    avg_up = round(sum(uptimes.values()) / len(uptimes), 1) if uptimes else 100.0
    label = "LIVE" if not historical else view
    return html.Div([
        _tile(f"Servers ({label})", str(total), C["text"]),
        _tile("Up now" if not historical else "Up (end of day)", str(up), C["green"]),
        _tile("Down", str(down), C["red"] if down else C["muted"]),
        _tile(f"Avg uptime ({view})", f"{avg_up}%", C["green"] if avg_up >= 99 else C["amber"]),
    ], style={"display": "flex", "gap": "12px", "flexWrap": "wrap", "marginBottom": "14px"})


def donut(servers):
    up = sum(1 for s in servers if s.get("status") == "UP")
    down = len(servers) - up
    fig = go.Figure(go.Pie(
        values=[up, down], labels=["UP", "DOWN"], hole=0.62, sort=False,
        marker=dict(colors=[C["green"], C["red"]], line=dict(color=C["bg"], width=2)),
        textinfo="none", hovertemplate="%{label}: %{value}<extra></extra>"))
    fig.add_annotation(text=f"<b>{up}/{len(servers)}</b><br>UP", showarrow=False,
                       font=dict(size=18, color=C["text"]))
    fig.update_layout(height=190, margin=dict(l=6, r=6, t=6, b=6), showlegend=False,
                      paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)")
    return dcc.Graph(figure=fig, config={"displayModeBar": False})


def timeline(servers, baseline, events, day_start, day_end):
    """Per-server up/down timeline across the day (green = up, red = down)."""
    names = [s["name"] for s in servers]
    fig = go.Figure()
    for s in servers:
        for seg_s, seg_e, st in _segments(s["name"], baseline, events, day_start, day_end):
            fig.add_trace(go.Bar(
                y=[s["name"]], x=[(seg_e - seg_s).total_seconds()], base=_secs(seg_s, day_start),
                orientation="h", width=0.62, showlegend=False,
                marker=dict(color=C["green"] if st == "UP" else C["red"], line=dict(width=0)),
                hovertemplate=(f"{s['name']} — {st}<br>"
                               f"{seg_s.strftime('%H:%M')}–{seg_e.strftime('%H:%M')}<extra></extra>"),
            ))
    total = max(1.0, (day_end - day_start).total_seconds())
    ticks = list(range(0, int(total) + 1, 3 * 3600))
    fig.update_layout(
        barmode="overlay", height=max(180, 34 * len(names) + 60),
        margin=dict(l=70, r=16, t=10, b=26), paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)", bargap=0.35,
        xaxis=dict(range=[0, total], tickvals=ticks,
                   ticktext=[(day_start + timedelta(seconds=t)).strftime("%H:%M") for t in ticks],
                   gridcolor=C["grid"], color=C["dim"], zeroline=False),
        yaxis=dict(color=C["text"], autorange="reversed"),
        font=dict(color=C["text"]))
    return dcc.Graph(figure=fig, config={"displayModeBar": False}, style={"width": "100%"})


def server_cards(servers, uptimes, downcounts, historical):
    cards = []
    for s in servers:
        up = s.get("status") == "UP"
        col = C["green"] if up else C["red"]
        name = s.get("name", "?")
        up_pct = uptimes.get(name, 100.0)
        lat = s.get("latency_ms")
        since = s.get("since", "")
        rows = [
            html.Div([
                html.Span(name, style={"color": C["text"], "fontSize": "17px", "fontWeight": "800"}),
                html.Span(("● UP" if up else "● DOWN"),
                          style={"marginLeft": "auto", "color": "#06281f" if up else "#2a0d0d",
                                 "background": col, "borderRadius": "6px", "padding": "2px 9px",
                                 "fontSize": "12px", "fontWeight": "800"}),
            ], style={"display": "flex", "alignItems": "center"}),
            html.Div([html.Span(s.get("ip", ""), style={"color": C["dim"], "fontSize": "12px"}),
                      html.Span("  ·  ", style={"color": C["muted"]}), _os_badge(s.get("os_type", "linux"))],
                     style={"marginTop": "6px"}),
            html.Div([
                html.Span(f"uptime {up_pct}%", style={"color": C["green"] if up_pct >= 99 else C["amber"],
                          "fontSize": "12px", "fontWeight": "700"}),
                html.Span(f"· {downcounts.get(name, 0)} drop(s)", style={"color": C["muted"],
                          "fontSize": "11px", "marginLeft": "8px"}),
                html.Span((f"· {lat} ms" if lat is not None and not historical else ""),
                          style={"color": C["muted"], "fontSize": "11px", "marginLeft": "8px"}),
            ], style={"marginTop": "8px"}),
            html.Div((f"since {since}" if since and not historical else ""),
                     style={"color": C["muted"], "fontSize": "10px", "marginTop": "4px"}),
        ]
        cards.append(_card(rows, extra={"flex": "1", "minWidth": "210px",
                     "borderLeft": f"3px solid {col}"}))
    return html.Div(cards, style={"display": "flex", "gap": "12px", "flexWrap": "wrap"})


def events_table(events):
    rows = []
    for e in sorted(events, key=lambda x: x.get("at", ""), reverse=True):
        to = e.get("to")
        col = C["green"] if to == "UP" else C["red"]
        rows.append(html.Tr([
            html.Td(e.get("at", ""), style={"color": C["dim"], "fontSize": "12px", "padding": "4px 10px"}),
            html.Td(e.get("name", ""), style={"color": C["text"], "fontWeight": "700", "padding": "4px 10px"}),
            html.Td(e.get("ip", ""), style={"color": C["muted"], "fontSize": "12px", "padding": "4px 10px"}),
            html.Td(f"{e.get('from','?')} → {to}",
                    style={"color": col, "fontWeight": "700", "padding": "4px 10px"}),
        ]))
    body = html.Table([html.Thead(html.Tr([
        html.Th(h, style={"color": C["muted"], "fontSize": "11px", "textAlign": "left",
                          "padding": "4px 10px", "textTransform": "uppercase"})
        for h in ("When", "Server", "IP", "Change")]))] + [html.Tbody(rows)],
        style={"width": "100%", "borderCollapse": "collapse"}) if rows else \
        html.Div("No status changes recorded — all servers stable. 🟢",
                 style={"color": C["muted"], "padding": "12px"})
    return _card([html.Div("DOWNTIME / STATUS-CHANGE LOG", style={"color": C["dim"], "fontSize": "12px",
                 "letterSpacing": "0.6px", "marginBottom": "8px"}), body])


# ── app ───────────────────────────────────────────────────────────────────────
app = dash.Dash(__name__, title="Ballom Health")
app.layout = html.Div([
    dcc.Interval(id="tick", interval=DASHBOARD_REFRESH_MS, n_intervals=0),
    html.Div([
        html.H2(APP_NAME, style={"color": C["text"], "margin": "0"}),
        html.Span(id="gen", style={"color": C["muted"], "fontSize": "12px", "marginLeft": "12px"}),
        html.Div([
            html.Span("View date", style={"color": C["muted"], "fontSize": "11px", "marginRight": "8px"}),
            dcc.Dropdown(id="viewdate", options=_recent_dates(), value=_today_iso(),
                         clearable=False, style={"width": "190px", "fontSize": "13px"}),
        ], style={"marginLeft": "auto", "display": "flex", "alignItems": "center", "minWidth": "260px"}),
    ], style={"display": "flex", "alignItems": "center", "marginBottom": "16px"}),
    html.Div(id="banner"),
    html.Div(id="tiles"),
    html.Div([
        _card([html.Div("STATUS", style={"color": C["dim"], "fontSize": "12px", "marginBottom": "4px"}),
               html.Div(id="donut")], extra={"flex": "0 0 230px"}),
        _card([html.Div(id="tl-title", style={"color": C["dim"], "fontSize": "12px", "marginBottom": "4px"}),
               html.Div(id="timeline")], extra={"flex": "1"}),
    ], style={"display": "flex", "gap": "12px", "marginBottom": "14px", "alignItems": "stretch"}),
    html.Div(id="cards", style={"marginBottom": "14px"}),
    html.Div(id="events"),
], style={"maxWidth": "1280px", "margin": "0 auto", "padding": "26px",
          "fontFamily": "Inter, system-ui, sans-serif", "background": C["bg"], "minHeight": "100vh"})


@app.callback(
    Output("viewdate", "options"), Output("viewdate", "value"),
    Input("tick", "n_intervals"), dash.dependencies.State("viewdate", "value"),
)
def _sync_dates(_, cur):
    opts = _recent_dates()
    vals = [o["value"] for o in opts]
    return opts, (cur if cur in vals else _today_iso())


@app.callback(
    Output("banner", "children"), Output("tiles", "children"), Output("donut", "children"),
    Output("tl-title", "children"), Output("timeline", "children"),
    Output("cards", "children"), Output("events", "children"), Output("gen", "children"),
    Input("viewdate", "value"), Input("tick", "n_intervals"),
)
def _refresh(view_date, _):
    today = _today_iso()
    view = view_date or today
    historical = view != today

    current = _read(CURRENT_FILE, {}) or {}
    day = _day_data(view)
    baseline = day.get("baseline", {}) or {}
    events = day.get("events", []) or []

    # servers to show: live set for today (auto-detected from ip_list via current.json / file),
    # the monitored set for a past day.
    if not historical and current.get("servers"):
        servers = current["servers"]
    elif baseline:
        live = {s["name"]: s for s in (current.get("servers") or [])}
        servers = [{"name": n, "ip": live.get(n, {}).get("ip", ""),
                    "os_type": live.get(n, {}).get("os_type", "linux"),
                    "status": None} for n in sorted(baseline)]
    else:
        servers = [{**s, "status": None} for s in load_servers()]

    # day window (to "now" for today, else full day)
    try:
        d0 = datetime.strptime(view, "%Y-%m-%d")
    except Exception:
        d0 = datetime.strptime(today, "%Y-%m-%d")
    day_start = d0
    day_end = _now().replace(tzinfo=None) if not historical else d0 + timedelta(days=1) - timedelta(seconds=1)
    if day_end <= day_start:
        day_end = day_start + timedelta(minutes=1)

    uptimes, downcounts = {}, {}
    for s in servers:
        segs = _segments(s["name"], baseline, events, day_start, day_end)
        uptimes[s["name"]] = _uptime_pct(segs)
        downcounts[s["name"]] = sum(1 for e in events if e.get("name") == s["name"] and e.get("to") == "DOWN")
        # for a past day with no live status, show end-of-day status from the timeline
        if s.get("status") is None:
            s["status"] = segs[-1][2] if segs else (baseline.get(s["name"]) or "UP")

    banner = ""
    if historical:
        banner = _card([html.Span(f"🕑 Viewing {view}", style={"color": C["amber"], "fontWeight": "800"}),
                        html.Span("  historical — status & uptime for that day; live latency is current-only",
                                  style={"color": C["dim"], "fontSize": "12px", "marginLeft": "10px"})],
                       pad="10px 16px", extra={"marginBottom": "12px"})

    tl_title = f"UPTIME TIMELINE — {view}  (green = up, red = down)"
    gen = f"monitor updated: {current.get('generated_at', '—')}" if not historical else f"day: {view}"
    return (banner, summary_tiles(servers, uptimes, view, historical), donut(servers),
            tl_title, timeline(servers, baseline, events, day_start, day_end),
            server_cards(servers, uptimes, downcounts, historical),
            events_table(events), gen)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else DASHBOARD_PORT
    print(f"health dashboard → http://127.0.0.1:{port}")
    app.run(debug=False, host="0.0.0.0", port=port)
