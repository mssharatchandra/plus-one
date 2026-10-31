"""Optional provider boundary; the three deterministic demo flows need no API key.

Only explicitly authorized general API accounts may be enabled. A coding-plan
subscription is not taken as authorization for this backend. Configuration is
operator-controlled: never populate API URLs/models from incoming chat text.
Text output is untrusted content and must never directly execute transactions.
No live requests are made on construction, and retries are deliberately absent.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import ipaddress
from http.client import HTTPException
import json
import os
from typing import Mapping, Protocol, Sequence
from urllib import error, request
from urllib.parse import urlsplit


class ProviderUnavailable(RuntimeError):
    """Provider is intentionally disabled or needs explicit setup."""


class ProviderError(RuntimeError):
    """Sanitized provider failure; does not contain request/response secrets."""


def _flag(value: str) -> bool:
    if value.lower().strip() not in {"", "false", "true", "0", "1"}:
        raise ValueError("Provider enable flags must be true or false.")
    return value.lower().strip() in {"true", "1"}


def _endpoint(base: str) -> str:
    """Validate a trusted operator-configured HTTPS base and append API route."""
    try:
        parsed = urlsplit(base)
        port = parsed.port
    except ValueError:
        raise ValueError("LLM_API_BASE_URL must be a valid HTTPS API base URL.") from None
    host = (parsed.hostname or "").lower()
    if (parsed.scheme != "https" or not host or parsed.username or parsed.password
            or parsed.query or parsed.fragment or port not in (None, 443)
            or "\\" in base or any(ord(c) < 33 for c in base)):
        raise ValueError("LLM_API_BASE_URL must be HTTPS without credentials, query, or fragment.")
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")) or "." not in host:
        raise ValueError("LLM_API_BASE_URL must use a public provider hostname.")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise ValueError("LLM_API_BASE_URL must use a public provider hostname, not an IP address.")
    return base.rstrip("/") + "/chat/completions"


@dataclass(frozen=True)
class LLMConfig:
    enabled: bool = False
    general_api_authorized: bool = False
    base_url: str = ""
    model: str = ""
    api_key: str = field(default="", repr=False)
    timeout_seconds: float = 20.0
    max_response_bytes: int = 262144
    max_input_chars: int = 16000
    max_output_tokens: int = 1024

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "LLMConfig":
        env = os.environ if environ is None else environ
        return cls(
            enabled=_flag(env.get("LLM_ENABLED", "false")),
            general_api_authorized=_flag(env.get("LLM_GENERAL_API_AUTHORIZED", "false")),
            base_url=env.get("LLM_API_BASE_URL", "").strip(),
            model=env.get("LLM_MODEL", "").strip(),
            api_key=env.get("LLM_API_KEY", ""),
        )

    def validate(self) -> str:
        if not self.enabled:
            raise ProviderUnavailable("Optional text AI is disabled; the deterministic demo remains available.")
        if not self.general_api_authorized:
            raise ProviderUnavailable("Confirm general API backend usage is authorized, then set LLM_GENERAL_API_AUTHORIZED=true. A coding-plan subscription alone is not sufficient.")
        if not self.api_key.strip() or not self.model.strip() or not self.base_url:
            raise ProviderUnavailable("Set LLM_API_BASE_URL, LLM_MODEL and LLM_API_KEY securely before enabling text AI.")
        if len(self.api_key) > 4096 or any(ord(c) < 33 or ord(c) > 126 for c in self.api_key):
            raise ValueError("Invalid LLM API key configuration.")
        if len(self.model) > 200 or any(ord(c) < 32 for c in self.model):
            raise ValueError("Invalid LLM model configuration.")
        if not 1 <= self.timeout_seconds <= 60:
            raise ValueError("Provider timeout must be between 1 and 60 seconds.")
        if not 1024 <= self.max_response_bytes <= 1048576 or not 1 <= self.max_input_chars <= 100000:
            raise ValueError("Invalid provider size limits.")
        if not 1 <= self.max_output_tokens <= 4096:
            raise ValueError("Provider output token limit must be between 1 and 4096.")
        return _endpoint(self.base_url)


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Prevent an API key from being forwarded to a different origin.
        return None


class OpenAICompatibleLLM:
    """Small text-only adapter for explicitly configured OpenAI-compatible APIs.

    The caller owns consent for sending its supplied messages to the provider.
    There is no provider fallback and no tool execution. Unknown fields (such as
    reasoning content) are ignored; only the final assistant text is returned.
    """

    def __init__(self, config: LLMConfig | None = None):
        self.config = config or LLMConfig.from_env()

    def complete(self, messages: Sequence[Mapping[str, str]]) -> str:
        endpoint = self.config.validate()
        if isinstance(messages, (str, bytes)) or not 1 <= len(messages) <= 32:
            raise ValueError("Supply between 1 and 32 text messages.")
        safe_messages = []
        total_chars = 0
        for item in messages:
            if not isinstance(item, Mapping):
                raise ValueError("Each message must have a role and text content.")
            role, content = item.get("role"), item.get("content")
            if role not in {"system", "user", "assistant"} or not isinstance(content, str) or not content.strip():
                raise ValueError("Only nonempty system, user, or assistant text messages are supported.")
            total_chars += len(content)
            safe_messages.append({"role": role, "content": content})
        if total_chars > self.config.max_input_chars:
            raise ValueError("Provider input exceeds the configured character limit.")
        payload = json.dumps({"model": self.config.model, "messages": safe_messages,
                              "max_tokens": self.config.max_output_tokens, "stream": False}).encode("utf-8")
        req = request.Request(endpoint, data=payload, method="POST", headers={
            "Authorization": "Bearer " + self.config.api_key,
            "Content-Type": "application/json", "Accept": "application/json",
        })
        opener = request.build_opener(request.ProxyHandler({}), _NoRedirect())
        try:
            with opener.open(req, timeout=self.config.timeout_seconds) as response:
                if response.status != 200:
                    raise ProviderError("Text AI returned an unsuccessful status; check provider configuration.")
                data = response.read(self.config.max_response_bytes + 1)
        except error.HTTPError as exc:
            # Never surface raw provider response bodies or request URLs.
            raise ProviderError(f"Text AI request failed (HTTP {exc.code}); check account access and configuration.") from None
        except (error.URLError, TimeoutError, OSError, HTTPException):
            raise ProviderError("Text AI request did not complete. No automatic retry was attempted.") from None
        if len(data) > self.config.max_response_bytes:
            raise ProviderError("Text AI response exceeded the configured size limit.")
        try:
            result = json.loads(data)
            choice = result["choices"][0]
            if not isinstance(choice, dict):
                raise ValueError()
            if choice.get("finish_reason") == "length":
                raise ProviderError("Text AI output was truncated; use a shorter request or a suitable output limit.")
            if not isinstance(choice.get("message"), dict):
                raise ValueError()
            if choice.get("finish_reason") != "stop" or choice["message"].get("tool_calls"):
                raise ProviderError("Text AI did not return a complete final answer; nothing was executed.")
            content = choice["message"]["content"]
            if not isinstance(content, str) or not content.strip():
                raise ValueError()
        except (ValueError, TypeError, KeyError, IndexError):
            raise ProviderError("Text AI returned no usable final text. The deterministic demo remains available.") from None
        return content.strip()


class SpeechToText(Protocol):
    """Audio adapters consume bytes only; they must not fetch arbitrary URLs."""
    def transcribe(self, audio: bytes, content_type: str) -> str: ...


class DisabledSpeechToText:
    """Honest default: no audio is uploaded and no recognition is fabricated."""
    def transcribe(self, audio: bytes, content_type: str) -> str:
        raise ProviderUnavailable(
            "Voice transcription is not configured. Please type the message, or paste a transcript to test the same flow. No audio was sent to an AI provider."
        )
