"""Environment-driven configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader (no extra dependency). Existing env vars win."""
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    # Claude
    anthropic_api_key: str | None
    claude_model: str
    # Market
    market: str  # "in" (NSE + Groww) or "us" (QuiverQuant + Alpaca)
    data_source: str  # "nse" | "quiver"
    broker: str  # "local" | "groww" | "alpaca"
    # Market data
    quiver_api_key: str | None
    # Brokerage: Groww (India)
    groww_access_token: str | None
    groww_api_key: str | None
    groww_api_secret: str | None
    groww_totp_secret: str | None
    groww_live_orders: bool
    groww_exchange: str
    # Brokerage: Alpaca paper (US)
    alpaca_key_id: str | None
    alpaca_secret: str | None
    alpaca_base_url: str
    # Strategy
    watch_investor: str
    watch_source: str
    paper_starting_cash: float
    auto_trade: bool
    # Notifications
    resend_api_key: str | None
    notify_email_to: str | None
    notify_email_from: str
    notify_webhook_url: str | None
    # Paths
    state_dir: Path = field(default_factory=lambda: Path("state"))
    # Live-order safety (Groww)
    max_slippage_pct: float = 0.5  # limit price = LTP +/- this percent
    groww_gtt_stops: bool = False  # keep a GTT stop-loss at Groww per live holding
    groww_allowed_ip: str | None = None  # the static IP registered with Groww (SEBI 2026); live orders refuse elsewhere
    groww_proxy_url: str | None = None  # send Groww calls through this fixed-IP proxy (e.g. from GitHub Actions)
    # Needed when the API key is not scoped to one workspace (the API then asks for it).
    anthropic_workspace_id: str | None = None

    @property
    def use_alpaca(self) -> bool:
        return self.broker == "alpaca"

    @property
    def use_groww(self) -> bool:
        return self.broker == "groww"

    @property
    def has_groww_credentials(self) -> bool:
        return bool(self.groww_access_token or (self.groww_api_key and
                                                (self.groww_api_secret or self.groww_totp_secret)))

    @property
    def currency(self) -> str:
        return "INR" if self.market == "in" else "USD"


def load_settings(dotenv: Path | None = Path(".env")) -> Settings:
    if dotenv is not None:
        _load_dotenv(dotenv)
    env = os.environ.get
    market = (env("MARKET") or "in").lower()
    if market not in {"in", "us"}:
        raise SystemExit(f"MARKET must be 'in' or 'us', got {market!r}")
    groww_token = env("GROWW_ACCESS_TOKEN") or None
    groww_key = env("GROWW_API_KEY") or None
    groww_secret = env("GROWW_API_SECRET") or None
    groww_totp = env("GROWW_TOTP_SECRET") or None
    alpaca_key = env("ALPACA_API_KEY_ID") or None
    alpaca_secret = env("ALPACA_API_SECRET_KEY") or None
    broker = (env("BROKER") or "").lower()
    if not broker:
        if groww_token or (groww_key and (groww_secret or groww_totp)):
            broker = "groww"
        elif alpaca_key and alpaca_secret:
            broker = "alpaca"
        else:
            broker = "local"
    if broker not in {"local", "groww", "alpaca"}:
        raise SystemExit(f"BROKER must be local, groww or alpaca, got {broker!r}")
    try:
        slippage = float(env("MAX_SLIPPAGE_PCT") or 0.5)
    except ValueError:
        raise SystemExit("MAX_SLIPPAGE_PCT must be a number (percent), e.g. 0.5") from None
    if not 0 < slippage <= 5:
        raise SystemExit(f"MAX_SLIPPAGE_PCT must be between 0 and 5 percent, got {slippage}")
    data_source = (env("DATA_SOURCE") or ("nse" if market == "in" else "quiver")).lower()
    default_investor = "ASHISH KACHOLIA" if market == "in" else "Nancy Pelosi"
    default_source = "deals" if data_source == "nse" else "congress"
    return Settings(
        anthropic_api_key=env("ANTHROPIC_API_KEY") or None,
        claude_model=env("CLAUDE_MODEL") or "claude-opus-5-5",
        market=market,
        data_source=data_source,
        broker=broker,
        quiver_api_key=env("QUIVER_API_KEY") or None,
        groww_access_token=groww_token,
        groww_api_key=groww_key,
        groww_api_secret=groww_secret,
        groww_totp_secret=groww_totp,
        groww_live_orders=_bool(env("GROWW_LIVE_ORDERS"), False),
        groww_exchange=(env("GROWW_EXCHANGE") or "NSE").upper(),
        alpaca_key_id=alpaca_key,
        alpaca_secret=alpaca_secret,
        alpaca_base_url=env("ALPACA_BASE_URL") or "https://paper-api.alpaca.markets",
        watch_investor=env("WATCH_INVESTOR") or default_investor,
        watch_source=(env("WATCH_SOURCE") or default_source).lower(),
        paper_starting_cash=float(env("PAPER_STARTING_CASH") or (500_000 if market == "in" else 80_000)),
        auto_trade=_bool(env("AUTO_TRADE"), False),
        resend_api_key=env("RESEND_API_KEY") or None,
        notify_email_to=env("NOTIFY_EMAIL_TO") or None,
        notify_email_from=env("NOTIFY_EMAIL_FROM") or "Trading Agent <onboarding@resend.dev>",
        notify_webhook_url=env("NOTIFY_WEBHOOK_URL") or None,
        state_dir=Path(env("STATE_DIR") or "state"),
        max_slippage_pct=slippage,
        groww_gtt_stops=_bool(env("GROWW_GTT_STOPS"), False),
        groww_allowed_ip=env("GROWW_ALLOWED_IP") or None,
        groww_proxy_url=env("GROWW_PROXY_URL") or None,
        anthropic_workspace_id=env("ANTHROPIC_WORKSPACE_ID") or None,
    )
