"""Twilio WhatsApp boundary; stdlib-only and disabled by default.

Reference: https://www.twilio.com/docs/usage/security
Reference: https://www.twilio.com/docs/messaging/api/message-resource
Only form webhooks are supported. Duplicate form keys fail closed. Use the official
Twilio SDK validator before production; this dependency-free PoC tests the published
signature vector. No raw media, credentials or message content is logged here.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass, field
import hashlib
import hmac
import json
import os
import re
from typing import Callable, Mapping, Protocol
import urllib.error
import urllib.parse
import urllib.request

ADDRESS = re.compile(r"whatsapp:\+[1-9][0-9]{6,14}\Z")
SID = re.compile(r"(?:SM|MM)[a-fA-F0-9]{32}\Z")
ACCOUNT_SID = re.compile(r"AC[a-fA-F0-9]{32}\Z")


class TransportDisabled(RuntimeError):
    pass


class DeliveryError(RuntimeError):
    """Definitely not accepted; safe error text, never provider payloads."""


class DeliveryUncertain(RuntimeError):
    """May have been accepted. Do not automatically retry: reconcile first."""


def whatsapp_address(value: str) -> str:
    if not isinstance(value, str) or not ADDRESS.fullmatch(value):
        raise ValueError("Use an E.164 WhatsApp address, e.g. whatsapp:+15551234567.")
    return value


def compute_signature(auth_token: str, url: str, params: Mapping[str, str]) -> str:
    """Published Twilio form-signing algorithm; include every received parameter."""
    signed = url + "".join(key + params[key] for key in sorted(params))
    digest = hmac.new(auth_token.encode(), signed.encode(), hashlib.sha1).digest()
    return base64.b64encode(digest).decode("ascii")


def validate_signature(auth_token: str, url: str, params: Mapping[str, str], signature: str) -> bool:
    if not auth_token or not isinstance(signature, str) or not signature.isascii() or not signature or len(signature) > 128:
        return False
    return hmac.compare_digest(compute_signature(auth_token, url, params), signature)


@dataclass(frozen=True)
class TwilioConfig:
    account_sid: str = ""
    auth_token: str = field(default="", repr=False)
    sender: str = ""
    webhook_url: str = ""
    allowed_recipients: frozenset[str] = field(default_factory=frozenset)
    send_enabled: bool = False
    content_sid: str = ""
    timeout_seconds: float = 10.0

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "TwilioConfig":
        env = os.environ if env is None else env
        return cls(
            account_sid=env.get("TWILIO_ACCOUNT_SID", ""),
            auth_token=env.get("TWILIO_AUTH_TOKEN", ""),
            sender=env.get("TWILIO_WHATSAPP_FROM", ""),
            webhook_url=env.get("TWILIO_WEBHOOK_URL", ""),
            allowed_recipients=frozenset(x.strip() for x in env.get("TWILIO_ALLOWED_RECIPIENTS", "").split(",") if x.strip()),
            send_enabled=env.get("ENABLE_TWILIO_SEND", "").lower() == "true",
            content_sid=env.get("TWILIO_CONTENT_SID", ""),
        )

    def validate_inbound(self) -> None:
        if not ACCOUNT_SID.fullmatch(self.account_sid) or not self.auth_token:
            raise ValueError("Twilio account SID and auth token are required.")
        whatsapp_address(self.sender)
        if not self.allowed_recipients:
            raise ValueError("Twilio tester allowlist must not be empty.")
        for recipient in self.allowed_recipients:
            whatsapp_address(recipient)
        url = urllib.parse.urlsplit(self.webhook_url)
        if url.scheme != "https" or not url.netloc or url.username or url.password or url.fragment:
            raise ValueError("TWILIO_WEBHOOK_URL must be an exact HTTPS URL without credentials or fragment.")
        if url.path != "/webhooks/twilio":
            raise ValueError("TWILIO_WEBHOOK_URL must use /webhooks/twilio.")


@dataclass(frozen=True)
class TwilioMedia:
    """Validated metadata only. An opt-in STT adapter owns authenticated download."""
    url: str
    content_type: str
    message_sid: str
    account_sid: str


class Transcriber(Protocol):
    def transcribe(self, media: TwilioMedia) -> str: ...


def validated_audio_media(params: Mapping[str, str], account_sid: str) -> TwilioMedia:
    if params.get("NumMedia") != "1":
        raise ValueError("Please send one voice note at a time, or type the message.")
    content_type = params.get("MediaContentType0", "").split(";", 1)[0].lower()
    if content_type not in {"audio/ogg", "audio/mpeg", "audio/mp4", "audio/amr", "audio/wav", "audio/x-wav", "audio/aac"}:
        raise ValueError("This demo supports audio notes only. Please type the message instead.")
    url = params.get("MediaUrl0", "")
    parsed = urllib.parse.urlsplit(url)
    sid = params.get("MessageSid", "")
    expected = rf"/2010-04-01/Accounts/{re.escape(account_sid)}/Messages/{re.escape(sid)}/Media/ME[a-fA-F0-9]{{32}}"
    if (parsed.scheme != "https" or parsed.netloc != "api.twilio.com" or parsed.query or parsed.fragment
            or not re.fullmatch(expected, parsed.path)):
        raise ValueError("I couldn't verify that voice-note attachment. Please type the message instead.")
    return TwilioMedia(url, content_type, sid, account_sid)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class TwilioTransport:
    """One send attempt only. SID means accepted by Twilio, not delivered.

    A caller-supplied session checker must verify an inbound WhatsApp message within
    the last 24 hours, OR TWILIO_CONTENT_SID must name an approved WhatsApp
    template accepting the reminder body in variable 1. Template approval, category
    and variable constraints must be verified in the Twilio account before use.
    idempotency_key is for caller/outbox bookkeeping, NOT a provider exactly-once
    guarantee. Reconcile an uncertain send manually before any retry.
    """
    def __init__(self, config: TwilioConfig, session_is_open: Callable[[str], bool] | None = None,
                 opener=None):
        self.config = config
        self.session_is_open = session_is_open
        self.opener = opener or urllib.request.build_opener(_NoRedirect())

    def send(self, recipient: str, body: str, idempotency_key: str | None = None) -> str:
        c = self.config
        if not c.send_enabled:
            raise TransportDisabled("Live WhatsApp sending is disabled; no network request was made.")
        if not ACCOUNT_SID.fullmatch(c.account_sid) or not c.auth_token:
            raise TransportDisabled("Twilio credentials are missing or invalid; no network request was made.")
        whatsapp_address(c.sender)
        whatsapp_address(recipient)
        if recipient not in c.allowed_recipients:
            raise DeliveryError("Recipient is not on the authorized tester allowlist.")
        if c.content_sid and not re.fullmatch(r"HX[a-fA-F0-9]{32}", c.content_sid):
            raise DeliveryError("TWILIO_CONTENT_SID is invalid; no network request was made.")
        if not c.content_sid and (self.session_is_open is None or not self.session_is_open(recipient)):
            raise DeliveryError("No verified open WhatsApp 24-hour session. Approved templates are not configured.")
        if not isinstance(body, str) or not body.strip() or len(body) > 1600:
            raise DeliveryError("WhatsApp message must contain 1–1600 characters.")
        fields = {"From": c.sender, "To": recipient}
        if c.content_sid:
            fields.update(ContentSid=c.content_sid, ContentVariables=json.dumps({"1": body}))
        else:
            fields["Body"] = body
        data = urllib.parse.urlencode(fields).encode()
        basic = base64.b64encode(f"{c.account_sid}:{c.auth_token}".encode()).decode()
        req = urllib.request.Request(
            f"https://api.twilio.com/2010-04-01/Accounts/{c.account_sid}/Messages.json",
            data=data, headers={"Authorization": f"Basic {basic}", "Content-Type": "application/x-www-form-urlencoded"},
            method="POST")
        try:
            with self.opener.open(req, timeout=c.timeout_seconds) as response:
                raw = response.read(65537)
                if len(raw) > 65536:
                    raise DeliveryUncertain("Twilio response was too large; reconcile before retrying.")
                payload = json.loads(raw)
                if not isinstance(payload, dict):
                    raise DeliveryUncertain("Twilio acceptance could not be verified; reconcile before retrying.")
                sid = payload.get("sid")
                if not isinstance(sid, str) or not SID.fullmatch(sid):
                    raise DeliveryUncertain("Twilio acceptance could not be verified; reconcile before retrying.")
                return sid
        except urllib.error.HTTPError as exc:
            if 400 <= exc.code < 500:
                raise DeliveryError(f"Twilio rejected the request (HTTP {exc.code}); no delivery confirmed.") from None
            raise DeliveryUncertain("Twilio returned an ambiguous response; reconcile before retrying.") from None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            raise DeliveryUncertain("Twilio request outcome is unknown; reconcile before retrying.") from None
