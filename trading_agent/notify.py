"""Deliver recommendations: console always, plus email (Resend) and/or a webhook."""

from __future__ import annotations

import logging
from typing import Any

import requests

log = logging.getLogger(__name__)


class Notifier:
    def __init__(self, resend_api_key: str | None = None, email_to: str | None = None,
                 email_from: str = "Trading Agent <onboarding@resend.dev>",
                 webhook_url: str | None = None, session: requests.Session | None = None):
        self.resend_api_key = resend_api_key
        self.email_to = email_to
        self.email_from = email_from
        self.webhook_url = webhook_url
        self.session = session or requests.Session()
        self.sent: list[dict[str, Any]] = []

    @property
    def channels(self) -> list[str]:
        out = ["console"]
        if self.resend_api_key and self.email_to:
            out.append("email")
        if self.webhook_url:
            out.append("webhook")
        return out

    def send(self, subject: str, body: str) -> list[str]:
        delivered = ["console"]
        print(f"\n=== {subject} ===\n{body}\n")
        if "email" in self.channels:
            try:
                r = self.session.post(
                    "https://api.resend.com/emails",
                    headers={"Authorization": f"Bearer {self.resend_api_key}"},
                    json={"from": self.email_from, "to": [self.email_to],
                          "subject": subject, "text": body},
                    timeout=30,
                )
                r.raise_for_status()
                delivered.append("email")
            except requests.RequestException as e:
                log.warning("email delivery failed: %s", e)
        if "webhook" in self.channels:
            try:
                r = self.session.post(self.webhook_url, json={"text": f"*{subject}*\n{body}"},
                                      timeout=30)
                r.raise_for_status()
                delivered.append("webhook")
            except requests.RequestException as e:
                log.warning("webhook delivery failed: %s", e)
        self.sent.append({"subject": subject, "body": body, "delivered": delivered})
        return delivered
