from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.settings.integrations import call_openai  # noqa: E402


class FakeResponse:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def read(self):
        return b'{"choices":[{"message":{"content":"ok"}}],"usage":{}}'


class ExplicitReasoningSummaryResponse(FakeResponse):
    def read(self):
        return b'{"choices":[{"message":{"content":"ok","reasoning_summary":"Resumen visible"}}],"reasoning":"razonamiento privado","usage":{}}'


def llm_settings(reasoning_effort: str):
    return SimpleNamespace(
        provider="openai",
        api_key_encrypted="encrypted-key",
        base_url=None,
        temperature=0.1,
        max_tokens=100,
        retries=0,
        timeout_seconds=2,
        reasoning_effort=reasoning_effort,
    )


class LLMReasoningTests(unittest.TestCase):
    @patch("app.settings.integrations.decrypt_secret", return_value="api-key")
    @patch("app.settings.integrations.urllib.request.urlopen", return_value=FakeResponse())
    def test_reasoning_effort_is_sent_only_to_supported_models(self, urlopen, _decrypt_secret):
        result = call_openai(llm_settings("high"), [{"role": "user", "content": "test"}], "gpt-5.6-luna")
        self.assertTrue(result["ok"])
        reasoning_payload = json.loads(urlopen.call_args.args[0].data)
        self.assertEqual(reasoning_payload["reasoning_effort"], "high")

        urlopen.reset_mock()
        result = call_openai(llm_settings("high"), [{"role": "user", "content": "test"}], "gpt-4.1-mini")
        self.assertTrue(result["ok"])
        non_reasoning_payload = json.loads(urlopen.call_args.args[0].data)
        self.assertNotIn("reasoning_effort", non_reasoning_payload)

    @patch("app.settings.integrations.decrypt_secret", return_value="api-key")
    @patch("app.settings.integrations.urllib.request.urlopen", return_value=ExplicitReasoningSummaryResponse())
    def test_provider_exposes_only_explicit_reasoning_summary(self, _urlopen, _decrypt_secret):
        result = call_openai(llm_settings("medium"), [{"role": "user", "content": "test"}], "gpt-5.6-luna")
        self.assertEqual(result["reasoning_summary"], "Resumen visible")
        self.assertNotIn("reasoning", result)


if __name__ == "__main__":
    unittest.main()
