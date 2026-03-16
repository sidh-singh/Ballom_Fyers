"""
fyers_api.py — Fyers market-data and option-pair scanning API wrapper.

Depends on fyers_auth.py for authentication.  This module provides:
  • NSE F&O symbol CSV download (cached daily)
  • Option chain scanning → best CE/PE pair selection
  • Historical candle data fetch (market-hours & holiday aware)
  • Trading holiday fetch & cache

All API calls go through FyersAuth.safe_api_call() so auth failures
trigger automatic re-authentication transparently.
"""

from __future__ import annotations

import json
import logging
import os
import time as _time
import warnings
from datetime import date, datetime, timedelta, time as dt_time
from pathlib import Path
from typing import Tuple

import numpy as np
import pandas as pd
import requests

from fyers_auth import FyersAuth
from constants import CACHE_DIR, SYMBOLS_COLS, MIN_CANDLES_FOR_ANALYSIS

warnings.filterwarnings("ignore")

logger = logging.getLogger("fyers_api")

# ── Public Fyers symbol CSV URL ────────────────────────────────────────────────
NSE_FO_URL = "https://public.fyers.in/sym_details/NSE_FO.csv"


class FyersAPI:
    """
    Wrapper around Fyers market-data APIs.

    Uses FyersAuth for authentication — all API calls auto-retry on
    auth failures via safe_api_call().

    Usage
    ─────
    auth = FyersAuth()
    api = FyersAPI(auth)
    option_df = FyersAPI.download_option_data()
    pair = api.fetch_option_pair("NSE:NIFTY50-INDEX", asset_type="INDEX")
    """

    # Market hours by asset type
    MARKET_HOURS = {
        "INDEX":     (dt_time(9, 15), dt_time(15, 30)),
        "COMMODITY": (dt_time(9, 0),  dt_time(23, 30)),
    }

    # Timeframe → (resolution, timedelta, category)
    _TF_MAP = {
        "5S":  ("5S",  timedelta(seconds=5),  "intraday"),
        "10S": ("10S", timedelta(seconds=10), "intraday"),
        "15S": ("15S", timedelta(seconds=15), "intraday"),
        "30S": ("30S", timedelta(seconds=30), "intraday"),
        "45S": ("45S", timedelta(seconds=45), "intraday"),
        "1":   ("1",   timedelta(minutes=1),  "intraday"),
        "2":   ("2",   timedelta(minutes=2),  "intraday"),
        "3":   ("3",   timedelta(minutes=3),  "intraday"),
        "5":   ("5",   timedelta(minutes=5),  "intraday"),
        "10":  ("10",  timedelta(minutes=10), "intraday"),
        "15":  ("15",  timedelta(minutes=15), "intraday"),
        "20":  ("20",  timedelta(minutes=20), "intraday"),
        "30":  ("30",  timedelta(minutes=30), "intraday"),
        "60":  ("60",  timedelta(hours=1),    "intraday"),
        "120": ("120", timedelta(hours=2),     "intraday"),
        "240": ("240", timedelta(hours=4),     "intraday"),
        "D":   ("D",   timedelta(days=1),      "daily"),
    }

    def __init__(self, auth: FyersAuth) -> None:
        self._auth = auth

    @property
    def model(self):
        """Return the current authenticated FyersModel."""
        return self._auth.get_model()

    def safe_api_call(self, api_method, *args, **kwargs):
        """Delegate to FyersAuth.safe_api_call (auto re-auth on failure)."""
        return self._auth.safe_api_call(api_method, *args, **kwargs)

    # ╔══════════════════════════════════════════════════════════════════════════╗
    # ║  MARKET-DATA DOWNLOADS  (cached daily on C: drive)                       ║
    # ╚══════════════════════════════════════════════════════════════════════════╝

    @staticmethod
    def download_option_data() -> pd.DataFrame:
        """Download NSE F&O symbol CSV once per day; cache to C:/Ballom_FYR/cache."""
        today = datetime.now().strftime("%Y%m%d")
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache_file = CACHE_DIR / f"fyers_options_{today}.csv"

        if cache_file.exists():
            df = pd.read_csv(cache_file)
        else:
            logger.info("Downloading NSE F&O symbols CSV...")
            df = pd.read_csv(NSE_FO_URL, header=None)
            df.columns = SYMBOLS_COLS
            df = df.drop_duplicates()
            df.to_csv(cache_file, index=False)
            # cleanup stale files
            for f in CACHE_DIR.glob("fyers_options_*.csv"):
                if today not in f.name:
                    f.unlink(missing_ok=True)
            logger.info("NSE F&O CSV cached: %s (%d rows)", cache_file.name, len(df))
        return df

    # ╔══════════════════════════════════════════════════════════════════════════╗
    # ║  LOT SIZE LOOKUP                                                         ║
    # ╚══════════════════════════════════════════════════════════════════════════╝

    @staticmethod
    def get_lot_size(ce_symbol: str, symbol_df: pd.DataFrame) -> int:
        """Look up 'Minimum lot size' for *ce_symbol* in the downloaded CSV."""
        row = symbol_df[symbol_df["Symbol ticker"] == ce_symbol]
        if row.empty:
            raise ValueError(f"Lot size not found for {ce_symbol}")
        return int(row["Minimum lot size"].iloc[0])

    # ╔══════════════════════════════════════════════════════════════════════════╗
    # ║  OPTION-PAIR SCANNER                                                     ║
    # ╚══════════════════════════════════════════════════════════════════════════╝

    def _count_available_candles(
        self,
        symbol: str,
        market_type: str = "INDEX",
    ) -> int:
        """Return how many 1-minute candles Fyers has for *symbol* (0 on error)."""
        try:
            df = self.fetch_historical_data(
                symbol, timeframe="1", candles=500,
                market_type=market_type,
            )
            return len(df)
        except Exception:
            return 0

    def fetch_option_pair(
        self,
        symbol: str,
        asset_type: str = "INDEX",
        expiry_mode: str = "AUTO",
        max_retries: int = 3,
        retry_delay: int = 5,
        min_trend_score: float = 0.65,
        max_expiry_days: int = 365,
        min_days_to_expiry: int = 14,
        min_oi_threshold: int = 10000,
        max_premium_per_lot: int = 55000,
        prefer_itm_otm: str = "SLIGHT_OTM",
    ) -> dict:
        """
        Scan the option chain for *symbol* and return the best CE/PE pair.

        Returns a dict with keys:
            Recommended, CE_Symbol, PE_Symbol, CE_Strike, PE_Strike,
            Expiry, Trend_Score, VIX, Debug, ...
        """
        # ── scoring helpers ────────────────────────────────────────────────────
        def _numeric(df, cols):
            for c in cols:
                df[c] = pd.to_numeric(df.get(c, np.nan), errors="coerce")
            return df

        def _normalize(series):
            s = pd.to_numeric(series.fillna(0), errors="coerce")
            mx = s.max()
            if not mx or np.isnan(mx):
                return pd.Series(0, index=s.index)
            return (s / mx).clip(0, 1)

        def _safe_div(num, denom, default=0):
            with np.errstate(divide="ignore", invalid="ignore"):
                result = np.where(denom != 0, num / denom, default)
            return np.nan_to_num(result, nan=default)

        def _bid_ask_eff(bid, ask):
            spread = ask - bid
            mid = (ask + bid) / 2
            return 1 - np.clip(_safe_div(spread, mid, 1.0), 0, 1)

        def _affordability(ltp, budget):
            if ltp <= 0 or budget <= 0:
                return 0.5
            r = ltp / budget
            if r > 1.0:   return 0.0
            if r < 0.1:   return 0.3
            if r < 0.3:   return 1.0
            if r < 0.6:   return 0.8
            return 0.5

        def _theta_score(days, _iv):
            if days < 7:   return 0.1
            if days < 14:  return 0.3
            if days < 21:  return 0.6
            if days <= 45: return 1.0
            if days <= 60: return 0.85
            return 0.7

        def _moneyness(strike, price, opt_type, pref):
            m = (strike - price) / price if opt_type == "CE" else (price - strike) / price
            if pref == "ATM":
                return float(np.exp(-abs(m) * 50))
            if pref == "SLIGHT_OTM":
                if 0.005 <= m <= 0.02:  return 1.0
                if 0.0   <= m <= 0.04:  return 0.85
                if -0.01  <= m < 0:     return 0.75
                if -0.03  <= m < -0.01: return 0.55
                if 0.04  <  m <= 0.07:  return 0.4
                return 0.2
            # OTM
            if 0.02 <= m <= 0.05: return 1.0
            if 0.01 <= m <= 0.07: return 0.7
            return 0.4

        # ── retry loop ─────────────────────────────────────────────────────────
        _log = []  # debug breadcrumbs

        for attempt in range(1, max_retries + 1):
            try:
                # ─── 1. Get underlying price ──────────────────────────────
                quote = self.safe_api_call(
                    self.model.quotes, data={"symbols": symbol},
                )
                if not isinstance(quote, dict) or quote.get("s") != "ok" or "d" not in quote:
                    err_msg = (
                        quote.get("message", quote.get("s", "unknown"))
                        if isinstance(quote, dict) else str(quote)[:100]
                    )
                    raise ValueError(f"Quotes API error for {symbol}: {err_msg}")
                current_price = quote["d"][0]["v"].get("lp")
                if not current_price:
                    raise ValueError(f"Underlying price unavailable for {symbol}")
                _log.append(f"price={current_price}")

                # ─── 2. Base option chain (expiry list + VIX) ─────────────
                base_chain = self.safe_api_call(
                    self.model.optionchain,
                    data={"symbol": symbol, "strikecount": 20},
                )
                if not isinstance(base_chain, dict) or base_chain.get("s") != "ok":
                    err_msg = (
                        base_chain.get("message", base_chain.get("s", "unknown"))
                        if isinstance(base_chain, dict) else str(base_chain)[:100]
                    )
                    raise ValueError(f"OptionChain API error for {symbol}: {err_msg}")
                data = base_chain.get("data", {})
                vix = data.get("indiavixData", {}).get("ltp", 20)
                _log.append(f"VIX={vix}")

                expiry_map = {
                    e["date"]: e["expiry"] for e in data.get("expiryData", [])
                }
                parsed = sorted(
                    [
                        (datetime.strptime(k, "%d-%m-%Y"), v)
                        for k, v in expiry_map.items()
                    ],
                    key=lambda x: x[0],
                )
                cutoff = datetime.now() + timedelta(days=max_expiry_days)
                parsed = [(d, e) for d, e in parsed if d <= cutoff]
                _log.append(f"expiries_total={len(parsed)}")

                if expiry_mode == "NEAR_MONTH":
                    expiry_list = parsed[:1]
                elif expiry_mode == "NEXT_MONTH":
                    expiry_list = parsed[1:2]
                else:
                    expiry_list = [
                        (d, e) for d, e in parsed
                        if min_days_to_expiry <= (d - datetime.now()).days <= 60
                    ] or parsed
                _log.append(f"expiries_filtered={len(expiry_list)}")

                ce_candidates = []
                pe_candidates = []

                # ─── 3. Loop through each expiry ─────────────────────────
                for exp_date, exp_epoch in expiry_list:
                    dte = max((exp_date - datetime.now()).days, 0)
                    if dte < min_days_to_expiry:
                        _log.append(
                            f"skip_exp={exp_date.strftime('%d%b')} "
                            f"dte={dte}<{min_days_to_expiry}"
                        )
                        continue

                    oc = self.safe_api_call(
                        self.model.optionchain,
                        data={
                            "symbol": symbol,
                            "strikecount": 20,
                            "timestamp": str(exp_epoch),
                        },
                    )
                    chain = oc.get("data", {}).get("optionsChain", [])
                    df = pd.DataFrame(chain)
                    if df.empty:
                        _log.append(f"exp={exp_date.strftime('%d%b')} chain=EMPTY")
                        continue

                    df = df[df["option_type"].isin(["CE", "PE"])]
                    if df.empty:
                        _log.append(f"exp={exp_date.strftime('%d%b')} CE+PE=0")
                        continue

                    df = _numeric(df, [
                        "strike_price", "oi", "prev_oi", "volume",
                        "ask", "bid", "ltp", "iv", "chng", "chng_oi",
                    ])
                    rows_before = len(df)
                    _ltp_floor = 5
                    _vol_floor = 100
                    df_strict = df[
                        (df["ltp"] <= max_premium_per_lot) & (df["ltp"] > _ltp_floor)
                    ]
                    df_strict = df_strict[
                        (df_strict["oi"] >= min_oi_threshold)
                        | (df_strict["volume"] > _vol_floor)
                    ]
                    if not df_strict.empty:
                        df = df_strict
                        _log.append(
                            f"exp={exp_date.strftime('%d%b')} "
                            f"dte={dte} rows={len(df)}"
                        )
                    else:
                        df = df[df["ltp"] > 0]
                        if df.empty:
                            _log.append(
                                f"exp={exp_date.strftime('%d%b')} "
                                f"rows={rows_before}->0(all_filtered)"
                            )
                            continue
                        _log.append(
                            f"exp={exp_date.strftime('%d%b')} "
                            f"dte={dte} rows={len(df)}(relaxed)"
                        )

                    # ─── 4. Score CE and PE separately ────────────────────
                    for otype in ["CE", "PE"]:
                        sub = df[df["option_type"] == otype].copy()
                        if sub.empty:
                            _log.append(f"  {otype}=0rows")
                            continue

                        oi_change = (sub["oi"] - sub["prev_oi"]).fillna(0)
                        if "chng_oi" in sub.columns:
                            chng_oi = sub["chng_oi"].fillna(0)
                            oi_change = np.maximum(oi_change, chng_oi)

                        price_momentum = pd.Series(0.5, index=sub.index)
                        if "chng" in sub.columns:
                            chng = sub["chng"].fillna(0)
                            price_momentum = np.where(
                                chng > 0,
                                np.clip(
                                    0.5 + chng / (sub["ltp"] * 0.1 + 1e-9),
                                    0.5, 1.0,
                                ),
                                np.clip(
                                    0.5 + chng / (sub["ltp"] * 0.1 + 1e-9),
                                    0.1, 0.5,
                                ),
                            )
                            price_momentum = pd.Series(
                                price_momentum, index=sub.index,
                            )

                        sub["score"] = (
                            sub["strike_price"].apply(
                                lambda x: _moneyness(
                                    x, current_price, otype, prefer_itm_otm,
                                )
                            ) * 0.25
                            + _bid_ask_eff(
                                sub["bid"].fillna(0), sub["ask"].fillna(0),
                            ) * 0.15
                            + sub["ltp"].apply(
                                lambda x: _affordability(x, max_premium_per_lot),
                            ) * 0.15
                            + sub["iv"].apply(
                                lambda iv: _theta_score(dte, iv),
                            ) * 0.10
                            + _normalize(oi_change.clip(lower=0)) * 0.15
                            + price_momentum * 0.10
                            + _normalize(np.log1p(sub["volume"])) * 0.10
                        ).clip(0, 1)

                        top_n = sub.nlargest(3, "score")
                        best_idx = top_n.index[0]
                        _log.append(
                            f"  {otype}: best={sub.loc[best_idx, 'score']:.3f} "
                            f"sym={sub.loc[best_idx, 'symbol']} "
                            f"strike={sub.loc[best_idx, 'strike_price']}"
                        )
                        for ri in top_n.index:
                            cand = (
                                float(sub.loc[ri, "score"]),
                                sub.loc[ri].to_dict(),
                                (exp_date, dte, vix),
                            )
                            if otype == "CE":
                                ce_candidates.append(cand)
                            else:
                                pe_candidates.append(cand)

                # ─── 5. Final decision — validate candle data ─────────────
                ce_candidates.sort(key=lambda x: x[0], reverse=True)
                pe_candidates.sort(key=lambda x: x[0], reverse=True)

                if not ce_candidates or not pe_candidates:
                    missing = "CE" if not ce_candidates else "PE"
                    msg = f"No {missing} candidates found across all expiries"
                    return {
                        "Recommended": False,
                        "Symbol": symbol,
                        "Message": msg,
                        "Debug": " | ".join(_log),
                    }

                # Pick the first candidate per side with enough candle
                # history for downstream SHA + RSI processing.
                selected_ce = None
                for sc, row, exp in ce_candidates[:5]:
                    count = self._count_available_candles(
                        row["symbol"], market_type=asset_type,
                    )
                    _log.append(
                        f"CE_candle_check: {row['symbol']} candles={count}"
                    )
                    if count >= MIN_CANDLES_FOR_ANALYSIS:
                        selected_ce = (sc, row, exp)
                        break
                if selected_ce is None:
                    selected_ce = ce_candidates[0]
                    _log.append("CE_fallback: using best-scored (low candle data)")

                selected_pe = None
                for sc, row, exp in pe_candidates[:5]:
                    count = self._count_available_candles(
                        row["symbol"], market_type=asset_type,
                    )
                    _log.append(
                        f"PE_candle_check: {row['symbol']} candles={count}"
                    )
                    if count >= MIN_CANDLES_FOR_ANALYSIS:
                        selected_pe = (sc, row, exp)
                        break
                if selected_pe is None:
                    selected_pe = pe_candidates[0]
                    _log.append("PE_fallback: using best-scored (low candle data)")

                ce_sc, ce_row, ce_exp = selected_ce
                pe_sc, pe_row, pe_exp = selected_pe
                combined = (ce_sc + pe_sc) / 2
                _log.append(f"combined={combined:.3f} threshold={min_trend_score}")

                exp_date, dte, vix = ce_exp
                _log.append(
                    f"SELECTED CE={ce_row['symbol']} PE={pe_row['symbol']}"
                )
                return {
                    "Recommended": True,
                    "CE_Symbol": ce_row["symbol"],
                    "PE_Symbol": pe_row["symbol"],
                    "CE_Strike": float(ce_row["strike_price"]),
                    "PE_Strike": float(pe_row["strike_price"]),
                    "CE_Premium": float(ce_row["ltp"]),
                    "PE_Premium": float(pe_row["ltp"]),
                    "Expiry": exp_date.strftime("%Y-%m-%d"),
                    "Days_To_Expiry": dte,
                    "Trend_Score": float(combined),
                    "VIX": float(vix),
                    "Debug": " | ".join(_log),
                }

            except Exception as e:
                _log.append(f"attempt{attempt}_err: {e}")
                if attempt == max_retries:
                    return {
                        "Recommended": False,
                        "Symbol": symbol,
                        "Message": f"Failed after {max_retries} attempts: {e}",
                        "Debug": " | ".join(_log),
                    }
                _time.sleep(retry_delay)

    # ╔══════════════════════════════════════════════════════════════════════════╗
    # ║  TRADING HOLIDAYS — fetched annually, cached on C: drive                 ║
    # ╚══════════════════════════════════════════════════════════════════════════╝

    @staticmethod
    def fetch_trading_holidays(year: int | None = None) -> dict:
        """
        Fetch Indian trading holidays for *year* from NSE and cache to JSON.

        Returns dict with keys: year, fetched_on, holidays, special_sessions.
        """
        if year is None:
            year = date.today().year

        cache_file = CACHE_DIR / f"trading_holidays_{year}.json"

        # return from cache if already fetched
        if cache_file.exists():
            try:
                with open(cache_file, "r") as f:
                    cached = json.load(f)
                if cached.get("year") == year:
                    return cached
            except Exception:
                pass

        holidays_data: dict = {
            "year": year,
            "fetched_on": date.today().isoformat(),
            "holidays": [],
            "special_sessions": [],
        }

        # attempt NSE holiday-master API
        try:
            ses = requests.Session()
            ses.headers.update({
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                "Accept": "application/json",
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": "https://www.nseindia.com/",
            })
            ses.get("https://www.nseindia.com", timeout=10)

            resp = ses.get(
                "https://www.nseindia.com/api/holiday-master?type=trading",
                timeout=15,
            )
            if resp.status_code == 200:
                data = resp.json()
                fo_holidays = data.get("FO", data.get("CM", []))
                holiday_dates: set[str] = set()
                for h in fo_holidays:
                    try:
                        dt_val = datetime.strptime(h["tradingDate"], "%d-%b-%Y")
                        if dt_val.year == year:
                            holiday_dates.add(dt_val.strftime("%Y-%m-%d"))
                    except (KeyError, ValueError):
                        continue
                holidays_data["holidays"] = sorted(holiday_dates)
        except Exception:
            pass

        # fallback: well-known fixed + approximate lunar holidays
        if not holidays_data["holidays"]:
            holidays_data["holidays"] = FyersAPI._known_fixed_holidays(year)

        holidays_data["special_sessions"] = FyersAPI._known_special_sessions(year)

        # persist to cache
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        with open(cache_file, "w") as f:
            json.dump(holidays_data, f, indent=2)
        return holidays_data

    @staticmethod
    def _known_fixed_holidays(year: int) -> list[str]:
        """Best-effort list of Indian market holidays (fixed + approximate lunar)."""
        fixed = [
            f"{year}-01-26",  # Republic Day
            f"{year}-05-01",  # Maharashtra Day
            f"{year}-08-15",  # Independence Day
            f"{year}-10-02",  # Gandhi Jayanti
            f"{year}-12-25",  # Christmas
        ]
        approx = [
            f"{year}-03-14", f"{year}-03-31", f"{year}-04-06",
            f"{year}-04-10", f"{year}-04-14", f"{year}-04-18",
            f"{year}-05-12", f"{year}-06-07", f"{year}-07-06",
            f"{year}-08-16", f"{year}-09-05", f"{year}-10-02",
            f"{year}-10-21", f"{year}-10-22", f"{year}-11-05",
        ]
        combined = sorted(set(fixed + approx))
        valid = []
        for d_str in combined:
            try:
                dt_val = datetime.strptime(d_str, "%Y-%m-%d")
                if dt_val.weekday() < 5:
                    valid.append(d_str)
            except ValueError:
                continue
        return valid

    @staticmethod
    def _known_special_sessions(year: int) -> list[dict]:
        """Return known special trading sessions (Budget day, Diwali Muhurat)."""
        sessions = []
        feb1 = date(year, 2, 1)
        if feb1.weekday() == 5:  # Saturday
            sessions.append({
                "date": feb1.isoformat(),
                "name": "Budget Day (Special Saturday)",
                "open": "09:15", "close": "15:30",
            })
        sessions.append({
            "date": date(year, 10, 21).isoformat(),
            "name": "Diwali Muhurat Trading",
            "open": "18:15", "close": "19:30",
        })
        return sessions

    @staticmethod
    def load_holiday_set(year: int | None = None) -> Tuple[set[str], list[dict]]:
        """Return (holiday_dates_set, special_sessions_list) from cache."""
        data = FyersAPI.fetch_trading_holidays(year)
        return set(data.get("holidays", [])), data.get("special_sessions", [])

    # ╔══════════════════════════════════════════════════════════════════════════╗
    # ║  HISTORICAL DATA FETCH — with market-hours & holiday awareness           ║
    # ╚══════════════════════════════════════════════════════════════════════════╝

    def fetch_historical_data(
        self,
        symbol: str,
        timeframe: str,
        candles: int = 100,
        market_type: str = "INDEX",
        holidays: set[str] | None = None,
        special_sessions: list[dict] | None = None,
    ) -> pd.DataFrame:
        """
        Fetch historical OHLCV candles from Fyers for *symbol*.

        Returns DataFrame: Timestamp, Open, High, Low, Close, Volume
        """
        if timeframe not in self._TF_MAP:
            raise ValueError(f"Unsupported timeframe '{timeframe}'")
        resolution, delta, _ = self._TF_MAP[timeframe]

        if holidays is None:
            holidays = set()
        if special_sessions is None:
            special_sessions = []

        special_map: dict[str, tuple[dt_time, dt_time]] = {}
        for ss in special_sessions:
            try:
                ss_date = ss["date"]
                ss_open = dt_time(*map(int, ss["open"].split(":")))
                ss_close = dt_time(*map(int, ss["close"].split(":")))
                special_map[ss_date] = (ss_open, ss_close)
            except (KeyError, ValueError):
                continue

        mkt_open, mkt_close = self.MARKET_HOURS.get(
            market_type.upper(), self.MARKET_HOURS["INDEX"],
        )

        def _is_trading_day(d: date) -> bool:
            d_str = d.isoformat()
            if d_str in special_map:
                return True
            if d.weekday() >= 5:
                return False
            if d_str in holidays:
                return False
            return True

        def _market_times(d: date) -> tuple[dt_time, dt_time]:
            d_str = d.isoformat()
            if d_str in special_map:
                return special_map[d_str]
            return mkt_open, mkt_close

        def _prev_trading_day(d: date) -> date:
            d = d - timedelta(days=1)
            while not _is_trading_day(d):
                d = d - timedelta(days=1)
            return d

        # determine end_dt
        now = datetime.now()
        today_d = now.date()
        current_time = now.time()
        today_open, today_close = _market_times(today_d)

        if not _is_trading_day(today_d) or current_time < today_open:
            ltd = _prev_trading_day(today_d)
            _, ltd_close = _market_times(ltd)
            end_dt = datetime.combine(ltd, ltd_close)
        elif current_time > today_close:
            end_dt = datetime.combine(today_d, today_close)
        else:
            end_dt = now

        # walk backwards to compute start_dt
        start_dt = end_dt
        candles_remaining = candles
        while candles_remaining > 0:
            start_dt = start_dt - delta
            day_open, _ = _market_times(start_dt.date())
            if start_dt.time() < day_open:
                prev_d = _prev_trading_day(start_dt.date())
                _, prev_close = _market_times(prev_d)
                start_dt = datetime.combine(prev_d, prev_close)
            candles_remaining -= 1

        # call Fyers history API
        sym_name = symbol.split(":")[-1] if ":" in symbol else symbol
        is_option = (
            sym_name.upper().endswith("CE") or sym_name.upper().endswith("PE")
        )

        payload = {
            "symbol": symbol,
            "resolution": resolution,
            "date_format": "0",
            "range_from": str(int(start_dt.timestamp())),
            "range_to": str(int(end_dt.timestamp())),
            "cont_flag": "0" if is_option else "1",
        }

        resp = self.safe_api_call(self.model.history, data=payload)

        response_status = resp.get("s") if resp else None
        if not resp or response_status not in ("ok", "no_data"):
            msg = resp.get("message", "Unknown error") if resp else "No response"
            raise ValueError(f"Fyers API error for {symbol}: {msg}")

        if response_status == "no_data":
            raise ValueError(f"No historical data for {symbol}")

        candles_data = resp.get("candles", [])
        if not candles_data:
            raise ValueError(f"No candle data returned for {symbol}")

        df = pd.DataFrame(
            candles_data,
            columns=["Timestamp", "Open", "High", "Low", "Close", "Volume"],
        )
        df["Timestamp"] = pd.to_datetime(df["Timestamp"], unit="s")

        # sort ascending + deduplicate for data quality
        df = (
            df.sort_values("Timestamp")
            .drop_duplicates(subset="Timestamp", keep="last")
            .reset_index(drop=True)
        )
        return df
