# Plus One · three-flow proof of concept

A warm, WhatsApp-oriented assistant for expenses, reminders, and small learning steps. **All three flows run locally today without credentials.** This is a deterministic backend demo—not a deployed service, live WhatsApp demonstration, or production pilot.

## Run in one minute

Requires Python **3.12**, an OS timezone database, and a POSIX environment for the worker lock. No third-party Python packages or installation are required.

```sh
cd plus-one
python -m plus_one demo
python -m unittest discover -s tests -v
python -m plus_one --db ./private-demo.sqlite3 chat --user demo
```

`demo` uses a temporary database and artificial clock. It demonstrates all three flows, expense confirmation, database restart, reminder deduplication, snoozing, learning continuity, stop, and deletion. It ends with an explicit offline PASS; that is **not evidence of WhatsApp delivery, voice recognition, or LLM execution**.

In `chat`, type `/quit` to exit or `/tick` to process due reminders as local simulations. Ordinary chat alone does not run a background scheduler.

## Try the three flows

Commands are case-insensitive; names and text retain their spelling. In a new chat, first set shared preferences:

```text
name Alex
confirm
timezone Asia/Kolkata
confirm
currency INR
confirm
```

### 1. Capture an expense → preview shares → confirm → private balances

```text
expense INR 100 for dinner with Maya, Rahul
confirm
balances
```

The preview allocates **INR 33.34 to you and INR 33.33 to each friend**. The remainder is distributed in input order, starting with you. `BALANCES` counts confirmed expenses only. Money is parsed with `Decimal` and stored in integer minor units—never binary floating-point arithmetic.

Supported forms include `spent INR 1200 on dinner with Maya and Rahul` and `paid 10.00 USD for coffee with Ana, Ben`. Supply named participants and an explicit supported ISO currency, or confirm a default with `currency INR` then `confirm`. A message’s explicit currency overrides the default. `I paid 600 for dinner split with Maya and Rahul` also works after setting a default. Supported currencies: AED, AUD, CAD, CHF, EUR, GBP, INR, JPY, KWD, NZD, SGD, USD. Currency precision is respected; invalid extra precision is not silently rounded.

**Named payer:** `Maya paid INR 600 for dinner with me and Rahul` proposes an equal split including you, Maya, and Rahul; only your share is added to what you owe Maya. Private balances net what each friend owes you against what you owe them, separately by currency. The payer and you are included exactly once.

**Scope:** equal splits only. Unequal shares, multiple payers, repayments, currency conversion, ambiguous symbols, and contact resolution are not implemented. Friends are private labels, not contacted people. No Splitwise writes or payments occur.

### 2. Capture a task → confirm local time → remind → done/snooze/cancel

```text
remind me to send the project update tomorrow at 9am
confirm
tasks
snooze 1 10m
confirm
done 1
```

Use the actual task ID printed by `TASKS`; `cancel 1` cancels instead of completing. Also accepted: `remind me to call Sam on 2026-11-02 at 14:00` (choose a future date) and `remind me to stretch in 5 minutes`. Relative durations may be seconds, minutes, hours, or days, up to 30 days. Vague timing, past dates, and ambiguous/nonexistent daylight-saving times require clarification.

Your timezone must be explicitly confirmed. Default quiet hours are **22:00–08:00 in the saved timezone**; reminders due during that window wait until it ends. Change them with `quiet 23:00-07:00` then `confirm`, or `quiet off` then `confirm`. The preview explains quiet-hour delay. Existing task instants do not move when you change timezone.

Run a scheduler in another terminal using the **same database**:

```sh
python -m plus_one --db ./private-demo.sqlite3 worker
```

This worker is offline by default. Tasks/outbox survive restart; a unique task/revision key prevents duplicate queue entries. Only one worker can hold the database's POSIX process lock. `serve` also runs a worker, so do not run `worker` alongside it for the same database.

### 3. Start a curiosity micro-journey → reflect → continue

```text
learn storytelling for work presentations in 5 minutes
reflection One concrete moment is easier to follow than a broad summary.
next
journey
memory
```

Storytelling is the supported curriculum, with a chosen goal and a 1–20 minute time budget. Each small step includes a lesson, exercise, and reflection prompt. `NEXT` waits for a reflection; later steps recall your latest reflection across process restarts. Replacing an existing journey requires confirmation and clears its reflections. This is a small authored curriculum, not unrestricted AI tutoring.

## Memory and control

- `memory` / `profile`: see remembered name, timezone, default currency, tone, quiet hours, and learning progress.
- `tone warm` / `tone concise`: selects reminder wording; not every reply is rewritten.
- `confirm`: accepts the latest pending proposal. `discard` abandons it. Proposals expire after 15 minutes; a new proposal replaces the previous one.
- Expenses and reminders are saved as **draft proposals first**, not recorded balances or scheduled tasks until confirmed. Drafts themselves are persisted.
- `stop`: pauses interaction and cancels active reminders while retaining memory. `start` resumes; it does not resurrect cancelled reminders.
- `delete my data`, then `confirm`: logically deletes profile, drafts, expenses, tasks, journey/reflections, and outgoing queues, scrubs personal data from inbound queues, and clears cached personal replies. Minimal hashed replay identifiers, timestamps, and a hashed suppression marker remain; `START` clears suppression and starts fresh. See [SECURITY.md](SECURITY.md) for deletion limits.

## CLI and authenticated local API

`--db` goes **before** the subcommand:

```sh
python -m plus_one --db ./private-demo.sqlite3 message 'help' --user demo --id example-1
python -m plus_one --db ./private-demo.sqlite3 state --user demo
python -m plus_one --db ./private-demo.sqlite3 tick
python -m plus_one --db ./private-demo.sqlite3 worker
```

`message` uses a fresh message ID unless `--id` is supplied. Reuse an ID only to retry the **same** message; replay does not execute it again. `state` prints private data to your terminal. `tick` performs one **simulated** scheduler pass, never live sending.

For the HTTP server, generate a local token rather than copying one into chat:

```sh
export DEMO_API_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
python -m plus_one --db ./private-demo.sqlite3 serve --port 8080
```

From a shell with the same token:

```sh
curl -s http://127.0.0.1:8080/health
curl -s http://127.0.0.1:8080/api/message \
  -H "Authorization: Bearer $DEMO_API_TOKEN" -H 'Content-Type: application/json' \
  -d '{"user_id":"demo","message_id":"api-example-1","text":"help"}'
curl -s 'http://127.0.0.1:8080/api/state?user_id=demo' \
  -H "Authorization: Bearer $DEMO_API_TOKEN"
```

The server binds to **127.0.0.1** by default. `/health` contains no user data. Both API endpoints require a token of at least 24 characters. **This is an operator/admin API: one token can access every user ID. It is not tenant authentication or a safe public application frontend.** No deployment or public tunnel is included or authorized by these instructions.

## Optional live integrations: setup still pending

`.env.example` lists the exact variables; it is a reference, **not automatically loaded**. Keep secrets in your authorized environment/secret manager, never source control, chat, screenshots, or shared shell transcripts.

### Gupshup WhatsApp — selected primary

Gupshup is the selected primary transport; Twilio is retained as an optional fallback. The implemented adapter uses current `/wa` messaging/template endpoints, not deprecated `/sm` routes. See [GUPSHUP.md](GUPSHUP.md) for exact account setup, payload contract, media constraints, and mocked verification. All external operations remain disabled by default; no account signup, spend, deployment, or live send has occurred.

Enable only after explicit tester/account/cost authorization. Required settings are `GUPSHUP_API_KEY`, `GUPSHUP_APP_NAME`, `GUPSHUP_APP_ID`, `GUPSHUP_SOURCE`, `GUPSHUP_ALLOWED_RECIPIENTS`, and a separate random `GUPSHUP_WEBHOOK_SECRET` of at least 32 characters. `ENABLE_GUPSHUP_SEND=true` is the live-send gate. Use an approved `GUPSHUP_TEMPLATE_ID` outside a verified 24-hour messaging session.

**Webhook setup has an account-dependent prerequisite:** the provider must send `X-Gupshup-Webhook-Secret`. The documented partner subscription API supports custom headers; equivalent support in the user's actual self-serve account is not yet verified. Do not remove authentication or put the outbound API key into a callback URL to work around this. Gupshup does not use TwiML replies: its callback must be acknowledged promptly and business processing/outbound replies handled through durable queues. Provider acceptance still does not mean delivery. No Gupshup HMAC signature scheme is invented.

After the authorized setup is complete:

```sh
python -m plus_one --db ./private-demo.sqlite3 serve --live-gupshup
# Standalone alternative; do not run with serve on the same database:
python -m plus_one --db ./private-demo.sqlite3 worker --live-gupshup
```

Use `/webhooks/gupshup` as the approved HTTPS callback path. It authenticates the header/JSON event, durably deduplicates/enqueues it, and returns an empty success response without waiting for model/STT/network sends. The sole worker handles inbox processing, reply outbox, and due reminders. `ENABLE_GUPSHUP_SEND=true` requires the explicit `--live-gupshup` flag, preventing accidental simulation of real callbacks; incomplete settings fail startup. Gupshup and Twilio live flags are mutually exclusive.

The operator-only state includes `inbox` and `reply_outbox`. Inbox entries progress through `pending`, `processing` or `transcribing`, then `completed`; stopped/deleted events can be `cancelled`, `ignored`, or `erased`. Interrupted transcription becomes `uncertain` rather than retrying a potentially billed call. Completed inbound payloads are cleared. The original provider receive timestamp—not retry arrival time—is used for 24-hour session checks. Replies use the same simulated/sent/failed/uncertain distinction as reminders; interrupted sends require manual reconciliation.

Confirmed deletion scrubs durable recipient/body/media data from these queues too. Its final acknowledgment may be attempted once from transient worker memory, without persisting the deleted phone/body again; a process crash can lose that acknowledgment even though deletion succeeded.

### Twilio WhatsApp — optional fallback

The adapter targets Twilio's official Messaging API. Webhooks use the published HMAC-SHA1 form signature algorithm with the **exact configured HTTPS URL**, not request-supplied Host headers. Sender, recipient, account, and tester allowlist are checked. Input `MessageSid` is passed to transactional deduplication. Duplicate form keys are rejected.

Before any live test, obtain explicit authorization for the tester, transport, and any cost, then configure an authorized account/sender, exact approved HTTPS webhook endpoint, allowlist, and credentials. Deployment/exposure is a separate action requiring authorization. Set `ENABLE_TWILIO_SEND=true` only when authorized: this gates **both outbound API sends and TwiML webhook replies**.

Scheduled reminders use an approved `TWILIO_CONTENT_SID` template with reminder text in variable `1`; validate its approved language/category/content and variable restrictions in the account. Arbitrary free-form WhatsApp messages cannot be assumed valid outside the customer-service session. The adapter supports a verified session checker, but the CLI live scheduler deliberately requires a template.

Once authorized and fully configured:

```sh
python -m plus_one --db ./private-demo.sqlite3 serve --live-twilio
# Or a standalone scheduler, not simultaneously for the same database:
python -m plus_one --db ./private-demo.sqlite3 worker --live-twilio
```

`simulated` means no send was attempted. `sent` means **the selected provider accepted the request**, not delivered/read. `failed` means policy/configuration rejection or definite provider rejection. `uncertain` means acceptance could not be determined: **do not automatically resend**. A restarted worker marks interrupted claimed sends uncertain. Reconcile with provider records manually before any retry; there is no delivery-callback UI or automatic reconciliation tool. Exactly-once external delivery is not guaranteed.

### Text LLM and voice

`providers.py` supplies an optional OpenAI-compatible **text-only** adapter; `interpretation.py` constrains its output to expense, reminder, or storytelling proposals, or a broad read-only balance/task/memory request. To opt in to interpretation of otherwise unrecognized conversational messages, set `ENABLE_CONVERSATIONAL_LLM=true`. Supply `LLM_API_BASE_URL`, `LLM_MODEL`, and `LLM_API_KEY`, and explicitly set both `LLM_ENABLED=true` and `LLM_GENERAL_API_AUTHORIZED=true` only after confirming the account permits general backend API use. A coding-plan subscription is not assumed sufficient. With these gates disabled, all offline flows still work. Enabling conversational interpretation with incomplete credentials or missing authorization fails startup rather than silently making requests. The interpreter sends only the current text, confirmed timezone/default currency, and current time—not the stored ledger, reflections, name, or user identifier. Model output is untrusted: strict schema and deterministic validators check the proposal, and expenses/reminders still require explicit user confirmation. Broad balance/task questions are answered from the local database; filtered questions are clarified instead of broadened. Control commands never go through the model. Invalid/ambiguous output falls back safely; provider failures do not trigger automatic retries. The official Z.ai general API and `glm-5.3-flash` passed one synthetic API canary; real message interpretation has not yet been verified. Set `LLM_REQUEST_PROFILE=zai-glm-5.3-flash` only with that model and `LLM_API_BASE_URL=https://api.z.ai/api/paas/v4` for forced thinking, low reasoning effort, and JSON output.

Voice transcription is **disabled by default and not live-verified**. Without an adapter, WhatsApp voice notes receive an honest request to type instead. `speech.py` implements the common opt-in compatible multipart `POST /audio/transcriptions` adapter. Gupshup and Twilio integrations supply bounded, validated in-memory audio downloads. Enable only after tester/provider data-sharing authorization with `STT_ENABLED=true`, `STT_PROVIDER_AUTHORIZED=true`, `STT_API_BASE_URL`, `STT_MODEL`, and `STT_API_KEY`; the selected transport must also be configured. Gupshup uses app-scoped media URLs without leaking the outbound API key to the media host; the Twilio fallback uses authenticated account-scoped media downloads. The server CLI connects this adapter when explicitly enabled, and invalid setup fails startup. See [SPEECH.md](SPEECH.md) for the common wire contract, SSRF defenses, MIME/size limits, privacy, and pending tests, and [GUPSHUP.md](GUPSHUP.md) for primary-transport media specifics. A separate, disabled Z.ai ASR profile handles documented WAV/MP3 uploads and bounded local OGG conversion; see [SPEECH.md](SPEECH.md). Pasting a transcript tests downstream text handling, **not speech recognition**. One synthetic Z.ai text request succeeded; no user audio or live ASR request has been tested.

## What this demo does not claim

No public deployment, live STT verification, production reliability/SLA, encryption at rest, multi-tenant admin authorization, broad natural-language understanding, external balances synchronization, payments, medical functions, calendar integration, or third-party friend messages. A temporary, own-number Gupshup Sandbox text loop and one synthetic Z.ai text call were verified on 2026-10-31; those checks do not establish production delivery or ongoing availability. Use synthetic data for evaluation.

Core code: `engine.py` (transactions/memory), `flows.py` (expenses/learning), `timeparse.py` (time/DST), `worker.py` (outbox), `server.py` / `gupshup.py` / `transport.py` (HTTP/Gupshup/Twilio fallback), `providers.py` / `interpretation.py` (optional constrained text interpretation), `speech.py` (opt-in voice adapter), `demo.py` (scripted evidence).
