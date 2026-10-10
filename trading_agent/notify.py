"""Deliver recommendations: console always, plus email (Resend) and/or a webhook."""

from __future__ import annotations

import base64
import hashlib
import html as _html
import json
import logging
import re
from email.utils import parseaddr
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


TELEGRAM_LIMIT = 4000   # Telegram allows 4096 characters per message; stay under it
_BOT_URL = re.compile(r"bot\d+:[A-Za-z0-9_-]+")


_SECRETS: set[str] = set()   # extra strings that must never reach a log line (the heartbeat URL and its path)
_FACTORY_INSTALLED = False


def add_log_secret(*values: str | None) -> None:
    for v in values:
        if v and len(v) >= 8:
            _SECRETS.add(v)


def _scrub(text: str) -> str:
    out = _BOT_URL.sub("bot***", text)
    for s in _SECRETS:
        out = out.replace(s, "***")
    return out


def install_log_redaction() -> None:
    """Every log record (any logger, any level, -v/DEBUG included) has ``bot<token>`` and registered secrets replaced
    before any handler sees it, and urllib3's request logging (which prints URLs) is held at WARNING."""
    global _FACTORY_INSTALLED
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    if _FACTORY_INSTALLED:
        return
    _FACTORY_INSTALLED = True
    old_fmt = logging.Formatter.formatException

    def format_exception(self: logging.Formatter, ei: Any) -> str:   # log.exception(...) text and tracebacks
        return _scrub(old_fmt(self, ei))
    logging.Formatter.formatException = format_exception  # type: ignore[method-assign]
    old = logging.getLogRecordFactory()

    def factory(*a: Any, **kw: Any) -> logging.LogRecord:
        rec = old(*a, **kw)
        try:
            msg = rec.getMessage()
            clean = _scrub(msg)
            if clean != msg:
                rec.msg, rec.args = clean, ()
        except Exception:  # noqa: BLE001 - logging must never fail because of the scrub
            pass
        return rec
    logging.setLogRecordFactory(factory)


def redact(text: object, secret: str | None = None) -> str:
    """``bot<token>`` becomes ``bot***`` (the Telegram bot token is part of every request URL, so a logged URL or
    requests exception would leak it); a known secret is removed wherever else it appears."""
    out = _BOT_URL.sub("bot***", str(text))
    return out.replace(secret, "***") if secret else out


def telegram_chunks(text: str, limit: int = TELEGRAM_LIMIT) -> list[str]:
    """``text`` escaped for Telegram's HTML mode and split into messages of at most ``limit`` characters, on line
    breaks where possible. Escaping happens before splitting but never inside an entity (a long line is cut in raw
    pieces first, small enough that their escaped form fits)."""
    pieces: list[str] = []
    for line in text.split("\n"):
        esc = _html.escape(line, quote=False)
        if len(esc) <= limit:
            pieces.append(esc)
        else:
            pieces += [_html.escape(line[i:i + 600], quote=False) for i in range(0, len(line), 600)]
    chunks: list[str] = []
    cur = ""
    for piece in pieces:
        if cur and len(cur) + 1 + len(piece) > limit:
            chunks.append(cur)
            cur = piece
        else:
            cur = f"{cur}\n{piece}" if cur else piece
    if cur.strip():
        chunks.append(cur)
    return chunks


def sender_domain(email_from: str | None) -> str:
    return (parseaddr(email_from or "")[1].rpartition("@")[2]).strip().lower()


def custom_domain(email_from: str | None) -> bool:
    """True when the sender is on the user's own verified domain, not Resend's shared test one (resend.dev)."""
    d = sender_domain(email_from)
    return bool(d) and d != "resend.dev" and not d.endswith(".resend.dev")


def _key(logical: str | None, variant: str) -> str | None:
    """Idempotency key for one variant of a logical send: sha256 of "<logical>:<variant>"."""
    return hashlib.sha256(f"{logical}:{variant}".encode()).hexdigest() if logical else None


class Notifier:
    def __init__(self, resend_api_key: str | None = None, email_to: str | None = None,
                 email_from: str = "Trading Agent <onboarding@resend.dev>",
                 webhook_url: str | None = None, session: requests.Session | None = None,
                 telegram_token: str | None = None, telegram_chat_id: str | None = None,
                 telegram_state_dir: Any | None = None):
        self.resend_api_key = resend_api_key
        self.email_to = email_to
        self.email_from = email_from
        self.webhook_url = webhook_url
        self.session = session or requests.Session()
        self.telegram_token = telegram_token
        self.telegram_chat_id = telegram_chat_id
        self.telegram_state_dir = telegram_state_dir   # state/telegram_sent: one marker per idempotency key
        add_log_secret(telegram_token)
        self.sent: list[dict[str, Any]] = []

    @property
    def channels(self) -> list[str]:
        out = ["console"]
        if self.resend_api_key and self.email_to:
            out.append("email")
        if self.webhook_url:
            out.append("webhook")
        if self.telegram_token and self.telegram_chat_id:
            out.append("telegram")
        return out

    def _post_email(self, subject: str, body: str, html: str | None, images: list[dict[str, Any]] | None,
                    key: str | None = None) -> None:
        payload: dict[str, Any] = {"from": self.email_from, "to": [self.email_to], "subject": subject, "text": body,
                                   **({"html": html} if html else {})}
        if images:   # inline pictures: <img src="cid:NAME"> in the HTML, content_id NAME here
            payload["attachments"] = [
                {"filename": i["filename"], "content": base64.b64encode(i["content"]).decode("ascii"),
                 "content_type": i.get("content_type", "image/png"), "content_id": i["cid"]} for i in images]
        if custom_domain(self.email_from):   # one-click unsubscribe header, only for a verified custom sender domain
            payload["headers"] = {"List-Unsubscribe": f"<mailto:{self.email_to}?subject=unsubscribe>",
                                  "List-Unsubscribe-Post": "List-Unsubscribe=One-Click"}
        headers = {"Authorization": f"Bearer {self.resend_api_key}"}
        if key:   # the same logical send always has the same key, so a retry after a timeout cannot make a second email
            headers["Idempotency-Key"] = key
        r = self.session.post("https://api.resend.com/emails", headers=headers, json=payload, timeout=30)
        r.raise_for_status()

    def send(self, subject: str, body: str, html: str | None = None,
             images: list[dict[str, Any]] | None = None, idempotency_key: str | None = None,
             telegram_text: str | None = None) -> list[str]:
        """``images``: [{"cid", "filename", "content" (bytes)}] shown inline in the HTML part through Resend's
        attachments. If Resend refuses them, the email goes again without the pictures (their <img> tags removed);
        webhooks get the text only. Telegram (when configured) gets ``telegram_text`` (default: the body) as plain text
        that is escaped here, and the images as one album; a Telegram error never affects the other channels. ``idempotency_key`` names the logical send (for example "evening:2026-10-12"); the
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
        if "telegram" in self.channels and not self._tg_done(idempotency_key):
            if self._telegram(subject, body if telegram_text is None else telegram_text, images):
                delivered.append("telegram")
                self._tg_mark(idempotency_key)
        self.sent.append({"subject": subject, "body": body, "delivered": delivered})
        return delivered

    # -- Telegram ---------------------------------------------------------------
    def _tg_path(self, key: str | None) -> Any:
        from pathlib import Path
        if not key or self.telegram_state_dir is None:
            return None
        return Path(self.telegram_state_dir) / re.sub(r"[^A-Za-z0-9._-]", "_", key)

    def _tg_done(self, key: str | None) -> bool:
        """True when Telegram already got this logical send (a retry of the email must not repeat it)."""
        p = self._tg_path(key)
        return bool(p and p.exists())

    def _tg_mark(self, key: str | None) -> None:
        p = self._tg_path(key)
        if p is not None:
            try:
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text("sent", encoding="utf-8")
            except OSError:
                pass

    def _tg(self, method: str, **kw: Any) -> None:
        r = self.session.post(f"https://api.telegram.org/bot{self.telegram_token}/{method}", timeout=30, **kw)
        r.raise_for_status()

    def _telegram(self, subject: str, text: str, images: list[dict[str, Any]] | None) -> bool:
        """Never raises and never logs the URL or the token: errors are redacted (``bot***``)."""
        ok = False
        try:
            msgs = telegram_chunks(f"{subject}\n{text}".strip())
            if msgs:
                first_line, _, rest = msgs[0].partition("\n")
                msgs[0] = f"<b>{first_line}</b>" + (f"\n{rest}" if rest else "")
            for m in msgs:
                self._tg("sendMessage", json={"chat_id": self.telegram_chat_id, "text": m, "parse_mode": "HTML",
                                              "disable_web_page_preview": True})
            ok = bool(msgs)
        except Exception as e:  # noqa: BLE001 - Telegram must never cost the email or the alert
            log.warning("telegram delivery failed: %s: %s", type(e).__name__, redact(e, self.telegram_token))
            return False
        if images:
            try:
                self._telegram_album(images[:10])
            except Exception as e:  # noqa: BLE001 - the text already went; the pictures are a bonus
                log.warning("telegram pictures not sent: %s: %s", type(e).__name__, redact(e, self.telegram_token))
        return ok

    def _telegram_album(self, images: list[dict[str, Any]]) -> None:
        if len(images) == 1:
            i = images[0]
            self._tg("sendPhoto", data={"chat_id": self.telegram_chat_id},
                     files={"photo": (i["filename"], i["content"], i.get("content_type", "image/png"))})
            return
        media = [{"type": "photo", "media": f"attach://p{n}"} for n, _ in enumerate(images)]
        files = {f"p{n}": (i["filename"], i["content"], i.get("content_type", "image/png")) for n, i in enumerate(images)}
        self._tg("sendMediaGroup", data={"chat_id": self.telegram_chat_id, "media": json.dumps(media)}, files=files)
