"""Opt-in, in-memory Twilio audio -> compatible STT multipart adapter.

Supported API contract is documented in SPEECH.md. No automatic provider retries,
raw audio persistence, redirects, proxy environment use, or content logging.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass, field
import http.client
import ipaddress
import json
import os
import re
import secrets
import socket
import ssl
from typing import Mapping
from urllib.parse import urlsplit
from urllib.request import Request

from .transport import ACCOUNT_SID, SID, TwilioConfig, TwilioMedia, validated_audio_media

MAX_AUDIO_BYTES = 10 * 1024 * 1024
MAX_RESPONSE_BYTES = 65536
MAX_TRANSCRIPT_CHARS = 4000
AUDIO_EXTENSIONS = {
    "audio/ogg": "ogg", "audio/mpeg": "mp3", "audio/mp4": "m4a",
    "audio/wav": "wav", "audio/x-wav": "wav", "audio/aac": "aac", "audio/amr": "amr",
}


class SpeechConfigurationError(ValueError):
    pass


class SpeechError(RuntimeError):
    """Sanitized failure; a provider request may already have incurred cost."""


class SpeechHTTPError(SpeechError):
    """Only numeric status and bounded provider error classification are retained."""
    def __init__(self, status: int, code: str = "unknown", category: str = "other"):
        self.status, self.code, self.category = status, code, category
        super().__init__(f"Speech provider returned HTTP {status}; code={code}; category={category}.")


def _provider_error_summary(raw: bytes) -> tuple[str, str]:
    if raw.lstrip().lower().startswith((b"<!doctype html", b"<html")):
        return "unknown", "html_error"
    try:
        obj = json.loads(raw)
        value = obj.get("code") if isinstance(obj, dict) else None
        code = str(value) if isinstance(value, (str, int)) and not isinstance(value, bool) else "unknown"
        if not re.fullmatch(r"[0-9]{1,9}", code):
            code = "unknown"
        message = obj.get("message", "") if isinstance(obj, dict) else ""
        message = message.casefold() if isinstance(message, str) else ""
        category = ("invalid_media_id" if "invalid media id" in message else
                    "rate" if any(x in message for x in ("rate limit", "too many requests")) else
                    "credit" if any(x in message for x in ("balance", "credit", "quota", "payment")) else
                    "auth" if any(x in message for x in ("unauthorized", "api key", "token", "authentication")) else
                    "model" if "model" in message else "other")
        return code, category
    except (TypeError, ValueError):
        return "unknown", "other"


def _url_parts(url: str, allow_query=False):
    try:
        p = urlsplit(url)
        host = p.hostname or ""
        # Hostnames only, port 443 only, no credentials/query/fragments/control chars.
        if (p.scheme != "https" or not host or p.username or p.password or (p.query and not allow_query) or p.fragment
                or p.port not in (None, 443) or not re.fullmatch(r"[a-zA-Z0-9.-]+", host)
                or "." not in host or host.endswith((".local", ".internal", ".localhost"))
                or any(ord(c) <= 32 or ord(c) >= 127 for c in url)
                or not re.fullmatch(r"/[A-Za-z0-9_./-]*", p.path or "/")):
            raise ValueError
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            raise ValueError
        return p
    except (ValueError, TypeError):
        raise SpeechConfigurationError("STT endpoint must use a public HTTPS hostname, port 443, and a plain path without credentials or query.") from None


@dataclass(frozen=True)
class SpeechConfig:
    enabled: bool = False
    provider_authorized: bool = False
    api_base_url: str = ""
    model: str = ""
    api_key: str = field(default="", repr=False)
    timeout_seconds: float = 20.0

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None):
        env = os.environ if env is None else env
        model = env.get("STT_MODEL", "")
        return cls(
            enabled=env.get("STT_ENABLED", "").lower() == "true",
            provider_authorized=env.get("STT_PROVIDER_AUTHORIZED", "").lower() == "true",
            api_base_url=env.get("STT_API_BASE_URL", "").rstrip("/"),
            model=model,
            api_key=env.get("SARVAM_API_KEY", "") if model == "saaras:v4" else env.get("STT_API_KEY", ""),
        )

    def validate(self):
        if not self.enabled or not self.provider_authorized:
            raise SpeechConfigurationError("Speech requires STT_ENABLED=true and explicit STT_PROVIDER_AUTHORIZED=true.")
        _url_parts(self.api_base_url)
        if self.model == "glm-asr-2512" and self.api_base_url != "https://api.z.ai/api/paas/v4":
            raise SpeechConfigurationError("Z.ai ASR requires its exact official API base.")
        if self.model == "saaras:v4" and self.api_base_url != "https://api.sarvam.ai":
            raise SpeechConfigurationError("Sarvam STT requires its exact official API base.")
        if not self.api_key or any(ord(c) < 33 or ord(c) > 126 for c in self.api_key):
            key_name = "SARVAM_API_KEY" if self.model == "saaras:v4" else "STT_API_KEY"
            raise SpeechConfigurationError(f"A valid {key_name} is required.")
        if not re.fullmatch(r"[a-zA-Z0-9_./:-]{1,128}", self.model):
            raise SpeechConfigurationError("A valid STT_MODEL is required.")
        if not 1 <= self.timeout_seconds <= 30:
            raise SpeechConfigurationError("STT timeout must be between 1 and 30 seconds.")


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """TLS SNI/certificate uses configured host; TCP uses already-validated IP."""
    def __init__(self, host, address, timeout):
        super().__init__(host, timeout=timeout, context=ssl.create_default_context())
        self.address = address

    def connect(self):
        sock = socket.create_connection((self.address, 443), self.timeout)
        try:
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
        except BaseException:
            sock.close()
            raise


class _Response:
    def __init__(self, response, connection):
        self.response, self.connection = response, connection
        self.headers, self.status = response.headers, response.status

    def read(self, limit):
        return self.response.read(limit)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.response.close()
        self.connection.close()


class PublicHTTPSClient:
    """No redirects or environment proxies; reject any non-public DNS answer.

    DNS is resolved once per request and the selected IP is pinned for TLS. Socket
    operations have a bounded timeout; OS DNS resolution follows system timeouts.
    """
    def __init__(self, allow_query=False):
        self.allow_query = allow_query

    def open(self, request: Request, timeout=20.0):
        p = _url_parts(request.full_url, allow_query=self.allow_query)
        addresses = socket.getaddrinfo(p.hostname, 443, type=socket.SOCK_STREAM)
        ips = [item[4][0] for item in addresses]
        if not ips or any(not ipaddress.ip_address(ip).is_global for ip in ips):
            raise SpeechError("Speech connection target is not public.")
        connection = _PinnedHTTPSConnection(p.hostname, ips[0], timeout)
        try:
            connection.request(request.get_method(), (p.path or "/") + ("?" + p.query if p.query else ""), body=request.data, headers=dict(request.header_items()))
            response = connection.getresponse()
            if not 200 <= response.status < 300:
                status = response.status
                raw = response.read(4096)
                response.close()
                code, category = _provider_error_summary(raw)
                raise SpeechHTTPError(status, code, category)
            return _Response(response, connection)
        except BaseException:
            connection.close()
            raise


def _bounded_read(response, limit):
    length = response.headers.get("Content-Length")
    if length is not None:
        try:
            if not 0 <= int(length) <= limit:
                raise ValueError
        except (TypeError, ValueError):
            raise SpeechError("Speech response exceeds the permitted size.") from None
    payload = response.read(limit + 1)
    if not payload or len(payload) > limit:
        raise SpeechError("Speech response is empty or exceeds the permitted size.")
    return payload


def _multipart(audio: bytes, content_type: str, model: str):
    boundary = "plusone-" + secrets.token_hex(24)
    # Random boundary plus collision check means audio cannot escape its file part.
    while boundary.encode() in audio:
        boundary = "plusone-" + secrets.token_hex(24)
    pieces = []
    for name, value in (("model", model), ("response_format", "json")):
        pieces.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    extension = AUDIO_EXTENSIONS[content_type]
    pieces.append(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="voice-note.{extension}"\r\nContent-Type: {content_type}\r\n\r\n'.encode())
    pieces.extend((audio, f"\r\n--{boundary}--\r\n".encode()))
    return b"".join(pieces), f"multipart/form-data; boundary={boundary}"


class CompatibleTranscriber:
    def __init__(self, twilio_config: TwilioConfig, config: SpeechConfig, client=None):
        self.twilio = twilio_config
        self.config = config
        self.client = client or PublicHTTPSClient()
        self._validate_configuration()

    def _validate_configuration(self):
        self.config.validate()
        if not ACCOUNT_SID.fullmatch(self.twilio.account_sid) or not self.twilio.auth_token:
            raise SpeechConfigurationError("Twilio account SID and auth token are required for authorized media access.")

    def transcribe(self, media: TwilioMedia) -> str:
        # Recheck gates/metadata even when called outside the signed webhook server.
        self._validate_configuration()
        try:
            if media.account_sid != self.twilio.account_sid or not SID.fullmatch(media.message_sid):
                raise ValueError
            validated = validated_audio_media({
                "NumMedia": "1", "MessageSid": media.message_sid,
                "MediaContentType0": media.content_type, "MediaUrl0": media.url,
            }, self.twilio.account_sid)
        except (ValueError, TypeError, AttributeError):
            raise SpeechError("Voice-note metadata could not be verified.") from None
        token = base64.b64encode(f"{self.twilio.account_sid}:{self.twilio.auth_token}".encode()).decode()
        request = Request(validated.url, headers={"Authorization": "Basic " + token, "Accept": "audio/*"})
        try:
            with self.client.open(request, timeout=self.config.timeout_seconds) as response:
                received_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower().strip()
                # Alias WAV types are equivalent; other type mismatches fail closed.
                normalized = lambda mime: "audio/wav" if mime == "audio/x-wav" else mime
                if received_type not in AUDIO_EXTENSIONS or normalized(received_type) != normalized(validated.content_type):
                    raise SpeechError("Voice-note content type could not be verified.")
                audio = _bounded_read(response, MAX_AUDIO_BYTES)
            return transcribe_audio(audio, received_type, self.config, self.client)
        except SpeechError:
            raise
        except Exception:
            # Never include provider exception bodies, audio, transcript, URLs or keys.
            # No retry: an STT POST may have been accepted and billed already.
            raise SpeechError("Voice transcription failed. Please type the message; no automatic retry was made.") from None



def transcribe_audio(audio: bytes, content_type: str, config: SpeechConfig, client=None) -> str:
    """Shared compatible-provider upload; callers own authenticated media retrieval."""
    config.validate()  # Must precede any provider network I/O.
    if config.model == "glm-asr-2512":
        from .zai_speech import transcribe_zai
        return transcribe_zai(audio, content_type, config, client)
    if config.model == "saaras:v4":
        from .sarvam_speech import transcribe_sarvam
        return transcribe_sarvam(audio, content_type, config, client)
    if not isinstance(audio, bytes) or not audio or len(audio) > MAX_AUDIO_BYTES or content_type not in AUDIO_EXTENSIONS:
        raise SpeechError("Audio is empty, oversized, or has an unsupported type.")
    client = client or PublicHTTPSClient()
    try:
        body, mime = _multipart(audio, content_type, config.model)
        request = Request(config.api_base_url.rstrip("/") + "/audio/transcriptions", data=body,
            headers={"Authorization": "Bearer " + config.api_key, "Content-Type": mime, "Accept": "application/json"}, method="POST")
        with client.open(request, timeout=config.timeout_seconds) as response:
            if response.headers.get("Content-Type", "").split(";", 1)[0].lower().strip() != "application/json":
                raise SpeechError("Speech provider did not return the supported JSON format.")
            result = json.loads(_bounded_read(response, MAX_RESPONSE_BYTES))
        text = result.get("text") if isinstance(result, dict) else None
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_TRANSCRIPT_CHARS:
            raise SpeechError("Speech provider returned an empty or oversized transcript.")
        return text.strip()
    except Exception:
        raise SpeechError("Voice transcription failed. Please type the message; no automatic retry was made.") from None


def configured_transcriber(twilio_config: TwilioConfig, env: Mapping[str, str] | None = None):
    """Disabled -> None. Enabled but invalid/unauthorized -> startup error, no I/O."""
    config = SpeechConfig.from_env(env)
    if not config.enabled:
        return None
    return CompatibleTranscriber(twilio_config, config)
