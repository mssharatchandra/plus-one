from dataclasses import replace
import io
import json
import unittest
from unittest.mock import patch
from urllib.request import Request

from plus_one.speech import (CompatibleTranscriber, SpeechConfig, SpeechConfigurationError,
    SpeechError, PublicHTTPSClient, configured_transcriber, MAX_AUDIO_BYTES)
from plus_one.transport import TwilioConfig, TwilioMedia

ACCOUNT = "AC" + "a" * 32
MESSAGE = "SM" + "b" * 32
TWILIO = TwilioConfig(account_sid=ACCOUNT, auth_token="fake-twilio-token")
MEDIA = TwilioMedia(f"https://api.twilio.com/2010-04-01/Accounts/{ACCOUNT}/Messages/{MESSAGE}/Media/ME{'c'*32}", "audio/ogg", MESSAGE, ACCOUNT)
CONFIG = SpeechConfig(enabled=True, provider_authorized=True, api_base_url="https://stt.example.com/v1", model="test-transcribe", api_key="fake-stt-key")


class Response(io.BytesIO):
    def __init__(self, raw, content_type, length=None):
        super().__init__(raw)
        self.headers = {"Content-Type": content_type}
        if length is not None:
            self.headers["Content-Length"] = str(length)


class FakeClient:
    def __init__(self, responses=None):
        self.requests = []
        self.responses = responses if responses is not None else [Response(b"OggS-fake-audio", "audio/ogg"), Response(json.dumps({"text": "spent INR 1200 with Maya"}).encode(), "application/json")]

    def open(self, request, timeout):
        self.requests.append((request, timeout))
        res = self.responses.pop(0)
        if isinstance(res, Exception):
            raise res
        return res


class SpeechTests(unittest.TestCase):
    def test_disabled_and_unauthorized_gates_no_io(self):
        self.assertIsNone(configured_transcriber(TWILIO, {}))
        self.assertIsNone(configured_transcriber(TWILIO, {"STT_API_KEY": "key"}))
        with self.assertRaises(SpeechConfigurationError):
            configured_transcriber(TWILIO, {"STT_ENABLED": "true"})
        client = FakeClient()
        with self.assertRaises(SpeechConfigurationError):
            CompatibleTranscriber(TWILIO, replace(CONFIG, provider_authorized=False), client)
        self.assertEqual(client.requests, [])

    def test_invalid_endpoint_and_model_fail_before_io(self):
        for url in ["http://stt.example.com/v1", "https://127.0.0.1/v1", "https://localhost/v1", "https://host.internal/v1", "https://user:pass@stt.example.com/v1", "https://stt.example.com:8443/v1", "https://stt.example.com/v1?x=1", "https://stt.example.com/v1#frag", "https://stt.example.com/\r\nInjected"]:
            with self.subTest(url=url), self.assertRaises(SpeechConfigurationError):
                CompatibleTranscriber(TWILIO, replace(CONFIG, api_base_url=url))
        with self.assertRaises(SpeechConfigurationError):
            CompatibleTranscriber(TWILIO, replace(CONFIG, model="x\r\ninject"))
        self.assertNotIn("fake-stt-key", repr(CONFIG))

    def test_authenticated_audio_then_multipart_transcription(self):
        client = FakeClient()
        text = CompatibleTranscriber(TWILIO, CONFIG, client).transcribe(MEDIA)
        self.assertEqual(text, "spent INR 1200 with Maya")
        self.assertEqual(len(client.requests), 2)
        audio, _ = client.requests[0]
        self.assertEqual(audio.full_url, MEDIA.url)
        self.assertTrue(audio.get_header("Authorization").startswith("Basic "))
        stt, timeout = client.requests[1]
        self.assertEqual(stt.full_url, "https://stt.example.com/v1/audio/transcriptions")
        self.assertEqual(stt.get_header("Authorization"), "Bearer fake-stt-key")
        self.assertEqual(stt.get_method(), "POST")
        self.assertIn(b'OggS-fake-audio', stt.data)
        self.assertIn(b'name="model"\r\n\r\ntest-transcribe', stt.data)
        self.assertIn(b'name="response_format"\r\n\r\njson', stt.data)
        self.assertIn(b'filename="voice-note.ogg"', stt.data)
        self.assertLessEqual(timeout, 30)

    def test_media_url_and_account_revalidated(self):
        client = FakeClient()
        adapter = CompatibleTranscriber(TWILIO, CONFIG, client)
        for media in [replace(MEDIA, url="https://evil.example/audio"), replace(MEDIA, account_sid="AC" + "d"*32), replace(MEDIA, message_sid="bogus")]:
            with self.assertRaises(SpeechError):
                adapter.transcribe(media)
        self.assertEqual(client.requests, [])

    def test_bad_type_oversize_empty_audio_never_reaches_stt(self):
        for response in [Response(b"<html>secret</html>", "text/html"), Response(b"fake", "audio/ogg", MAX_AUDIO_BYTES+1), Response(b"", "audio/ogg"), Response(b"fake", "audio/mp4"), Response(b"fake", "audio/ogg", "invalid")]:
            client = FakeClient([response])
            with self.assertRaises(SpeechError):
                CompatibleTranscriber(TWILIO, CONFIG, client).transcribe(MEDIA)
            self.assertEqual(len(client.requests), 1)

    def test_oversized_transcript_invalid_json_and_type(self):
        for response in [Response(json.dumps({"text":"x"*4001}).encode(), "application/json"), Response(b"private malformed json", "application/json"), Response(b'{"text":""}', "application/json"), Response(b'{"text":"hi"}', "text/html")]:
            client = FakeClient([Response(b"audio", "audio/ogg"), response])
            with self.assertRaises(SpeechError) as error:
                CompatibleTranscriber(TWILIO, CONFIG, client).transcribe(MEDIA)
            self.assertNotIn("private", str(error.exception))
            self.assertEqual(len(client.requests), 2)

    def test_network_error_no_retry_or_secret_leak(self):
        client = FakeClient([Response(b"audio", "audio/ogg"), TimeoutError("fake-stt-key private-user-data")])
        with self.assertRaises(SpeechError) as error:
            CompatibleTranscriber(TWILIO, CONFIG, client).transcribe(MEDIA)
        self.assertNotIn("fake-stt-key", str(error.exception))
        self.assertNotIn("private-user-data", str(error.exception))
        self.assertEqual(len(client.requests), 2)

    def test_public_dns_check_blocks_private_and_mixed_answers(self):
        client = PublicHTTPSClient()
        for ips in [["127.0.0.1"], ["169.254.169.254"], ["10.0.0.1"], ["93.184.216.34", "192.168.1.1"], ["::1"]]:
            answers = [(2, 1, 6, "", (ip, 443)) for ip in ips]
            with patch("plus_one.speech.socket.getaddrinfo", return_value=answers), patch("plus_one.speech._PinnedHTTPSConnection") as conn:
                with self.assertRaises(SpeechError):
                    client.open(Request("https://stt.example.com/v1/audio/transcriptions"))
                conn.assert_not_called()

    def test_public_connection_is_pinned_and_redirect_not_followed(self):
        answers = [(2, 1, 6, "", ("93.184.216.34", 443))]
        with patch("plus_one.speech.socket.getaddrinfo", return_value=answers), patch("plus_one.speech._PinnedHTTPSConnection") as cls:
            conn = cls.return_value
            conn.getresponse.return_value.status = 302
            with self.assertRaises(SpeechError):
                PublicHTTPSClient().open(Request("https://stt.example.com/v1/audio/transcriptions"))
            cls.assert_called_once_with("stt.example.com", "93.184.216.34", 20.0)
            conn.close.assert_called_once()
            conn.request.assert_called_once()


if __name__ == "__main__":
    unittest.main()
