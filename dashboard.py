"""
dashboard.py — trimmed LIVE dashboard for fyers_live (ARCHITECTURE D1/D2).

Separate process, READ-ONLY: reads the JSON state files the engine writes under
C:/Ballom_FYR/state/ and renders:
  • engine status strip        (app_status.json)
  • API-usage meter            (api_usage.json)      ← the budget counter
  • per-asset P/L booking chart (profit_history.json) ← bar-per-close + cumulative/drawdown
  • recent strategy log        (strategy_log/<date>.json)

Run:  python dashboard.py [port]    (default 8050)  →  http://127.0.0.1:8050
Never calls Fyers; never writes state. Grows as more features land.
"""

from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from datetime import datetime

import dash
from dash import dcc, html
from dash.dependencies import Input, Output
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from constants import (
    STATE_DIR, API_USAGE_FILE, APP_STATUS_FILE, STRATEGY_LOG_DIR,
    API_LIMITS, now_ist,
)

PROFIT_HISTORY = STATE_DIR / "profit_history.json"
REFRESH_MS = 5000

COLORS = {
    "bg": "#0a0e17", "card": "#121826", "border": "#1f2937",
    "text": "#e4e8f4", "dim": "#8b95b0", "muted": "#4b5569",
    "pos": "#22c55e", "neg": "#ef4444", "accent": "#7c3aed",
    "amber": "#f59e0b", "grid": "rgba(140,150,180,0.08)",
}


# ── IO ──────────────────────────────────────────────────────────────────────
def _read_json(path, default=None):
    try:
        return json.loads(path.read_text()) if path.exists() else default
    except Exception:
        return default


def _asset_of(option_symbol: str) -> str:
    """'NSE:NIFTY25NOV23500CE' → 'NIFTY'."""
    s = option_symbol.split(":")[-1]
    m = re.match(r"^([A-Za-z]+?)\d", s)
    return m.group(1) if m else s


def _pct_color(pct: float) -> str:
    return COLORS["pos"] if pct < 60 else (COLORS["amber"] if pct < 85 else COLORS["neg"])


# ── component: status strip ─────────────────────────────────────────────────
def status_strip():
    st = _read_json(APP_STATUS_FILE, {}) or {}
    status = str(st.get("status", "unknown")).upper()
    color = {"RUNNING": COLORS["pos"], "SCANNING": COLORS["accent"],
             "BRAKE": COLORS["amber"], "AUTH_FAILED": COLORS["neg"],
             "STOPPED": COLORS["muted"]}.get(status, COLORS["dim"])
    return html.Div([
        html.Span("●", style={"color": color, "fontSize": "18px", "marginRight": "8px"}),
        html.Span(f"Engine: {status}", style={"color": COLORS["text"], "fontWeight": "600"}),
        html.Span(st.get("message", ""), style={"color": COLORS["dim"], "marginLeft": "16px"}),
        html.Span(f"updated {st.get('timestamp', '—')}",
                  style={"color": COLORS["muted"], "marginLeft": "auto", "fontSize": "12px"}),
    ], style={"display": "flex", "alignItems": "center", "padding": "14px 20px",
              "background": COLORS["card"], "borderRadius": "12px",
              "border": f"1px solid {COLORS['border']}"})


# ── component: API-usage meter ──────────────────────────────────────────────
def api_panel():
    usage = _read_json(API_USAGE_FILE, {}) or {}
    cats = usage.get("categories", {})
    cards = []
    for cat, lim in API_LIMITS.items():
        c = cats.get(cat, {})
        day = c.get("day", {"used": 0, "limit": lim["day"]})
        used, limit = day.get("used", 0), day.get("limit", lim["day"])
        pct = round(100 * used / limit, 1) if limit else 0
        sec = c.get("sec", {"used": 0, "limit": lim["sec"]})
        mn = c.get("min", {"used": 0, "limit": lim["min"]})
        cards.append(html.Div([
            html.Div(cat, style={"color": COLORS["dim"], "fontSize": "12px",
                                 "letterSpacing": "1px", "marginBottom": "6px"}),
            html.Div([html.Span(f"{used:,}", style={"fontSize": "22px", "fontWeight": "700",
                                                    "color": COLORS["text"]}),
                      html.Span(f" / {limit:,} day", style={"color": COLORS["muted"],
                                                            "fontSize": "12px"})]),
            html.Div(style={"height": "6px", "borderRadius": "3px", "marginTop": "8px",
                            "background": COLORS["border"], "overflow": "hidden"},
                     children=html.Div(style={"height": "100%", "width": f"{min(pct, 100)}%",
                                              "background": _pct_color(pct)})),
            html.Div(f"{pct}%  ·  {sec.get('used', 0)}/{sec.get('limit')}·s  "
                     f"{mn.get('used', 0)}/{mn.get('limit')}·m",
                     style={"color": COLORS["muted"], "fontSize": "11px", "marginTop": "6px"}),
        ], style={"flex": "1", "padding": "16px", "background": COLORS["card"],
                  "borderRadius": "12px", "border": f"1px solid {COLORS['border']}"}))
    return html.Div([
        html.Div("API USAGE (Standard plan)", style={"color": COLORS["dim"],
                 "fontSize": "13px", "margin": "4px 2px 10px"}),
        html.Div(cards, style={"display": "flex", "gap": "14px"}),
    ])


# ── component: per-asset P/L booking chart (Alcadeias shape) ────────────────
def booking_chart(asset: str, closes: list):
    closes = sorted(closes, key=lambda r: r.get("timestamp", ""))
    labels, profits, colors, cum_list, peaks, hover = [], [], [], [], [], []
    cum = peak = 0.0
    for i, r in enumerate(closes, 1):
        ts = r.get("timestamp", "")
        t = ts[11:16] if len(ts) >= 16 else f"#{i}"
        p = float(r.get("effective_pl", 0))
        labels.append(t); profits.append(p)
        colors.append(COLORS["pos"] if p >= 0 else COLORS["neg"])
        cum += p; cum_list.append(round(cum, 2))
        peak = max(peak, cum); peaks.append(round(peak, 2))
        dd = peak - cum
        hover.append(f"{t}<br>Booked: ₹{p:.2f}<br>Cumulative: ₹{cum:.2f}<br>"
                     f"Peak: ₹{peak:.2f}<br>Drawdown: ₹{dd:.2f}<br>qty {r.get('qty', 0)}")

    fig = make_subplots(rows=2, cols=1, row_heights=[0.45, 0.55], vertical_spacing=0.16,
                        subplot_titles=[f"{asset} — P/L per booking",
                                        "Cumulative P/L & drawdown"])
    fig.add_trace(go.Bar(x=labels, y=profits, marker=dict(color=colors),
                         text=[f"₹{p:.0f}" for p in profits], textposition="outside",
                         textfont=dict(size=10, color=COLORS["dim"]),
                         hovertext=hover, hoverinfo="text", showlegend=False), row=1, col=1)
    cum_color = COLORS["pos"] if cum >= 0 else COLORS["neg"]
    fig.add_trace(go.Scatter(x=labels, y=cum_list, mode="lines",
                             line=dict(color=cum_color, width=2, shape="spline"),
                             fill="tozeroy", fillcolor="rgba(124,58,237,0.06)",
                             showlegend=False, hoverinfo="skip"), row=2, col=1)
    fig.add_trace(go.Scatter(x=labels, y=peaks, mode="lines",
                             line=dict(color=COLORS["amber"], width=1.5, dash="dot"),
                             showlegend=False, hoverinfo="skip"), row=2, col=1)
    fig.add_trace(go.Scatter(x=labels, y=peaks, mode="lines", line=dict(width=0),
                             showlegend=False, hoverinfo="skip"), row=2, col=1)
    fig.add_trace(go.Scatter(x=labels, y=cum_list, mode="lines", line=dict(width=0),
                             fill="tonexty", fillcolor="rgba(239,68,68,0.15)",
                             showlegend=False, hoverinfo="skip"), row=2, col=1)
    fig.add_hline(y=0, line_dash="dot", line_color=COLORS["muted"], opacity=0.5, row=2, col=1)
    fig.update_layout(height=430, margin=dict(l=48, r=18, t=30, b=28),
                      paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                      font=dict(color=COLORS["dim"], size=11))
    for i in (1, 2):
        fig.update_xaxes(showgrid=False, row=i, col=1, tickfont=dict(size=9, color=COLORS["muted"]))
        fig.update_yaxes(showgrid=True, gridcolor=COLORS["grid"], zeroline=True,
                         zerolinecolor=COLORS["muted"], row=i, col=1,
                         tickfont=dict(size=10, color=COLORS["muted"]))
    for ann in fig["layout"]["annotations"]:
        ann["font"] = dict(size=12, color=COLORS["dim"])
    return dcc.Graph(figure=fig, config={"displayModeBar": False})


def booking_section():
    hist = _read_json(PROFIT_HISTORY, []) or []
    today = now_ist().date().isoformat()
    by_asset = defaultdict(list)
    booked = defaultdict(float)
    for r in hist:
        if r.get("action") == "CLOSE" and r.get("date") == today:
            a = _asset_of(r.get("symbol", ""))
            by_asset[a].append(r); booked[a] += float(r.get("effective_pl", 0))
    if not by_asset:
        return html.Div("No bookings yet today.", style={
            "color": COLORS["dim"], "padding": "32px", "textAlign": "center",
            "background": COLORS["card"], "borderRadius": "12px",
            "border": f"1px solid {COLORS['border']}"})
    children = []
    for asset, closes in sorted(by_asset.items()):
        total = booked[asset]
        children.append(html.Div([
            html.Div([
                html.Span(asset, style={"fontWeight": "700", "color": COLORS["text"]}),
                html.Span(f"  {len(closes)} closes · booked ₹{total:,.2f}",
                          style={"color": COLORS["pos"] if total >= 0 else COLORS["neg"],
                                 "marginLeft": "10px", "fontSize": "13px"}),
            ], style={"padding": "10px 14px 0"}),
            booking_chart(asset, closes),
        ], style={"background": COLORS["card"], "borderRadius": "12px", "marginBottom": "14px",
                  "border": f"1px solid {COLORS['border']}"}))
    return html.Div(children)


# ── component: strategy log ─────────────────────────────────────────────────
def log_table():
    today = now_ist().strftime("%Y-%m-%d")
    entries = _read_json(STRATEGY_LOG_DIR / f"{today}.json", []) or []
    rows = []
    for e in entries[-25:][::-1]:
        rows.append(html.Tr([
            html.Td(e.get("timestamp", "")[11:], style={"color": COLORS["muted"], "fontSize": "11px"}),
            html.Td(e.get("symbol", ""), style={"color": COLORS["dim"]}),
            html.Td(e.get("action", ""), style={"color": COLORS["text"], "fontWeight": "600"}),
            html.Td(str(e.get("qty", "")), style={"color": COLORS["dim"]}),
            html.Td(e.get("details", ""), style={"color": COLORS["muted"], "fontSize": "11px"}),
        ]))
    return html.Div([
        html.Div("STRATEGY LOG", style={"color": COLORS["dim"], "fontSize": "13px",
                 "margin": "4px 2px 10px"}),
        html.Table(rows, style={"width": "100%", "borderCollapse": "collapse"}) if rows
        else html.Div("No events today.", style={"color": COLORS["muted"], "padding": "16px"}),
    ], style={"padding": "16px", "background": COLORS["card"], "borderRadius": "12px",
              "border": f"1px solid {COLORS['border']}"})


# ── app ─────────────────────────────────────────────────────────────────────
app = dash.Dash(__name__, title="fyers_live")

app.layout = html.Div([
    dcc.Interval(id="tick", interval=REFRESH_MS, n_intervals=0),
    html.Div([
        html.H2("fyers_live", style={"color": COLORS["text"], "margin": "0"}),
        html.Span("LIVE", style={"color": COLORS["neg"], "fontWeight": "700",
                                 "marginLeft": "12px", "fontSize": "13px"}),
    ], style={"display": "flex", "alignItems": "baseline", "marginBottom": "16px"}),
    html.Div(id="status"),
    html.Div(id="api", style={"marginTop": "18px"}),
    html.Div(id="bookings", style={"marginTop": "18px"}),
    html.Div(id="log", style={"marginTop": "18px"}),
], style={"maxWidth": "1100px", "margin": "0 auto", "padding": "28px",
          "fontFamily": "Inter, system-ui, sans-serif", "background": COLORS["bg"],
          "minHeight": "100vh"})


@app.callback(
    Output("status", "children"), Output("api", "children"),
    Output("bookings", "children"), Output("log", "children"),
    Input("tick", "n_intervals"),
)
def _refresh(_):
    return status_strip(), api_panel(), booking_section(), log_table()


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8050
    print(f"fyers_live dashboard → http://127.0.0.1:{port}")
    app.run(debug=False, host="127.0.0.1", port=port)
