"""
dashboard.py — rich read-only dashboard for fyers_demo / fyers_live.

Separate process. Reads the JSON state files the engine writes under
C:/Ballom_FYR/state/ and renders:
  • account KPIs      (account_state.json + profit_history.json)
  • API-usage meter   (api_usage.json)
  • per-symbol panel  (signal_state.json): signal, best CE/PE pair, RSI+MACD slope
    scores (open/book/fc) + per-TF slope arrows, multi-timeframe RSI, live position
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
    SIGNAL_STATE_FILE, ACCOUNT_STATE_FILE, API_LIMITS, API_LIMITS_PRIME,
    RSI_OVERSOLD, RSI_OVERBOUGHT, now_ist,
    ENTRY_S, BOOK_S, FORCE_CLOSE_S,
    OPEN_SCORE_WEIGHTS, BOOK_SCORE_WEIGHTS, FORCE_CLOSE_SCORE_WEIGHTS,
    SLOPE_TIMEFRAMES, LEG_SLOPE_TIMEFRAMES,
)

PROFIT_HISTORY = STATE_DIR / "profit_history.json"
REFRESH_MS = 5000
IS_DEMO = True   # demo-branch header tag (set False on fyers_live)

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
    acct = _read(ACCOUNT_STATE_FILE, {}) or {}
    plan = str(acct.get("api_plan", "standard")).upper()
    limits = API_LIMITS_PRIME if plan == "PRIME" else API_LIMITS   # plan-aware thresholds
    cards = []
    for cat, lim in limits.items():
        c = usage.get(cat, {})
        day = c.get("day", {"used": 0, "limit": lim["day"]})
        used, limit = day.get("used", 0), day.get("limit", lim["day"])
        sec_lim, min_lim = lim["sec"], lim["min"]
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
            html.Div(f"{pct}%  ·  {sec.get('used',0)}/{sec.get('limit',sec_lim)}·s  "
                     f"{mn.get('used',0)}/{mn.get('limit',min_lim)}·m",
                     style={"color": C["muted"], "fontSize": "10.5px", "marginTop": "5px"}),
        ], pad="13px 15px", extra={"flex": "1"}))
    renew = acct.get("prime_renewal_days")
    plan_col = C["amber"] if plan == "PRIME" else C["grey"]
    header_bits = [
        html.Span("API USAGE", style={"color": C["dim"], "fontSize": "12px", "letterSpacing": "0.6px"}),
        html.Span(_chip(f"{plan} PLAN", plan_col), style={"marginLeft": "10px"}),
    ]
    if isinstance(renew, (int, float)):
        rv = int(renew)
        txt = f"· renews in {rv}d" if rv >= 0 else f"· EXPIRED {abs(rv)}d ago"
        header_bits.append(html.Span(txt, style={
            "color": C["red"] if rv <= 7 else C["dim"], "fontSize": "11px", "marginLeft": "10px"}))
    return html.Div([
        html.Div(header_bits, style={"margin": "2px 2px 9px", "display": "flex", "alignItems": "center"}),
        html.Div(cards, style={"display": "flex", "gap": "12px"}),
    ])


# ── per-symbol strategy panel ────────────────────────────────────────────────
# Timeframes drawn, coarsest→finest (matches the slope-score sections).
# Timeframes drawn in the pivot grids — DERIVED from the live config (index SLOPE_TIMEFRAMES +
# leg LEG_SLOPE_TIMEFRAMES), so the dashboard always shows exactly whichever timeframes the
# strategy actually uses and never a stale one. Ordered coarsest→finest.
_TF_RANK = {"D1": 0, "H6": 1, "H4": 2, "H1": 3, "M30": 4, "M15": 5, "M5": 6, "M1": 7}
SLOPE_TF_ORDER = sorted(
    {tf.replace("TIMEFRAME_", "") for tf in (list(SLOPE_TIMEFRAMES) + list(LEG_SLOPE_TIMEFRAMES))},
    key=lambda k: _TF_RANK.get(k, 99),
)


def _score_axis(weights):
    """Raw-score range of a section = ±(2·Σ|weight|); the meter axis uses this full span."""
    return max(1.0, 2.0 * sum(abs(w) for w in (weights or {}).values()))


def _meter(title, note, value, thr, axis, hi, lo, within):
    """Diverging −axis…+axis threshold meter (open / book / force-close scores).
    value sits on the track; ±thr are marked; fill + marker are coloured by zone and always
    paired with the signed number + a labelled chip (never colour-alone). hi/lo = (label,color)
    for value ≥ +thr / ≤ −thr; within = (label,color) for the neutral band."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        v = 0.0
    t = float(thr)
    ax = float(axis) or 1.0

    def pos(x):
        return max(0.0, min(100.0, (x + ax) / (2.0 * ax) * 100.0))

    lo_pct, hi_pct, zero_pct, p = pos(-t), pos(t), pos(0), pos(v)
    fl, fw = (zero_pct, p - zero_pct) if v >= 0 else (p, zero_pct - p)
    if v >= t:
        zlab, zcol = hi
    elif v <= -t:
        zlab, zcol = lo
    else:
        zlab, zcol = within
    track = html.Div(style={
        "position": "relative", "height": "12px", "background": C["bg"],
        "borderRadius": "6px", "border": f"1px solid {C['border']}"}, children=[
        html.Div(style={"position": "absolute", "top": "0", "bottom": "0", "left": f"{lo_pct}%",
                        "width": f"{hi_pct - lo_pct}%", "background": C["grey"], "opacity": "0.18"}),
        html.Div(style={"position": "absolute", "top": "1px", "bottom": "1px", "left": f"{fl}%",
                        "width": f"{fw}%", "background": zcol, "borderRadius": "3px", "opacity": "0.9"}),
        html.Div(style={"position": "absolute", "left": f"{zero_pct}%", "top": "-2px",
                        "bottom": "-2px", "width": "1px", "background": C["muted"]}),
        html.Div(style={"position": "absolute", "left": f"{lo_pct}%", "top": "-3px",
                        "bottom": "-3px", "width": "2px", "background": lo[1]}),
        html.Div(style={"position": "absolute", "left": f"{hi_pct}%", "top": "-3px",
                        "bottom": "-3px", "width": "2px", "background": hi[1]}),
        html.Div(style={"position": "absolute", "left": f"{p}%", "top": "50%",
                        "transform": "translate(-50%, -50%)", "width": "11px", "height": "11px",
                        "borderRadius": "50%", "background": zcol,
                        "border": f"2px solid {C['card']}", "boxShadow": f"0 0 0 1px {zcol}"}),
    ])
    return html.Div(style={"padding": "5px 2px 7px"}, children=[
        html.Div([
            html.Span(title, style={"fontSize": "10px", "fontWeight": "700", "color": C["dim"],
                      "letterSpacing": "0.4px"}),
            html.Span(f"{note} · ±{ax:g}", style={"fontSize": "9px", "color": C["muted"],
                      "marginLeft": "auto"}),
        ], style={"display": "flex", "alignItems": "baseline", "marginBottom": "5px"}),
        html.Div([
            html.Span(f"{v:+g}", style={"fontWeight": "800", "fontSize": "16px", "color": zcol,
                      "minWidth": "40px", "textAlign": "right"}),
            html.Div(track, style={"flex": "1"}),
            _chip(zlab, zcol),
        ], style={"display": "flex", "alignItems": "center", "gap": "9px"}),
    ])


def _profit_meter(cycle_pl, target):
    """₹ progress meter: current cycle P&L toward the booking target (close_threshold).
    Green once bookable (≥ target), red if underwater, amber while climbing. The target line
    and the % toward it make the 'how close to booking' read without colour-alone."""
    try:
        v = float(cycle_pl)
    except (TypeError, ValueError):
        v = 0.0
    try:
        t = float(target)
    except (TypeError, ValueError):
        t = 0.0
    hi = max(t * 1.25, v * 1.1, 1.0)
    lo = min(0.0, v * 1.1)
    span = (hi - lo) or 1.0

    def pos(x):
        return max(0.0, min(100.0, (x - lo) / span * 100.0))

    t_pct, zero_pct, p = pos(t), pos(0), pos(v)
    booked = v >= t and t > 0
    col = C["green"] if booked else (C["red"] if v < 0 else C["amber"])
    fl, fw = (zero_pct, p - zero_pct) if v >= 0 else (p, zero_pct - p)
    pct_to = (v / t * 100.0) if t else 0.0
    track = html.Div(style={
        "position": "relative", "height": "12px", "background": C["bg"],
        "borderRadius": "6px", "border": f"1px solid {C['border']}"}, children=[
        html.Div(style={"position": "absolute", "top": "1px", "bottom": "1px", "left": f"{fl}%",
                        "width": f"{fw}%", "background": col, "borderRadius": "3px", "opacity": "0.9"}),
        html.Div(style={"position": "absolute", "left": f"{zero_pct}%", "top": "-2px",
                        "bottom": "-2px", "width": "1px", "background": C["muted"]}),
        html.Div(style={"position": "absolute", "left": f"{t_pct}%", "top": "-3px", "bottom": "-3px",
                        "width": "2px", "background": C["green"]}),
        html.Div(style={"position": "absolute", "left": f"{p}%", "top": "50%",
                        "transform": "translate(-50%, -50%)", "width": "11px", "height": "11px",
                        "borderRadius": "50%", "background": col,
                        "border": f"2px solid {C['card']}", "boxShadow": f"0 0 0 1px {col}"}),
    ])
    return html.Div(style={"padding": "5px 2px 7px"}, children=[
        html.Div([
            html.Span("PROFIT → BOOK", style={"fontSize": "10px", "fontWeight": "700",
                      "color": C["dim"], "letterSpacing": "0.4px"}),
            html.Span(f"target ₹{t:,.0f} (₹100×units + tax)", style={"fontSize": "9px",
                      "color": C["muted"], "marginLeft": "auto"}),
        ], style={"display": "flex", "alignItems": "baseline", "marginBottom": "5px"}),
        html.Div([
            html.Span(f"₹{v:,.0f}", style={"fontWeight": "800", "fontSize": "16px", "color": col,
                      "minWidth": "56px", "textAlign": "right"}),
            html.Div(track, style={"flex": "1"}),
            _chip("BOOKABLE" if booked else f"{max(0, pct_to):.0f}%", col),
        ], style={"display": "flex", "alignItems": "center", "gap": "9px"}),
    ])


def _fmt_pivot_val(v, clamp):
    """RSI (clamp set) → 1dp; MACD (clamp None) → signed, adaptive precision (unbounded hist)."""
    if clamp is not None:
        return f"{v:.1f}"
    a = abs(v)
    if a == 0:
        return "0"
    if a < 1:
        return f"{v:+.5f}"
    if a < 1000:
        return f"{v:+.3f}"
    return f"{v:+.1f}"


def _pivot_mini_graph(tf_label, g):
    """Compact pivot chart for one timeframe/indicator: the recent series line, the latest
    pivot marker, a dotted pivot→current line coloured by the slope sign (green +ve / red −ve),
    the current-value diamond, and reference lines (RSI OB/OS bands, or the MACD zero line)."""
    g = g or {}
    name = g.get("name", "RSI")
    try:
        series = [float(v) for v in (g.get("series") or [])]
        if not series:
            raise ValueError("no series")
        cur_x = len(series) - 1
        current = float(g.get("current", series[-1]))
        sign = g.get("sign", 0) or 0
        bands = [float(b) for b in (g.get("bands") or []) if isinstance(b, (int, float))]
        clamp = g.get("clamp", [0, 100])
        lc = C["green"] if sign > 0 else (C["red"] if sign < 0 else C["dim"])
        fig = go.Figure()
        for b in bands:
            fig.add_hline(y=b, line_dash="dot", line_color=C["muted"], line_width=0.8, opacity=0.5)
        fig.add_trace(go.Scatter(x=list(range(len(series))), y=series, mode="lines",
                      line=dict(color=C["blue"], width=1.6), hoverinfo="skip", showlegend=False))
        pba, pv = g.get("pivot_bars_ago"), g.get("pivot_value")
        if isinstance(pba, int) and pv is not None and 0 <= (cur_x - pba) <= cur_x:
            piv_x = cur_x - pba
            fig.add_trace(go.Scatter(x=[piv_x, cur_x], y=[float(pv), current], mode="lines",
                          line=dict(color=lc, width=1.8, dash="dot"), hoverinfo="skip", showlegend=False))
            fig.add_trace(go.Scatter(x=[piv_x], y=[float(pv)], mode="markers",
                          marker=dict(color=lc, size=9, symbol="circle", line=dict(width=1, color=C["bg"])),
                          hoverinfo="skip", showlegend=False))
        fig.add_trace(go.Scatter(x=[cur_x], y=[current], mode="markers",
                      marker=dict(color=C["text"], size=7, symbol="diamond", line=dict(width=1, color=C["bg"])),
                      hoverinfo="skip", showlegend=False))
        pts = series + bands + [current]
        pmin, pmax = min(pts), max(pts)
        if clamp is not None:
            lo = max(clamp[0], pmin - 5)
            hi = min(clamp[1], pmax + 5)
            if hi <= lo:
                lo, hi = float(clamp[0]), float(clamp[1])
            ydtick = 20
        else:
            span = pmax - pmin
            pad = (span * 0.12) if span > 0 else (abs(pmax) * 0.12 or 1.0)
            lo, hi = pmin - pad, pmax + pad
            if hi <= lo:
                lo, hi = pmin - 1.0, pmax + 1.0
            ydtick = None
        fig.update_layout(height=104, margin=dict(l=34, r=6, t=3, b=3),
                          paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                          font=dict(color=C["dim"], size=9), showlegend=False)
        fig.update_xaxes(visible=False, showgrid=False, range=[0, max(cur_x, 1)])
        fig.update_yaxes(range=[lo, hi], showgrid=True, gridcolor=C["grid"], gridwidth=0.5,
                         dtick=ydtick, tickfont=dict(size=8, color=C["muted"]))
        arrow = "▲" if sign > 0 else ("▼" if sign < 0 else "—")
        label = f"{tf_label} · {name} {_fmt_pivot_val(current, clamp)} {arrow}"
        return html.Div([
            html.Div(label, style={"fontSize": "10px", "color": lc, "fontWeight": "700",
                     "padding": "2px 0 0 4px"}),
            dcc.Graph(figure=fig, config={"displayModeBar": False}, style={"height": "104px"}),
        ])
    except Exception:
        return html.Div(f"{tf_label}: no {name}", style={"fontSize": "10px", "color": C["muted"],
                        "padding": "6px 4px"})


def _slope_block(s):
    """RSI+MACD slope analysis panel: entry chips, score/exit/₹ meters, and per-timeframe
    RSI + MACD pivot mini-charts (slope from the last OB/OS pivot to the current value)."""
    scores = s.get("scores", {}) or {}
    pos = s.get("position")
    variant = (s.get("variant") or "netting").lower()
    ed = (s.get("entry_direction") or "WAIT").upper()
    ed_col = C["green"] if ed == "BUY" else (C["red"] if ed == "SELL" else C["grey"])
    chop = s.get("entry_chop_block")
    cooldown = not s.get("entry_allowed", True)
    min_hold = int(s.get("min_hold_left", 0) or 0)
    header = html.Div([
        html.Span("ENTRY", style={"color": C["dim"], "fontSize": "11px", "fontWeight": "700",
                  "marginRight": "6px", "letterSpacing": "0.4px"}),
        _chip(ed, ed_col),
        html.Span(_chip("CHOP", C["amber"]) if chop else "", style={"marginLeft": "6px"}),
        html.Span(_chip("COOLDOWN", C["purple"]) if cooldown else "", style={"marginLeft": "6px"}),
        html.Span(_chip(f"MIN-HOLD {min_hold}s", C["teal"]) if min_hold > 0 else "",
                  style={"marginLeft": "6px"}),
    ], style={"display": "flex", "alignItems": "center", "flexWrap": "wrap", "margin": "2px 0 6px"})

    fc_axis = _score_axis(FORCE_CLOSE_SCORE_WEIGHTS)
    meters = []
    if pos:
        meters.append(_profit_meter(pos.get("cycle_pl"), pos.get("target")))
        if variant == "netting" and scores.get("book") is not None:
            meters.append(_meter("BOOK (CE/PE M5)", f"both reverse ±{BOOK_S:g}",
                          scores.get("book"), BOOK_S, _score_axis(BOOK_SCORE_WEIGHTS),
                          hi=("book SHORT", C["purple"]), lo=("book LONG", C["purple"]),
                          within=("hold", C["grey"])))
        meters.append(_meter("FORCE-CLOSE (CE/PE M15)", f"either ±{FORCE_CLOSE_S:g}", scores.get("fc"),
                      FORCE_CLOSE_S, fc_axis, hi=("close SHORT", C["amber"]),
                      lo=("close LONG", C["amber"]), within=("hold", C["grey"])))
    else:
        meters.append(_meter("OPEN SCORE (index)", f"opens ±{ENTRY_S:g}", scores.get("open"),
                      ENTRY_S, _score_axis(OPEN_SCORE_WEIGHTS), hi=("BUY opens", C["green"]),
                      lo=("SELL opens", C["red"]), within=("WAIT", C["grey"])))
        meters.append(_meter("FORCE-CLOSE (index gate)", "must agree", scores.get("fc"),
                      FORCE_CLOSE_S, fc_axis, hi=("favours BUY", C["green"]),
                      lo=("favours SELL", C["red"]), within=("neutral", C["grey"])))
    return html.Div([header] + meters)


def _pivot_grid(rg, mg, title):
    """Full-width grid of per-timeframe RSI + MACD pivot mini-charts (coarsest→finest) for one
    source (index or the active CE/PE leg). Returns empty when there are no graphs."""
    rg = rg or {}
    mg = mg or {}
    rows = []
    for tf in SLOPE_TF_ORDER:
        if tf not in rg and tf not in mg:
            continue
        rows.append(html.Div([
            html.Div(_pivot_mini_graph(tf, rg.get(tf)), style={"flex": "1", "minWidth": "0"}),
            html.Div(_pivot_mini_graph(tf, mg.get(tf)), style={"flex": "1", "minWidth": "0"}),
        ], style={"display": "flex", "gap": "8px", "marginBottom": "2px"}))
    if not rows:
        return html.Div()
    return _card([
        html.Div(title, style={"color": C["dim"], "fontSize": "11px", "margin": "0 0 6px",
                               "letterSpacing": "0.3px", "fontWeight": "700"}),
        html.Div(rows),
    ], pad="10px 12px", extra={"marginTop": "2px"})


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
    variant = (s.get("variant") or "").upper()
    head = html.Div([
        html.Span(symbol, style={"color": C["text"], "fontSize": "18px", "fontWeight": "800"}),
        html.Span(s.get("asset_type", ""), style={"color": C["muted"], "fontSize": "11px",
                  "marginLeft": "8px"}),
        html.Span(_chip(sig, _sig_color(sig)), style={"marginLeft": "12px"}),
        html.Span(_chip(variant, C["teal"]) if variant else "", style={"marginLeft": "8px"}),
        html.Span(f"units {s.get('units','-')}", style={"color": C["dim"], "fontSize": "12px",
                  "marginLeft": "auto"}),
    ], style={"display": "flex", "alignItems": "center", "marginBottom": "10px"})
    left = _card([
        head,
        _pair_block(s.get("pair")),
        html.Div(style={"height": "10px"}),
        _slope_block(s),
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
    top = html.Div([left, right], style={"display": "flex", "gap": "14px", "marginBottom": "10px"})
    grids = [_pivot_grid(s.get("rsi_graphs"), s.get("macd_graphs"),
                         "INDEX pivot slopes (entry)  ·  peak → now, per timeframe")]
    leg_rg, leg_mg = s.get("leg_rsi_graphs"), s.get("leg_macd_graphs")
    if leg_rg or leg_mg:
        side = (s.get("position") or {}).get("side", "")
        grids.append(_pivot_grid(leg_rg, leg_mg,
                     f"LEG {side} pivot slopes (book / force-close)  ·  the option's own chart"))
    return html.Div([top] + grids, style={"marginBottom": "16px"})


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


# ── full API rate-limit panel (bottom of the page) ───────────────────────────
def api_limits_panel():
    """Every API category (DATA / ORDER / USERINFO) with sec / min / day usage vs the plan's
    limit, and a live rate-limit detector: shows ⛔ RATE-LIMITED + the cooldown while the meter is
    in a Fyers -429 back-off, or 'N× hit today' if it has been limited earlier today."""
    usage = _read(API_USAGE_FILE, {}) or {}
    cats = usage.get("categories", {})
    acct = _read(ACCOUNT_STATE_FILE, {}) or {}
    plan = str(acct.get("api_plan", usage.get("plan", "standard"))).upper()
    limits = API_LIMITS_PRIME if plan == "PRIME" else API_LIMITS
    breaches = int(usage.get("minute_breaches", 0) or 0)

    def _cell(label, used, limit):
        p = round(100 * used / limit, 1) if limit else 0
        col = C["red"] if p >= 85 else (C["amber"] if p >= 60 else C["dim"])
        return html.Span(f"{label} {used:,}/{limit:,}", style={
            "color": col, "fontSize": "11.5px", "marginRight": "14px",
            "fontFamily": "monospace", "minWidth": "118px", "display": "inline-block"})

    rows = []
    for cat, lim in limits.items():
        c = cats.get(cat, {})
        sec, mn, day = c.get("sec", {}), c.get("min", {}), c.get("day", {})
        limited = bool(c.get("rate_limited"))
        cooldown = float(c.get("cooldown_s", 0) or 0)
        today = int(c.get("limited_today", 0) or 0)
        dused, dlim = day.get("used", 0), day.get("limit", lim["day"])
        dpct = round(100 * dused / dlim, 1) if dlim else 0
        dcol = C["red"] if dpct >= 85 else (C["amber"] if dpct >= 60 else C["green"])
        status = (_chip(f"⛔ RATE-LIMITED · {cooldown:.0f}s", C["red"]) if limited
                  else (_chip(f"{today}× limited today", C["amber"]) if today
                        else _chip("OK", C["green"])))
        rows.append(html.Div([
            html.Span(cat, style={"fontWeight": "800", "fontSize": "13px", "color": C["text"],
                                  "width": "92px", "display": "inline-block"}),
            _cell("sec", sec.get("used", 0), sec.get("limit", lim["sec"])),
            _cell("min", mn.get("used", 0), mn.get("limit", lim["min"])),
            _cell("day", dused, dlim),
            html.Div(style={"display": "inline-block", "width": "130px", "height": "6px",
                            "verticalAlign": "middle", "background": C["border"],
                            "borderRadius": "3px", "overflow": "hidden", "marginRight": "12px"},
                     children=html.Div(style={"height": "100%", "width": f"{min(dpct,100)}%",
                                              "background": dcol})),
            html.Span(f"{dpct}%", style={"color": dcol, "fontSize": "11px", "fontWeight": "700",
                                         "marginRight": "10px"}),
            html.Span(status, style={"marginLeft": "auto"}),
        ], style={"display": "flex", "alignItems": "center", "padding": "7px 4px",
                  "borderBottom": f"1px solid {C['border']}"}))

    title = html.Div([
        html.Span("API RATE LIMITS", style={"color": C["dim"], "fontSize": "12px",
                  "letterSpacing": "0.6px", "fontWeight": "700"}),
        html.Span(_chip(f"{plan} PLAN", C["amber"] if plan == "PRIME" else C["grey"]),
                  style={"marginLeft": "10px"}),
        html.Span(_chip(f"⚠ {breaches} MIN-BREACHES", C["red"]) if breaches > 3 else "",
                  style={"marginLeft": "8px"}),
        html.Span("used / limit per sec·min·day — amber ≥60%, red ≥85%, ⛔ = Fyers -429 cooldown",
                  style={"color": C["muted"], "fontSize": "10px", "marginLeft": "auto"}),
    ], style={"display": "flex", "alignItems": "center", "marginBottom": "8px", "flexWrap": "wrap"})
    return _card([title] + rows)


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
    html.Div(id="api_limits", style={"marginTop": "16px"}),
], style={"maxWidth": "1400px", "margin": "0 auto", "padding": "26px",
          "fontFamily": "Inter, system-ui, sans-serif", "background": C["bg"], "minHeight": "100vh"})


@app.callback(
    Output("status", "children"), Output("kpis", "children"), Output("api", "children"),
    Output("symbols", "children"), Output("log", "children"), Output("api_limits", "children"),
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
    return status_strip(), kpi_row(), api_panel(), panels, log_table(), api_limits_panel()


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8050
    print(f"dashboard → http://127.0.0.1:{port}")
    app.run(debug=False, host="127.0.0.1", port=port)
