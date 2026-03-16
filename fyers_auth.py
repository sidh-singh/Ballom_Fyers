"""
fyers_auth.py — Robust Fyers authentication manager.

Single responsibility: authenticate with Fyers API and provide a
ready-to-use FyersModel instance.  All other branches (scanner,
updater, trading, dashboard) import this instead of doing their own auth.

Token lifecycle
───────────────
  1. On startup → load token from C:/Ballom_FYR/fyers_token.json
  2. If token file exists, not expired, and verifies → reuse immediately
  3. Otherwise → full TOTP-based login + persist new token
  4. On any API auth failure → automatic re-auth + retry the failed call
  5. On day-change → force re-auth (called by application.py loop)
  6. Token file stores an 'expired' flag so other branches (read-only)
     can instantly see whether the token is stale without verifying

Robustness features
───────────────────
  • Exponential backoff with jitter on TOTP login retries
  • TOTP window-edge avoidance (waits if < 3s or > 25s into 30s window)
  • Token verification with retries (handles transient network flakes)
  • Thread-safe token file writes (atomic temp + rename)
  • Shared token file on C: drive — only one process does TOTP login,
    others read from the file (read_only mode)
  • 'expired' flag in token JSON — lets consumers skip verify on stale tokens
  • Heartbeat: periodic background verification to detect server-side
    token invalidation (Fyers single-session policy)

Fyers API v3 Auth Endpoints
───────────────────────────
  Base (login):  https://api-t2.fyers.in/vagator/v2
    POST /send_login_otp  → request_key
    POST /verify_otp      → request_key (TOTP verification)
    POST /verify_pin      → access_token (PIN verification)

  Base (token):  https://api-t1.fyers.in/api/v3
    POST /token           → auth_code (authorization code grant)

  SessionModel.generate_token() → final access_token

  Verification:  FyersModel.get_profile() → {"s": "ok"} when valid
"""

from __future__ import annotations

import json
import logging
import os
import random
import shutil
import tempfile
import threading
from datetime import date, datetime
from pathlib import Path
from time import sleep
from typing import Tuple
from urllib.parse import parse_qs, urlparse

import pyotp
import requests
from fyers_apiv3 import fyersModel

from constants import TOKEN_FILE, TOKEN_DIR

# ═══════════════════════════════════════════════════════════════════════════════
#  LOGGING
# ═══════════════════════════════════════════════════════════════════════════════

logger = logging.getLogger("fyers_auth")


# ═══════════════════════════════════════════════════════════════════════════════
#  TOKEN FILE SCHEMA
# ═══════════════════════════════════════════════════════════════════════════════
#
#  {
#    "access_token": "eyJ...",
#    "date": "2026-03-17",
#    "created_at": "2026-03-17 09:15:02",
#    "expired": false
#  }
#
#  'expired' is set to True when any API call detects an auth failure.
#  This lets read-only consumers (updaters) skip a verify round-trip
#  and immediately wait for the scanner to refresh the token.
# ═══════════════════════════════════════════════════════════════════════════════


class FyersAuth:
    """
    Manages Fyers API authentication — TOTP login, token persistence,
    verification, and automatic re-authentication.

    Usage
    ─────
    auth = FyersAuth()
    model = auth.get_model()          # ready-to-use FyersModel
    resp = auth.safe_api_call(model.quotes, data={...})
    """

    # ── Fyers credentials ──────────────────────────────────────────────────────
    FY_ID        = "YS07018"
    SECRET_KEY   = "9FBBBL2MAY"
    APP_ID       = "OUDS3XQTRU"
    APP_TYPE     = "100"
    CLIENT_ID    = f"{APP_ID}-{APP_TYPE}"
    GRANT_TYPE   = "authorization_code"
    RESPONSE_TYPE = "code"
    STATE        = "sample"
    PIN          = "0000"
    TOTP_KEY     = "NTXL3YEXLUC2QRZYAGC2ZTUMC3FJLBLZ"
    REDIRECT_URI = "https://jenkin.thealgotrading.in/"

    # ── Fyers API base URLs ────────────────────────────────────────────────────
    BASE_URL_LOGIN = "https://api-t2.fyers.in/vagator/v2"
    BASE_URL_TOKEN = "https://api-t1.fyers.in/api/v3"

    # ── Retry / backoff config ─────────────────────────────────────────────────
    AUTH_MAX_RETRIES      = 5       # max TOTP login attempts
    AUTH_RETRY_BASE_WAIT  = 10      # seconds — base backoff (doubles each retry)
    AUTH_RETRY_MAX_WAIT   = 120     # seconds — cap on backoff
    AUTH_VERIFY_RETRIES   = 3       # token verification attempts
    HEARTBEAT_INTERVAL    = 300     # seconds between background token checks (5 min)

    def __init__(self, read_only: bool = False) -> None:
        """
        Parameters
        ──────────
        read_only : If True, never perform TOTP login — only load tokens
                    written by another process (e.g. dev_scanner writes,
                    dev_updater reads).
        """
        self._model: fyersModel.FyersModel | None = None
        self._token: str | None = None
        self._token_date: date | None = None
        self._read_only: bool = read_only
        self._lock = threading.Lock()
        self._heartbeat_thread: threading.Thread | None = None
        self._heartbeat_stop = threading.Event()

    # ╔══════════════════════════════════════════════════════════════════════════╗
    # ║  PUBLIC API                                                              ║
    # ╚══════════════════════════════════════════════════════════════════════════╝

    def get_model(self, force: bool = False) -> fyersModel.FyersModel:
        """
        Return a ready-to-use FyersModel.

        - Reuses today's verified token unless *force* is True.
        - On day-change, caller passes force=True.
        - In read_only mode, only loads from the shared token file.

        Thread-safe via internal lock.
        """
        with self._lock:
            return self._ensure_session(force=force)

    def re_authenticate(self) -> fyersModel.FyersModel:
        """
        Force a full re-authentication cycle.

        Use when API calls fail with auth errors mid-day — the existing
        token may have been invalidated server-side.
        """
        with self._lock:
            self._invalidate()
            if self._read_only:
                return self._ensure_session(force=True)
            model = self._authenticate_with_retry()
            self._token_date = date.today()
            return model

    def invalidate(self) -> None:
        """Clear cached session; next get_model() will re-load from file."""
        with self._lock:
            self._invalidate()

    def safe_api_call(self, api_method, *args, **kwargs):
        """
        Call a Fyers API method, automatically re-authenticating on auth errors.

        Usage:
            resp = auth.safe_api_call(model.quotes, data={"symbols": "NSE:NIFTY50-INDEX"})

        If the response indicates an auth failure:
          1. Marks token as expired in the JSON file
          2. Performs re-authentication
          3. Retries the call once on the new model
        """
        resp = api_method(*args, **kwargs)
        if self._is_auth_error(resp):
            logger.warning("Auth error detected in API response — re-authenticating")
            self._mark_token_expired()
            try:
                new_model = self.re_authenticate()
            except Exception as e:
                logger.error(f"Re-auth failed during safe_api_call: {e}")
                return resp
            # Re-resolve the method on the NEW model (old model is invalid)
            new_method = getattr(new_model, api_method.__name__)
            resp = new_method(*args, **kwargs)
        return resp

    def start_heartbeat(self) -> None:
        """
        Start a background thread that periodically verifies the token
        is still valid.  If verification fails, marks it expired so the
        next API call triggers re-auth immediately instead of failing.
        """
        if self._heartbeat_thread and self._heartbeat_thread.is_alive():
            return  # already running
        self._heartbeat_stop.clear()
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop, daemon=True, name="fyers-auth-heartbeat",
        )
        self._heartbeat_thread.start()
        logger.info("Token heartbeat started (interval=%ds)", self.HEARTBEAT_INTERVAL)

    def stop_heartbeat(self) -> None:
        """Stop the background heartbeat thread."""
        self._heartbeat_stop.set()
        if self._heartbeat_thread:
            self._heartbeat_thread.join(timeout=10)
        logger.info("Token heartbeat stopped")

    @property
    def is_authenticated(self) -> bool:
        """True if we have a model and it was authenticated today."""
        return self._model is not None and self._token_date == date.today()

    @property
    def token_date(self) -> date | None:
        return self._token_date

    # ╔══════════════════════════════════════════════════════════════════════════╗
    # ║  INTERNAL — SESSION MANAGEMENT                                           ║
    # ╚══════════════════════════════════════════════════════════════════════════╝

    def _ensure_session(self, force: bool = False) -> fyersModel.FyersModel:
        """Core logic — must be called inside self._lock."""
        today = date.today()

        # Fast path: already authenticated today
        if not force and self._model and self._token_date == today:
            return self._model

        # Always try loading from shared token file first.
        # Prevents race conditions when multiple processes detect day-change
        # simultaneously — only the first does TOTP, others reuse the file.
        token_data = self._load_token()
        if token_data:
            token = token_data.get("access_token")
            token_dt = token_data.get("date")
            expired = token_data.get("expired", False)

            if token and token_dt == today and not expired:
                if self._verify_token_safe(token):
                    self._model = self._build_model(token)
                    self._token = token
                    self._token_date = today
                    logger.info("Reusing today's token from file")
                    return self._model
                else:
                    # Token date matches but verification failed — mark expired
                    logger.warning("Today's token failed verification — marking expired")
                    self._mark_token_expired()

            # Day-change grace: Fyers tokens stay valid past midnight until
            # a new TOTP login invalidates them.  Verify stale tokens.
            if token and token_dt and token_dt != today and not expired:
                if self._verify_token_safe(token):
                    logger.info(
                        "Previous day's token still valid (date=%s) — reusing",
                        token_dt,
                    )
                    self._model = self._build_model(token)
                    self._token = token
                    self._token_date = today
                    # Update the date in file so other processes see today's date
                    self._save_token(token)
                    return self._model

        # read_only mode: cannot do TOTP login — must wait for writer
        if self._read_only:
            # One more attempt: verify whatever token we have, even if expired flag is set
            if token_data and token_data.get("access_token"):
                token = token_data["access_token"]
                if self._verify_token_safe(token):
                    self._model = self._build_model(token)
                    self._token = token
                    self._token_date = today
                    # Clear expired flag since it actually works
                    self._save_token(token)
                    return self._model
            raise RuntimeError(
                f"No valid token for {today} in {TOKEN_FILE} "
                f"(read_only mode) — waiting for scanner to refresh"
            )

        # Full TOTP login (only when no valid token exists)
        logger.info("Performing full TOTP authentication")
        model = self._authenticate_with_retry()
        self._token_date = today
        return model

    def _invalidate(self) -> None:
        """Clear cached session state — must be called inside self._lock."""
        self._model = None
        self._token = None
        self._token_date = None

    # ╔══════════════════════════════════════════════════════════════════════════╗
    # ║  TOKEN PERSISTENCE (C:/Ballom_FYR/fyers_token.json)                      ║
    # ╚══════════════════════════════════════════════════════════════════════════╝

    def _save_token(self, token: str) -> None:
        """Atomically write token to the shared file with metadata."""
        TOKEN_DIR.mkdir(parents=True, exist_ok=True)
        data = {
            "access_token": token,
            "date": date.today().isoformat(),
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "expired": False,
        }
        self._write_json_atomic(TOKEN_FILE, data)
        logger.info("Token saved to %s (date=%s)", TOKEN_FILE, data["date"])

    @staticmethod
    def _load_token() -> dict | None:
        """
        Load token data from the shared file.

        Returns dict with keys: access_token, date (as date obj), expired, created_at
        or None if file missing / corrupt.
        """
        if not TOKEN_FILE.exists():
            return None
        try:
            blob = json.loads(TOKEN_FILE.read_text())
            blob["date"] = date.fromisoformat(blob.get("date", ""))
            blob.setdefault("expired", False)
            return blob
        except Exception as e:
            logger.warning("Failed to load token file: %s", e)
            return None

    def _mark_token_expired(self) -> None:
        """Set the 'expired' flag to True in the token file.

        This lets read-only consumers (updaters) skip verification and
        immediately wait for a fresh token instead of burning retries
        on a known-bad token.
        """
        try:
            if not TOKEN_FILE.exists():
                return
            blob = json.loads(TOKEN_FILE.read_text())
            blob["expired"] = True
            self._write_json_atomic(TOKEN_FILE, blob)
            logger.info("Token marked as expired in %s", TOKEN_FILE)
        except Exception as e:
            logger.warning("Failed to mark token expired: %s", e)

    @staticmethod
    def _write_json_atomic(path: Path, data: dict) -> None:
        """Write *data* to *path* atomically via temp-file + rename."""
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(suffix=".json", dir=str(path.parent))
        try:
            with open(fd, "w") as f:
                json.dump(data, f, indent=2, default=str)
            shutil.move(tmp, str(path))
        except Exception:
            if Path(tmp).exists():
                Path(tmp).unlink()
            raise

    # ╔══════════════════════════════════════════════════════════════════════════╗
    # ║  TOKEN VERIFICATION                                                      ║
    # ╚══════════════════════════════════════════════════════════════════════════╝

    def _verify_token(self, token: str) -> bool:
        """Single-shot token verification via get_profile()."""
        try:
            m = self._build_model(token)
            resp = m.get_profile()
            return resp.get("s") == "ok"
        except Exception:
            return False

    def _verify_token_safe(self, token: str) -> bool:
        """Verify token with retries to handle transient network errors."""
        for attempt in range(1, self.AUTH_VERIFY_RETRIES + 1):
            try:
                m = self._build_model(token)
                resp = m.get_profile()
                if resp.get("s") == "ok":
                    return True
                # Definitive auth rejection — no point retrying
                if self._is_auth_error(resp):
                    logger.debug("Token rejected by server (attempt %d)", attempt)
                    return False
            except Exception as e:
                logger.debug("Token verify error (attempt %d): %s", attempt, e)
            if attempt < self.AUTH_VERIFY_RETRIES:
                sleep(2 * attempt)
        return False

    def _build_model(self, token: str) -> fyersModel.FyersModel:
        """Construct a FyersModel from an access token."""
        return fyersModel.FyersModel(
            client_id=self.CLIENT_ID,
            is_async=False,
            token=token,
            log_path=os.getcwd(),
        )

    # ╔══════════════════════════════════════════════════════════════════════════╗
    # ║  AUTH ERROR DETECTION                                                    ║
    # ╚══════════════════════════════════════════════════════════════════════════╝

    @staticmethod
    def _is_auth_error(resp: dict | None) -> bool:
        """True if a Fyers API response indicates an authentication failure."""
        if not resp or not isinstance(resp, dict):
            return False
        code = resp.get("code")
        # Fyers uses specific error codes for auth failures
        if code in (-16, -17, -18):
            return True
        msg = str(resp.get("message", "")).lower()
        return any(phrase in msg for phrase in (
            "could not authenticate",
            "invalid token",
            "token is expired",
            "token has expired",
            "access denied",
            "unauthorized",
            "session expired",
        ))

    @staticmethod
    def is_auth_failure_exception(exc: Exception) -> bool:
        """True if an exception message indicates a Fyers auth failure."""
        msg = str(exc).lower()
        return any(phrase in msg for phrase in (
            "could not authenticate",
            "invalid token",
            "token is expired",
            "authentication failed",
            "access denied",
            "unauthorized",
        ))

    # ╔══════════════════════════════════════════════════════════════════════════╗
    # ║  FULL TOTP LOGIN (with retries + exponential backoff + jitter)           ║
    # ╚══════════════════════════════════════════════════════════════════════════╝

    def _authenticate_with_retry(self) -> fyersModel.FyersModel:
        """Run TOTP-based login with retries and exponential backoff + jitter."""
        last_err: Exception | None = None
        for attempt in range(1, self.AUTH_MAX_RETRIES + 1):
            try:
                logger.info(
                    "TOTP login attempt %d/%d", attempt, self.AUTH_MAX_RETRIES,
                )
                model = self._authenticate()
                logger.info("TOTP login succeeded on attempt %d", attempt)
                return model
            except Exception as e:
                last_err = e
                logger.warning("TOTP login attempt %d failed: %s", attempt, e)
                if attempt < self.AUTH_MAX_RETRIES:
                    # Exponential backoff with jitter
                    base_wait = self.AUTH_RETRY_BASE_WAIT * (2 ** (attempt - 1))
                    wait = min(base_wait, self.AUTH_RETRY_MAX_WAIT)
                    jitter = random.uniform(0, wait * 0.3)
                    total_wait = wait + jitter
                    logger.info("Retrying in %.1f seconds...", total_wait)
                    sleep(total_wait)
        raise RuntimeError(
            f"Authentication failed after {self.AUTH_MAX_RETRIES} attempts: {last_err}"
        )

    def _authenticate(self) -> fyersModel.FyersModel:
        """
        Perform the full Fyers TOTP-based login flow.

        Steps:
          1. POST /send_login_otp  → get request_key
          2. Generate TOTP via pyotp, POST /verify_otp → new request_key
          3. POST /verify_pin → Bearer access_token
          4. POST /token → authorization code (via redirect URL or response body)
          5. SessionModel.generate_token() → final access_token
          6. Persist token to C:/Ballom_FYR/fyers_token.json
          7. Build and return FyersModel
        """
        url_otp    = f"{self.BASE_URL_LOGIN}/send_login_otp"
        url_verify = f"{self.BASE_URL_LOGIN}/verify_otp"
        url_pin    = f"{self.BASE_URL_LOGIN}/verify_pin"
        url_token  = f"{self.BASE_URL_TOKEN}/token"

        # ── Step 1: Send login OTP ─────────────────────────────────────────
        r1 = requests.post(
            url_otp,
            json={"fy_id": self.FY_ID, "app_id": self.APP_ID},
            timeout=30,
        ).json()
        if "request_key" not in r1:
            raise RuntimeError(f"send_login_otp failed: {r1}")
        rk = r1["request_key"]
        logger.debug("OTP sent, request_key obtained")

        # ── Step 2: TOTP generation with window-edge avoidance ─────────────
        # TOTP codes change every 30 seconds.  If we're near the boundary
        # (last 5s or first 2s of the window), wait to avoid stale codes.
        sec = datetime.now().second % 30
        if sec > 25 or sec < 2:
            wait_secs = max(30 - sec, 3)
            logger.debug("Near TOTP window edge (sec=%d), waiting %ds", sec, wait_secs)
            sleep(wait_secs)
        totp = pyotp.TOTP(self.TOTP_KEY).now()

        # ── Step 3: Verify OTP ─────────────────────────────────────────────
        r2 = requests.post(
            url_verify,
            json={"request_key": rk, "otp": totp},
            timeout=30,
        ).json()
        if "request_key" not in r2:
            raise RuntimeError(f"verify_otp failed: {r2}")
        rk = r2["request_key"]
        logger.debug("OTP verified")

        # ── Step 4: Verify PIN ─────────────────────────────────────────────
        ses = requests.Session()
        r3 = ses.post(
            url_pin,
            json={
                "request_key": rk,
                "identity_type": "pin",
                "identifier": self.PIN,
            },
            timeout=30,
        ).json()
        if not r3.get("data", {}).get("access_token"):
            raise RuntimeError(f"verify_pin failed: {r3}")
        ses.headers.update(
            {"authorization": f"Bearer {r3['data']['access_token']}"}
        )
        logger.debug("PIN verified")

        # ── Step 5: Get authorization code ─────────────────────────────────
        payload = {
            "fyers_id": self.FY_ID,
            "app_id": self.APP_ID,
            "redirect_uri": self.REDIRECT_URI,
            "appType": self.APP_TYPE,
            "code_challenge": "",
            "state": self.STATE,
            "scope": "",
            "nonce": "",
            "response_type": self.RESPONSE_TYPE,
            "create_cookie": True,
        }
        r4 = ses.post(url_token, json=payload, timeout=30).json()

        auth_code: str | None = None
        if "Url" in r4:
            parsed_url = urlparse(r4["Url"])
            qs = parse_qs(parsed_url.query)
            if "auth_code" not in qs:
                raise RuntimeError(f"No auth_code in redirect URL: {r4['Url']}")
            auth_code = qs["auth_code"][0]
        elif r4.get("data", {}).get("auth"):
            auth_code = r4["data"]["auth"]

        if not auth_code:
            raise RuntimeError(f"Token exchange failed — no auth_code: {r4}")
        logger.debug("Authorization code obtained")

        # ── Step 6: Generate final access token via SessionModel ───────────
        session = fyersModel.SessionModel(
            client_id=self.CLIENT_ID,
            secret_key=self.SECRET_KEY,
            redirect_uri=self.REDIRECT_URI,
            response_type=self.RESPONSE_TYPE,
            grant_type=self.GRANT_TYPE,
        )
        session.set_token(auth_code)
        resp = session.generate_token()

        if resp.get("s") == "ERROR":
            raise RuntimeError(f"Token generation failed: {resp}")

        token = resp["access_token"]
        logger.info("Access token generated successfully")

        # ── Step 7: Persist + build model ──────────────────────────────────
        self._save_token(token)
        self._model = self._build_model(token)
        self._token = token
        return self._model

    # ╔══════════════════════════════════════════════════════════════════════════╗
    # ║  HEARTBEAT — background token health check                               ║
    # ╚══════════════════════════════════════════════════════════════════════════╝

    def _heartbeat_loop(self) -> None:
        """Periodically verify the token is still valid in the background."""
        while not self._heartbeat_stop.wait(self.HEARTBEAT_INTERVAL):
            if not self._token:
                continue
            try:
                if not self._verify_token(self._token):
                    logger.warning("Heartbeat: token no longer valid — marking expired")
                    self._mark_token_expired()
                    with self._lock:
                        self._invalidate()
                else:
                    logger.debug("Heartbeat: token OK")
            except Exception as e:
                logger.debug("Heartbeat check error: %s", e)
