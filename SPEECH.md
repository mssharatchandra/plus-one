# Optional real voice adapter (not live-verified)

The default demo does **not** claim to transcribe voice. With no configured adapter,
a voice note receives an honest request to type its transcript. The expense/task
engine remains deterministic and requires confirmation before saving a proposal.

`plus_one.speech.configured_transcriber(twilio_config, env=None)` returns `None` when
disabled. Pass its result as `transcriber=` to `make_server`. If enabled but missing
permission/configuration, it raises `SpeechConfigurationError` before network I/O.

## Explicit setup, only after provider authorization

Set secrets in the process environment, never source control or shell history:

| Variable | Required value |
| --- | --- |
| `STT_ENABLED` | `true` to opt into voice processing |
| `STT_PROVIDER_AUTHORIZED` | `true` only after tester/provider data-sharing authorization |
| `STT_API_BASE_URL` | Compatible HTTPS API base, e.g. `https://api.openai.com/v1` |
| `STT_MODEL` | An audio transcription model actually supported by that endpoint |
| `STT_API_KEY` | Provider credential |

The configured Twilio account SID/auth token are additionally required. The signed
webhook still requires the allowlisted tester, exact account/recipient identity,
and `ENABLE_TWILIO_SEND=true`. Do not enable provider gates merely to run tests.

## Supported provider wire contract

The adapter sends `POST <base>/audio/transcriptions`, Bearer authorization, and
`multipart/form-data` fields `file`, `model`, and `response_format=json`. It expects
`Content-Type: application/json` and a JSON object containing a nonempty string
`text`. This contract follows the [official transcription endpoint](https://developers.openai.com/api/reference/resources/audio/subresources/transcriptions/methods/create).
Other providers must explicitly support this contract. In particular, **Z.ai GLM
chat compatibility is not evidence that its ASR2512 audio API supports this route**.
Verify that provider's exact audio endpoint before enabling; a different contract
requires another adapter. Model availability and costs have not been checked live.

## Data boundary and limits

- Audio is fetched only from the exact validated Twilio account/message media path
  on `https://api.twilio.com`, with Twilio Basic authentication.
- The provider endpoint must be HTTPS on port 443, hostname-only, with no URL
  credentials/query/fragment. Public DNS answers are checked; private/reserved
  addresses fail closed. The selected public IP is pinned while TLS validates the
  original hostname. Redirects and environment proxy settings are not followed.
- Maximum audio: 10 MiB. Maximum provider response: 64 KiB. Maximum transcript:
  4,000 characters. Audio MIME must match the webhook declaration (WAV aliases
  accepted). Provider format support still varies; this adapter does not transcode.
- Socket operations use a 20-second timeout. OS DNS lookups follow system resolver
  timeouts. The stdlib HTTP server is a local PoC, not a production service.
- Audio stays in process memory and is not saved or logged. The STT provider sees
  the audio; Twilio and provider retention policies apply independently. Successful
  transcripts then follow the engine's normal message/proposal retention rules.
- Failures return sanitized text. No automatic retry is made: a timed-out provider
  POST may have been accepted and billed. Signed inbound webhook replays could
  invoke STT again before engine-level MessageSid deduplication; avoid deliberate
  retries of voice notes until a production media-job deduplication layer exists.

## Verification

`python -m unittest discover -s tests -p test_speech.py -v`

All audio/provider operations in these tests are mocked. Tests cover opt-in gates,
authenticated fetch, multipart payload, unsafe URL/DNS rejection, redirects, size
and type limits, sanitized failures, and no retries. **Actual voice recognition
accuracy, Twilio attachment responses, provider authentication, and live cost are
pending authorized end-to-end testing.**
