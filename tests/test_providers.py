import json
from pathlib import Path
import unittest

import httpx

from patchtrail.config import Settings
from patchtrail.providers import DeepSeekProvider, ProviderError, ScriptedProvider, initial_messages
from patchtrail.tasks import get_task


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.settings = Settings(data_dir=Path("unused"), api_key="test-provider-secret", admin_token="test-operator")
        self.task = get_task("quantity-shortfall")

    def test_real_transport_contract_uses_tool_messages_and_disabled_thinking(self):
        recorded = []
        def handler(request):
            body = json.loads(request.content)
            recorded.append(body)
            self.assertEqual(request.headers["Authorization"], "Bearer test-provider-secret")
            self.assertEqual(body["thinking"], {"type": "disabled"})
            self.assertEqual(body["model"], "deepseek-flash")
            self.assertEqual({tool["function"]["name"] for tool in body["tools"]},
                             {"read_file", "search_code", "run_check", "propose_patch", "propose_patch_bundle"})
            return httpx.Response(200, json={"choices": [{"finish_reason": "tool_calls", "message": {
                "role": "assistant", "content": None, "tool_calls": [{"id": "call-a", "type": "function",
                 "function": {"name": "read_file", "arguments": '{"path":"src/inventory.py"}'}}]}}],
                 "usage": {"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20}})
        provider = DeepSeekProvider(self.settings, httpx.MockTransport(handler))
        messages = initial_messages(self.task)
        message, usage = provider.complete(messages, self.task, 1, 10)
        self.assertEqual(usage["total_tokens"], 20)
        messages.extend([message, {"role": "tool", "tool_call_id": "call-a", "content": "read result"}])
        provider.complete(messages, self.task, 2, 10)
        self.assertEqual(recorded[1]["messages"][-1]["tool_call_id"], "call-a")

    def test_http_failure_is_bounded_and_does_not_echo_provider_body_or_key(self):
        count = []
        def handler(request):
            count.append(1)
            return httpx.Response(401, text="test-provider-secret arbitrary private error")
        with self.assertRaises(ProviderError) as error:
            DeepSeekProvider(self.settings, httpx.MockTransport(handler)).complete([], self.task, 1, 1)
        self.assertEqual(len(count), 1)
        self.assertNotIn("test-provider-secret", str(error.exception))
        self.assertFalse(error.exception.uncertain)

    def test_unknown_network_outcome_has_no_automatic_retry(self):
        calls = []
        def handler(request):
            calls.append(1)
            raise httpx.ReadTimeout("private endpoint and secret", request=request)
        with self.assertRaises(ProviderError) as error:
            DeepSeekProvider(self.settings, httpx.MockTransport(handler)).complete([], self.task, 1, 1)
        self.assertEqual(len(calls), 1)
        self.assertTrue(error.exception.uncertain)
        self.assertNotIn("private endpoint", str(error.exception))

    def test_invalid_and_overlong_completions_are_rejected(self):
        cases = [[], {"choices": ["wrong"]}, {"choices": [{"message": "wrong"}]},
                 {"choices": [{"finish_reason": "length", "message": {"role": "assistant"}}]},
                 {"choices": [{"message": {"role": "assistant", "tool_calls": [None]}}]}]
        for body in cases:
            with self.subTest(body=body):
                provider = DeepSeekProvider(self.settings, httpx.MockTransport(lambda r: httpx.Response(200, json=body)))
                with self.assertRaises(ProviderError):
                    provider.complete([], self.task, 1, 1)
        provider = DeepSeekProvider(self.settings, httpx.MockTransport(lambda r: httpx.Response(200, content=b"x" * 256001)))
        with self.assertRaises(ProviderError):
            provider.complete([], self.task, 1, 1)

    def test_scripted_mode_is_explicit_and_has_no_network_transport(self):
        provider = ScriptedProvider()
        self.assertIn("no model call", provider.label)
        names = []
        for call in (1, 2, 3):
            message, usage = provider.complete([], self.task, call, 10)
            names.append(message["tool_calls"][0]["function"]["name"])
            self.assertEqual(usage, {})
        self.assertEqual(names, ["read_file", "run_check", "propose_patch"])


if __name__ == "__main__":
    unittest.main()
