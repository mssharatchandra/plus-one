import io
import json
import unittest
import wave
from unittest.mock import Mock, patch

from plus_one.speech import SpeechConfig, SpeechConfigurationError, SpeechHTTPError, transcribe_audio
from plus_one.sarvam_speech import (BASE, MODEL, SarvamProviderDiagnostic,
                                    transcribe_sarvam)


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


if __name__ == "__main__":
    unittest.main()
