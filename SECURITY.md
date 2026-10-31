# Security and privacy scope

This is a local, operator-run proof of concept, not a production service. Evaluate with synthetic data. Do not publish the stdlib HTTP server, expose a tunnel, provision services, or send live messages without separate authorization.

## Boundaries and protections

- The CLI server binds to loopback by default. `/api/message` and `/api/state` require a random bearer token of at least 24 characters; comparisons are constant-time. `/health` exposes no user data.
- **The bearer token is an administrator credential.** Its holder can select any user ID and inspect/mutate that user's demo state. User-ID-scoped database queries are isolation within the engine, not authenticated end-user identity. There is no public UI, tenant login, rate-limiting layer, or hardened production proxy.
- Gupshup is the selected primary transport. Its callback requires a separate random secret header, checked in constant time, plus app identity/schema/tester checks and event deduplication. No HMAC scheme is assumed. Actual account support for custom headers must be verified before live use; unsupported accounts remain fail-closed. Current `/wa` send endpoints and an explicit enable flag are required. See GUPSHUP.md.
- Twilio webhooks require a configured exact HTTPS signature URL, valid HMAC-SHA1 signature, matching account/sender, and an explicitly allowed tester. Both TwiML responses and API sends require `ENABLE_TWILIO_SEND=true`. Live testing needs account/tester/cost authorization in addition to configuration.
- Duplicate message IDs are transactionally deduplicated. Proposals are separate from confirmed ledger/task writes. Monetary arithmetic uses exact minor units. No payments, friend messaging, Splitwise writes, or calendar operations exist.
- HTTP request/response sizes are bounded. Default HTTP logging is disabled to avoid exposing message bodies, identifiers, or credentials. Provider errors are sanitized. External-provider credentials are not part of application database state.
- Optional LLM requests are disabled by default, require an explicit general-API authorization flag, use an operator-configured HTTPS hostname, and disallow redirects. The adapter does not execute tools. An additional `ENABLE_CONVERSATIONAL_LLM=true` opt-in enables constrained interpretation before transactional processing: only expense/reminder/storytelling proposals are accepted, then rerun through deterministic validation. Expenses/reminders still require explicit user confirmation. Control commands bypass interpretation. Only the current message, confirmed timezone/default currency, and current time are sent; ledger/reflections/name/user ID are not included by the interpreter. Do not allow chat input to set provider URLs. STT is disabled by default, so raw voice is not downloaded or uploaded by the default path. An explicitly configured/authorized `speech.py` adapter can download validated transport media and upload bounded audio to a compatible transcription endpoint; audio remains in memory, but provider retention applies. Its network client validates public DNS answers and pins the selected IP with TLS hostname verification; no redirects or environment proxies are followed. See SPEECH.md for limitations.

## Local data and deletion

SQLite contains profiles, draft proposals, confirmed expenses/tasks, learning reflections, incoming events (including private media URLs while pending), outgoing reminder/reply queues, and cached replies. Completed inbound payloads are cleared; operator state endpoints can inspect inbox/reply records. Its database file is permissioned `0600`; CLI processes use a restrictive umask. Protect the containing directory, SQLite WAL/SHM sidecars, backups, terminal output, and any copied artifacts. There is **no encryption at rest**. Local administrators and other processes with filesystem access may read data.

`STOP` pauses the user and cancels active reminders while retaining memory. `DELETE MY DATA` requires confirmation and logically deletes personal application rows and clears cached personal response text. Minimal SHA-256 replay keys, user-identifier hashes, provider/event metadata and timestamps, and a suppression reason remain to prevent replay/re-enrollment; these identifiers are **pseudonymous, not anonymous**. There is no automatic retention-expiry job. `START` clears the suppression marker, not historical replay keys.

SQLite `secure_delete` is enabled, but deletion is **not a forensic erasure guarantee**: WAL/journal remnants, filesystem snapshots, copies, backups, previously printed terminal output, and provider-retained messages or media may remain. Local deletion does not request deletion from Gupshup, Twilio, or an AI provider. Private capability-bearing media URLs must be protected like message content. Choose retention/encryption/backup policies before handling real personal data.

After confirmed deletion, the final acknowledgment may be attempted once from transient worker memory; the deleted recipient/body are not re-persisted just to deliver it. A crash can lose that acknowledgment after deletion has already succeeded.

## Delivery and concurrency limits

Gupshup callbacks authenticate and durably enqueue before returning empty 2xx; the sole worker handles text/STT, reply sends, and reminders. Original provider timestamps are retained for session checks; duplicate arrival does not extend the session. Interrupted transcription is marked uncertain to avoid repeating a potentially billed call.

A POSIX lock permits one supported scheduler process per database. The durable outbox deduplicates each task revision. Default deliveries are explicitly `simulated`; a live `sent` status means provider acceptance, not delivery/read.

There is an unavoidable boundary between committing state and an external network send. After an interrupted or ambiguous send, the worker marks the item `uncertain` and does not automatically retry. Reconcile with provider records before any retry. Cancellation, STOP, or deletion cannot recall an already accepted message or guarantee suppression of a request already in flight. Do not claim exactly-once external delivery.

The demo has no delivery status callback processing, automatic uncertain-send reconciliation, production monitoring, key rotation workflow, abuse protection, or general database migration system (only a small compatibility upgrade for the currency profile field). The dependency-free Twilio validator covers supported form webhooks and published signature vectors; production adoption should use/review the official SDK validation path and full deployment threat model.

## Before an authorized live test

1. Confirm tester identity/consent, sender/account permissions, costs, and permitted data.
2. Provide secrets through the approved environment or secret manager, not chat or source control.
3. Obtain authorization for any HTTPS hosting/proxy/tunnel; configure the exact webhook URL and tester allowlist.
4. Verify the approved WhatsApp template, including language/category/variable constraints, before scheduler sends.
5. Separately authorize and configure any audio retrieval/STT or model use, verifying provider wire compatibility. Confirm that API-account terms permit backend usage; a coding subscription alone is not evidence.
6. Verify signature rejection, authentication, isolation, STOP/deletion behavior, and provider delivery evidence in the authorized environment. Local mocked tests do not establish live readiness.
