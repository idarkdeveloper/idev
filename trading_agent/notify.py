"""Deliver recommendations: console always, plus email (Resend) and/or a webhook."""

from __future__ import annotations

import base64
import hashlib
import logging
import re
from typing import Any

import requests

log = logging.getLogger(__name__)
_WARNED_INLINE = False   # the "Resend refused the inline images" warning is logged once per process


def _mentions(text: str) -> str:
    """Break Slack/Discord mentions and markup openers, keeping line breaks."""
    for bad in ("<!", "<@", "<#"):
        text = text.replace(bad, bad[0] + " " + bad[1])
    return re.sub(r"@(everyone|here|channel)", lambda m: "@ " + m.group(1), text)


def clean_text(text: object, limit: int = 200) -> str:
    """Third-party or model text going into a subject or webhook line: one line, no Slack/Discord mentions or
    markup openers (<! <@ <# @everyone @here @channel), capped in length."""
    t = " ".join(str(text).split())
    for bad in ("<!", "<@", "<#"):
        t = t.replace(bad, bad[0] + " " + bad[1])
    t = re.sub(r"@(everyone|here|channel)", lambda m: "@ " + m.group(1), t)
    return t if len(t) <= limit else t[:limit - 3].rstrip() + "..."


def _key(logical: str | None, variant: str) -> str | None:
    """Idempotency key for one variant of a logical send: sha256 of "<logical>:<variant>"."""
    return hashlib.sha256(f"{logical}:{variant}".encode()).hexdigest() if logical else None


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

    def _post_email(self, subject: str, body: str, html: str | None, images: list[dict[str, Any]] | None,
                    key: str | None = None) -> None:
        payload: dict[str, Any] = {"from": self.email_from, "to": [self.email_to], "subject": subject, "text": body,
                                   **({"html": html} if html else {})}
        if images:   # inline pictures: <img src="cid:NAME"> in the HTML, content_id NAME here
            payload["attachments"] = [
                {"filename": i["filename"], "content": base64.b64encode(i["content"]).decode("ascii"),
                 "content_type": i.get("content_type", "image/png"), "content_id": i["cid"]} for i in images]
        headers = {"Authorization": f"Bearer {self.resend_api_key}"}
        if key:   # the same logical send always has the same key, so a retry after a timeout cannot make a second email
            headers["Idempotency-Key"] = key
        r = self.session.post("https://api.resend.com/emails", headers=headers, json=payload, timeout=30)
        r.raise_for_status()

    def send(self, subject: str, body: str, html: str | None = None,
             images: list[dict[str, Any]] | None = None, idempotency_key: str | None = None) -> list[str]:
        """``images``: [{"cid", "filename", "content" (bytes)}] shown inline in the HTML part through Resend's
        attachments. If Resend refuses them, the email goes again without the pictures (their <img> tags removed);
        webhooks get the text only. ``idempotency_key`` names the logical send (for example "evening:2026-10-12"); the
        Resend key is sha256 of it plus "img" or "plain", so a retried send reuses it. Without one no key is sent."""
        delivered = ["console"]
        print(f"\n=== {subject} ===\n{body}\n")
        if "email" in self.channels:
            try:
                try:
                    self._post_email(subject, body, html, images, _key(idempotency_key, "img" if images else "plain"))
                except requests.RequestException as e:
                    status = getattr(getattr(e, "response", None), "status_code", None)
                    if not images or status not in (400, 422):
                        raise   # a timeout or connection error may have been delivered: never resend; 401/429/5xx as before
                    global _WARNED_INLINE
                    if not _WARNED_INLINE:
                        _WARNED_INLINE = True
                        log.warning("email with inline images was refused (HTTP %s); sending without the images", status)
                    plain = re.sub(r"<img\b[^>]*\bsrc=\"cid:[^\"]*\"[^>]*>", "", html) if html else html
                    plain_text = re.sub(r"(?m)^\[Chart: .*\]\n", "", body)   # no mention of pictures that are not there
                    self._post_email(subject, plain_text, plain, None, _key(idempotency_key, "plain"))
                delivered.append("email")
            except requests.RequestException as e:
                log.warning("email delivery failed: %s", e)
        if "webhook" in self.channels:
            try:
                r = self.session.post(self.webhook_url, json={"text": _mentions(f"*{subject}*\n{body}")},
                                      timeout=30)
                r.raise_for_status()
                delivered.append("webhook")
            except requests.RequestException as e:
                log.warning("webhook delivery failed: %s", e)
        self.sent.append({"subject": subject, "body": body, "delivered": delivered})
        return delivered
