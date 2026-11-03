"""
dashboard.py — rich read-only dashboard for fyers_demo / fyers_live.

Separate process. Reads the JSON state files the engine writes under
C:/Ballom_FYR/state/ and renders:
  • account KPIs      (account_state.json + profit_history.json)
  • API-usage meter   (api_usage.json)
  • per-symbol panel  (signal_state.json): signal, best CE/PE pair, SHA candles,
    multi-timeframe RSI, live position
  • P/L booking chart (profit_history.json): bar-per-close + cumulative/drawdown
  • strategy log      (strategy_log/<date>.json)

Colour semantics: BUY/positive = green, SELL/negative = red, nothing/neutral = grey,
ADD = amber, CLOSE = purple, HOLD = blue; every status colour is paired with a label.

Run:  python dashboard.py [port]   (default 8050)  →  http://127.0.0.1:8050
"""

from __future__ import annotations

import json
import re
import sys
from collections import defaultdict

import dash
from dash import dcc, html
from dash.dependencies import Input, Output
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from constants import (
    STATE_DIR, API_USAGE_FILE, APP_STATUS_FILE, STRATEGY_LOG_DIR,
    SIGNAL_STATE_FILE, ACCOUNT_STATE_FILE, API_LIMITS,
    RSI_OVERSOLD, RSI_OVERBOUGHT, now_ist,
)

PROFIT_HISTORY = STATE_DIR / "profit_history.json"
REFRESH_MS = 5000
IS_DEMO = False  # fyers_live (real trading)

C = {
    "bg": "#0a0e17", "card": "#141a28", "card2": "#1a2133", "border": "#232c42",
    "text": "#e6e9f2", "dim": "#93a0bd", "muted": "#5b6680",
    "green": "#22c55e", "red": "#ef4444", "grey": "#6b7280",
    "amber": "#f59e0b", "purple": "#a78bfa", "blue": "#60a5fa", "teal": "#2dd4bf",
    "grid": "rgba(140,150,180,0.08)",
}
RSI_ORDER = ["TIMEFRAME_M1", "TIMEFRAME_M5", "TIMEFRAME_M15", "TIMEFRAME_M30",
             "TIMEFRAME_H1", "TIMEFRAME_H4", "TIMEFRAME_H6"]
RSI_LABEL = {"TIMEFRAME_M1": "1m", "TIMEFRAME_M5": "5m", "TIMEFRAME_M15": "15m",
             "TIMEFRAME_M30": "30m", "TIMEFRAME_H1": "1h", "TIMEFRAME_H4": "4h",
             "TIMEFRAME_H6": "6h"}


def _read(path, default=None):
    try:
        return json.loads(path.read_text()) if path.exists() else default
    except Exception:
        return default


def _asset_of(sym: str) -> str:
    s = sym.split(":")[-1]
    m = re.match(r"^([A-Za-z]+?)\d", s)
    return m.group(1) if m else s


def _sig_color(sig: str) -> str:
    s = (sig or "").upper()
    if s == "BUY":        return C["green"]
    if s == "SELL":       return C["red"]
    if s == "ADD":        return C["amber"]
    if s == "CLOSE":      return C["purple"]
    if s.startswith("HOLD"):  return C["blue"]
    return C["grey"]      # DO_NOTHING


def _pl_color(v: float) -> str:
    return C["green"] if v > 0 else (C["red"] if v < 0 else C["grey"])


def _card(children, pad="16px", extra=None):
    st = {"background": C["card"], "border": f"1px solid {C['border']}",
          "borderRadius": "14px", "padding": pad}
    if extra:
        st.update(extra)
    return html.Div(children, style=st)


def _chip(text, color, bg=None):
    return html.Span(text, style={
        "color": color, "background": bg or "rgba(255,255,255,0.04)",
        "border": f"1px solid {color}", "borderRadius": "7px",
        "padding": "2px 9px", "fontSize": "12px", "fontWeight": "700",
        "letterSpacing": "0.3px"})


# ── account KPIs ─────────────────────────────────────────────────────────────
def kpi_row():
    acct = _read(ACCOUNT_STATE_FILE, {}) or {}
    hist = _read(PROFIT_HISTORY, []) or []
    today = now_ist().date().isoformat()
    closes_today = [r for r in hist if r.get("action") == "CLOSE" and r.get("date") == today]
    realized = round(sum(float(r.get("net_pl", r.get("effective_pl", 0))) for r in closes_today), 2)
    taxes = round(sum(float(r.get("charges", 0)) for r in closes_today), 2)
    bal = float(acct.get("balance", 0))
    eq = float(acct.get("equity", bal))
    margin = float(acct.get("margin_used", 0))
    unreal = float(acct.get("unrealized", 0))
    total = round(realized + unreal, 2)
    tiles = [
        ("Balance", f"₹{bal:,.0f}", C["text"]),
        ("Equity", f"₹{eq:,.0f}", C["teal"]),
        ("Margin used", f"₹{margin:,.0f}", C["amber"]),
        ("Unrealised", f"₹{unreal:,.2f}", _pl_color(unreal)),
        ("Realised net (today)", f"₹{realized:,.2f}", _pl_color(realized)),
        ("Taxes (today)", f"₹{taxes:,.2f}", C["purple"]),
        ("Total P&L", f"₹{total:,.2f}", _pl_color(total)),
    ]
    cells = [_card([
        html.Div(lbl, style={"color": C["dim"], "fontSize": "11px",
                             "textTransform": "uppercase", "letterSpacing": "0.6px"}),
        html.Div(val, style={"color": col, "fontSize": "21px", "fontWeight": "800",
                             "marginTop": "5px"}),
    ], pad="14px 16px", extra={"flex": "1"}) for lbl, val, col in tiles]
    return html.Div(cells, style={"display": "flex", "gap": "12px", "flexWrap": "wrap"})


# ── API meter ────────────────────────────────────────────────────────────────
def api_panel():
    usage = (_read(API_USAGE_FILE, {}) or {}).get("categories", {})
    cards = []
    for cat, lim in API_LIMITS.items():
        c = usage.get(cat, {})
        day = c.get("day", {"used": 0, "limit": lim["day"]})
        used, limit = day.get("used", 0), day.get("limit", lim["day"])
        pct = round(100 * used / limit, 1) if limit else 0
        col = C["green"] if pct < 60 else (C["amber"] if pct < 85 else C["red"])
        sec = c.get("sec", {}); mn = c.get("min", {})
        cards.append(_card([
            html.Div(cat, style={"color": C["dim"], "fontSize": "11px", "letterSpacing": "0.8px"}),
            html.Div([html.Span(f"{used:,}", style={"fontSize": "19px", "fontWeight": "800",
                                                    "color": C["text"]}),
                      html.Span(f" / {limit:,}·day", style={"color": C["muted"], "fontSize": "11px"})]),
            html.Div(style={"height": "5px", "borderRadius": "3px", "marginTop": "7px",
                            "background": C["border"], "overflow": "hidden"},
                     children=html.Div(style={"height": "100%", "width": f"{min(pct,100)}%", "background": col})),
            html.Div(f"{pct}%  ·  {sec.get('used',0)}/{sec.get('limit','-')}·s  "
                     f"{mn.get('used',0)}/{mn.get('limit','-')}·m",
                     style={"color": C["muted"], "fontSize": "10.5px", "marginTop": "5px"}),
        ], pad="13px 15px", extra={"flex": "1"}))
    return html.Div([
        html.Div("API USAGE — Standard plan", style={"color": C["dim"], "fontSize": "12px",
                 "margin": "2px 2px 9px", "letterSpacing": "0.6px"}),
        html.Div(cards, style={"display": "flex", "gap": "12px"}),
    ])


# ── per-symbol strategy panel ────────────────────────────────────────────────
def _sha_block(label, sha):
    lst = sha.get("list", []) or []
    power = sha.get("power", 0)
    sq = [html.Span(style={"display": "inline-block", "width": "15px", "height": "15px",
                           "borderRadius": "4px", "marginRight": "3px",
                           "background": C["green"] if v == 1 else C["red"],
                           "opacity": "0.9"}) for v in lst[:10]]
    return html.Div([
        html.Span(label, style={"color": C["dim"], "fontSize": "11px", "width": "78px",
                                "display": "inline-block"}),
        html.Span(f"{power}/{len(lst) or '-'}", style={
            "color": C["green"] if power * 2 >= (len(lst) or 1) else C["red"],
            "fontWeight": "700", "fontSize": "12px", "marginRight": "10px"}),
        html.Span(sq),
    ], style={"margin": "3px 0"})


def _rsi_block(rsi):
    cells = []
    for tf in RSI_ORDER:
        if tf not in rsi:
            continue
        v = float(rsi[tf])
        col = C["green"] if v <= RSI_OVERSOLD else (C["red"] if v >= RSI_OVERBOUGHT else C["grey"])
        cells.append(html.Div([
            html.Div(RSI_LABEL[tf], style={"color": C["muted"], "fontSize": "10px"}),
            html.Div(f"{v:.0f}", style={"color": col, "fontWeight": "800", "fontSize": "14px"}),
        ], style={"textAlign": "center", "padding": "4px 8px", "borderRadius": "7px",
                  "background": "rgba(255,255,255,0.03)", "border": f"1px solid {C['border']}"}))
    if not cells:
        cells = [html.Span("no RSI yet", style={"color": C["muted"], "fontSize": "11px"})]
    return html.Div(cells, style={"display": "flex", "gap": "6px", "flexWrap": "wrap", "margin": "6px 0"})


def _pair_block(pair):
    if not pair:
        return html.Div("scanning…", style={"color": C["muted"], "fontSize": "12px"})
    def leg(tag, sym, strike, prem, color):
        return _card([
            html.Div([_chip(tag, color), html.Span(f"  {strike}", style={"color": C["text"],
                     "fontWeight": "700", "marginLeft": "6px"})]),
            html.Div(sym or "-", style={"color": C["dim"], "fontSize": "11px", "marginTop": "4px",
                     "fontFamily": "monospace"}),
            html.Div(f"₹{prem}", style={"color": C["text"], "fontSize": "13px", "marginTop": "2px"}),
        ], pad="10px 12px", extra={"flex": "1", "background": C["card2"]})
    return html.Div([
        leg("CE", pair.get("CE_Symbol"), pair.get("CE_Strike"), pair.get("CE_Premium"), C["green"]),
        leg("PE", pair.get("PE_Symbol"), pair.get("PE_Strike"), pair.get("PE_Premium"), C["red"]),
        html.Div([
            html.Div(f"Exp {pair.get('Expiry','-')}", style={"color": C["dim"], "fontSize": "11px"}),
            html.Div(f"DTE {pair.get('DTE','-')}", style={"color": C["blue"], "fontSize": "13px",
                     "fontWeight": "700"}),
        ], style={"display": "flex", "flexDirection": "column", "justifyContent": "center",
                  "padding": "0 6px"}),
    ], style={"display": "flex", "gap": "8px"})


def _position_block(pos):
    if not pos:
        return html.Div([_chip("FLAT", C["grey"])], style={"margin": "6px 0"})
    cpl = float(pos.get("cycle_pl", 0) or 0)
    side_col = C["green"] if pos.get("side") == "CE" else C["red"]
    return html.Div([
        _chip(f"IN {pos.get('side')}", side_col),
        html.Span(f"  qty {pos.get('netQty')}", style={"color": C["text"], "marginLeft": "8px"}),
        html.Span(f"  L{pos.get('mg',0)}", style={"color": C["amber"], "marginLeft": "8px",
                  "fontSize": "12px"}),
        html.Span(f"  cycle ₹{cpl:,.2f}", style={"color": _pl_color(cpl), "marginLeft": "8px",
                  "fontWeight": "700"}),
    ], style={"margin": "6px 0"})


def symbol_panel(symbol, s, closes):
    sig = s.get("signal", "DO_NOTHING")
    rsi_blocked = s.get("rsi_blocked")
    head = html.Div([
        html.Span(symbol, style={"color": C["text"], "fontSize": "18px", "fontWeight": "800"}),
        html.Span(s.get("asset_type", ""), style={"color": C["muted"], "fontSize": "11px",
                  "marginLeft": "8px"}),
        html.Span(_chip(sig, _sig_color(sig)), style={"marginLeft": "12px"}),
        html.Span(f"units {s.get('units','-')}", style={"color": C["dim"], "fontSize": "12px",
                  "marginLeft": "auto"}),
        html.Span(_chip("RSI-BLOCK", C["amber"]) if rsi_blocked else "",
                  style={"marginLeft": "8px"}),
    ], style={"display": "flex", "alignItems": "center", "marginBottom": "10px"})
    left = _card([
        head,
        _pair_block(s.get("pair")),
        html.Div(style={"height": "10px"}),
        _sha_block("SHA signal", s.get("sha", {})),
        _sha_block("SHA trend", s.get("sha_trend", {})),
        _rsi_block(s.get("rsi", {})),
        _position_block(s.get("position")),
    ], extra={"flex": "1.1"})
    g = sum(float(r.get("effective_pl", 0)) for r in closes)
    tx = sum(float(r.get("charges", 0)) for r in closes)
    n = round(sum(float(r.get("net_pl", r.get("effective_pl", 0))) for r in closes), 2)
    booked_line = html.Div([
        html.Span(f"{len(closes)} closes today", style={"color": C["dim"], "fontSize": "11px"}),
        html.Span(f"gross ₹{g:,.0f}", style={"color": C["dim"], "fontSize": "11px", "marginLeft": "10px"}),
        html.Span(f"tax ₹{tx:,.0f}", style={"color": C["purple"], "fontSize": "11px", "marginLeft": "10px"}),
        html.Span(f"net ₹{n:,.2f}", style={"color": _pl_color(n), "fontSize": "13px",
                  "fontWeight": "800", "marginLeft": "10px"}),
    ], style={"padding": "4px 10px 2px"})
    right = _card([booked_line, booking_chart(symbol, closes)], pad="8px", extra={"flex": "1.4"})
    return html.Div([left, right], style={"display": "flex", "gap": "14px", "marginBottom": "14px"})


# ── per-asset P/L booking chart ──────────────────────────────────────────────
def booking_chart(asset, closes):
    closes = sorted(closes, key=lambda r: r.get("timestamp", ""))
    labels, profits, colors, cum, peaks, hover = [], [], [], [], [], []
    c = peak = 0.0
    for i, r in enumerate(closes, 1):
        ts = r.get("timestamp", "")
        t = ts[11:16] if len(ts) >= 16 else f"#{i}"
        gross = float(r.get("effective_pl", 0))
        tax = float(r.get("charges", 0))
        p = float(r.get("net_pl", gross - tax))        # actual profit (after tax)
        labels.append(t); profits.append(p)
        colors.append(C["green"] if p >= 0 else C["red"])
        c += p; cum.append(round(c, 2)); peak = max(peak, c); peaks.append(round(peak, 2))
        hover.append(f"{t}<br>Gross ₹{gross:.2f}<br>Tax ₹{tax:.2f}<br>"
                     f"<b>Net ₹{p:.2f}</b><br>Cum net ₹{c:.2f}<br>Peak ₹{peak:.2f}<br>DD ₹{peak-c:.2f}")
    fig = make_subplots(rows=2, cols=1, row_heights=[0.5, 0.5], vertical_spacing=0.18,
                        subplot_titles=[f"{asset} — net P/L per booking (after tax)",
                                        "Cumulative net & drawdown"])
    if closes:
        fig.add_trace(go.Bar(x=labels, y=profits, marker=dict(color=colors),
                             hovertext=hover, hoverinfo="text", showlegend=False), row=1, col=1)
        cc = C["green"] if c >= 0 else C["red"]
        fig.add_trace(go.Scatter(x=labels, y=cum, mode="lines+markers",
                                 line=dict(color=cc, width=2, shape="spline"),
                                 marker=dict(size=6, color=cc, line=dict(width=1, color=C["bg"])),
                                 fill="tozeroy", fillcolor="rgba(124,58,237,0.06)",
                                 showlegend=False, hoverinfo="skip"), row=2, col=1)
        fig.add_trace(go.Scatter(x=labels, y=peaks, mode="lines",
                                 line=dict(color=C["amber"], width=1.3, dash="dot"),
                                 showlegend=False, hoverinfo="skip"), row=2, col=1)
        fig.add_trace(go.Scatter(x=labels, y=peaks, mode="lines", line=dict(width=0),
                                 showlegend=False, hoverinfo="skip"), row=2, col=1)
        fig.add_trace(go.Scatter(x=labels, y=cum, mode="lines", line=dict(width=0), fill="tonexty",
                                 fillcolor="rgba(239,68,68,0.15)", showlegend=False,
                                 hoverinfo="skip"), row=2, col=1)
    fig.add_hline(y=0, line_dash="dot", line_color=C["muted"], opacity=0.5, row=2, col=1)
    fig.update_layout(height=300, margin=dict(l=42, r=14, t=26, b=22),
                      paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                      font=dict(color=C["dim"], size=10), bargap=0.35)
    for i in (1, 2):
        fig.update_xaxes(showgrid=False, row=i, col=1, tickfont=dict(size=8, color=C["muted"]))
        fig.update_yaxes(showgrid=True, gridcolor=C["grid"], zeroline=True,
                         zerolinecolor=C["muted"], row=i, col=1, tickfont=dict(size=9, color=C["muted"]))
    for ann in fig["layout"]["annotations"]:
        ann["font"] = dict(size=11, color=C["dim"])
    return dcc.Graph(figure=fig, config={"displayModeBar": False})


# ── status + log ─────────────────────────────────────────────────────────────
def status_strip():
    st = _read(APP_STATUS_FILE, {}) or {}
    status = str(st.get("status", "unknown")).upper()
    col = {"RUNNING": C["green"], "SCANNING": C["purple"], "BRAKE": C["amber"],
           "AUTH_FAILED": C["red"], "STOPPED": C["muted"]}.get(status, C["dim"])
    return _card([html.Div([
        html.Span("●", style={"color": col, "fontSize": "16px", "marginRight": "8px"}),
        html.Span(f"Engine {status}", style={"color": C["text"], "fontWeight": "700"}),
        html.Span(st.get("message", ""), style={"color": C["dim"], "marginLeft": "14px", "fontSize": "13px"}),
        html.Span(f"updated {st.get('timestamp','—')}", style={"color": C["muted"],
                  "marginLeft": "auto", "fontSize": "11px"}),
    ], style={"display": "flex", "alignItems": "center"})], pad="12px 18px")


def log_table():
    today = now_ist().strftime("%Y-%m-%d")
    entries = _read(STRATEGY_LOG_DIR / f"{today}.json", []) or []
    badge = {"ENTRY": C["green"], "ADD": C["amber"], "CLOSE_SENT": C["purple"],
             "CLOSE_BOOKED": C["purple"], "SCAN_EMPTY": C["grey"]}
    rows = []
    for e in entries[-30:][::-1]:
        act = e.get("action", "")
        rows.append(html.Tr([
            html.Td(e.get("timestamp", "")[11:], style={"color": C["muted"], "fontSize": "11px", "padding": "3px 8px"}),
            html.Td(e.get("symbol", ""), style={"color": C["dim"], "padding": "3px 8px"}),
            html.Td(act, style={"color": badge.get(act, C["text"]), "fontWeight": "700", "padding": "3px 8px"}),
            html.Td(str(e.get("qty", "")), style={"color": C["dim"], "padding": "3px 8px"}),
            html.Td(e.get("details", ""), style={"color": C["muted"], "fontSize": "11px", "padding": "3px 8px"}),
        ]))
    return _card([
        html.Div("STRATEGY LOG", style={"color": C["dim"], "fontSize": "12px",
                 "margin": "0 0 8px", "letterSpacing": "0.6px"}),
        html.Table(rows, style={"width": "100%", "borderCollapse": "collapse"})
        if rows else html.Div("No events today.", style={"color": C["muted"]}),
    ])


# ── app ──────────────────────────────────────────────────────────────────────
app = dash.Dash(__name__, title="fyers dashboard")
app.layout = html.Div([
    dcc.Interval(id="tick", interval=REFRESH_MS, n_intervals=0),
    html.Div([
        html.H2("Ballom Fyers", style={"color": C["text"], "margin": "0"}),
        html.Span("DEMO" if IS_DEMO else "LIVE",
                  style={"color": C["amber"] if IS_DEMO else C["red"], "fontWeight": "800",
                         "marginLeft": "12px", "fontSize": "13px"}),
    ], style={"display": "flex", "alignItems": "baseline", "marginBottom": "14px"}),
    html.Div(id="status"),
    html.Div(id="kpis", style={"marginTop": "14px"}),
    html.Div(id="api", style={"marginTop": "16px"}),
    html.Div(id="symbols", style={"marginTop": "18px"}),
    html.Div(id="log", style={"marginTop": "16px"}),
], style={"maxWidth": "1280px", "margin": "0 auto", "padding": "26px",
          "fontFamily": "Inter, system-ui, sans-serif", "background": C["bg"], "minHeight": "100vh"})


@app.callback(
    Output("status", "children"), Output("kpis", "children"), Output("api", "children"),
    Output("symbols", "children"), Output("log", "children"),
    Input("tick", "n_intervals"),
)
def _refresh(_):
    signals = _read(SIGNAL_STATE_FILE, {}) or {}
    hist = _read(PROFIT_HISTORY, []) or []
    today = now_ist().date().isoformat()
    closes_by_asset = defaultdict(list)
    for r in hist:
        if r.get("action") == "CLOSE" and r.get("date") == today:
            closes_by_asset[_asset_of(r.get("symbol", ""))].append(r)
    panels = [symbol_panel(sym, s, closes_by_asset.get(sym, []))
              for sym, s in signals.items() if isinstance(s, dict)]
    if not panels:
        panels = [html.Div("Waiting for engine signals…", style={
            "color": C["muted"], "padding": "28px", "textAlign": "center"})]
    return status_strip(), kpi_row(), api_panel(), panels, log_table()


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8050
    print(f"dashboard → http://127.0.0.1:{port}")
    app.run(debug=False, host="127.0.0.1", port=port)
