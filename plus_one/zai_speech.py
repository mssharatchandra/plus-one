"""Opt-in Z.ai ASR adapter with bounded, in-memory audio conversion.

No audio files, shell, decoder logs, provider retries, or automatic installation.
"""
from __future__ import annotations

import io
import json
import os
import secrets
import shutil
import subprocess
import wave
from urllib.request import Request

from .speech import (MAX_AUDIO_BYTES, MAX_RESPONSE_BYTES, MAX_TRANSCRIPT_CHARS,
                     PublicHTTPSClient, SpeechConfigurationError, SpeechError,
                     _bounded_read)

BASE = "https://api.z.ai/api/paas/v4"
MODEL = "glm-asr-2512"
RATE = 16000
MAX_PCM = 30 * RATE * 2
FORMATS = {"audio/ogg": "ogg", "audio/wav": "wav", "audio/x-wav": "wav",
           "audio/mpeg": "mp3"}


class VoiceInputError(SpeechError):
    """Safe input-specific message that may be returned to the sender."""


def validate_zai_config(config):
    config.validate()
    if config.api_base_url != BASE or config.model != MODEL:
        raise SpeechConfigurationError("Z.ai ASR requires the official general API base and glm-asr-2512.")


def normalized_wav(audio: bytes, content_type: str, ffmpeg_path=None) -> bytes:
    if not isinstance(audio, bytes) or not audio or len(audio) > MAX_AUDIO_BYTES:
        raise VoiceInputError("That voice note is empty or too large. Please send a note under 10 MiB, or type the message.")
    demuxer = FORMATS.get(content_type)
    if demuxer is None:
        raise VoiceInputError("Please send an OGG, WAV or MP3 voice note of 30 seconds or less, or type the message.")
    executable = ffmpeg_path or os.environ.get("STT_FFMPEG_PATH") or shutil.which("ffmpeg")
    if (not executable or not os.path.isabs(executable) or not os.path.isfile(executable)
            or not os.access(executable, os.X_OK)):
        raise VoiceInputError("Voice conversion is unavailable. Please type the message while setup is completed.")
    # Decode one second beyond the limit, so overlong input is rejected rather
    # than silently clipped and uploaded as an apparently valid voice note.
    command = [executable, "-nostdin", "-hide_banner", "-loglevel", "error",
               "-protocol_whitelist", "pipe", "-threads", "1", "-filter_threads", "1",
               "-f", demuxer, "-i", "pipe:0", "-map", "0:a:0", "-vn", "-sn", "-dn",
               "-map_metadata", "-1", "-ac", "1", "-ar", str(RATE), "-t", "31",
               "-c:a", "pcm_s16le", "-threads", "1", "-f", "s16le", "-fs", "1000000", "pipe:1"]
    try:
        result = subprocess.run(command, input=audio, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, timeout=12, check=False,
                                shell=False, close_fds=True,
                                env={"PATH": os.path.dirname(executable), "LANG": "C"})
    except (OSError, subprocess.TimeoutExpired):
        raise VoiceInputError("That voice note could not be converted safely. Please send a shorter note or type the message.") from None
    pcm = result.stdout
    if result.returncode != 0 or not pcm or len(pcm) % 2:
        raise VoiceInputError("That voice note could not be read. Please resend it as OGG, WAV or MP3, or type the message.")
    if len(pcm) > MAX_PCM:
        raise VoiceInputError("That voice note is longer than 30 seconds. Please send a shorter note or type the message.")
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(RATE)
        wav.writeframes(pcm)
    return output.getvalue()


def transcribe_zai(audio, content_type, config, client=None, ffmpeg_path=None):
    validate_zai_config(config)
    wav = normalized_wav(audio, content_type, ffmpeg_path)
    boundary = "plusone-zai-" + secrets.token_hex(24)
    while boundary.encode() in wav:
        boundary = "plusone-zai-" + secrets.token_hex(24)
    parts = []
    for key, value in (("model", MODEL), ("stream", "false")):
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode())
    parts.extend((f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="voice-note.wav"\r\nContent-Type: audio/wav\r\n\r\n'.encode(),
                  wav, f'\r\n--{boundary}--\r\n'.encode()))
    req = Request(BASE + "/audio/transcriptions", data=b"".join(parts), method="POST", headers={
        "Authorization": "Bearer " + config.api_key, "Accept": "application/json",
        "Content-Type": "multipart/form-data; boundary=" + boundary})
    try:
        with (client or PublicHTTPSClient()).open(req, timeout=config.timeout_seconds) as response:
            if response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
                raise ValueError()
            obj = json.loads(_bounded_read(response, MAX_RESPONSE_BYTES))
        text = obj.get("text") if isinstance(obj, dict) else None
        if (not isinstance(text, str) or not text.strip() or len(text) > MAX_TRANSCRIPT_CHARS
                or any(ord(c) < 32 and c not in "\n\t\r" for c in text)):
            raise ValueError()
        return " ".join(text.split())
    except Exception:
        raise SpeechError("Voice transcription failed. Please type the message; no automatic retry was made.") from None
