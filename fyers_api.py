"""
fyers_api.py — Read-only Fyers API wrapper for the dev_updater_nifty branch.

This module handles:
  • Token loading from scanner's shared file (C:/Ballom_FYR/fyers_token.json)
  • Token verification (profile API check)
  • Flagging expired tokens so dev_scanner can re-authenticate
  • Historical OHLCV data fetching (market-hours & holiday aware)
  • Trading holiday calendar management

IMPORTANT: This branch NEVER performs TOTP login.  Only dev_scanner
writes the token.  If the token is invalid, we flag it as expired
so the scanner picks it up and re-authenticates.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
from datetime import date, datetime, timedelta, time as dt_time
from pathlib import Path
from time import sleep
from typing import Tuple

import pandas as pd
import requests
from fyers_apiv3 import fyersModel

from constants import TOKEN_FILE, TOKEN_DIR, CACHE_DIR

logger = logging.getLogger("fyers_api")


# ═══════════════════════════════════════════════════════════════════════════════
#  FYERS API WRAPPER  (read-only auth + historical data + holidays)
# ═══════════════════════════════════════════════════════════════════════════════

class FyersAPI:
    """
    Read-only Fyers API client for updater branches.

    Uses the token written by dev_scanner at C:/Ballom_FYR/fyers_token.json.
    Never performs TOTP login — if token is invalid, flags it as expired
    so the scanner re-authenticates on its next heartbeat.
    """

    # ── Fyers credentials (same as scanner — only used for FyersModel init) ──
    APP_ID    = "OUDS3XQTRU"
    APP_TYPE  = "100"
    CLIENT_ID = f"{APP_ID}-{APP_TYPE}"

    # ── Market hours by asset type ────────────────────────────────────────────
    MARKET_HOURS = {
        "INDEX":     (dt_time(9, 15), dt_time(15, 30)),
        "COMMODITY": (dt_time(9, 0),  dt_time(23, 30)),
    }

    # ── Timeframe map: resolution → (fyers_resolution, timedelta, category) ──
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
        "120": ("120", timedelta(hours=2),    "intraday"),
        "240": ("240", timedelta(hours=4),    "intraday"),
        "D":   ("D",   timedelta(days=1),     "daily"),
    }

    def __init__(self) -> None:
        self._model: fyersModel.FyersModel | None = None
        self._token: str | None = None
        self._token_date: date | None = None

    # ╔══════════════════════════════════════════════════════════════════════════╗
    # ║  TOKEN LOADING  (read-only — from scanner's shared file)                 ║
    # ╚══════════════════════════════════════════════════════════════════════════╝

    @property
    def model(self) -> fyersModel.FyersModel:
        """Return the current FyersModel, raising if not loaded."""
        if self._model is None:
            raise RuntimeError("No Fyers session loaded — call load_token() first")
        return self._model

    @property
    def is_loaded(self) -> bool:
        return self._model is not None

    def load_token(self, force: bool = False) -> bool:
        """
        Load the Fyers token from the shared file written by dev_scanner.

        Returns True if a valid session was established, False otherwise.

        The updater is strictly read-only — it NEVER performs TOTP login.
        If the token file doesn't exist or is expired, returns False.
        """
        today = date.today()

        # Fast path: already loaded today's token
        if not force and self._model and self._token_date == today:
            return True

        token, token_dt, expired = self._read_token_file()

        if not token:
            logger.warning("No token file found at %s", TOKEN_FILE)
            return False

        if expired:
            logger.warning("Token is flagged as expired — waiting for scanner re-auth")
            return False

        # Prefer today's token; accept yesterday's with verification
        if token_dt == today:
            if self._verify_token(token):
                self._set_model(token, today)
                return True
            else:
                # Token is today's but invalid — flag it
                self._flag_token_expired()
                return False

        # Day-change grace: verify yesterday's token
        if token_dt and self._verify_token(token):
            self._set_model(token, today)
            return True

        logger.warning(
            "Token date=%s doesn't match today=%s and verification failed",
            token_dt, today,
        )
        return False

    def invalidate(self) -> None:
        """Clear cached session and flag the token as expired."""
        self._model = None
        self._token = None
        self._token_date = None
        self._flag_token_expired()

    def verify_session(self) -> bool:
        """
        Verify the current token is still valid via profile API call.
        If invalid, attempt to reload from file.  If that fails too,
        flag the token as expired.

        Returns True if session is valid/refreshed, False otherwise.
        """
        if not self._model:
            return self.load_token(force=True)

        try:
            resp = self._model.get_profile()
            if resp.get("s") == "ok":
                return True
        except Exception:
            pass

        # Token appears dead — try reloading from file
        logger.warning("Token verification failed — reloading from file")
        self._model = None
        self._token = None
        self._token_date = None

        if self.load_token(force=True):
            return True

        self._flag_token_expired()
        return False

    # ── internal helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _read_token_file() -> Tuple[str | None, date | None, bool]:
        """Read token file. Returns (token, date, expired_flag)."""
        if not TOKEN_FILE.exists():
            return None, None, False
        try:
            blob = json.loads(TOKEN_FILE.read_text())
            token = blob.get("access_token")
            token_dt = date.fromisoformat(blob["date"]) if blob.get("date") else None
            expired = blob.get("expired", False)
            return token, token_dt, expired
        except Exception:
            return None, None, False

    def _verify_token(self, token: str) -> bool:
        """Verify token against Fyers profile API (3 retries)."""
        for attempt in range(1, 4):
            try:
                m = self._build_model(token)
                resp = m.get_profile()
                if resp.get("s") == "ok":
                    return True
                # Definitive auth rejection — don't retry
                msg = str(resp.get("message", "")).lower()
                if "authenticate" in msg or "token" in msg or "expired" in msg:
                    return False
            except Exception:
                pass
            if attempt < 3:
                sleep(2 * attempt)
        return False

    def _build_model(self, token: str) -> fyersModel.FyersModel:
        return fyersModel.FyersModel(
            client_id=self.CLIENT_ID,
            is_async=False,
            token=token,
            log_path=os.getcwd(),
        )

    def _set_model(self, token: str, dt: date) -> None:
        self._model = self._build_model(token)
        self._token = token
        self._token_date = dt

    @staticmethod
    def _flag_token_expired() -> None:
        """Set the 'expired' flag in the token file so scanner re-auths."""
        if not TOKEN_FILE.exists():
            return
        try:
            blob = json.loads(TOKEN_FILE.read_text())
            blob["expired"] = True
            # Atomic write
            TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(suffix=".json", dir=str(TOKEN_FILE.parent))
            try:
                with open(fd, "w") as f:
                    json.dump(blob, f, indent=2)
                shutil.move(tmp, str(TOKEN_FILE))
            except Exception:
                if Path(tmp).exists():
                    Path(tmp).unlink()
                raise
            logger.info("Flagged token as expired for scanner to re-auth")
        except Exception as e:
            logger.error("Failed to flag token as expired: %s", e)

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

    def safe_api_call(self, api_method, *args, **kwargs):
        """
        Call a Fyers API method, reloading token on auth errors.

        If the response indicates auth failure, reloads from the shared
        token file and retries once.  If that also fails, flags the
        token as expired.
        """
        resp = api_method(*args, **kwargs)
        if self._is_auth_error(resp):
            logger.warning("Auth error detected — reloading token from file")
            if self.load_token(force=True):
                new_method = getattr(self.model, api_method.__name__)
                resp = new_method(*args, **kwargs)
                if self._is_auth_error(resp):
                    self._flag_token_expired()
            else:
                self._flag_token_expired()
        return resp

    # ╔══════════════════════════════════════════════════════════════════════════╗
    # ║  HISTORICAL DATA FETCH  (market-hours & holiday aware)                   ║
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

        Timing logic
        ────────────
        • INDEX:     9:15 AM → 3:30 PM  Mon–Fri, skip NSE holidays
        • COMMODITY: 9:00 AM → 11:30 PM Mon–Fri, skip NSE holidays

        When walking backwards to fill *candles*, weekends AND holidays
        are skipped.  Special sessions (Budget Saturday, Diwali Muhurat)
        are treated as valid trading days.

        Returns
        ───────
        DataFrame with columns: Timestamp, Open, High, Low, Close, Volume
        """
        if timeframe not in self._TF_MAP:
            raise ValueError(f"Unsupported timeframe '{timeframe}'")
        resolution, delta, _ = self._TF_MAP[timeframe]

        if holidays is None:
            holidays = set()
        if special_sessions is None:
            special_sessions = []

        # Build fast-lookup for special sessions
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

        # ── determine end_dt ───────────────────────────────────────────────
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

        # ── walk backwards to compute start_dt ─────────────────────────────
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

        # ── call Fyers history API ─────────────────────────────────────────
        # Options (CE/PE) must use cont_flag=0 (specific contract data).
        # cont_flag=1 returns underlying futures series, not option prices.
        sym_name = symbol.split(":")[-1] if ":" in symbol else symbol
        is_option = sym_name.upper().endswith("CE") or sym_name.upper().endswith("PE")

        payload = {
            "symbol": symbol,
            "resolution": resolution,
            "date_format": "0",  # Unix timestamps
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

        # ── Data quality: sort ascending + deduplicate ────────────────
        was_unsorted = not df["Timestamp"].is_monotonic_increasing
        n_before = len(df)
        df = (
            df.sort_values("Timestamp")
            .drop_duplicates(subset="Timestamp", keep="last")
            .reset_index(drop=True)
        )
        n_dupes = n_before - len(df)

        df.attrs["_data_quality"] = {
            "symbol": symbol,
            "timeframe": timeframe,
            "was_unsorted": was_unsorted,
            "duplicates_removed": n_dupes,
            "candles_returned": len(df),
            "candles_requested": candles,
        }

        return df

    # ╔══════════════════════════════════════════════════════════════════════════╗
    # ║  TRADING HOLIDAYS  (cached annually on C: drive)                         ║
    # ╚══════════════════════════════════════════════════════════════════════════╝

    @staticmethod
    def fetch_trading_holidays(year: int | None = None) -> dict:
        """
        Fetch NSE trading holidays for *year* from Fyers / known lists.

        Caches to C:/Ballom_FYR/cache/fyers_holidays_{year}.json.
        Returns dict with keys: holidays (set of ISO date strings),
                                special_sessions (list of dicts)
        """
        if year is None:
            year = date.today().year

        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache_file = CACHE_DIR / f"fyers_holidays_{year}.json"

        if cache_file.exists():
            try:
                return json.loads(cache_file.read_text())
            except Exception:
                pass

        # Build from known fixed holidays + any Fyers API data
        data = FyersAPI._known_fixed_holidays(year)

        # Try Fyers holiday API
        try:
            url = f"https://public.fyers.in/sym_details/holidays_{year}.json"
            r = requests.get(url, timeout=15)
            if r.status_code == 200:
                fyers_data = r.json()
                if isinstance(fyers_data, dict):
                    for key, dates in fyers_data.items():
                        if isinstance(dates, list):
                            for d in dates:
                                if isinstance(d, str) and len(d) == 10:
                                    data.setdefault("holidays", []).append(d)
        except Exception:
            pass

        # Deduplicate
        data["holidays"] = sorted(set(data.get("holidays", [])))

        # Cache
        try:
            cache_file.write_text(json.dumps(data, indent=2))
        except Exception:
            pass

        return data

    @staticmethod
    def load_holiday_set(year: int | None = None) -> Tuple[set[str], list[dict]]:
        """Return (holiday_dates_set, special_sessions_list) for *year*."""
        data = FyersAPI.fetch_trading_holidays(year)
        return set(data.get("holidays", [])), data.get("special_sessions", [])

    @staticmethod
    def _known_fixed_holidays(year: int) -> dict:
        """
        Return a dict of known fixed NSE holidays and special sessions.

        These are approximate — the actual calendar may vary slightly
        each year.  The Fyers API response (when available) overrides these.
        """
        holidays = [
            f"{year}-01-26",  # Republic Day
            f"{year}-08-15",  # Independence Day
            f"{year}-10-02",  # Gandhi Jayanti
            f"{year}-12-25",  # Christmas
        ]
        return {
            "holidays": holidays,
            "special_sessions": [],
        }

    @staticmethod
    def _known_special_sessions() -> list[dict]:
        """Known special Saturday trading sessions (Muhurat etc.)."""
        return []
