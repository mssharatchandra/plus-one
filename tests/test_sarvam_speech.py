import io
import json
import os
import unittest
import wave
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from plus_one.speech import SpeechConfig, SpeechConfigurationError, SpeechHTTPError, transcribe_audio
from plus_one.sarvam_speech import (BASE, MODEL, SarvamProviderDiagnostic,
                                    transcribe_sarvam)
from plus_one.gupshup import GupshupConfig, GupshupMedia, GupshupTranscriber, VoicePipelineDiagnostic


def config(**kwargs):
    values = dict(enabled=True, provider_authorized=True, api_base_url=BASE,
                  model=MODEL, api_key="synthetic-test-key")
    values.update(kwargs)
    return SpeechConfig(**values)


def wav():
    out = io.BytesIO()
    with wave.open(out, "wb") as f:
        f.setnchannels(1); f.setsampwidth(2); f.setframerate(16000)
        f.writeframes(b"\0\0" * 1600)
    return out.getvalue()


class Response(io.BytesIO):
    headers = {"Content-Type": "application/json"}


class SarvamSpeechTests(unittest.TestCase):
    def test_env_selects_only_sarvam_key_for_sarvam_model(self):
        cfg = SpeechConfig.from_env({"STT_ENABLED": "true", "STT_PROVIDER_AUTHORIZED": "true",
            "STT_API_BASE_URL": BASE, "STT_MODEL": MODEL,
            "STT_API_KEY": "wrong-provider-key", "SARVAM_API_KEY": "sarvam-key"})
        self.assertEqual(cfg.api_key, "sarvam-key")
        missing = SpeechConfig.from_env({"STT_ENABLED": "true", "STT_PROVIDER_AUTHORIZED": "true",
            "STT_API_BASE_URL": BASE, "STT_MODEL": MODEL, "STT_API_KEY": "wrong-provider-key"})
        with self.assertRaises(SpeechConfigurationError):
            missing.validate()

    def test_gates_and_host_before_conversion_or_network(self):
        for cfg in [config(enabled=False), config(provider_authorized=False),
                    config(api_base_url="https://other.example"), config(api_key="")]:
            with self.subTest(cfg=cfg), patch("plus_one.sarvam_speech.normalized_wav") as convert:
                with self.assertRaises(SpeechConfigurationError):
                    transcribe_sarvam(b"x", "audio/ogg", cfg)
                convert.assert_not_called()

    @patch("plus_one.sarvam_speech.normalized_wav", return_value=wav())
    def test_exact_request_and_dispatch(self, convert):
        client = Mock()
        client.open.return_value = Response(json.dumps({"transcript": "hello\n world", "language_code": "en-IN"}).encode())
        self.assertEqual(transcribe_audio(b"synthetic", "audio/ogg", config(), client), "hello world")
        request = client.open.call_args.args[0]
        self.assertEqual(request.full_url, BASE + "/speech-to-text")
        self.assertEqual(request.get_header("Api-subscription-key"), "synthetic-test-key")
        self.assertIsNone(request.get_header("Authorization"))
        self.assertIn(b'name="model"\r\n\r\nsaaras:v4', request.data)
        self.assertIn(b'name="language_code"\r\n\r\nunknown', request.data)
        self.assertIn(b'filename="voice-note.wav"', request.data)
        self.assertNotIn(b"response_format", request.data)
        self.assertEqual(client.open.call_count, 1)

    @patch("plus_one.sarvam_speech.normalized_wav", return_value=wav())
    def test_http_categories_and_no_retry(self, convert):
        for status, expected in [(402, "credit"), (403, "auth"), (429, "rate")]:
            with self.subTest(status=status):
                client = Mock()
                client.open.side_effect = SpeechHTTPError(status, "unknown", expected)
                with self.assertRaises(SarvamProviderDiagnostic) as caught:
                    transcribe_sarvam(b"x", "audio/ogg", config(), client)
                self.assertEqual((caught.exception.status, caught.exception.category), (status, expected))
                self.assertEqual(client.open.call_count, 1)

    @patch("plus_one.sarvam_speech.normalized_wav", return_value=wav())
    def test_bad_response_never_surfaces_content(self, convert):
        for response in [Response(b"{private-not-json"), Response(b'{"text":"private"}'),
                         Response(b'{"transcript":""}'), Response(b'{"transcript":"x\\u0000y"}')]:
            client = Mock(); client.open.return_value = response
            with self.assertRaises(SarvamProviderDiagnostic) as caught:
                transcribe_sarvam(b"x", "audio/ogg", config(), client)
            self.assertNotIn("private", str(caught.exception))
            self.assertEqual(client.open.call_count, 1)

    def test_media_fetch_failure_has_only_safe_stage_and_status(self):
        app_id = "11111111-2222-3333-4444-555555555555"
        cfg = GupshupConfig(api_key="private-key", app_name="Demo", app_id=app_id,
            source="15557654321", allowed_recipients=frozenset({"whatsapp:+15551234567"}),
            webhook_secret="private-webhook-secret-1234567890")
        media = GupshupMedia(f"https://filemanager.gupshup.io/wa/{app_id}/wa/media/opaque-media-id?download=false",
            "audio/ogg", "msg1", app_id, "Demo", datetime.now(timezone.utc).timestamp()+60)
        client = Mock(); client.open.side_effect = SpeechHTTPError(403, "unknown", "auth")
        with self.assertRaises(VoicePipelineDiagnostic) as caught:
            GupshupTranscriber(cfg, config(), client).transcribe(media)
        self.assertEqual((caught.exception.stage, caught.exception.category, caught.exception.status),
                         ("media_fetch", "auth", 403))
        self.assertNotIn("private", str(caught.exception))
        self.assertEqual(client.open.call_count, 1)

    def test_worker_logs_only_safe_enums_for_failed_voice(self):
        from plus_one.engine import Engine
        from plus_one.worker import process_inbox
        with TemporaryDirectory() as tmp:
            engine = Engine(Path(tmp)/"test.sqlite3", clock=lambda:datetime(2026,10,31,12,tzinfo=timezone.utc))
            try:
                engine.enqueue_inbound("gupshup", "synthetic-id", "synthetic-user",
                    {"kind":"audio", "text":None, "media":{}, "received_at":"2026-10-31T12:00:00+00:00"})
                transcriber = Mock()
                transcriber.transcribe.side_effect = VoicePipelineDiagnostic("media_type", "octet_stream")
                diagnostic = Path(tmp)/"voice.jsonl"
                with patch.dict(os.environ, {"VOICE_DIAGNOSTIC_PATH":str(diagnostic)}):
                    process_inbox(engine, transcribers={"gupshup":transcriber})
                row = json.loads(diagnostic.read_text().strip())
                self.assertEqual((row["stage"],row["category"]),("media_type","octet_stream"))
                self.assertEqual(diagnostic.stat().st_mode & 0o777,0o600)
                self.assertNotIn("synthetic-user",diagnostic.read_text())
                self.assertNotIn("synthetic-id",diagnostic.read_text())
            finally:
                engine.close()


if __name__ == "__main__":
    unittest.main()
