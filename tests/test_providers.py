import io
import json
import unittest
from unittest.mock import patch
from urllib import error

from plus_one.providers import (
    DisabledSpeechToText, LLMConfig, OpenAICompatibleLLM,
    ProviderError, ProviderUnavailable, _NoRedirect,
)


class Response(io.BytesIO):
    status = 200


class ProviderTests(unittest.TestCase):
    def config(self, **overrides):
        values = dict(enabled=True, general_api_authorized=True,
                      base_url="https://api.example.com/v1", model="test-model",
                      api_key="secret-key")
        values.update(overrides)
        return LLMConfig(**values)

    @patch("plus_one.providers.request.build_opener")
    def test_disabled_and_unauthorized_do_not_send(self, build):
        for config in [LLMConfig(), self.config(general_api_authorized=False), self.config(api_key="")]:
            with self.assertRaises(ProviderUnavailable):
                OpenAICompatibleLLM(config).complete([{"role": "user", "content": "Hi"}])
        build.assert_not_called()

    def test_environment_defaults_and_secret_repr(self):
        self.assertFalse(LLMConfig.from_env({}).enabled)
        config = LLMConfig.from_env({"LLM_ENABLED": "true", "LLM_API_KEY": "secret-key"})
        self.assertTrue(config.enabled)
        self.assertNotIn("secret-key", repr(config))
        with self.assertRaises(ValueError):
            LLMConfig.from_env({"LLM_ENABLED": "yes"})

    def test_rejects_unsafe_endpoints(self):
        for base in ["http://api.example.com", "https://user:pass@api.example.com", "https://api.example.com?x=1",
                     "https://api.example.com/#secret", "https://localhost", "https://127.0.0.1", "https://[::1]",
                     "https://private.internal", "https://api.example.com:8443", "https://api.example.com/\n"]:
            with self.subTest(base=base), self.assertRaises(ValueError):
                self.config(base_url=base).validate()

    @patch("plus_one.providers.request.build_opener")
    def test_text_completion_request(self, build):
        build.return_value.open.return_value = Response(json.dumps({"choices": [{"message": {"content": " A small step ", "reasoning_content": "private"}, "finish_reason": "stop"}]}).encode())
        out = OpenAICompatibleLLM(self.config()).complete([{"role": "user", "content": "Teach storytelling"}])
        self.assertEqual(out, "A small step")
        args, kwargs = build.return_value.open.call_args
        req = args[0]
        self.assertEqual(req.full_url, "https://api.example.com/v1/chat/completions")
        self.assertEqual(req.get_header("Authorization"), "Bearer secret-key")
        self.assertEqual(json.loads(req.data)["model"], "test-model")
        self.assertFalse(json.loads(req.data)["stream"])
        self.assertEqual(kwargs["timeout"], 20)

    @patch("plus_one.providers.request.build_opener")
    def test_sanitized_http_error_no_retry(self, build):
        build.return_value.open.side_effect = error.HTTPError("https://secret-key.example.com", 401, "secret-key", {}, io.BytesIO(b"private prompt"))
        with self.assertRaises(ProviderError) as caught:
            OpenAICompatibleLLM(self.config()).complete([{"role": "user", "content": "private prompt"}])
        self.assertIn("401", str(caught.exception))
        self.assertNotIn("secret-key", str(caught.exception))
        self.assertNotIn("private prompt", str(caught.exception))
        self.assertEqual(build.return_value.open.call_count, 1)

    @patch("plus_one.providers.request.build_opener")
    def test_network_failure(self, build):
        build.return_value.open.side_effect = error.URLError("secret-key")
        with self.assertRaisesRegex(ProviderError, "No automatic retry"):
            OpenAICompatibleLLM(self.config()).complete([{"role": "user", "content": "Hello"}])

    @patch("plus_one.providers.request.build_opener")
    def test_invalid_output(self, build):
        for data in [b"not json", b"{}", b'{"choices":[]}', b'{"choices":[[]]}', b'{"choices":[{"message":{"content":null}}]}',
                     b'{"choices":[{"message":{"content":"partial"},"finish_reason":"length"}]}', b"x" * 1025]:
            build.return_value.open.return_value = Response(data)
            with self.subTest(data=data[:50]), self.assertRaises(ProviderError):
                OpenAICompatibleLLM(self.config(max_response_bytes=1024)).complete([{"role": "user", "content": "Hi"}])

    @patch("plus_one.providers.request.build_opener")
    def test_rejects_invalid_inputs_before_send(self, build):
        for messages in [[], "hello", [{"role": "tool", "content": "anything"}], [{"role": "user", "content": []}], [{"role": "user", "content": "x" * 16001}]]:
            with self.assertRaises(ValueError):
                OpenAICompatibleLLM(self.config()).complete(messages)
        build.assert_not_called()

    def test_no_redirect(self):
        self.assertIsNone(_NoRedirect().redirect_request(None, None, 302, "", {}, "https://attacker.example"))

    def test_stt_honestly_disabled(self):
        with self.assertRaisesRegex(ProviderUnavailable, "No audio was sent"):
            DisabledSpeechToText().transcribe(b"not real audio", "audio/ogg")


if __name__ == "__main__":
    unittest.main()
