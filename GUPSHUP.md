# Gupshup — selected WhatsApp transport

Gupshup is the user's chosen primary WhatsApp transport; Twilio is retained as an
optional fallback. This is a local PoC: no account creation, deposit, payment,
subscription change, deployment, or live provider call has been performed.

## Required account/configuration

| Variable | Meaning |
| --- | --- |
| `ENABLE_GUPSHUP_SEND` | Defaults false. Set true only for authorized tester transport. |
| `GUPSHUP_API_KEY` | Outbound API credential; never use it as the webhook secret. |
| `GUPSHUP_APP_NAME` | Exact app name, used as `src.name` and checked against callback `app`. |
| `GUPSHUP_APP_ID` | App UUID, required to scope current inbound media URLs. |
| `GUPSHUP_SOURCE` | Registered or sandbox source phone, country-code digits without `+`. |
| `GUPSHUP_ALLOWED_RECIPIENTS` | Comma-separated authorized testers, `whatsapp:+15551234567`. |
| `GUPSHUP_WEBHOOK_SECRET` | Separate random ASCII secret, at least 32 characters. |
| `GUPSHUP_TEMPLATE_ID` | Optional approved text-template UUID with one body parameter; required outside a verified 24-hour session. |
| `GUPSHUP_SEND_URL` | Optional; only `https://api.gupshup.io/wa/api/v1/msg` accepted. |
| `GUPSHUP_TEMPLATE_URL` | Optional; only `https://api.gupshup.io/wa/api/v1/template/msg` accepted. |

This adapter uses only current `/wa` APIs. Older `/sm` examples are deliberately
rejected: [Gupshup's deprecation notice](https://support.gupshup.io/hc/en-us/articles/34057158742297-Prepare-for-sm-endpoints-End-of-Life-DEPRECATED)
requires migration. The send adapter implements the [current send API](https://docs.gupshup.io/reference/msg)
and [template API](https://docs.gupshup.io/docs/template-messages). A response
`status=submitted` with `messageId` means **accepted, not delivered**. Delivery and
read confirmations require authenticated status-event processing; the PoC does not
claim delivery from an API acceptance response.

## Secure webhook setup: account-dependent, fail closed

No Gupshup HMAC signature scheme is invented or assumed. The server must require
`X-Gupshup-Webhook-Secret` and compare it in constant time, then verify app identity,
v2 schema, tester sender, and duplicate message ID before business processing.

Gupshup's [partner subscription API](https://partner-docs.gupshup.io/reference/setsubscription-api-v3)
explicitly supports a `meta` JSON map forwarded as callback HTTP headers, e.g.:

```json
{"X-Gupshup-Webhook-Secret":"<separate random secret>"}
```

That endpoint is `POST https://partner.gupshup.io/partner/app/{appId}/subscription`,
with the partner app token in `Authorization`, subscription `version=2`, HTTPS URL,
unique tag and selected event modes. This is setup documentation only: no
subscription is created by the application.

**Self-serve account caveat:** The public self-serve subscription update reference
does not establish equivalent custom-header support. Confirm that the actual
account can configure this header before enabling its callback. Without verified
header delivery the webhook remains unusable/fail-closed; do not remove auth or
paste the outbound API key into a public URL to make a test work. TLS ingress is
also required for any subsequently authorized deployment.

## Ingress and durable processing contract

The accepted message event is:

```json
{
  "app": "PlusOneDemo", "timestamp": 1793437200000, "version": 2,
  "type": "message",
  "payload": {
    "id": "wamid.unique-message-id", "source": "15551234567", "type": "text",
    "payload": {"text": "spent INR 1200 with Maya"},
    "sender": {"phone": "15551234567", "name": "Tester"}
  }
}
```

`parse_inbound_event` returns a serializable dataclass with canonical `user_id`,
`message_id`, `kind`, `text`, `media`, and vendor-timestamp `received_at`. The timestamp
is not reset on retry, so replay cannot reopen a WhatsApp session. Events beyond
seven days old or five minutes in the future are rejected. `message-event` and
other known system events return `None`; they are not interpreted as user messages.

The HTTP integration must durably enqueue by `(provider, message_id)` and return
empty 2xx quickly, then let a worker process the engine and queue outgoing replies.
Gupshup does not interpret TwiML or send callback response bodies as WhatsApp replies.
The [callback guidance](https://docs.gupshup.io/docs/subscriptions-and-notifications)
expects prompt acknowledgement; no media fetch, STT, or outbound send should block
that acknowledgement. One send attempt is made; uncertain sends require manual
reconciliation instead of blind retry. A local idempotency key does not create an
upstream exactly-once guarantee.

## Voice notes

`configured_gupshup_transcriber(config, env)` uses the same explicit `STT_ENABLED`,
`STT_PROVIDER_AUTHORIZED`, `STT_API_BASE_URL`, `STT_MODEL`, and `STT_API_KEY` gates as
[SPEECH.md](SPEECH.md). It accepts the dataclass or its serialized media dictionary.

The [official media schema](https://docs.gupshup.io/docs/media) provides a downloadable
`filemanager.gupshup.io` URL, content type, and expiry. This PoC permits only the exact
configured app UUID under `/wa/{appId}/wa/media/{mediaUuid}` with optional
`download=false|true`, or the documented legacy `/fm/wamedia/{appName}/{mediaId}`
shape. Expired/foreign-app/arbitrary-host links fail closed. The media URL contract
does not document an API-key header: **the outbound API key is never sent to the
media host**. Redirects are not followed. Public DNS checks, pinned TLS connections,
10 MiB limit and MIME validation apply. No raw media is persisted or logged.

The media URL may be a bearer-capability URL; treat it as private in the durable
inbox and purge it with the user's data. Actual account-specific media behavior,
URL format, retention, and STT compatibility remain pending live verification.

## Local verification

```sh
python -m unittest discover -s tests -p test_gupshup.py -v
python -m unittest discover -s tests -p test_speech.py -v
```

All external requests are mocked. Coverage includes current API fields, template
and session gates, constant-time secret auth, sender/app checks, serializable v2
text/audio schema, stale events, media SSRF/expiry/MIME rejection, credentials not
sent to media hosts, sanitized errors, and no automatic retries. **Actual WhatsApp
receipt/delivery, sandbox enrollment, webhook-header support, approved template
availability and STT recognition accuracy have not been verified live.**
