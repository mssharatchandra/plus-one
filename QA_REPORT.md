# Plus One PoC v0.1.0 — verification snapshot

**Cloud build date:** 2026-10-31 UTC. **Scope:** local, synthetic-data proof of concept, not a deployed pilot.

## Passed

- `python -m unittest discover -s tests -v`: **149 tests passed** on Python 3.12.
- `python -m compileall -q plus_one`: passed.
- `python -m plus_one demo`: passed; all three flows, artificial clock and local simulated delivery.
- Independent QA exercised real loopback HTTP + real SQLite, multiple database connections, CLI server subprocess shutdown/restart, sole-worker locking, exact rounding, two-way payer/net balances, timezone/DST confirmation, snooze/done/cancel, reflection continuity, privacy deletion, signed Twilio and secret-authenticated Gupshup ingress, voice-provider-call deduplication, and paused/deleted voice suppression. Provider transports/transcribers were fake or mocked; these checks were not live WhatsApp tests.

## Notable regressions found and fixed

- Fresh CONFIRM after deletion recreated a plaintext profile: now all non-START input stays paused without recreation.
- Oversized task IDs overflowed SQLite: now safely rejected.
- Voice retries and paused voice invoked STT unnecessarily: preflight/serialization and durable inbox guards now prevent it.
- STOP cancelled later queued START/deletion controls: FIFO cutoff now preserves subsequent control events.
- Cancelled work could resume after STOP/START: inbox status is rechecked inside the engine transaction.
- A learning validation error could leave an invalid journey: validation now occurs before persistence.

## Three-flow evidence

1. **Expenses:** self or named payer, explicit/confirmed-default currency, exact minor-unit remainder allocation, preview before recording, and bilateral net balances. Equal splits only; no repayments, currency conversion, or external Splitwise changes.
2. **Tasks:** confirmed IANA timezone and exact local date/time, durable SQLite reminder queue, quiet hours, restart/deduplication, done/snooze/cancel. Uncertain external sends are not automatically retried.
3. **Curiosity:** fixed six-step storytelling curriculum tailored to stated goal/time, recorded reflection, and continuity after database restart. Lessons are not represented as live-generated AI content.

## Live/setup checks still pending

- Actual Gupshup account access, authorized tester registration/consent, current subscription custom-secret-header support, source/app credentials, and approved templates.
- Approved HTTPS deployment/reverse proxy and external callback reachability. Nothing was deployed or published.
- Actual WhatsApp receipt/delivery. A provider acceptance ID is not delivery confirmation; callback delivery-status reconciliation is not implemented.
- Real compatible STT endpoint/credentials, tester voice-data sharing authorization, and recognition quality. Only mocked audio-provider calls were tested.
- Real general-API LLM entitlement/credentials and live model behavior. A coding-plan subscription alone is not treated as backend API authorization.
- macOS transfer and local run: separate verification after this cloud archive.

## Intentional limitations

See README.md, SECURITY.md, GUPSHUP.md and SPEECH.md. This is a conservative deterministic parser with optional constrained LLM interpretation, not unrestricted conversation. Operator bearer auth is not per-user tenant auth. SQLite is not encrypted. Deletion is logical, with pseudonymous hashed replay/suppression metadata retained; no forensic or backup-erasure guarantee. Deletion acknowledgement is a single transient attempt, so a crash may lose it rather than retain erased recipient/content data. Cancellation cannot retract an already in-flight external request. No account signup, spend, external messages, medical functions, payments, calendar integration, or friend contact actions occurred.
