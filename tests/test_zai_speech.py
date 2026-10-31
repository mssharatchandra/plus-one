import io
import json
import shutil
import subprocess
import sys
import unittest
import wave
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from plus_one.speech import SpeechConfig, SpeechConfigurationError, SpeechError, transcribe_audio
from plus_one.zai_speech import BASE, MAX_PCM, MODEL, VoiceInputError, normalized_wav, transcribe_zai


def config(**overrides):
    values = dict(enabled=True, provider_authorized=True, api_base_url=BASE,
                  model=MODEL, api_key="synthetic-not-real")
    values.update(overrides)
    return SpeechConfig(**values)


def wav(seconds=1):
    data = io.BytesIO()
    with wave.open(data, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        output.writeframes(b"\0\0" * (seconds * 16000))
    return data.getvalue()


class Response(io.BytesIO):
    headers = {"Content-Type": "application/json"}


class ZaiSpeechTests(unittest.TestCase):
    def test_configuration_gates_before_decoding(self):
        for cfg in (config(enabled=False), config(provider_authorized=False),
                    config(api_base_url="https://bad.example/v1"), config(model="other")):
            with self.subTest(cfg=cfg), patch("plus_one.zai_speech.normalized_wav") as decode:
                with self.assertRaises(SpeechConfigurationError):
                    transcribe_zai(b"x", "audio/ogg", cfg)
                decode.assert_not_called()

    def test_invalid_input_without_decoder(self):
        for audio, mime in ((b"", "audio/ogg"),
                            (b"x" * (10 * 1024 * 1024 + 1), "audio/ogg"),
                            (b"x", "audio/mp4")):
            with patch("plus_one.zai_speech.subprocess.run") as run:
                with self.assertRaises(VoiceInputError):
                    normalized_wav(audio, mime)
                run.assert_not_called()
        with self.assertRaises(VoiceInputError):
            normalized_wav(b"x", "audio/ogg", "/no/such/ffmpeg")

    @patch("plus_one.zai_speech.subprocess.run")
    def test_decoder_is_bounded_and_has_no_shell_or_ambient_secrets(self, run):
        run.return_value = Mock(returncode=0, stdout=b"\0\0" * 16000)
        output = normalized_wav(b"synthetic", "audio/ogg", sys.executable)
        command = run.call_args.args[0]
        kwargs = run.call_args.kwargs
        self.assertFalse(kwargs["shell"])
        self.assertEqual(kwargs["stderr"], subprocess.DEVNULL)
        self.assertEqual(kwargs["timeout"], 12)
        self.assertEqual(command[command.index("-protocol_whitelist") + 1], "pipe")
        self.assertEqual(command[command.index("-f") + 1], "ogg")
        self.assertNotIn("HOME", kwargs["env"])
        self.assertNotIn("STT_API_KEY", kwargs["env"])
        with wave.open(io.BytesIO(output)) as decoded:
            self.assertEqual((decoded.getnchannels(), decoded.getframerate(), decoded.getnframes()),
                             (1, 16000, 16000))

    @patch("plus_one.zai_speech.subprocess.run")
    def test_bad_decode_and_overlong_pcm_are_rejected(self, run):
        for result in (Mock(returncode=1, stdout=b"private"), Mock(returncode=0, stdout=b""),
                       Mock(returncode=0, stdout=b"x"),
                       Mock(returncode=0, stdout=b"\0" * (MAX_PCM + 2))):
            run.return_value = result
            with self.assertRaises(VoiceInputError):
                normalized_wav(b"x", "audio/ogg", sys.executable)
        run.side_effect = subprocess.TimeoutExpired("private-token", 12)
        with self.assertRaises(VoiceInputError) as caught:
            normalized_wav(b"x", "audio/ogg", sys.executable)
        self.assertNotIn("private-token", str(caught.exception))

    @patch("plus_one.zai_speech.normalized_wav", return_value=wav())
    def test_provider_contract_and_sanitized_failure(self, convert):
        client = Mock()
        client.open.return_value = Response(b'{"text":"hello\\nworld"}')
        self.assertEqual(transcribe_audio(b"synthetic", "audio/ogg", config(), client), "hello world")
        req = client.open.call_args.args[0]
        self.assertEqual(req.full_url, BASE + "/audio/transcriptions")
        self.assertEqual(req.get_header("Authorization"), "Bearer synthetic-not-real")
        self.assertIn(b'name="model"\r\n\r\nglm-asr-2512', req.data)
        self.assertIn(b'name="stream"\r\n\r\nfalse', req.data)
        self.assertIn(b'filename="voice-note.wav"', req.data)
        self.assertNotIn(b"response_format", req.data)
        self.assertEqual(client.open.call_count, 1)
        client.open.side_effect = RuntimeError("private-token")
        with self.assertRaises(SpeechError) as caught:
            transcribe_zai(b"x", "audio/ogg", config(), client)
        self.assertNotIn("private-token", str(caught.exception))

    @patch("plus_one.zai_speech.normalized_wav", return_value=wav())
    def test_bad_transcripts_are_rejected(self, convert):
        for text in (None, "", "x" * 4001, "x\0y"):
            client = Mock()
            client.open.return_value = Response(json.dumps({"text": text}).encode())
            with self.assertRaises(SpeechError):
                transcribe_zai(b"x", "audio/ogg", config(), client)

    def test_worker_returns_safe_input_error_without_retaining_audio_payload(self):
        from plus_one.engine import Engine
        from plus_one.worker import process_inbox
        now = datetime(2026, 10, 31, 12, tzinfo=timezone.utc)
        with TemporaryDirectory() as tmp:
            engine = Engine(Path(tmp) / "synthetic.sqlite3", clock=lambda: now)
            try:
                engine.enqueue_inbound("gupshup", "synthetic-id", "synthetic-user",
                                       {"kind": "audio", "text": None, "media": {},
                                        "received_at": now.isoformat()})
                transcriber = Mock()
                transcriber.transcribe.side_effect = VoiceInputError(
                    "That voice note is longer than 30 seconds. Please send a shorter note or type the message.")
                process_inbox(engine, transcribers={"gupshup": transcriber})
                row = engine.db.execute("SELECT body FROM reply_outbox").fetchone()
                self.assertIn("longer than 30 seconds", row["body"])
                self.assertIsNone(engine.db.execute("SELECT payload FROM inbox").fetchone()["payload"])
            finally:
                engine.close()

    def test_voice_only_mode_replies_with_transcript_without_task_proposal(self):
        from plus_one.engine import Engine
        from plus_one.worker import process_inbox
        now = datetime(2026, 10, 31, 12, tzinfo=timezone.utc)
        with TemporaryDirectory() as tmp, patch.dict('os.environ', {'VOICE_TRANSCRIPTION_TEST_ONLY':'true'}):
            engine = Engine(Path(tmp) / 'synthetic.sqlite3', clock=lambda: now)
            try:
                engine.enqueue_inbound('gupshup', 'synthetic-voice', 'synthetic-user',
                                       {'kind':'audio','text':None,'media':{},'received_at':now.isoformat()})
                transcriber=Mock()
                transcriber.transcribe.return_value='Remind me to buy apples on Monday.'
                process_inbox(engine, transcribers={'gupshup':transcriber})
                body=engine.db.execute('SELECT body FROM reply_outbox').fetchone()['body']
                self.assertIn('I heard: Remind me to buy apples on Monday.',body)
                self.assertIn('Nothing was scheduled',body)
                self.assertEqual(engine.db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0],0)
                self.assertEqual(engine.db.execute('SELECT COUNT(*) FROM pending').fetchone()[0],0)
                self.assertIsNone(engine.db.execute('SELECT payload FROM inbox').fetchone()['payload'])
            finally:engine.close()

    @unittest.skipUnless(shutil.which("ffmpeg"), "No ffmpeg installed")
    def test_real_synthetic_codec_and_duration_boundaries(self):
        for seconds, accepted in ((1, True), (30, True), (31, False)):
            if accepted:
                with wave.open(io.BytesIO(normalized_wav(wav(seconds), "audio/wav"))) as output:
                    self.assertEqual(output.getnframes(), seconds * 16000)
            else:
                with self.assertRaisesRegex(VoiceInputError, "longer than 30"):
                    normalized_wav(wav(seconds), "audio/wav")
        for fmt, codec, mime in (("ogg", "libopus", "audio/ogg"),
                                 ("mp3", "libmp3lame", "audio/mpeg")):
            encoded = subprocess.run([shutil.which("ffmpeg"), "-v", "error", "-f", "wav", "-i", "pipe:0",
                                      "-c:a", codec, "-f", fmt, "pipe:1"], input=wav(), stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, timeout=12, check=True).stdout
            with wave.open(io.BytesIO(normalized_wav(encoded, mime))) as output:
                self.assertLessEqual(output.getnframes(), MAX_PCM // 2)


if __name__ == "__main__":
    unittest.main()
