"""Groww Trade API brokerage (Indian equities, NSE/BSE cash segment).

Groww has no paper-trading sandbox, so by default this client is used **read-only**:
real holdings and live prices feed the local paper simulator. Every call that can
place, change or cancel a real order (orders and GTT smart orders) refuses to run
unless the broker was built with ``live_orders=True`` (``GROWW_LIVE_ORDERS=true``).
The agent additionally needs ``AUTO_TRADE=true`` before it is given an order tool.

Live orders are LIMIT orders priced at LTP +/- ``MAX_SLIPPAGE_PCT`` and rounded to the
stock's tick size, carry an ``order_reference_id`` so a retry cannot double-place, and
are confirmed by polling the order status after placement.

API reference: https://groww.in/trade-api/docs/curl (paths cross-checked against the
official ``growwapi`` 1.5.0 Python SDK source).
"""

from __future__ import annotations

import base64
import csv
import hashlib
import hmac
import json
import logging
import math
import os
import re
import struct
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone, time as dtime
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, Callable

import requests

from .broker import Account, Position, refuse_bse_only
from .timezones import IST

log = logging.getLogger(__name__)

BASE_URL = "https://api.groww.in/v1"
INSTRUMENT_CSV_URL = "https://growwapi-assets.groww.in/instruments/instrument.csv"
TOKEN_ROLLOVER = dtime(6, 0)  # Groww access tokens expire at 06:00 IST
DEFAULT_TICK = 0.05
SEGMENT = "CASH"

# Order statuses (Groww annexure). Anything not listed here is still working ("open").
FILLED_STATUSES = {"EXECUTED", "COMPLETED", "DELIVERY_AWAITED"}
FAILED_STATUSES = {"REJECTED", "FAILED", "CANCELLED"}


class LiveOrdersDisabled(PermissionError):
    """Raised by every call that would place, modify or cancel a real order."""


class WrongIP(LiveOrdersDisabled):
    """Live order refused: this machine's public IP is not the one registered with Groww."""


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #
def totp_now(secret_b32: str, digits: int = 6, period: int = 30) -> str:
    """RFC 6238 TOTP (SHA-1), so no extra dependency is needed."""
    key = base64.b32decode(secret_b32.strip().replace(" ", "").upper() + "=" * (-len(secret_b32) % 8))
    counter = struct.pack(">Q", int(time.time()) // period)
    digest = hmac.new(key, counter, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = (struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
    return str(code).zfill(digits)


def round_to_tick(price: float, tick: float | None = DEFAULT_TICK, mode: str = "nearest") -> float:
    """Round ``price`` to a multiple of ``tick``: mode "down", "up" or "nearest"."""
    t = Decimal(str(tick or DEFAULT_TICK))
    if t <= 0:
        t = Decimal(str(DEFAULT_TICK))
    rounding = {"down": ROUND_FLOOR, "up": ROUND_CEILING, "nearest": ROUND_HALF_UP}[mode]
    steps = (Decimal(str(price)) / t).to_integral_value(rounding=rounding)
    return float(steps * t)


def limit_price(ltp: float, side: str, slippage_pct: float, tick: float | None = DEFAULT_TICK) -> float:
    """Marketable limit: buys at LTP x (1 + slippage), sells at LTP x (1 - slippage).

    Rounded towards the LTP (buys down, sells up) so the price never gives away more
    than ``slippage_pct``.
    """
    if ltp <= 0:
        raise ValueError("LTP must be positive")
    s = float(slippage_pct) / 100.0
    if side == "buy":
        return round_to_tick(ltp * (1 + s), tick, "down")
    if side == "sell":
        return max(round_to_tick(ltp * (1 - s), tick, "up"), float(tick or DEFAULT_TICK))
    raise ValueError("side must be 'buy' or 'sell'")


_REF_RE = re.compile(r"^[A-Za-z0-9-]{8,20}$")


def valid_reference_id(ref: str) -> bool:
    """Groww: 8-20 alphanumeric characters with at most two hyphens."""
    return bool(_REF_RE.match(ref or "")) and ref.count("-") <= 2 and not ref.startswith("-") \
        and not ref.endswith("-")


def make_reference_id(prefix: str = "TA") -> str:
    """e.g. ``TA-3F9A1C07B24E5D`` (17 characters, one hyphen)."""
    ref = f"{prefix}-{uuid.uuid4().hex[:14].upper()}"
    assert valid_reference_id(ref), ref
    return ref


def sellable_quantity(holding: dict[str, Any]) -> float:
    """Free shares only: demat_free_quantity + t1_quantity, capped at the holding.

    Pledged, repledged, demat-locked and Groww-locked shares are never counted. If
    Groww leaves both fields out we treat nothing as sellable rather than guess.
    """
    free = float(holding.get("demat_free_quantity") or 0) + float(holding.get("t1_quantity") or 0)
    total = float(holding.get("quantity") or 0)
    return max(0.0, min(free, total))


def next_token_expiry(now: datetime | None = None) -> datetime:
    """The next 06:00 IST strictly after ``now``."""
    now = (now or datetime.now(IST)).astimezone(IST)
    roll = datetime.combine(now.date(), TOKEN_ROLLOVER, tzinfo=IST)
    return roll if now < roll else roll + timedelta(days=1)


def classify_status(order_status: str | None) -> str:
    s = (order_status or "").upper()
    if s in FILLED_STATUSES:
        return "filled"
    if s in FAILED_STATUSES:
        return "failed"
    return "open"


def _money_str(x: float) -> str:
    return f"{x:.2f}"


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #
def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "X-API-VERSION": "1.0",
        "x-request-id": str(uuid.uuid4()),
    }


class TokenRequestError(requests.HTTPError):
    """Groww answered the token request with an error status (429, 401, 5xx ...)."""

    def __init__(self, status: int, *, retry_after: str | None = None, text: str = ""):
        super().__init__(f"Groww token request failed: HTTP {status}")
        self.status = status
        self.retry_after = retry_after
        self.text = text


class GrowwTokenUnavailable(RuntimeError):
    """No Groww token can be had right now: a recent token request failed and we are waiting.

    Raised without any network call while the cool-down is active. ``str(e)`` is the plain
    message to show the user; ``until`` is when the next request is allowed.
    """

    def __init__(self, message: str, until: datetime | None = None, status: int | None = None):
        super().__init__(message)
        self.until = until
        self.status = status


def _retry_after_header(resp: Any) -> str | None:
    headers = getattr(resp, "headers", None)
    try:
        return (headers.get("Retry-After") or headers.get("retry-after")) if headers else None
    except Exception:  # noqa: BLE001
        return None


def request_access_token(api_key: str, *, secret: str | None = None, totp: str | None = None,
                         session: requests.Session | None = None, base_url: str = BASE_URL) -> dict[str, Any]:
    """Exchange an API key for a daily access token. Returns {"token", "expiry"?}."""
    if (secret is None) == (totp is None):
        raise ValueError("pass exactly one of secret or totp")
    if secret is not None:
        ts = str(int(time.time()))
        body: dict[str, Any] = {"key_type": "approval", "timestamp": ts,
                                "checksum": hashlib.sha256((secret + ts).encode()).hexdigest()}
    else:
        body = {"key_type": "totp", "totp": totp}
    sess = session or requests.Session()
    resp = sess.post(f"{base_url}/token/api/access", headers=_headers(api_key), json=body, timeout=30)
    status = getattr(resp, "status_code", 200)
    if isinstance(status, int) and status >= 400:
        # The reply text is kept only to spot a "daily limit" wording; it is never shown or stored.
        raise TokenRequestError(status, retry_after=_retry_after_header(resp),
                                text=str(getattr(resp, "text", "") or "")[:500])
    resp.raise_for_status()
    data = resp.json()
    payload = data.get("payload") if isinstance(data.get("payload"), dict) else data
    token = data.get("token") or payload.get("token")
    if not token:
        # Never echo the response: it may contain credentials.
        raise RuntimeError(f"Groww token response had no token (keys: {sorted(data)})")
    return {"token": token, "expiry": data.get("expiry") or payload.get("expiry")}


def _parse_expiry(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        if isinstance(value, (int, float)):
            v = float(value)
            return datetime.fromtimestamp(v / 1000 if v > 1e11 else v, IST)
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=IST)
    except (ValueError, OSError, OverflowError):
        return None


_mem_blocks: dict[str, dict[str, Any]] = {}  # in-process copy of the cool-down, used if the file cannot be written
_TOKEN_LOCK = threading.Lock()  # one token request at a time in this process


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=IST)


def _atomic_write_private(path: Path, body: str) -> None:
    """Write via a uniquely named temp file (so concurrent writers never share one), owner-only, then rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(body)
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


class TokenCache:
    """Generated access token kept on disk until it expires (06:00 IST next day).

    Groww allows only 150 token generations a day, so every process reuses one token.
    The file is written owner-only (0600) where the OS supports it, and is tied to a
    fingerprint of the API key so changing keys never reuses a stale token.
    """

    def __init__(self, path: Path):
        self.path = Path(path)

    @staticmethod
    def fingerprint(api_key: str) -> str:
        return hashlib.sha256(api_key.encode()).hexdigest()[:16]

    def get(self, api_key: str, now: datetime | None = None) -> str | None:
        now = now or datetime.now(IST)
        try:
            data = json.loads(self.path.read_text())
            expires = datetime.fromisoformat(data["expires_at"])
        except (OSError, ValueError, KeyError, TypeError):
            return None
        if data.get("key") != self.fingerprint(api_key) or now >= expires or not data.get("token"):
            return None
        return str(data["token"])

    def put(self, api_key: str, token: str, expires_at: datetime) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        body = json.dumps({"token": token, "key": self.fingerprint(api_key),
                           "expires_at": expires_at.isoformat(timespec="seconds"),
                           "created_at": datetime.now(IST).isoformat(timespec="seconds")})
        _atomic_write_private(self.path, body)

    def clear(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass

    # -- cool-down after a refused token request ---------------------------------
    @property
    def block_path(self) -> Path:
        return self.path.with_suffix(".block")

    def read_block(self) -> dict[str, Any] | None:
        """The last failure record ({until, status, reason, at, strikes, last_429, key}), or None.

        A block file that exists but cannot be read or understood fails closed: blocked until its
        modification time + 2 minutes. When there is no usable file the in-process copy is used."""
        try:
            raw = self.block_path.read_text()
        except FileNotFoundError:
            return _mem_blocks.get(str(self.block_path))
        except OSError:
            raw = None
        try:
            data = json.loads(raw) if raw is not None else None
            if isinstance(data, dict):
                data["until"] = _aware(datetime.fromisoformat(data["until"])).isoformat()
                return data
        except (ValueError, KeyError, TypeError):
            pass
        try:
            mtime = datetime.fromtimestamp(self.block_path.stat().st_mtime, IST)
        except OSError:
            return _mem_blocks.get(str(self.block_path))
        return {"until": (mtime + timedelta(minutes=2)).isoformat(timespec="seconds"), "status": None,
                "reason": "unreadable cool-down file", "at": mtime.isoformat(timespec="seconds")}

    def active_block(self, api_key: str, now: datetime | None = None) -> dict[str, Any] | None:
        """The cool-down still in force for this key, else None."""
        now = now or datetime.now(IST)
        best = None
        for b in (self.read_block(), _mem_blocks.get(str(self.block_path))):
            if not b or now >= datetime.fromisoformat(b["until"]):
                continue
            if b.get("status") in (401, 403) and b.get("key") != self.fingerprint(api_key):
                continue  # the key was changed since: the old rejection says nothing about the new one
            if best is None or datetime.fromisoformat(b["until"]) > datetime.fromisoformat(best["until"]):
                best = b
        return best

    def block_error(self, api_key: str, now: datetime | None = None) -> "GrowwTokenUnavailable | None":
        """The error to raise right now if a cool-down is in force (no network, no token needed), else None."""
        now = now or datetime.now(IST)
        b = self.active_block(api_key, now)
        return _block_error(b, now) if b else None

    def write_block(self, record: dict[str, Any]) -> None:
        """Atomic, owner-only. Holds no token, key or secret: only the status, a fixed phrase and times."""
        _atomic_write_private(self.block_path, json.dumps(record))

    def clear_block(self) -> None:
        _mem_blocks.pop(str(self.block_path), None)
        try:
            self.block_path.unlink()
        except OSError:  # missing, or locked / read-only: the success itself must not fail over it
            pass


TOKEN_BLOCK_429_S = 15 * 60
TOKEN_BLOCK_429_CAP_S = 6 * 3600
TOKEN_BLOCK_AUTH_S = 30 * 60
TOKEN_BLOCK_NETWORK_S = 2 * 60
RETRY_AFTER_MAX_S = 24 * 3600  # a Retry-After beyond a day is clamped (garbage or hostile values)

_REASONS = {429: "429 Too Many Requests", 401: "401 Unauthorized", 403: "403 Forbidden"}
_warned_blocks: set[str] = set()


def _parse_retry_after(value: str | None, now: datetime) -> float | None:
    """Seconds to wait from a Retry-After header: delta-seconds or an HTTP-date."""
    if not value:
        return None
    value = str(value).strip()
    try:
        secs = float(value)
        return max(0.0, min(secs, RETRY_AFTER_MAX_S)) if math.isfinite(secs) else float(RETRY_AFTER_MAX_S)
    except ValueError:
        pass
    except OverflowError:
        return float(RETRY_AFTER_MAX_S)
    try:
        from email.utils import parsedate_to_datetime
        when = parsedate_to_datetime(value)
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return max(0.0, min((when - now).total_seconds(), RETRY_AFTER_MAX_S))
    except OverflowError:
        return float(RETRY_AFTER_MAX_S)
    except (TypeError, ValueError):
        return None


IN_PROGRESS = "request in progress"


def _block_message(status: int | None, until: datetime, now: datetime, reason: str | None = None) -> str:
    u = until.astimezone(IST)
    when = u.strftime("%H:%M IST") + ("" if u.date() == now.astimezone(IST).date() else u.strftime(" on %d %b"))
    if reason == IN_PROGRESS:
        return ("Another Groww login token request is in progress. Try again in a minute "
                f"(after {when}); the agent keeps working without Groww data until then.")
    if reason and reason.startswith("unreadable"):
        return ("Groww login token requests are paused because the cool-down file could not be read. "
                f"Next try after {when}. The agent keeps working without Groww data until then.")
    if status == 429:
        return (f"Groww refused a new login token (429 Too Many Requests). Next try after {when}. "
                "Nothing else to do; the agent keeps working without Groww data until then.")
    if status in (401, 403):
        return (f"Groww rejected the API key, secret or TOTP ({_REASONS.get(status, status)}). Check "
                f"GROWW_API_KEY and GROWW_API_SECRET (or GROWW_TOTP_SECRET) in .env; retrying will not help until "
                f"they are fixed. Next try after {when}. The agent keeps working without Groww data until then.")
    return (f"Could not get a login token from Groww ({'HTTP ' + str(status) if status else 'network error'}). "
            f"Next try after {when}. Nothing else to do; the agent keeps working without Groww data until then.")


def _record_token_failure(cache: TokenCache, api_key: str, exc: BaseException, now: datetime,
                          prev: dict[str, Any] | None = None) -> GrowwTokenUnavailable:
    status = exc.status if isinstance(exc, TokenRequestError) else None
    prev = prev or {}
    strikes = int(prev.get("strikes") or 0)
    last_429 = prev.get("last_429")
    if status == 429:
        base = float(TOKEN_BLOCK_429_S)
        ra = _parse_retry_after(getattr(exc, "retry_after", None), now)
        if ra is not None:
            base = max(base, ra)  # what Groww asked for is never capped
        try:
            recent = bool(last_429 and now - _aware(datetime.fromisoformat(last_429)) < timedelta(hours=12))
        except (ValueError, TypeError):
            recent = False
        strikes = strikes + 1 if recent else 1
        wait = base
        if strikes > 1:
            wait = max(base, min(base * 2 ** (strikes - 1), TOKEN_BLOCK_429_CAP_S))
        last_429 = now.isoformat(timespec="seconds")
        until = now + timedelta(seconds=wait)
        text = (getattr(exc, "text", "") or "").lower()
        if "daily" in text and "limit" in text:
            until = max(until, next_token_expiry(now))
    elif status in (401, 403):
        until = now + timedelta(seconds=TOKEN_BLOCK_AUTH_S)
    else:
        until = now + timedelta(seconds=TOKEN_BLOCK_NETWORK_S)  # strikes / last_429 carried over unchanged
    reason = _REASONS.get(status, f"HTTP {status}" if status else "network error")
    record = {"until": until.isoformat(timespec="seconds"), "status": status, "reason": reason,
              "at": now.isoformat(timespec="seconds"), "strikes": strikes, "last_429": last_429,
              "key": cache.fingerprint(api_key)}
    _mem_blocks[str(cache.block_path)] = record  # kept even if the file cannot be written
    try:
        cache.write_block(record)
    except OSError:
        log.warning("Could not save the Groww token cool-down file; remembering it in this process only")
    return GrowwTokenUnavailable(_block_message(status, until, now, reason), until, status)


def _block_error(b: dict[str, Any], now: datetime) -> GrowwTokenUnavailable:
    until = datetime.fromisoformat(b["until"])
    return GrowwTokenUnavailable(_block_message(b.get("status"), until, now, b.get("reason")), until, b.get("status"))


def warn_token_block_once(exc: GrowwTokenUnavailable, logger: logging.Logger | None = None) -> bool:
    """Log the cool-down message once per block period (not on every poll). True if it logged."""
    key = exc.until.isoformat() if exc.until else str(exc)
    if key in _warned_blocks:
        return False
    _warned_blocks.add(key)
    (logger or log).warning("%s", exc)
    return True


def cached_access_token(api_key: str, cache: TokenCache, *, secret: str | None = None,
                        totp_fn: Callable[[], str] | None = None, now: datetime | None = None,
                        session: requests.Session | None = None, fresh: bool = False,
                        force: bool = False) -> str:
    """Reuse the cached token while valid, otherwise generate one and cache it.

    After a failed request the failure is remembered in ``<cache>.block`` and no new request is made
    (GrowwTokenUnavailable, no network) until the cool-down ends, so a refusal is not made worse by
    retrying. ``force=True`` ignores the cool-down.
    """
    now = now or datetime.now(IST)
    if not fresh:
        tok = cache.get(api_key, now)
        if tok:
            return tok
    with _TOKEN_LOCK:
        # Another thread may have fetched a token, or hit a refusal, while we waited for the lock.
        if not fresh:
            tok = cache.get(api_key, now)
            if tok:
                return tok
        if not force:
            blk = cache.active_block(api_key, now)
            if blk:
                raise _block_error(blk, now)
        prev = cache.read_block()
        # Tell other processes on this machine a request is under way, so they wait instead of piling on.
        try:
            cache.write_block({"until": (now + timedelta(seconds=60)).isoformat(timespec="seconds"),
                               "status": None, "reason": IN_PROGRESS, "at": now.isoformat(timespec="seconds"),
                               "strikes": int((prev or {}).get("strikes") or 0),
                               "last_429": (prev or {}).get("last_429"), "key": cache.fingerprint(api_key)})
        except OSError:
            pass
        try:
            info = request_access_token(api_key, secret=secret, totp=totp_fn() if totp_fn else None,
                                        session=session)
        except (requests.RequestException, OSError, RuntimeError) as e:  # HTTP error, network, or no token in reply
            raise _record_token_failure(cache, api_key, e, now, prev) from None
        except BaseException:
            cache.clear_block()
            raise
        expires = next_token_expiry(now)
        reported = _parse_expiry(info.get("expiry"))
        if reported is not None and reported > now:
            expires = min(expires, reported)
        cache.put(api_key, info["token"], expires)
        cache.clear_block()
        return info["token"]


# --------------------------------------------------------------------------- #
# Tick sizes
# --------------------------------------------------------------------------- #
class InstrumentTicks:
    """Tick size per symbol from Groww's public instrument CSV (no credentials needed).

    The CSV is downloaded at most once a day into ``cache_dir`` and only the first time a
    tick size is actually needed (i.e. when a live order is priced).
    """

    def __init__(self, cache_dir: Path, *, session: requests.Session | None = None,
                 url: str = INSTRUMENT_CSV_URL, max_age_s: int = 20 * 3600):
        self.path = Path(cache_dir) / "groww_instruments.csv"
        self.session = session or requests.Session()
        self.url = url
        self.max_age_s = max_age_s
        self._ticks: dict[tuple[str, str], float] | None = None

    def _load(self) -> dict[tuple[str, str], float]:
        if self._ticks is not None:
            return self._ticks
        fresh = self.path.exists() and time.time() - self.path.stat().st_mtime < self.max_age_s
        if not fresh:
            resp = self.session.get(self.url, timeout=60)
            resp.raise_for_status()
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(resp.text, encoding="utf-8")
        ticks: dict[tuple[str, str], float] = {}
        with self.path.open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if (row.get("segment") or "").upper() != SEGMENT:
                    continue
                try:
                    ticks[(row["exchange"].upper(), row["trading_symbol"].upper())] = float(row["tick_size"])
                except (KeyError, TypeError, ValueError):
                    continue
        self._ticks = ticks
        return ticks

    def tick_size(self, symbol: str, exchange: str = "NSE") -> float | None:
        try:
            return self._load().get((exchange.upper(), symbol.upper()))
        except Exception as e:  # noqa: BLE001 - fall back to the default tick
            log.warning("Groww instrument list unavailable (%s); using tick %.2f", e, DEFAULT_TICK)
            return None


# --------------------------------------------------------------------------- #
# Broker
# --------------------------------------------------------------------------- #
class GrowwBroker:
    name = "groww"

    def __init__(self, access_token: str, *, live_orders: bool = False,
                 exchange: str = "NSE", product: str = "CNC",
                 session: requests.Session | None = None, timeout: float = 30.0,
                 base_url: str = BASE_URL, price_fallback: Any | None = None,
                 max_slippage_pct: float = 0.5,
                 tick_size_fn: Callable[[str], float | None] | None = None,
                 sleep: Callable[[float], None] = time.sleep,
                 confirm_backoff: tuple[float, ...] = (0.5, 1.0, 2.0, 3.0),
                 allowed_ip: str | None = None, ip_fn: Callable[[], str] | None = None,
                 ip_cache_s: float = 600.0, clock: Callable[[], float] = time.time):
        self.token = access_token
        # SEBI 2026: Groww accepts API orders only from the registered IP. With allowed_ip set,
        # every order-changing call first checks the public IP (through this client's own
        # session, so through the same proxy Groww sees) and refuses from any other address.
        self.allowed_ip = (allowed_ip or "").strip() or None
        self.ip_fn = ip_fn
        self.ip_cache_s = ip_cache_s
        self.clock = clock
        self._ip_seen: tuple[float, str] | None = None
        self.live_orders = live_orders
        # Called as price_fallback(symbol) when Groww's Live Data API is unavailable
        # (e.g. the Free Trial plan) or returns nothing for a symbol.
        self.price_fallback = price_fallback
        self.exchange = exchange
        self.product = product
        self.session = session or requests.Session()
        self.timeout = timeout
        self.base_url = base_url.rstrip("/")
        self.max_slippage_pct = float(max_slippage_pct)
        self.tick_size_fn = tick_size_fn
        self.sleep = sleep
        self.confirm_backoff = confirm_backoff

    def _req(self, method: str, path: str, **kw: Any) -> dict[str, Any]:
        resp = self.session.request(method, f"{self.base_url}/{path.lstrip('/')}",
                                    headers=_headers(self.token), timeout=self.timeout, **kw)
        data = resp.json() if resp.content else {}
        if isinstance(data, dict) and data.get("status") == "FAILURE":
            err = data.get("error") or {}
            raise RuntimeError(f"Groww {err.get('code')}: {err.get('message')}")
        resp.raise_for_status()
        return data.get("payload", data) if isinstance(data, dict) else data

    def _require_live(self, what: str) -> None:
        if not self.live_orders:
            raise LiveOrdersDisabled(
                f"Groww live orders are disabled; refusing to {what} "
                "(set GROWW_LIVE_ORDERS=true to place real orders)."
            )
        if self.allowed_ip:
            ip = self.current_ip()
            if ip != self.allowed_ip:
                raise WrongIP(f"refusing to {what}: this machine's public IP is {ip}, but the IP registered "
                              f"with Groww is {self.allowed_ip} (GROWW_ALLOWED_IP). Orders from any other "
                              "IP are rejected under SEBI's rules.")

    def current_ip(self) -> str:
        """The public IP Groww sees, cached for a few minutes. Raises WrongIP when it can't be found."""
        now = self.clock()
        if self._ip_seen and now - self._ip_seen[0] < self.ip_cache_s:
            return self._ip_seen[1]
        try:
            if self.ip_fn is not None:
                ip = self.ip_fn()
            else:
                from .netcheck import public_ip
                ip = public_ip(self.session)
        except Exception as e:  # noqa: BLE001
            raise WrongIP(f"could not confirm this machine's public IP, so no live order was sent ({e})") from e
        self._ip_seen = (now, ip)
        return ip

    # -- read ----------------------------------------------------------------
    def holdings(self) -> list[dict[str, Any]]:
        return list(self._req("GET", "holdings/user").get("holdings", []))

    def positions(self) -> list[Position]:
        out = []
        rows = self.holdings()
        prices = self.ltp_many([h["trading_symbol"] for h in rows]) if rows else {}
        for h in rows:
            qty = float(h.get("quantity") or 0)
            if qty == 0:
                continue
            sym = h["trading_symbol"]
            out.append(Position(symbol=sym, qty=qty, avg_entry_price=float(h.get("average_price") or 0),
                                current_price=prices.get(sym), sellable_qty=sellable_quantity(h)))
        return out

    def sellable_qty(self, symbol: str) -> float:
        symbol = symbol.upper()
        for h in self.holdings():
            if str(h.get("trading_symbol", "")).upper() == symbol:
                return sellable_quantity(h)
        return 0.0

    def account(self) -> Account:
        margin = self._req("GET", "margins/detail/user")
        cash = float(margin.get("clear_cash") or margin.get("available_cash")
                     or margin.get("cash_balance") or 0)
        holdings_value = sum((p.market_value or p.qty * p.avg_entry_price) for p in self.positions())
        return Account(cash=round(cash, 2), equity=round(cash + holdings_value, 2),
                       currency="INR", paper=not self.live_orders)

    def ltp_many(self, symbols: list[str]) -> dict[str, float]:
        out: dict[str, float] = {}
        for i in range(0, len(symbols), 50):
            chunk = symbols[i:i + 50]
            keys = [f"{self.exchange}_{s.upper()}" for s in chunk]
            try:
                payload = self._req("GET", "live-data/ltp",
                                    params={"segment": SEGMENT, "exchange_symbols": ",".join(keys)})
            except Exception:  # noqa: BLE001 - plan without Live Data, outage, ...
                if self.price_fallback is None:
                    raise
                payload = {}
            for s, k in zip(chunk, keys):
                v = payload.get(k)
                if v is not None:
                    out[s.upper()] = float(v)
                elif self.price_fallback is not None:
                    try:
                        out[s.upper()] = float(self.price_fallback(s))
                    except Exception:  # noqa: BLE001
                        pass
        return out

    def latest_price(self, symbol: str) -> float:
        symbol = symbol.upper().replace(f"{self.exchange}_", "")
        prices = self.ltp_many([symbol])
        if symbol not in prices:
            raise LookupError(f"Groww returned no LTP for {self.exchange}_{symbol}")
        return prices[symbol]

    def is_listed(self, symbol: str) -> bool:
        """False when the instrument list is known and lacks the symbol (unlisted shares,
        some bonds): no order or GTT can be placed for those."""
        if self.tick_size_fn is None:
            return True
        try:
            return bool(self.tick_size_fn(symbol.upper()))
        except Exception:  # noqa: BLE001 - list unavailable: don't block on it
            return True

    def tick_size(self, symbol: str) -> float:
        tick = None
        if self.tick_size_fn is not None:
            try:
                tick = self.tick_size_fn(symbol.upper())
            except Exception:  # noqa: BLE001
                tick = None
        return float(tick) if tick and tick > 0 else DEFAULT_TICK

    # -- order status (read-only) ----------------------------------------------
    def order_status(self, groww_order_id: str) -> dict[str, Any]:
        return self._req("GET", f"order/status/{groww_order_id}", params={"segment": SEGMENT})

    def order_status_by_reference(self, reference_id: str) -> dict[str, Any]:
        return self._req("GET", f"order/status/reference/{reference_id}", params={"segment": SEGMENT})

    def order_detail(self, groww_order_id: str) -> dict[str, Any]:
        return self._req("GET", f"order/detail/{groww_order_id}", params={"segment": SEGMENT})

    def confirm_order(self, groww_order_id: str, *, tries: int | None = None) -> dict[str, Any]:
        """Poll the order status with a short backoff until it is filled or failed.

        Returns groww_order_id, order_status, filled_quantity, average_fill_price,
        remark and ``status`` = "filled" | "failed" | "open". An unfilled DAY limit
        order simply stays "open"; ``orders --refresh`` re-checks it later.
        """
        waits = list(self.confirm_backoff)
        tries = len(waits) if tries is None else max(1, tries)
        st: dict[str, Any] = {}
        for i in range(tries):
            if i < len(waits) and waits[i] > 0:
                self.sleep(waits[i])
            try:
                st = self.order_status(groww_order_id)
            except Exception as e:  # noqa: BLE001 - keep polling, report at the end
                st = {"error": str(e)}
                continue
            if classify_status(st.get("order_status")) != "open":
                break
        out: dict[str, Any] = {
            "groww_order_id": groww_order_id,
            "order_status": st.get("order_status"),
            "filled_quantity": float(st.get("filled_quantity") or 0),
            "average_fill_price": None,
            "remark": st.get("remark") or st.get("error"),
            "status": classify_status(st.get("order_status")),
            "checked_at": datetime.now(IST).isoformat(timespec="seconds"),
        }
        if out["filled_quantity"] > 0 or out["status"] == "filled":
            try:
                d = self.order_detail(groww_order_id)
                if d.get("average_fill_price") is not None:
                    out["average_fill_price"] = float(d["average_fill_price"])
                if d.get("filled_quantity") is not None:
                    out["filled_quantity"] = float(d["filled_quantity"])
            except Exception as e:  # noqa: BLE001
                log.warning("order detail for %s unavailable: %s", groww_order_id, e)
        return out

    # -- write: orders -------------------------------------------------------
    def submit_order(self, symbol: str, side: str, notional: float | None = None,
                     qty: float | None = None, *, order_type: str = "LIMIT",
                     reference_id: str | None = None, confirm: bool = True) -> dict[str, Any]:
        side = side.lower()
        refuse_bse_only(symbol)
        if side not in {"buy", "sell"}:
            raise ValueError("side must be 'buy' or 'sell'")
        if (notional is None) == (qty is None):
            raise ValueError("pass exactly one of notional or qty")
        self._require_live("place an order")  # before any network call
        order_type = order_type.upper()
        if order_type not in {"LIMIT", "MARKET"}:
            raise ValueError("order_type must be LIMIT or MARKET")
        symbol = symbol.upper()
        ltp = self.latest_price(symbol)
        if qty is None:
            qty = math.floor(float(notional) / ltp)
        qty = int(qty)
        if qty < 1:
            raise ValueError(f"{symbol}: amount buys fewer than one whole share")
        if side == "sell":
            free = self.sellable_qty(symbol)
            if qty > free + 1e-9:
                raise ValueError(f"Cannot sell {qty} {symbol}: only {free:g} free shares "
                                 "(pledged, locked and unsettled shares are excluded)")
        tick = self.tick_size(symbol)
        price = limit_price(ltp, side, self.max_slippage_pct, tick) if order_type == "LIMIT" else 0
        ref = reference_id or make_reference_id()
        if not valid_reference_id(ref):
            raise ValueError("order_reference_id must be 8-20 alphanumerics with at most two hyphens")
        body = {
            "trading_symbol": symbol, "quantity": qty, "price": price, "trigger_price": None,
            "validity": "DAY", "exchange": self.exchange, "segment": SEGMENT,
            "product": self.product, "order_type": order_type,
            "transaction_type": side.upper(), "order_reference_id": ref,
        }
        payload = self._place(body)
        order: dict[str, Any] = {
            "id": payload.get("groww_order_id"), "groww_order_id": payload.get("groww_order_id"),
            "order_reference_id": payload.get("order_reference_id") or ref,
            "symbol": symbol, "side": side, "qty": qty, "order_type": order_type,
            "limit_price": price or None, "ltp": ltp, "tick_size": tick,
            "order_status": payload.get("order_status"), "remark": payload.get("remark"),
            "filled_quantity": 0.0, "average_fill_price": None,
            "status": classify_status(payload.get("order_status")),
            "placed_at": datetime.now(IST).isoformat(timespec="seconds"),
            "broker": "groww", "live": True, "exchange": self.exchange, "product": self.product,
        }
        if confirm and order["groww_order_id"]:
            order.update(self.confirm_order(order["groww_order_id"]))
        return order

    def _place(self, body: dict[str, Any]) -> dict[str, Any]:
        """POST order/create; on a network failure look the order up by its reference
        before retrying once with the *same* reference, so it cannot be placed twice."""
        try:
            return self._req("POST", "order/create", json=body)
        except (requests.ConnectionError, requests.Timeout) as e:
            ref = body["order_reference_id"]
            log.warning("order/create for %s failed (%s); checking reference %s", body["trading_symbol"], e, ref)
            try:
                found = self.order_status_by_reference(ref)
                if found.get("groww_order_id"):
                    return found
            except Exception:  # noqa: BLE001 - not found / still unreachable
                pass
            return self._req("POST", "order/create", json=body)

    def cancel_order(self, groww_order_id: str) -> dict[str, Any]:
        self._require_live("cancel an order")
        return self._req("POST", "order/cancel", json={"segment": SEGMENT, "groww_order_id": groww_order_id})

    # -- write: GTT smart orders (stop-loss) -----------------------------------
    def gtt_stop_prices(self, symbol: str, stop: float) -> tuple[float, float]:
        """(trigger, limit) for a stop at ``stop``: limit sits MAX_SLIPPAGE below it."""
        tick = self.tick_size(symbol)
        trigger = round_to_tick(stop, tick, "down")
        limit = min(trigger, limit_price(trigger, "sell", self.max_slippage_pct, tick))
        return trigger, limit

    def create_gtt_stop(self, symbol: str, qty: int, trigger: float, limit: float,
                        reference_id: str | None = None) -> dict[str, Any]:
        refuse_bse_only(symbol)
        self._require_live("create a GTT order")
        ref =reference_id or make_reference_id("SL")
        body = {
            "reference_id": ref, "smart_order_type": "GTT", "segment": SEGMENT,
            "trading_symbol": symbol.upper(), "quantity": int(qty),
            "trigger_price": _money_str(trigger), "trigger_direction": "DOWN",
            "order": {"order_type": "LIMIT", "price": _money_str(limit), "transaction_type": "SELL"},
            "product_type": self.product, "exchange": self.exchange, "duration": "DAY",
        }
        out = self._req("POST", "order-advance/create", json=body)
        return {**out, "reference_id": ref}

    def modify_gtt_stop(self, smart_order_id: str, qty: int, trigger: float, limit: float) -> dict[str, Any]:
        self._require_live("modify a GTT order")
        body = {
            "smart_order_type": "GTT", "segment": SEGMENT, "quantity": int(qty),
            "trigger_price": _money_str(trigger), "trigger_direction": "DOWN",
            "order": {"order_type": "LIMIT", "price": _money_str(limit), "transaction_type": "SELL"},
        }
        return self._req("PUT", f"order-advance/modify/{smart_order_id}", json=body)

    def cancel_gtt(self, smart_order_id: str) -> dict[str, Any]:
        self._require_live("cancel a GTT order")
        return self._req("POST", f"order-advance/cancel/{SEGMENT}/GTT/{smart_order_id}")

    def get_gtt(self, smart_order_id: str) -> dict[str, Any]:
        return self._req("GET", f"order-advance/status/{SEGMENT}/GTT/internal/{smart_order_id}")

