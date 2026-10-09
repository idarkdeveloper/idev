"""This machine's public IP address, as the internet (and Groww) sees it.

SEBI's 2026 rules for API trading make brokers accept orders only from IP addresses the
client registered. ``public_ip`` asks two independent echo services; the Groww client uses
it, through the same proxy as its own requests, to refuse live orders from any other IP.
"""

from __future__ import annotations

import ipaddress
from typing import Any

import requests

ECHO_URLS = ("https://api.ipify.org?format=json", "https://ifconfig.me/ip")


def _parse(resp: Any) -> str:
    text = (resp.text or "").strip()
    try:
        data = resp.json()
        if isinstance(data, dict) and data.get("ip"):
            text = str(data["ip"]).strip()
    except Exception:  # noqa: BLE001 - plain-text services
        pass
    return str(ipaddress.ip_address(text))  # raises ValueError on anything that isn't an IP


def public_ip(session: requests.Session | None = None, urls: tuple[str, ...] = ECHO_URLS,
              timeout: float = 10.0) -> str:
    """The public IP outbound requests come from. Raises RuntimeError if no service answers."""
    s = session or requests.Session()
    errors = []
    for url in urls:
        try:
            resp = s.get(url, timeout=timeout)
            resp.raise_for_status()
            return _parse(resp)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{url}: {e}")
    raise RuntimeError("could not find this machine's public IP (" + "; ".join(errors) + ")")
