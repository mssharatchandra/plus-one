"""Opt-in Sarvam recorded-audio transcription for short WhatsApp voice notes.

The provider receives only the converted note. No audio files, raw provider
responses, redirects, ambient proxies, or automatic retries are used.
"""
from __future__ import annotations

import json
import secrets
import socket
import ssl
from urllib.request import Request

from .speech import (MAX_RESPONSE_BYTES, MAX_TRANSCRIPT_CHARS, PublicHTTPSClient,
                     SpeechConfigurationError, SpeechError, SpeechHTTPError, _bounded_read)
from .zai_speech import VoiceInputError, normalized_wav

BASE = "https://api.sarvam.ai"
MODEL = "saaras:v4"


class SarvamProviderDiagnostic(SpeechError):
    """Safe status/category; never includes the provider body or user audio."""
    def __init__(self, stage: str, category: str, status: int | None = None,
                 code: str = "unknown"):
        self.stage, self.category, self.status, self.code = stage, category, status, code
        super().__init__(f"Sarvam STT {stage}: {category}; HTTP {status or 'unknown'}; "
                         f"code {code}. No retry was attempted.")


def transcribe_sarvam(audio: bytes, content_type: str, config, client=None,
                      ffmpeg_path=None) -> str:
    config.validate()  # Authorization and exact host before decoding or network.
    if config.api_base_url != BASE or config.model != MODEL:
        raise SpeechConfigurationError("Sarvam STT requires the official API base and saaras:v4.")
    # The REST API accepts OGG, but normalizing to 16-kHz mono WAV enforces the
    # 30-second limit and strips container metadata before provider upload.
    wav = normalized_wav(audio, content_type, ffmpeg_path)
    boundary = "plusone-sarvam-" + secrets.token_hex(24)
    while boundary.encode() in wav:
        boundary = "plusone-sarvam-" + secrets.token_hex(24)
    parts = []
    for name, value in (("model", MODEL), ("language_code", "unknown")):
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    parts.extend((f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="voice-note.wav"\r\nContent-Type: audio/wav\r\n\r\n'.encode(),
                  wav, f'\r\n--{boundary}--\r\n'.encode()))
    req = Request(BASE + "/speech-to-text", data=b"".join(parts), method="POST", headers={
        "api-subscription-key": config.api_key, "Accept": "application/json",
        "Content-Type": "multipart/form-data; boundary=" + boundary})
    try:
        response = (client or PublicHTTPSClient()).open(req, timeout=config.timeout_seconds)
    except SpeechHTTPError as exc:
        category = "credit" if exc.status == 402 else exc.category
        raise SarvamProviderDiagnostic("http", category, exc.status, exc.code) from None
    except socket.gaierror:
        raise SarvamProviderDiagnostic("network", "dns") from None
    except ssl.SSLError:
        raise SarvamProviderDiagnostic("network", "tls") from None
    except TimeoutError:
        raise SarvamProviderDiagnostic("network", "timeout") from None
    except (OSError, SpeechError):
        raise SarvamProviderDiagnostic("network", "connect_or_policy") from None
    except Exception:
        raise SarvamProviderDiagnostic("network", "unexpected") from None
    try:
        with response:
            if response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
                raise SarvamProviderDiagnostic("response", "content_type")
            obj = json.loads(_bounded_read(response, MAX_RESPONSE_BYTES))
        text = obj.get("transcript") if isinstance(obj, dict) else None
        if not isinstance(text, str) or not text.strip():
            raise SarvamProviderDiagnostic("response", "empty_transcript")
        if len(text) > MAX_TRANSCRIPT_CHARS or any(ord(c) < 32 and c not in "\n\t\r" for c in text):
            raise SarvamProviderDiagnostic("response", "invalid_transcript")
        return " ".join(text.split())
    except SarvamProviderDiagnostic:
        raise
    except (ValueError, TypeError, SpeechError):
        raise SarvamProviderDiagnostic("response", "invalid_json_or_size") from None
