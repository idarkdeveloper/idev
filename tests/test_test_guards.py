"""The test suite's own guards (conftest.py): no credential in the environment, the repository's .env is never read,
and nothing reaches a non-loopback address. A test that could send a real email or Telegram message must fail here."""
import os
import socket
from pathlib import Path

import pytest

from trading_agent import config


def test_no_secret_is_in_the_environment():
    for k in ("RESEND_API_KEY", "NOTIFY_EMAIL_TO", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "GROWW_API_KEY",
              "HEARTBEAT_URL", "ANTHROPIC_API_KEY"):
        assert k not in os.environ, k


def test_a_dotenv_outside_the_test_folder_is_ignored_and_one_inside_is_read(tmp_path, monkeypatch):
    outside = Path(__file__).resolve().parents[1] / ".env"   # the repository's own .env (it may not exist here)
    config._load_dotenv(outside)
    assert "RESEND_API_KEY" not in os.environ
    mine = tmp_path / ".env"
    mine.write_text("TA_GUARD_PROBE=1\n", encoding="utf-8")
    monkeypatch.delenv("TA_GUARD_PROBE", raising=False)
    config._load_dotenv(mine)
    assert os.environ.get("TA_GUARD_PROBE") == "1"


def test_the_environment_is_restored_after_a_test_writes_it_directly():
    os.environ["TA_GUARD_LEAK"] = "x"       # what _load_dotenv does: no monkeypatch to undo it


def test_the_previous_tests_write_did_not_leak():
    assert "TA_GUARD_LEAK" not in os.environ


def test_network_is_blocked_but_loopback_works():
    with pytest.raises(OSError, match="blocked"):
        socket.getaddrinfo("api.resend.com", 443)
    with pytest.raises(OSError, match="blocked"):
        socket.create_connection(("1.1.1.1", 443), timeout=1)
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0)); srv.listen(1)
    c = socket.create_connection(srv.getsockname(), timeout=1)
    c.close(); srv.close()
