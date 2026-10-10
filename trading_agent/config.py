"""Environment-driven configuration."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader (no extra dependency). Existing env vars win."""
    if not path.exists():
        return
    data = path.read_bytes()
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:  # saved by an older writer or a Windows editor in the local code page
        text = data.decode("cp1252", errors="replace")
    for raw in text.splitlines():
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


MAX_INVESTORS = 10
DIGEST_WRITERS = ("auto", "ollama", "claude", "none")


def parse_hhmm(value: object) -> str:
    """"9:05" or "09:05" as "09:05"; ValueError with a plain message for anything that is not a time of day."""
    m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", str(value))
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise ValueError("enter a time as HH:MM, for example 09:00")
    return f"{int(m.group(1)):02d}:{m.group(2)}"


def parse_investors(raw: "str | list[str] | tuple[str, ...] | None") -> list[str]:
    """Followed-investor names from INVESTORS (comma separated) or a list (one name per line also works).

    Trimmed, de-duplicated ignoring case (the first spelling and the order are kept). Raises ValueError with a
    plain message when nothing is left or there are more than ``MAX_INVESTORS``.
    """
    if raw is None:
        items: list[str] = []
    elif isinstance(raw, str):
        items = [raw]
    else:
        items = [str(i) for i in raw]
    parts = [p for item in items for p in re.split(r"[,\r\n]+", item)]
    out: list[str] = []
    seen: set[str] = set()
    for part in parts:
        name = " ".join(part.split())
        if name and name.upper() not in seen:
            seen.add(name.upper())
            out.append(name)
    if not out:
        raise ValueError("name at least one investor to follow (INVESTORS is empty)")
    if len(out) > MAX_INVESTORS:
        raise ValueError(f"you can follow at most {MAX_INVESTORS} investors, got {len(out)}")
    return out


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
    # News headline tagging: auto = local Ollama when it is running, else untagged (never Claude implicitly)
    news_tagger: str = "auto"  # auto | ollama | claude | none
    ollama_url: str = "http://127.0.0.1:11434"
    ollama_model: str = "qwen2.5:3b"
    news_claude_model: str = "claude-haiku-4-5"
    # Everyone followed, from INVESTORS. Empty means "just watch_investor" (the single-name setting).
    watch_investors: list[str] = field(default_factory=list)
    # Daily emails (digest.py): a morning "today" brief and an evening "close" report, sent by the watch service.
    digest_enabled: bool = True  # still needs an email or webhook channel
    digest_morning_on: bool = True
    digest_evening_on: bool = True
    digest_morning: str = "09:00"  # IST, HH:MM
    digest_evening: str = "15:45"
    digest_universe: str = "NIFTYMIDCAP150"
    digest_top: int = 10
    digest_writer: str = "auto"  # auto (Ollama, then Claude) | ollama | claude | none
    digest_claude_model: str = "claude-sonnet-5-5"

    def __post_init__(self) -> None:
        self.watch_investors = list(self.watch_investors)  # never shared between copies of the settings

    @property
    def investors(self) -> list[str]:
        """The followed names, in order. ``watch_investor`` is the first of them."""
        return list(self.watch_investors) or [self.watch_investor]

    @investors.setter
    def investors(self, names: "list[str]") -> None:
        names = parse_investors(names)
        self.watch_investors, self.watch_investor = names, names[0]

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
    news_tagger = (env("NEWS_TAGGER") or "auto").lower()
    if news_tagger not in {"auto", "ollama", "claude", "none"}:
        raise SystemExit(f"NEWS_TAGGER must be auto, ollama, claude or none, got {news_tagger!r}")
    digest_writer = (env("DIGEST_WRITER") or "auto").lower()
    if digest_writer not in DIGEST_WRITERS:
        raise SystemExit(f"DIGEST_WRITER must be auto, ollama, claude or none, got {digest_writer!r}")
    try:
        digest_top = int(env("DIGEST_TOP") or 10)
        digest_morning = parse_hhmm(env("DIGEST_MORNING") or "09:00")
        digest_evening = parse_hhmm(env("DIGEST_EVENING") or "15:45")
    except ValueError as e:
        raise SystemExit(f"DIGEST_TOP must be a whole number and DIGEST_MORNING / DIGEST_EVENING a time (HH:MM): {e}") from None
    if not 1 <= digest_top <= 50:
        raise SystemExit(f"DIGEST_TOP must be between 1 and 50, got {digest_top}")
    data_source = (env("DATA_SOURCE") or ("nse" if market == "in" else "quiver")).lower()
    default_investor = "ASHISH KACHOLIA" if market == "in" else "Nancy Pelosi"
    raw_investors = (env("INVESTORS") or "").strip()
    try:
        investors = parse_investors(raw_investors) if raw_investors else []
    except ValueError as e:
        raise SystemExit(f"INVESTORS: {e}") from None
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
        watch_investor=investors[0] if investors else (env("WATCH_INVESTOR") or default_investor),
        watch_investors=investors,
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
        news_tagger=news_tagger,
        ollama_url=(env("OLLAMA_URL") or "http://127.0.0.1:11434").rstrip("/"),
        ollama_model=env("OLLAMA_MODEL") or "qwen2.5:3b",
        news_claude_model=env("NEWS_CLAUDE_MODEL") or "claude-haiku-4-5",
        digest_enabled=_bool(env("DIGEST_ENABLED"), True),
        digest_morning_on=_bool(env("DIGEST_MORNING_ON"), True),
        digest_evening_on=_bool(env("DIGEST_EVENING_ON"), True),
        digest_morning=digest_morning,
        digest_evening=digest_evening,
        digest_universe=(env("DIGEST_UNIVERSE") or "NIFTYMIDCAP150").strip().upper().replace(" ", ""),
        digest_top=digest_top,
        digest_writer=digest_writer,
        digest_claude_model=env("DIGEST_CLAUDE_MODEL") or "claude-sonnet-5-5",
    )
