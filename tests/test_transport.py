import base64
from dataclasses import replace
import io
import json
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from xml.etree import ElementTree

from plus_one.server import ServerConfig, make_server, twiml
from plus_one.transport import (TwilioConfig, TwilioTransport, DeliveryError,
    DeliveryUncertain, TransportDisabled, compute_signature, validate_signature,
    validated_audio_media)

ACCOUNT = "AC" + "a" * 32
MID = "SM" + "b" * 32
FROM = "whatsapp:+15551234567"
TO = "whatsapp:+15557654321"
TOKEN = "x" * 32
TWILIO = TwilioConfig(account_sid=ACCOUNT, auth_token="test-auth-not-real", sender=TO,
                      webhook_url="https://example.test/webhooks/twilio?mode=demo",
                      allowed_recipients=frozenset({FROM}))


class FakeEngine:
    def __init__(self):
        self.calls = []
        self.answers = {}

    def handle(self, user_id, text, message_id):
        if message_id not in self.answers:
            self.calls.append((user_id, text, message_id))
            self.answers[message_id] = "Ready <confirm> & save?"
        return self.answers[message_id]

    def snapshot(self, user_id):
        return {"user_id": user_id, "private": "only-authenticated"}


class FakeResponse(io.BytesIO):
    pass


class FakeOpener:
    def __init__(self, data=None, error=None):
        self.requests = []
        self.data = data if data is not None else json.dumps({"sid": MID}).encode()
        self.error = error

    def open(self, request, timeout):
        self.requests.append(request)
        if self.error:
            raise self.error
        return FakeResponse(self.data)


class SignatureTests(unittest.TestCase):
    def test_published_twilio_vector(self):
        params = {"CallSid": "CA1234567890ABCDE", "Caller": "+14158675310", "Digits": "1234",
                  "From": "+14158675310", "To": "+18005551212"}
        url = "https://example.com/myapp.php?foo=1&bar=2"
        signature = "L/OH5YylLD5NRKLltdqwSvS0BnU="
        self.assertEqual(compute_signature("12345", url, params), signature)
        self.assertTrue(validate_signature("12345", url, params, signature))
        self.assertFalse(validate_signature("bad", url, params, signature))
        self.assertFalse(validate_signature("12345", url, params, "👋"))
        self.assertFalse(validate_signature("12345", url + "/", params, signature))
        self.assertFalse(validate_signature("12345", url, {**params, "NewField": "value"}, signature))

    def test_twiml_escapes_text_and_removes_controls(self):
        root = ElementTree.fromstring(twiml("hi <me> & you\x00"))
        self.assertEqual(root.find("Message").text, "hi <me> & you")

    def test_twiml_chunks_preserve_full_text_within_provider_limits(self):
        text = ("Exact expense <INR 1200> & shares. 👋\n" * 180)
        root = ElementTree.fromstring(twiml(text))
        chunks = [node.text or "" for node in root.findall("Message")]
        self.assertGreater(len(chunks), 1)
        self.assertLessEqual(len(chunks), 10)
        self.assertEqual("".join(chunks), text)
        self.assertTrue(all(len(chunk.encode("utf-16-le")) // 2 <= 1500 for chunk in chunks))
        with self.assertRaises(ValueError):
            twiml("x" * 15001)

    def test_configs_fail_closed(self):
        with self.assertRaises(ValueError):
            ServerConfig.from_env({})
        with self.assertRaises(ValueError):
            ServerConfig(TOKEN, replace(TWILIO, webhook_url="http://example.test/webhooks/twilio"))
        with self.assertRaises(ValueError):
            ServerConfig(TOKEN, replace(TWILIO, allowed_recipients=frozenset()))
        self.assertNotIn("test-auth-not-real", repr(TWILIO))
        self.assertNotIn(TOKEN, repr(ServerConfig(TOKEN)))

    def test_media_rejects_ssrf_and_other_account(self):
        good = {"NumMedia": "1", "MessageSid": MID, "MediaContentType0": "audio/ogg",
                "MediaUrl0": f"https://api.twilio.com/2010-04-01/Accounts/{ACCOUNT}/Messages/{MID}/Media/ME{'c'*32}"}
        self.assertEqual(validated_audio_media(good, ACCOUNT).message_sid, MID)
        for url in ["http://169.254.169.254/latest/meta-data", good["MediaUrl0"] + "?x=1",
                    good["MediaUrl0"].replace("api.twilio.com", "api.twilio.com.evil.test"),
                    good["MediaUrl0"].replace(ACCOUNT, "AC" + "d" * 32)]:
            with self.assertRaises(ValueError):
                validated_audio_media({**good, "MediaUrl0": url}, ACCOUNT)


class OutboundTests(unittest.TestCase):
    def test_disabled_and_allowlist_and_session_gates(self):
        opener = FakeOpener()
        with self.assertRaises(TransportDisabled):
            TwilioTransport(TWILIO, opener=opener).send(FROM, "hello")
        c = replace(TWILIO, send_enabled=True)
        with self.assertRaises(DeliveryError):
            TwilioTransport(c, opener=opener).send(FROM, "hello")
        with self.assertRaises(DeliveryError):
            TwilioTransport(c, lambda _: True, opener).send("whatsapp:+15551111111", "hello")
        self.assertEqual(opener.requests, [])

    def test_official_api_request(self):
        opener = FakeOpener()
        sid = TwilioTransport(replace(TWILIO, send_enabled=True), lambda _: True, opener).send(FROM, "hello & goodbye", "outbox-1")
        self.assertEqual(sid, MID)
        req = opener.requests[0]
        self.assertEqual(req.full_url, f"https://api.twilio.com/2010-04-01/Accounts/{ACCOUNT}/Messages.json")
        fields = urllib.parse.parse_qs(req.data.decode())
        self.assertEqual(fields, {"From": [TO], "To": [FROM], "Body": ["hello & goodbye"]})
        self.assertEqual(req.get_header("Authorization"), "Basic " + base64.b64encode(f"{ACCOUNT}:test-auth-not-real".encode()).decode())

    def test_template_without_session(self):
        opener = FakeOpener()
        c = replace(TWILIO, send_enabled=True, content_sid="HX" + "e" * 32)
        TwilioTransport(c, opener=opener).send(FROM, "Reminder")
        fields = urllib.parse.parse_qs(opener.requests[0].data.decode())
        self.assertNotIn("Body", fields)
        self.assertEqual(json.loads(fields["ContentVariables"][0]), {"1": "Reminder"})

    def test_uncertain_send_is_single_attempt_and_redacts(self):
        opener = FakeOpener(error=urllib.error.URLError("secret-token-and-message"))
        transport = TwilioTransport(replace(TWILIO, send_enabled=True), lambda _: True, opener)
        with self.assertRaises(DeliveryUncertain) as err:
            transport.send(FROM, "private-body")
        self.assertNotIn("secret", str(err.exception))
        self.assertEqual(len(opener.requests), 1)

    def test_http_rejection_and_invalid_response(self):
        opener = FakeOpener(error=urllib.error.HTTPError("url", 400, "private", {}, None))
        with self.assertRaises(DeliveryError):
            TwilioTransport(replace(TWILIO, send_enabled=True), lambda _: True, opener).send(FROM, "hi")
        opener = FakeOpener(data=b"not json")
        with self.assertRaises(DeliveryUncertain):
            TwilioTransport(replace(TWILIO, send_enabled=True), lambda _: True, opener).send(FROM, "hi")


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.engine = FakeEngine()
        self.server = make_server(self.engine, ServerConfig(TOKEN, replace(TWILIO, send_enabled=True)), port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def request(self, path, data=None, headers=None):
        req = urllib.request.Request(self.base + path, data=data, headers=headers or {})
        try:
            with urllib.request.urlopen(req) as res:
                return res.status, res.read(), res.headers
        except urllib.error.HTTPError as e:
            return e.code, e.read(), e.headers

    def webhook(self, overrides=None, *, signature=None, extra_headers=None):
        params = {"AccountSid": ACCOUNT, "MessageSid": MID, "From": FROM, "To": TO, "Body": "spent INR 120 with Maya", "NumMedia": "0"}
        params.update(overrides or {})
        headers = {"Content-Type": "application/x-www-form-urlencoded", "X-Twilio-Signature": signature or compute_signature(TWILIO.auth_token, TWILIO.webhook_url, params)}
        headers.update(extra_headers or {})
        return self.request("/webhooks/twilio?mode=demo", urllib.parse.urlencode(params).encode(), headers)

    def test_health_and_auth(self):
        code, raw, headers = self.request("/health")
        self.assertEqual(code, 200)
        self.assertNotIn(b"private", raw)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(self.request("/api/state?user_id=test")[0], 401)
        self.assertEqual(self.request("/api/state?user_id=test", headers={"Authorization": "Bearer " + TOKEN})[0], 200)

    def test_api_validates_and_deduplicates(self):
        headers = {"Content-Type": "application/json", "Authorization": "Bearer " + TOKEN}
        data = json.dumps({"user_id": "tester", "text": "hello", "message_id": "demo-1"}).encode()
        self.assertEqual(self.request("/api/message", data, headers)[0], 200)
        self.assertEqual(self.request("/api/message", data, headers)[0], 200)
        self.assertEqual(len(self.engine.calls), 1)
        for bad in [b"{", b"[]", b'{"user_id":"tester"}']:
            self.assertEqual(self.request("/api/message", bad, headers)[0], 400)
        self.assertEqual(self.request("/api/message", data, {"Content-Type": "application/json"})[0], 401)

    def test_signed_webhook_passes_exact_identity_and_sid(self):
        code, raw, _ = self.webhook(extra_headers={"Host": "evil.example", "X-Forwarded-Host": "evil.example"})
        self.assertEqual(code, 200)
        self.assertEqual(ElementTree.fromstring(raw).find("Message").text, "Ready <confirm> & save?")
        self.assertEqual(self.engine.calls, [(FROM, "spent INR 120 with Maya", MID)])
        self.webhook()
        self.assertEqual(len(self.engine.calls), 1)

    def test_webhook_bad_signature_sender_and_sid(self):
        self.assertEqual(self.webhook(signature="bad")[0], 403)
        self.assertEqual(self.webhook({"From": "whatsapp:+15551111111"})[0], 403)
        self.assertEqual(self.webhook({"AccountSid": "AC" + "c"*32})[0], 403)
        self.assertEqual(self.webhook({"MessageSid": "fake"})[0], 400)
        self.assertEqual(self.engine.calls, [])

    def test_media_without_stt_is_honest_no_processing(self):
        code, raw, _ = self.webhook({"NumMedia": "1", "Body": "", "MediaUrl0": "https://evil.example/secret"})
        self.assertEqual(code, 200)
        self.assertIn(b"transcription isn't connected", raw)
        self.assertEqual(self.engine.calls, [])

    def test_duplicate_form_keys_and_url_mismatch_rejected(self):
        headers = {"Content-Type": "application/x-www-form-urlencoded", "X-Twilio-Signature": "bad"}
        self.assertEqual(self.request("/webhooks/twilio?mode=demo", b"From=a&From=b", headers)[0], 400)
        self.assertEqual(self.request("/webhooks/twilio", b"From=a", headers)[0], 403)

    def test_adapter_exception_does_not_leak(self):
        def fail(*args):
            raise RuntimeError("super-secret-private-text")
        self.engine.handle = fail
        code, raw, _ = self.webhook()
        self.assertEqual(code, 500)
        self.assertNotIn(b"secret", raw)


if __name__ == "__main__":
    unittest.main()
