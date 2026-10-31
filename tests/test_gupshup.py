from dataclasses import replace
from datetime import datetime, timezone
import io
import json
import time
import unittest
import urllib.error
from urllib.parse import parse_qs

from plus_one.gupshup import (GupshupConfig, GupshupTransport, GupshupTranscriber,
    GupshupMedia, parse_inbound_event, validate_webhook_secret, validate_gupshup_media,
    configured_gupshup_transcriber, SEND_URL, TEMPLATE_URL)
from plus_one.speech import SpeechConfig, SpeechError, SpeechConfigurationError
from plus_one.transport import DeliveryError, DeliveryUncertain, TransportDisabled

APP_ID = "11111111-2222-3333-4444-555555555555"
MEDIA_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
USER = "whatsapp:+15551234567"
SECRET = "random-separate-webhook-secret-12345678"
CONFIG = GupshupConfig(api_key="fake-api-key", app_name="PlusOneDemo", app_id=APP_ID,
    source="15557654321", allowed_recipients=frozenset({USER}), webhook_secret=SECRET)
NOW = datetime(2026, 10, 31, 9, 0, tzinfo=timezone.utc)


def event(kind="text", data=None):
    return {"app":"PlusOneDemo", "timestamp":NOW.timestamp()*1000, "version":2, "type":"message",
        "payload":{"id":"wamid.abc123", "source":"15551234567", "type":kind,
            "payload":data or {"text":"spent INR 120 with Maya"}, "sender":{"phone":"15551234567","name":"Tester"}}}


def media_url():
    return f"https://filemanager.gupshup.io/wa/{APP_ID}/wa/media/{MEDIA_ID}?download=false"


class Response(io.BytesIO):
    def __init__(self, body=b'{"status":"submitted","messageId":"provider-id-1"}', content_type="text/html"):
        super().__init__(body)
        self.headers = {"Content-Type":content_type}


class FakeClient:
    def __init__(self, responses=None):
        self.responses = responses or [Response()]
        self.requests = []

    def open(self, request, timeout):
        self.requests.append(request)
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class GupshupInboundTests(unittest.TestCase):
    def test_header_auth_is_separate_secret_not_signature(self):
        self.assertTrue(validate_webhook_secret(CONFIG, SECRET))
        for value in ["", "bad", "fake-api-key", "👋"]:
            self.assertFalse(validate_webhook_secret(CONFIG, value))
        with self.assertRaises(ValueError):
            replace(CONFIG, webhook_secret=CONFIG.api_key).validate_inbound()
        self.assertNotIn(SECRET, repr(CONFIG))
        self.assertNotIn("fake-api-key", repr(CONFIG))

    def test_text_schema_and_serialization(self):
        parsed = parse_inbound_event(CONFIG, event(), NOW)
        self.assertEqual(parsed.user_id, USER)
        self.assertEqual(parsed.message_id, "wamid.abc123")
        self.assertEqual(parsed.text, "spent INR 120 with Maya")
        self.assertEqual(parsed.received_at, NOW.isoformat())
        self.assertIsNone(parsed.media)
        self.assertEqual(json.loads(json.dumps(parsed.to_dict()))["kind"], "text")

    def test_wrong_app_version_sender_identity_and_timestamp_rejected(self):
        bad_events = []
        for key, val in [("app","OtherApp"),("version",1),("timestamp",0),("timestamp",(NOW.timestamp()+301)*1000)]:
            bad = event(); bad[key]=val; bad_events.append(bad)
        bad=event(); bad["payload"]["source"]="15551111111"; bad_events.append(bad)
        bad=event(); bad["payload"]["sender"]["phone"]="15551111111"; bad_events.append(bad)
        bad=event(); bad["payload"]["id"]=""; bad_events.append(bad)
        for bad in bad_events:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                parse_inbound_event(CONFIG,bad,NOW)

    def test_delivery_events_ignored_not_treated_as_chat(self):
        payload = event(); payload["type"]="message-event"
        self.assertIsNone(parse_inbound_event(CONFIG,payload,NOW))
        payload["type"]="user-event"
        self.assertIsNone(parse_inbound_event(CONFIG,payload,NOW))
        self.assertEqual(parse_inbound_event(CONFIG,event("image",{"url":"https://evil.test"}),NOW).kind,"unsupported")

    def test_audio_schema_scoped_to_app_and_expiry(self):
        parsed = parse_inbound_event(CONFIG,event("audio",{"url":media_url(),"contentType":"audio/ogg; codecs=opus","urlExpiry":(NOW.timestamp()+60)*1000}),NOW)
        self.assertEqual(parsed.media.content_type,"audio/ogg")
        self.assertEqual(parsed.to_dict()["media"]["app_id"],APP_ID)
        sandbox_url=f"https://filemanager.gupshup.io/wa/{APP_ID}/wa/media/opaqueMedia_123?download=false"
        parsed_sandbox=parse_inbound_event(CONFIG,event("audio",{"url":sandbox_url,"contentType":"audio/ogg; codecs=opus"}),NOW)
        self.assertEqual(parsed_sandbox.media.url,sandbox_url)
        for bad_suffix in ("../outside", "other/item", "opaque%2Fitem", "opaque.item", ""):
            bad=f"https://filemanager.gupshup.io/wa/{APP_ID}/wa/media/{bad_suffix}?download=false"
            with self.subTest(suffix=bad_suffix),self.assertRaises(ValueError):
                parse_inbound_event(CONFIG,event("audio",{"url":bad,"contentType":"audio/ogg"}),NOW)
        for bad_url in ["https://169.254.169.254/latest",media_url().replace(APP_ID,MEDIA_ID),media_url()+"&url=https://evil.test",media_url().replace("filemanager.gupshup.io","filemanager.gupshup.io.evil.test")]:
            with self.assertRaises(ValueError):
                parse_inbound_event(CONFIG,event("audio",{"url":bad_url,"contentType":"audio/ogg"}),NOW)
        with self.assertRaises(ValueError):
            parse_inbound_event(CONFIG,event("audio",{"url":media_url(),"contentType":"audio/ogg","urlExpiry":(NOW.timestamp()-1)*1000}),NOW)


class GupshupOutboundTests(unittest.TestCase):
    def test_disabled_allowlist_and_session_gates_no_network(self):
        client=FakeClient()
        with self.assertRaises(TransportDisabled):
            GupshupTransport(CONFIG,opener=client).send(USER,"hello")
        with self.assertRaises(DeliveryError):
            GupshupTransport(replace(CONFIG,send_enabled=True),opener=client).send(USER,"hello")
        with self.assertRaises(DeliveryError):
            GupshupTransport(replace(CONFIG,send_enabled=True),lambda _:True,client).send("whatsapp:+15551111111","hello")
        self.assertEqual(client.requests,[])

    def test_current_wa_session_api_form_and_acceptance(self):
        client=FakeClient()
        mid=GupshupTransport(replace(CONFIG,send_enabled=True),lambda _:True,client).send(USER,"Hello & goodbye","id1")
        self.assertEqual(mid,"provider-id-1")
        request=client.requests[0]
        self.assertEqual(request.full_url,SEND_URL)
        self.assertEqual(request.get_header("Apikey"),"fake-api-key")
        fields=parse_qs(request.data.decode())
        self.assertEqual(fields["source"],[CONFIG.source])
        self.assertEqual(fields["destination"],["15551234567"])
        self.assertEqual(fields["src.name"],["PlusOneDemo"])
        self.assertEqual(json.loads(fields["message"][0]),{"type":"text","text":"Hello & goodbye"})

    def test_out_of_session_template_and_invalid_fields(self):
        client=FakeClient()
        config=replace(CONFIG,send_enabled=True,template_id=MEDIA_ID)
        GupshupTransport(config,opener=client).send(USER,"Reminder")
        fields=parse_qs(client.requests[0].data.decode())
        self.assertEqual(client.requests[0].full_url,TEMPLATE_URL)
        self.assertEqual(json.loads(fields["template"][0]),{"id":MEDIA_ID,"params":["Reminder"]})
        for body in ["multi\nline","x"*4097]:
            with self.assertRaises(DeliveryError):
                GupshupTransport(config,opener=client).send(USER,body)
        self.assertEqual(len(client.requests),1)

    def test_deprecated_or_arbitrary_endpoints_rejected(self):
        for url in ["https://api.gupshup.io/sm/api/v1/msg","https://evil.example/wa/api/v1/msg"]:
            with self.assertRaises(TransportDisabled):
                GupshupTransport(replace(CONFIG,send_enabled=True,send_url=url),lambda _:True).send(USER,"hi")

    def test_uncertain_rejection_and_single_attempt(self):
        for result, error_type in [(TimeoutError("fake-api-key private body"),DeliveryUncertain),
                (urllib.error.HTTPError(SEND_URL,400,"private",{},None),DeliveryError),
                (Response(b'{"status":"other"}'),DeliveryUncertain)]:
            client=FakeClient([result])
            with self.assertRaises(error_type) as error:
                GupshupTransport(replace(CONFIG,send_enabled=True),lambda _:True,client).send(USER,"hi")
            self.assertNotIn("fake-api-key",str(error.exception))
            self.assertNotIn("private",str(error.exception))
            self.assertEqual(len(client.requests),1)


class GupshupSpeechTests(unittest.TestCase):
    def test_disabled_and_unauthorized(self):
        self.assertIsNone(configured_gupshup_transcriber(CONFIG,{}))
        with self.assertRaises(SpeechConfigurationError):
            configured_gupshup_transcriber(CONFIG,{"STT_ENABLED":"true"})

    def test_download_without_api_key_then_authorized_stt(self):
        client=FakeClient([Response(b"OggS-audio","audio/ogg"),Response(b'{"text":"spent INR 12 with Maya"}',"application/json")])
        speech=SpeechConfig(True,True,"https://stt.example.com/v1","transcribe","fake-stt-key")
        media=GupshupMedia(media_url(),"audio/ogg","msg1",APP_ID,"PlusOneDemo",time.time()+60)
        text=GupshupTranscriber(CONFIG,speech,client).transcribe(media.to_dict())
        self.assertEqual(text,"spent INR 12 with Maya")
        self.assertIsNone(client.requests[0].get_header("Authorization"))
        self.assertIsNone(client.requests[0].get_header("Apikey"))
        self.assertEqual(client.requests[1].get_header("Authorization"),"Bearer fake-stt-key")

    def test_audio_expiry_and_mime_failure_no_provider_upload(self):
        speech=SpeechConfig(True,True,"https://stt.example.com/v1","transcribe","fake-stt-key")
        client=FakeClient([Response(b"private html","text/html")])
        media=GupshupMedia(media_url(),"audio/ogg","msg1",APP_ID,"PlusOneDemo",time.time()-1)
        adapter=GupshupTranscriber(CONFIG,speech,client)
        with self.assertRaises(SpeechError):
            adapter.transcribe(media)
        self.assertEqual(client.requests,[])
        with self.assertRaises(SpeechError):
            adapter.transcribe(replace(media,expires_at=time.time()+60))
        self.assertEqual(len(client.requests),1)


if __name__ == "__main__":
    unittest.main()
