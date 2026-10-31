"""Gupshup WhatsApp v2 ingress / current /wa API egress; disabled by default.

Authentication is an explicitly configured subscription custom secret header,
NOT an invented provider signature. See GUPSHUP.md for verified setup boundaries.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hmac
import json
import os
import re
import time
from typing import Callable, Mapping
from urllib.parse import parse_qsl, urlsplit, urlencode
import urllib.error
import urllib.request

from .transport import DeliveryError, DeliveryUncertain, TransportDisabled, _NoRedirect, whatsapp_address
from .speech import (AUDIO_EXTENSIONS, MAX_AUDIO_BYTES, PublicHTTPSClient, SpeechConfig,
                     SpeechError, _bounded_read, transcribe_audio)

SEND_URL = "https://api.gupshup.io/wa/api/v1/msg"
TEMPLATE_URL = "https://api.gupshup.io/wa/api/v1/template/msg"
PHONE = re.compile(r"[1-9][0-9]{6,14}\Z")
IDENTIFIER = re.compile(r"[A-Za-z0-9_.:+/=\-]{1,190}\Z")
UUID = re.compile(r"[a-fA-F0-9]{8}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{12}\Z")


@dataclass(frozen=True)
class GupshupConfig:
    api_key: str = field(default="", repr=False)
    app_name: str = ""
    app_id: str = ""
    source: str = ""
    allowed_recipients: frozenset[str] = field(default_factory=frozenset)
    send_enabled: bool = False
    webhook_secret: str = field(default="", repr=False)
    template_id: str = ""
    send_url: str = SEND_URL
    template_url: str = TEMPLATE_URL
    timeout_seconds: float = 10.0

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None):
        env = os.environ if env is None else env
        return cls(
            api_key=env.get("GUPSHUP_API_KEY", ""), app_name=env.get("GUPSHUP_APP_NAME", ""),
            app_id=env.get("GUPSHUP_APP_ID", ""), source=env.get("GUPSHUP_SOURCE", ""),
            allowed_recipients=frozenset(x.strip() for x in env.get("GUPSHUP_ALLOWED_RECIPIENTS", "").split(",") if x.strip()),
            send_enabled=env.get("ENABLE_GUPSHUP_SEND", "").lower() == "true",
            webhook_secret=env.get("GUPSHUP_WEBHOOK_SECRET", ""), template_id=env.get("GUPSHUP_TEMPLATE_ID", ""),
            send_url=env.get("GUPSHUP_SEND_URL", SEND_URL), template_url=env.get("GUPSHUP_TEMPLATE_URL", TEMPLATE_URL),
        )

    def validate_common(self):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", self.app_name):
            raise ValueError("GUPSHUP_APP_NAME must be the exact registered app name.")
        if not PHONE.fullmatch(self.source):
            raise ValueError("GUPSHUP_SOURCE must be a country-code phone number containing digits only.")
        if not self.allowed_recipients:
            raise ValueError("Gupshup authorized tester allowlist must not be empty.")
        for recipient in self.allowed_recipients:
            whatsapp_address(recipient)
        if self.send_url != SEND_URL or self.template_url != TEMPLATE_URL:
            raise ValueError("Only the verified current api.gupshup.io /wa send endpoints are permitted.")
        if not 1 <= self.timeout_seconds <= 30:
            raise ValueError("Gupshup timeout must be between 1 and 30 seconds.")

    def validate_inbound(self):
        self.validate_common()
        if len(self.webhook_secret) < 32 or not self.webhook_secret.isascii() or any(c.isspace() for c in self.webhook_secret):
            raise ValueError("GUPSHUP_WEBHOOK_SECRET must be a separate random ASCII secret of at least 32 characters.")
        if self.webhook_secret == self.api_key:
            raise ValueError("Webhook secret must differ from the outbound API key.")


def validate_webhook_secret(config: GupshupConfig, provided: str) -> bool:
    """Validate X-Gupshup-Webhook-Secret configured through subscription meta."""
    if len(config.webhook_secret) < 32 or not isinstance(provided, str) or not provided.isascii() or len(provided) > 4096:
        return False
    return hmac.compare_digest(config.webhook_secret.encode(), provided.encode())


@dataclass(frozen=True)
class GupshupMedia:
    url: str
    content_type: str
    message_id: str
    app_id: str
    app_name: str
    expires_at: float | None = None

    def to_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class IncomingGupshupMessage:
    user_id: str
    message_id: str
    kind: str
    text: str | None
    media: GupshupMedia | None
    received_at: str

    def to_dict(self):
        return asdict(self)


def _now_timestamp(now=None):
    if now is None:
        return time.time()
    if isinstance(now, datetime):
        if now.tzinfo is None:
            raise ValueError("Use a timezone-aware current time.")
        return now.timestamp()
    return float(now)


def validate_gupshup_media(config: GupshupConfig, media: GupshupMedia, now=None) -> GupshupMedia:
    """Scope supplied download URL to configured app; never attach the API key."""
    p = urlsplit(media.url)
    if (p.scheme != "https" or p.netloc != "filemanager.gupshup.io" or p.fragment
            or len(media.url) > 2048 or media.app_name != config.app_name
            or media.app_id != config.app_id or not IDENTIFIER.fullmatch(media.message_id)):
        raise ValueError("Gupshup audio attachment could not be verified.")
    current_prefix = f"/wa/{config.app_id}/wa/media/"
    legacy_prefix = f"/fm/wamedia/{config.app_name}/"
    if UUID.fullmatch(config.app_id) and p.path.startswith(current_prefix):
        if not UUID.fullmatch(p.path[len(current_prefix):]):
            raise ValueError("Invalid Gupshup media identifier.")
        if p.query and parse_qsl(p.query, keep_blank_values=True) not in [[("download", "false")], [("download", "true")]]:
            raise ValueError("Unsupported Gupshup media URL query.")
    elif p.path.startswith(legacy_prefix) and not p.query:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", p.path[len(legacy_prefix):]):
            raise ValueError("Invalid Gupshup media identifier.")
    else:
        raise ValueError("Gupshup media URL does not match the configured app.")
    content_type = media.content_type.split(";", 1)[0].lower().strip()
    if content_type not in AUDIO_EXTENSIONS:
        raise ValueError("This demo supports audio notes only.")
    if media.expires_at is not None and (not isinstance(media.expires_at, (float, int)) or media.expires_at <= _now_timestamp(now)):
        raise ValueError("Gupshup audio link has expired; please send the note again.")
    return GupshupMedia(media.url, content_type, media.message_id, media.app_id, media.app_name, media.expires_at)


def parse_inbound_event(config: GupshupConfig, event: dict, now=None) -> IncomingGupshupMessage | None:
    """Call ONLY after header authentication. Accept the verified v2 message schema."""
    config.validate_inbound()
    if not isinstance(event, dict) or event.get("app") != config.app_name or event.get("version") != 2:
        raise ValueError("Webhook app/version does not match this configured app.")
    if event.get("type") in {"message-event", "user-event", "system-event", "template-event"}:
        return None
    if event.get("type") != "message":
        raise ValueError("Unsupported Gupshup event type.")
    p = event.get("payload")
    if not isinstance(p, dict):
        raise ValueError("Invalid Gupshup message payload.")
    source, mid = p.get("source"), p.get("id")
    if not isinstance(source, str) or not PHONE.fullmatch(source):
        raise ValueError("Invalid Gupshup sender.")
    user_id = "whatsapp:+" + source
    if user_id not in config.allowed_recipients:
        raise ValueError("Sender is not an authorized tester.")
    sender = p.get("sender")
    if sender is not None and (not isinstance(sender, dict) or sender.get("phone") != source):
        raise ValueError("Gupshup sender identities do not match.")
    if not isinstance(mid, str) or not IDENTIFIER.fullmatch(mid):
        raise ValueError("Valid inbound message ID required.")
    stamp = event.get("timestamp")
    if isinstance(stamp, bool) or not isinstance(stamp, (int, float)):
        raise ValueError("Valid Gupshup millisecond timestamp required.")
    timestamp = stamp / 1000.0
    current = _now_timestamp(now)
    if not current - 7 * 86400 <= timestamp <= current + 300:
        raise ValueError("Gupshup event timestamp is too old or in the future.")
    received_at = datetime.fromtimestamp(timestamp, timezone.utc).isoformat()
    kind, data = p.get("type"), p.get("payload")
    if not isinstance(data, dict):
        raise ValueError("Invalid inner message payload.")
    if kind == "text":
        text = data.get("text")
        if not isinstance(text, str) or not text.strip() or len(text) > 4000:
            raise ValueError("Text must contain 1–4000 characters.")
        return IncomingGupshupMessage(user_id, mid, "text", text.strip(), None, received_at)
    if kind == "audio":
        url, content_type = data.get("url"), data.get("contentType", data.get("content-type"))
        expiry = data.get("urlExpiry")
        if not isinstance(url, str) or not isinstance(content_type, str) or (expiry is not None and (isinstance(expiry, bool) or not isinstance(expiry, (int, float)))):
            raise ValueError("Invalid Gupshup audio metadata.")
        media = GupshupMedia(url, content_type, mid, config.app_id, config.app_name, expiry / 1000.0 if expiry is not None else None)
        media = validate_gupshup_media(config, media, now)
        return IncomingGupshupMessage(user_id, mid, "audio", None, media, received_at)
    return IncomingGupshupMessage(user_id, mid, "unsupported", None, None, received_at)


class GupshupTransport:
    """One API attempt. Submitted ID is provider acceptance, not delivery.

    A proven open-session callback enables session text. Otherwise only an approved
    configured template with one body parameter is permitted. No automatic retries.
    """
    def __init__(self, config: GupshupConfig, session_is_open: Callable[[str], bool] | None = None, opener=None):
        self.config, self.session_is_open = config, session_is_open
        self.opener = opener or urllib.request.build_opener(_NoRedirect())

    def send(self, recipient: str, body: str, idempotency_key=None) -> str:
        c = self.config
        if not c.send_enabled:
            raise TransportDisabled("Live Gupshup sending is disabled; no network request was made.")
        try:
            c.validate_common()
        except ValueError:
            raise TransportDisabled("Gupshup configuration is incomplete or invalid.") from None
        if not c.api_key or any(ord(char) < 33 or ord(char) > 126 for char in c.api_key):
            raise TransportDisabled("Gupshup API key is missing or invalid.")
        whatsapp_address(recipient)
        if recipient not in c.allowed_recipients:
            raise DeliveryError("Recipient is not on the authorized tester allowlist.")
        if not isinstance(body, str) or not body.strip() or len(body.encode("utf-16-le")) // 2 > 4096:
            raise DeliveryError("Gupshup text must contain 1–4096 characters.")
        fields = {"channel": "whatsapp", "source": c.source, "destination": recipient[len("whatsapp:+"):], "src.name": c.app_name}
        session_open = self.session_is_open is not None and self.session_is_open(recipient)
        if session_open:
            url = c.send_url
            fields["message"] = json.dumps({"type": "text", "text": body}, ensure_ascii=False)
        elif c.template_id:
            if not UUID.fullmatch(c.template_id):
                raise DeliveryError("Gupshup template ID is invalid.")
            if "\n" in body or "\r" in body or "\t" in body:
                raise DeliveryError("The configured template parameter must be one line; no text was silently modified.")
            url = c.template_url
            fields["template"] = json.dumps({"id": c.template_id, "params": [body]}, ensure_ascii=False)
        else:
            raise DeliveryError("No verified open WhatsApp session or approved Gupshup template is configured.")
        request = urllib.request.Request(url, data=urlencode(fields).encode(), headers={"apikey": c.api_key, "Content-Type": "application/x-www-form-urlencoded"}, method="POST")
        try:
            with self.opener.open(request, timeout=c.timeout_seconds) as response:
                raw = response.read(65537)
                if len(raw) > 65536:
                    raise DeliveryUncertain("Gupshup response exceeded the limit; reconcile before retrying.")
                result = json.loads(raw)
                mid = result.get("messageId") if isinstance(result, dict) else None
                if not isinstance(result, dict) or result.get("status") != "submitted" or not isinstance(mid, str) or not IDENTIFIER.fullmatch(mid):
                    raise DeliveryUncertain("Gupshup acceptance could not be verified; reconcile before retrying.")
                return mid
        except urllib.error.HTTPError as exc:
            if 400 <= exc.code < 500:
                raise DeliveryError(f"Gupshup rejected the request (HTTP {exc.code}); no delivery confirmed.") from None
            raise DeliveryUncertain("Gupshup returned an ambiguous response; reconcile before retrying.") from None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            raise DeliveryUncertain("Gupshup request outcome is unknown; reconcile before retrying.") from None


class GupshupTranscriber:
    def __init__(self, config: GupshupConfig, speech: SpeechConfig, client=None):
        speech.validate()
        config.validate_inbound()
        self.config, self.speech = config, speech
        self.client = client or PublicHTTPSClient(allow_query=True)

    def transcribe(self, media: GupshupMedia | dict) -> str:
        self.speech.validate()  # Gates before media fetch, even for direct callers.
        try:
            if isinstance(media, dict):
                media = GupshupMedia(**media)
            media = validate_gupshup_media(self.config, media)
            # Official downloadable filemanager URL has no API-key auth documented.
            # NEVER forward the outbound Gupshup API key to a media host.
            request = urllib.request.Request(media.url, headers={"Accept": "audio/*"})
            with self.client.open(request, timeout=self.speech.timeout_seconds) as response:
                content_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower().strip()
                normalize = lambda mime: "audio/wav" if mime == "audio/x-wav" else mime
                if normalize(content_type) != normalize(media.content_type):
                    raise SpeechError("Gupshup media content type mismatch.")
                audio = _bounded_read(response, MAX_AUDIO_BYTES)
            return transcribe_audio(audio, content_type, self.speech, self.client)
        except Exception:
            raise SpeechError("Voice transcription failed. Please type the message; no automatic retry was made.") from None


def configured_gupshup_transcriber(config: GupshupConfig, env: Mapping[str, str] | None = None):
    speech = SpeechConfig.from_env(env)
    return GupshupTranscriber(config, speech) if speech.enabled else None
