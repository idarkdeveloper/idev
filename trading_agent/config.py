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
    # Market data
    quiver_api_key: str | None
    # Brokerage
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

    @property
    def use_alpaca(self) -> bool:
        return bool(self.alpaca_key_id and self.alpaca_secret)


def load_settings(dotenv: Path | None = Path(".env")) -> Settings:
    if dotenv is not None:
        _load_dotenv(dotenv)
    env = os.environ.get
    return Settings(
        anthropic_api_key=env("ANTHROPIC_API_KEY") or None,
        claude_model=env("CLAUDE_MODEL") or "claude-opus-5-5",
        quiver_api_key=env("QUIVER_API_KEY") or None,
        alpaca_key_id=env("ALPACA_API_KEY_ID") or None,
        alpaca_secret=env("ALPACA_API_SECRET_KEY") or None,
        alpaca_base_url=env("ALPACA_BASE_URL") or "https://paper-api.alpaca.markets",
        watch_investor=env("WATCH_INVESTOR") or "Nancy Pelosi",
        watch_source=(env("WATCH_SOURCE") or "congress").lower(),
        paper_starting_cash=float(env("PAPER_STARTING_CASH") or 80000),
        auto_trade=_bool(env("AUTO_TRADE"), False),
        resend_api_key=env("RESEND_API_KEY") or None,
        notify_email_to=env("NOTIFY_EMAIL_TO") or None,
        notify_email_from=env("NOTIFY_EMAIL_FROM") or "Trading Agent <onboarding@resend.dev>",
        notify_webhook_url=env("NOTIFY_WEBHOOK_URL") or None,
        state_dir=Path(env("STATE_DIR") or "state"),
    )
