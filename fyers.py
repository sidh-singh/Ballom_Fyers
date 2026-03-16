"""
fyers.py — Fyers API wrapper for the dev_trading branch.

Provides:
  - Token loading (read-only from dev_scanner's fyers_token.json)
  - Token invalidation (flags expired so scanner re-authenticates)
  - Order execution (buy / sell / position / funds)
  - Historical data fetch (market-hours aware, holiday-aware)
  - Holiday calendar management

This branch NEVER performs TOTP login.  If the token is invalid or
expired, it flags ``expired: true`` in fyers_token.json so that
dev_scanner can re-authenticate.
"""

from fyers_apiv3 import fyersModel
from datetime import datetime, date, timedelta, time as dt_time
from time import sleep
from typing import Tuple
from dataclasses import asdict, replace
from pathlib import Path
import os, json, requests, warnings

import numpy as np
import pandas as pd

from constants import (
    SYMBOLS_COLS, PlaceOrder, Transaction, CloseBySymbol, CloseBySection,
    POSITION_COL, TRADE_COLS, ORDER_COLS, OverallPosition,
    TOKEN_FILE, TOKEN_DIR, CACHE_DIR,
)

warnings.filterwarnings("ignore")

# Public Fyers symbol CSVs
NSE_FO_URL  = "https://public.fyers.in/sym_details/NSE_FO.csv"


class Fyers:
    """Thin wrapper around FyersModel for auth, orders, positions, and data."""

    # ── credentials ────────────────────────────────────────────────────────────
    APP_ID      = "OUDS3XQTRU"
    APP_TYPE    = "100"
    CLIENT_ID   = f"{APP_ID}-{APP_TYPE}"

    # ── auth retry config ───────────────────────────────────────────────────
    AUTH_VERIFY_RETRIES  = 3     # token verification attempts (network flakes)

    # Market hours by asset type
    MARKET_HOURS = {
        "INDEX":     (dt_time(9, 15), dt_time(15, 30)),
    }

    def __init__(self) -> None:
        self._api: fyersModel.FyersModel | None = None
        self._token_date: date | None = None

        self._buy_tpl = PlaceOrder(
            symbol="", qty=0, type=2, side=Transaction.BUY.value,
            productType="MARGIN", limitPrice=0, stopPrice=0, validity="DAY",
            disclosedQty=0, stopLoss=0, takeProfit=0, offlineOrder=False, orderTag="",
        )
        self._sell_tpl = PlaceOrder(
            symbol="", qty=0, type=2, side=Transaction.SELL.value,
            productType="MARGIN", limitPrice=0, stopPrice=0, validity="DAY",
            disclosedQty=0, stopLoss=0, takeProfit=0, offlineOrder=False, orderTag="",
        )
        self._close_by_symbol = CloseBySymbol(id=[])
        self._close_by_section = CloseBySection(segment=[], side=[], productType=[])

    # ╔══════════════════════════════════════════════════════════════════════════╗
    # ║  AUTH  (read-only — token loaded from dev_scanner's file)                ║
    # ╚══════════════════════════════════════════════════════════════════════════╝

    def load_token(self) -> bool:
        """
        Load the Fyers token from C: drive (written by dev_scanner).
        Returns True if token loaded and verified successfully.
        """
        for attempt in range(self.AUTH_VERIFY_RETRIES):
            try:
                if not TOKEN_FILE.exists():
                    sleep(2)
                    continue

                with open(TOKEN_FILE, "r") as f:
                    data = json.load(f)

                # Check expired flag
                if data.get("expired", False):
                    return False

                token = data.get("access_token", "")
                if not token:
                    sleep(2)
                    continue

                self._api = self._build_model(token)
                self._token_date = date.today()

                if self._verify_token(token):
                    return True
                else:
                    sleep(2)
                    continue

            except Exception:
                sleep(2)
                continue

        return False

    def invalidate(self) -> None:
        """
        Flag the token as expired in fyers_token.json so that
        dev_scanner can detect it and re-authenticate.
        """
        self._flag_token_expired()
        self._api = None
        self._token_date = None

    def verify_session(self) -> bool:
        """Verify the current session is valid. Auto-reloads from file if invalid."""
        if self._api is None:
            return self.load_token()
        try:
            resp = self._api.get_profile()
            if resp.get("s") == "ok":
                return True
        except Exception:
            pass
        # Token invalid — try reloading from file
        return self.load_token()

    @staticmethod
    def _is_auth_error(resp: dict | None) -> bool:
        """True if a Fyers API response indicates an authentication failure."""
        if not resp or not isinstance(resp, dict):
            return False
        msg = str(resp.get("message", "")).lower()
        return (
            "could not authenticate" in msg
            or "invalid token" in msg
            or "token is expired" in msg
        )

    @property
    def api(self) -> fyersModel.FyersModel:
        """Return the FyersModel (may be None if not loaded)."""
        if self._api is None:
            self.load_token()
        return self._api

    # ── token helpers ──────────────────────────────────────────────────────────

    def _verify_token(self, token: str) -> bool:
        """Verify token by calling the profile API."""
        try:
            m = self._build_model(token)
            return m.get_profile().get("s") == "ok"
        except Exception:
            return False

    def _build_model(self, token: str) -> fyersModel.FyersModel:
        return fyersModel.FyersModel(
            client_id=self.CLIENT_ID,
            is_async=False,
            token=token,
            log_path=os.getcwd(),
        )

    @staticmethod
    def _flag_token_expired() -> None:
        """Atomically set expired=true in fyers_token.json."""
        try:
            if TOKEN_FILE.exists():
                with open(TOKEN_FILE, "r") as f:
                    data = json.load(f)
            else:
                data = {}
            data["expired"] = True
            TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
            TOKEN_FILE.write_text(json.dumps(data, indent=2))
        except Exception:
            pass

    def safe_api_call(self, api_method, *args, **kwargs):
        """
        Call a Fyers API method; on auth error, reload token and retry once.
        If reload fails, flag token expired for dev_scanner to re-auth.
        """
        resp = api_method(*args, **kwargs)
        if self._is_auth_error(resp):
            if self.load_token():
                new_method = getattr(self.api, api_method.__name__)
                resp = new_method(*args, **kwargs)
            else:
                self.invalidate()
        return resp

    # ╔══════════════════════════════════════════════════════════════════════════╗
    # ║  TRADING                                                                 ║
    # ╚══════════════════════════════════════════════════════════════════════════╝

    def buy(self, symbol: str, qty: int, product_type: str = "MARGIN"):
        payload = asdict(replace(
            self._buy_tpl, symbol=symbol, qty=qty,
            productType=product_type, orderTag=symbol.split(":")[1] if ":" in symbol else symbol,
        ))
        return self.api.place_order(data=payload)

    def sell(self, symbol: str, qty: int, product_type: str = "MARGIN"):
        payload = asdict(replace(
            self._sell_tpl, symbol=symbol, qty=int(qty),
            productType=product_type, orderTag=symbol.split(":")[1] if ":" in symbol else symbol,
        ))
        return self.api.place_order(data=payload)

    def position(self) -> Tuple[pd.DataFrame, OverallPosition]:
        resp = self.api.positions()
        try:
            rows = resp["netPositions"]
            overall = OverallPosition(**resp["overall"])
        except KeyError:
            rows, overall = [], OverallPosition(0, 0, 0.0, 0.0, 0.0)
        return pd.DataFrame(rows, columns=POSITION_COL), overall

    def tradebook(self) -> pd.DataFrame:
        resp = self.api.tradebook()
        return pd.DataFrame(resp.get("tradeBook", []), columns=TRADE_COLS)

    def orderbook(self) -> pd.DataFrame:
        resp = self.api.orderbook()
        return pd.DataFrame(resp.get("orderBook", []), columns=ORDER_COLS)

    def funds(self) -> dict:
        return self.api.funds()

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
            df = pd.read_csv(NSE_FO_URL, header=None)
            df.columns = SYMBOLS_COLS
            df = df.drop_duplicates()
            df.to_csv(cache_file, index=False)
            for f in CACHE_DIR.glob("fyers_options_*.csv"):
                if today not in f.name:
                    f.unlink(missing_ok=True)
        return df

    @staticmethod
    def get_lot_size(ce_symbol: str, symbol_df: pd.DataFrame) -> int:
        """Look up 'Minimum lot size' for *ce_symbol* in the downloaded CSV DataFrame."""
        row = symbol_df[symbol_df["Symbol ticker"] == ce_symbol]
        if row.empty:
            raise ValueError(f"Lot size not found for {ce_symbol}")
        return int(row["Minimum lot size"].iloc[0])

    @staticmethod
    def has_index_positions(position_df: pd.DataFrame) -> bool:
        """True if any open NSE/NFO position with non-zero qty exists."""
        if position_df.empty:
            return False
        for _, row in position_df.iterrows():
            sym = str(row.get("symbol", "")).upper()
            if row.get("qty", 0) != 0 and ("NSE" in sym or "NFO" in sym) and "MCX" not in sym:
                return True
        return False

    # ╔══════════════════════════════════════════════════════════════════════════╗
    # ║  TRADING HOLIDAYS                                                        ║
    # ╚══════════════════════════════════════════════════════════════════════════╝

    @staticmethod
    def fetch_trading_holidays(year: int | None = None) -> dict:
        """Fetch Indian trading holidays for *year* from NSE and cache to JSON."""
        if year is None:
            year = date.today().year

        cache_file = CACHE_DIR / f"trading_holidays_{year}.json"

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

        if not holidays_data["holidays"]:
            holidays_data["holidays"] = Fyers._known_fixed_holidays(year)

        holidays_data["special_sessions"] = Fyers._known_special_sessions(year)

        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        with open(cache_file, "w") as f:
            json.dump(holidays_data, f, indent=2)
        return holidays_data

    @staticmethod
    def _known_fixed_holidays(year: int) -> list[str]:
        """Best-effort list of Indian market holidays."""
        fixed = [
            f"{year}-01-26", f"{year}-05-01", f"{year}-08-15",
            f"{year}-10-02", f"{year}-12-25",
        ]
        approx = [
            f"{year}-03-14", f"{year}-03-31", f"{year}-04-06",
            f"{year}-04-10", f"{year}-04-14", f"{year}-04-18",
            f"{year}-05-12", f"{year}-06-07", f"{year}-07-06",
            f"{year}-08-16", f"{year}-09-05", f"{year}-10-21",
            f"{year}-10-22", f"{year}-11-05",
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
        """Return known special trading sessions."""
        sessions = []
        feb1 = date(year, 2, 1)
        if feb1.weekday() == 5:
            sessions.append({
                "date": feb1.isoformat(),
                "name": "Budget Day (Special Saturday)",
                "open": "09:15", "close": "15:30",
            })
        muhurat_date = date(year, 10, 21)
        sessions.append({
            "date": muhurat_date.isoformat(),
            "name": "Diwali Muhurat Trading",
            "open": "18:15", "close": "19:30",
        })
        return sessions

    @staticmethod
    def load_holiday_set(year: int | None = None) -> tuple[set[str], list[dict]]:
        """Return (holiday_dates_set, special_sessions_list)."""
        data = Fyers.fetch_trading_holidays(year)
        return set(data.get("holidays", [])), data.get("special_sessions", [])

    # ╔══════════════════════════════════════════════════════════════════════════╗
    # ║  HISTORICAL DATA FETCH — with market-hours & holiday awareness           ║
    # ╚══════════════════════════════════════════════════════════════════════════╝

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
        Market-hours aware, walks backwards skipping weekends and holidays.
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
            market_type.upper(), self.MARKET_HOURS["INDEX"]
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

        sym_name = symbol.split(":")[-1] if ":" in symbol else symbol
        is_option = sym_name.upper().endswith("CE") or sym_name.upper().endswith("PE")

        payload = {
            "symbol": symbol,
            "resolution": resolution,
            "date_format": "0",
            "range_from": str(int(start_dt.timestamp())),
            "range_to": str(int(end_dt.timestamp())),
            "cont_flag": "0" if is_option else "1",
        }

        resp = self.safe_api_call(self.api.history, data=payload)
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
        df = df.sort_values("Timestamp").drop_duplicates(
            subset="Timestamp", keep="last"
        ).reset_index(drop=True)

        return df
