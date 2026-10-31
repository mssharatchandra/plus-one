"""Loopback-only-by-default HTTP demo API and signed Twilio webhook.

This stdlib HTTP server is a local PoC, not a production-facing web server. Put an
approved TLS reverse proxy in front only after deployment authorization. The
configured public webhook URL is used for signing; Host/Forwarded headers are not.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import re
import threading
from typing import Mapping
from urllib.parse import parse_qsl, urlsplit
from xml.sax.saxutils import escape

from .gupshup import GupshupConfig, validate_webhook_secret, parse_inbound_event
from .timeparse import parse_reminder
from .transport import SID, Transcriber, TwilioConfig, validate_signature, validated_audio_media

MAX_BODY = 16384
MAX_TEXT = 4000
USER_ID = re.compile(r"[A-Za-z0-9_:@+.\-]{1,128}\Z")
MESSAGE_ID = re.compile(r"[A-Za-z0-9_:@+.\-]{1,128}\Z")
VOICE_UNAVAILABLE = "I received your voice note, but transcription isn't connected in this demo yet. Please type what you said; I'll show you a proposal before saving an expense or reminder."


@dataclass(frozen=True)
class ServerConfig:
    api_token: str = field(repr=False)
    twilio: TwilioConfig = field(default_factory=TwilioConfig)
    gupshup: GupshupConfig = field(default_factory=GupshupConfig)
    bolna_tool_secret: str = field(default="", repr=False)

    def __post_init__(self):
        if not isinstance(self.api_token, str) or len(self.api_token) < 24:
            raise ValueError("DEMO_API_TOKEN must contain at least 24 characters. Generate a random token; do not commit it.")
        if self.twilio.webhook_url:
            self.twilio.validate_inbound()
        if self.gupshup.webhook_secret or self.gupshup.send_enabled:
            self.gupshup.validate_inbound()
        if self.bolna_tool_secret and (len(self.bolna_tool_secret) < 32 or
                not self.bolna_tool_secret.isascii() or
                any(c.isspace() for c in self.bolna_tool_secret) or
                self.bolna_tool_secret in {self.api_token, self.gupshup.webhook_secret}):
            raise ValueError("BOLNA_TOOL_SECRET must be a distinct ASCII secret of at least 32 characters.")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None):
        env = os.environ if env is None else env
        return cls(env.get("DEMO_API_TOKEN", ""), TwilioConfig.from_env(env), GupshupConfig.from_env(env),
                   env.get("BOLNA_TOOL_SECRET", ""))


def twiml(text: str) -> bytes:
    # Strip XML 1.0 forbidden control code points, preserving normal whitespace.
    safe = "".join(c for c in text if c in "\t\n\r" or 0x20 <= ord(c) <= 0xD7FF or 0xE000 <= ord(c) <= 0xFFFD or 0x10000 <= ord(c) <= 0x10FFFF)
    # Twilio permits <=10 Message verbs. Keep each below 1600 characters;
    # count UTF-16 units conservatively for non-BMP characters such as emoji.
    # Sources: /docs/messaging/twiml/message and /docs/api/errors/14109.
    chunks, start, units = [], 0, 0
    for i, char in enumerate(safe):
        width = 2 if ord(char) > 0xFFFF else 1
        if units + width > 1500:
            chunks.append(safe[start:i])
            start, units = i, 0
        units += width
    chunks.append(safe[start:])
    if len(chunks) > 10:
        raise ValueError("Reply exceeds Twilio's ten-message limit; narrow the requested output.")
    messages = "".join("<Message>" + escape(chunk) + "</Message>" for chunk in chunks)
    return ('<?xml version="1.0" encoding="UTF-8"?><Response>' + messages + '</Response>').encode()


def _form(raw: bytes) -> dict[str, str]:
    try:
        pairs = parse_qsl(raw.decode("utf-8", errors="strict"), keep_blank_values=True,
                          strict_parsing=True, max_num_fields=100, errors="strict")
    except (ValueError, UnicodeError):
        raise ValueError("Invalid form.") from None
    if len({key for key, _ in pairs}) != len(pairs):
        raise ValueError("Duplicate form keys are unsupported.")
    return dict(pairs)


def make_server(engine, config: ServerConfig, host: str = "127.0.0.1", port: int = 8080,
                transcriber: Transcriber | None = None) -> ThreadingHTTPServer:
    """engine.handle(user_id, text, message_id)->str; engine.snapshot(user_id)->dict.

    Engine is responsible for transactional MessageSid deduplication and its own
    thread safety. No scheduler is started implicitly by the HTTP server.
    """
    media_lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        server_version = "PlusOneDemo"
        sys_version = ""

        def setup(self):
            super().setup()
            self.connection.settimeout(10)

        def log_message(self, fmt, *args):
            # Default HTTP logs expose user identifiers, message text and headers.
            pass

        def _respond(self, code, payload, content_type="application/json; charset=utf-8"):
            raw = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=True).encode()
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(raw)

        def _auth(self):
            expected = "Bearer " + config.api_token
            provided = self.headers.get("Authorization", "")
            if len(provided) > 4096 or not hmac.compare_digest(provided.encode(), expected.encode()):
                self._respond(401, {"error": "Unauthorized"})
                return False
            return True

        def _body(self, expected_type):
            if self.headers.get("Transfer-Encoding") or len(self.headers.get_all("Content-Length", [])) != 1:
                raise ValueError("A single Content-Length is required.")
            if self.headers.get_content_type() != expected_type:
                raise ValueError("Unsupported Content-Type.")
            try:
                length = int(self.headers.get("Content-Length", ""))
            except ValueError:
                raise ValueError("Invalid Content-Length.") from None
            if length < 0 or length > MAX_BODY:
                raise ValueError("Request body exceeds the demo limit.")
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ValueError("Incomplete request body.")
            return raw

        def do_GET(self):
            path = urlsplit(self.path)
            if path.path == "/health":
                self._respond(200, {"status": "ok", "mode": "local-poc"})
            elif path.path == "/api/state":
                if not self._auth():
                    return
                try:
                    args = _form(path.query.encode())
                    user_id = args.get("user_id", "")
                    if not USER_ID.fullmatch(user_id):
                        raise ValueError("Provide a valid user_id.")
                    self._respond(200, engine.snapshot(user_id))
                except ValueError as exc:
                    self._respond(400, {"error": str(exc)})
                except Exception:
                    self._respond(500, {"error": "State is temporarily unavailable."})
            else:
                self._respond(404, {"error": "Not found"})

        def do_POST(self):
            path = urlsplit(self.path)
            if path.path == "/tools/bolna/reminder-preview":
                if not config.bolna_tool_secret:
                    self._respond(404, {"error": "Not found"}); return
                expected = "Bearer " + config.bolna_tool_secret
                authorization = self.headers.get_all("Authorization", [])
                if (len(authorization) != 1 or len(authorization[0]) > 4096 or
                        not authorization[0].isascii() or
                        not hmac.compare_digest(authorization[0], expected)):
                    self._respond(401, {"error": "Unauthorized"}); return
                try:
                    data = json.loads(self._body("application/json"))
                    if not isinstance(data, dict) or set(data) != {"text"}:
                        raise ValueError("One text field required.")
                    text = data["text"]
                    if not isinstance(text, str) or not 1 <= len(text) <= 1000:
                        raise ValueError("A short reminder request is required.")
                    proposal = parse_reminder(text, "Asia/Kolkata")
                except (ValueError, UnicodeError):
                    self._respond(200, {"status": "clarify", "question":
                        "Please say the task and an explicit future time, such as tomorrow at 9am.",
                        "saved": False}); return
                self._respond(200, {"status": "proposal", "title": proposal["title"],
                    "due_at": proposal["due_at"], "local_display": proposal["local_display"],
                    "saved": False})
            elif path.path == "/api/message":
                if not self._auth():
                    return
                try:
                    data = json.loads(self._body("application/json"))
                    if not isinstance(data, dict):
                        raise ValueError("JSON object required.")
                    user_id, text, mid = (data.get(k) for k in ("user_id", "text", "message_id"))
                    if not isinstance(user_id, str) or not USER_ID.fullmatch(user_id):
                        raise ValueError("Provide a valid user_id.")
                    if not isinstance(mid, str) or not MESSAGE_ID.fullmatch(mid):
                        raise ValueError("Provide a unique message_id (reuse only to retry the same message).")
                    if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT:
                        raise ValueError("text must contain 1–4000 characters.")
                except (ValueError, UnicodeError) as exc:
                    self._respond(400, {"error": str(exc) if not isinstance(exc, json.JSONDecodeError) else "Invalid JSON."})
                    return
                try:
                    reply = engine.handle(user_id, text, mid)
                    self._respond(200, {"reply": reply, "transport": "local-demo"})
                except Exception:
                    self._respond(500, {"error": "Message could not be processed. Retry with the same message_id."})
            elif path.path == "/webhooks/gupshup":
                self._gupshup()
            elif path.path == "/webhooks/twilio":
                self._twilio()
            else:
                self._respond(404, {"error": "Not found"})

        def _gupshup(self):
            c=config.gupshup
            if not c.send_enabled or not c.webhook_secret:
                self._respond(503,{"error":"Gupshup webhook is not enabled."}); return
            if self.path!='/webhooks/gupshup':
                self._respond(403,{"error":"Invalid webhook URL."}); return
            if len(self.headers.get_all('X-Gupshup-Webhook-Secret',[]))!=1 or not validate_webhook_secret(c,self.headers.get('X-Gupshup-Webhook-Secret','')):
                self._respond(403,{"error":"Invalid webhook authentication."}); return
            try:
                event=json.loads(self._body('application/json'))
                incoming=parse_inbound_event(c,event)
                if incoming is not None:
                    engine.enqueue_inbound('gupshup',incoming.message_id,incoming.user_id,incoming.to_dict())
            except (ValueError,UnicodeError):
                self._respond(400,{"error":"Invalid or unauthorized Gupshup event."}); return
            except Exception:
                self._respond(503,{"error":"Event was not accepted; retry with the same message ID."}); return
            # Provider expects a quick, empty 2xx. All user work is durable/asynchronous.
            self._respond(200,b'',"text/plain; charset=utf-8")

        def _twilio(self):
            c = config.twilio
            if not c.webhook_url or not c.send_enabled:
                self._respond(503, {"error": "WhatsApp webhook is not configured or live replies are disabled."})
                return
            expected_url = urlsplit(c.webhook_url)
            expected_target = expected_url.path + ("?" + expected_url.query if expected_url.query else "")
            if self.path != expected_target:
                self._respond(403, {"error": "Invalid webhook URL."})
                return
            try:
                params = _form(self._body("application/x-www-form-urlencoded"))
            except ValueError:
                self._respond(400, {"error": "Invalid webhook form."})
                return
            if len(self.headers.get_all("X-Twilio-Signature", [])) != 1 or not validate_signature(c.auth_token, c.webhook_url, params, self.headers.get("X-Twilio-Signature", "")):
                self._respond(403, {"error": "Invalid webhook signature."})
                return
            sender, sid = params.get("From", ""), params.get("MessageSid", "")
            if sender not in c.allowed_recipients or params.get("To") != c.sender or params.get("AccountSid") != c.account_sid:
                self._respond(403, {"error": "Webhook identity is not authorized."})
                return
            if not SID.fullmatch(sid):
                self._respond(400, {"error": "Valid MessageSid required."})
                return
            text = params.get("Body", "").strip()
            if params.get("NumMedia", "0") != "0":
                # Serialize voice preflight+transcribe+receipt to prevent a retried
                # webhook charging STT twice while the first request is in flight.
                with media_lock:
                    cached = engine.inbound_preflight(sender,sid) if hasattr(engine,'inbound_preflight') else None
                    if cached is not None:
                        self._respond(200,twiml(cached),"application/xml; charset=utf-8")
                        return
                    if transcriber is None:
                        reply = VOICE_UNAVAILABLE
                    else:
                        try:
                            text = transcriber.transcribe(validated_audio_media(params,c.account_sid))
                            if not isinstance(text,str) or not text.strip() or len(text)>MAX_TEXT:
                                raise ValueError('Invalid transcript')
                            reply = engine.handle(sender,text,sid)
                        except Exception:
                            reply = "I couldn't transcribe that voice note. Please type the message instead; nothing from the note has been saved."
                    if hasattr(engine,'remember_response'):
                        engine.remember_response(sender,sid,reply)
                    self._respond(200,twiml(reply),"application/xml; charset=utf-8")
                    return
            if not text or len(text) > MAX_TEXT:
                self._respond(400, {"error": "Message text is empty or too long."})
                return
            try:
                reply = engine.handle(sender, text, sid)
                self._respond(200, twiml(reply), "application/xml; charset=utf-8")
            except Exception:
                self._respond(500, {"error": "Message processing failed. Retry with the same MessageSid."})

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    return server
