"""OpenAI-compatible transport contracts use only in-memory HTTP responses."""
import asyncio
from dataclasses import replace
import json
from pathlib import Path
import time
import unittest
from unittest.mock import patch

import httpx

from patchtrail.config import Settings
from patchtrail.providers import (DeepSeekProvider, ModelProvider, OpenAICompatibleProvider,
                                 ProviderError, ScriptedProvider, TOOL_SCHEMAS, create_provider,
                                 initial_messages)
from patchtrail.tasks import get_task


def completion(*, model="second-vendor-model", content=None):
    return {"id": "response-second-01", "model": model, "choices": [{
        "finish_reason": "tool_calls", "message": {"role": "assistant", "content": content,
            "tool_calls": [{"id": "tool-second-01", "type": "function", "function": {
                "name": "read_file", "arguments": '{"path":"src/inventory.py"}'}}]}}],
        "usage": {"prompt_tokens": 20, "completion_tokens": 7, "total_tokens": 27}}


class CompatibleProviderTests(unittest.TestCase):
    def setUp(self):
        self.task = get_task("quantity-shortfall")
        self.settings = Settings(data_dir=Path("unused"), api_key="only-a-synthetic-provider-key",
            model="second-vendor-model", base_url="https://vendor.example.invalid/api/v4")

    def test_compatible_vendor_uses_same_tool_contract_and_preserves_tool_history(self):
        observed = []
        def handler(request):
            observed.append(json.loads(request.content))
            self.assertEqual(str(request.url), "https://vendor.example.invalid/api/v4/chat/completions")
            self.assertEqual(request.headers["Authorization"], "Bearer " + self.settings.api_key)
            return httpx.Response(200, json=completion())
        provider = OpenAICompatibleProvider(self.settings, httpx.MockTransport(handler),
            request_options={"thinking": {"type": "disabled"}, "temperature": 0.1},
            label="Second vendor live model")
        messages = initial_messages(self.task)
        original = json.dumps(messages)
        message, usage = provider.complete(messages, self.task, 1, 2)
        self.assertEqual(json.dumps(messages), original)
        self.assertEqual(usage, {"prompt_tokens": 20, "completion_tokens": 7, "total_tokens": 27})
        self.assertEqual(provider.label, "Second vendor live model")
        self.assertEqual(provider.last_completion_metadata,
            {"reported_model": "second-vendor-model", "response_id": "response-second-01",
             "finish_reason": "tool_calls"})
        messages.extend([message, {"role": "tool", "tool_call_id": "tool-second-01", "content": "observed source"}])
        provider.complete(messages, self.task, 2, 2)
        self.assertEqual(observed[1]["messages"][-1]["tool_call_id"], "tool-second-01")
        self.assertEqual(observed[0]["tools"], TOOL_SCHEMAS)
        self.assertEqual(observed[0]["thinking"], {"type": "disabled"})
        self.assertEqual(observed[0]["temperature"], 0.1)
        self.assertEqual(observed[0]["tool_choice"], "auto")
        self.assertFalse(observed[0]["stream"])

    def test_protocol_covers_scripted_and_both_real_transport_adapters(self):
        for provider in (ScriptedProvider(), DeepSeekProvider(Settings(api_key="synthetic")),
                         OpenAICompatibleProvider(self.settings)):
            with self.subTest(provider=type(provider).__name__):
                self.assertIsInstance(provider, ModelProvider)

    def test_factory_uses_separate_compatible_key_and_cannot_fall_back_to_deepseek(self):
        requests = []
        def handler(request):
            requests.append(request)
            return httpx.Response(200, json=completion())
        settings = replace(self.settings, provider_kind="openai-compatible",
            compatible_api_key="synthetic-compatible-only-key",
            model_options={"thinking": {"type": "disabled"}}, preserve_reasoning_content=True)
        provider = create_provider(settings, httpx.MockTransport(handler))
        self.assertIsInstance(provider, OpenAICompatibleProvider)
        self.assertNotIsInstance(provider, DeepSeekProvider)
        provider.complete([], self.task, 1, 1)
        self.assertEqual(requests[0].headers["Authorization"], "Bearer synthetic-compatible-only-key")
        self.assertNotIn(self.settings.api_key, json.dumps(json.loads(requests[0].content)))
        self.assertEqual(json.loads(requests[0].content)["thinking"], {"type": "disabled"})
        self.assertTrue(provider.preserve_reasoning_content)
        with self.assertRaises(ProviderError):
            create_provider(replace(settings, compatible_api_key=""), httpx.MockTransport(handler)).complete([], self.task, 1, 1)
        self.assertEqual(len(requests), 1)
        self.assertEqual(settings.api_key, self.settings.api_key)

    def test_factory_deepseek_default_ignores_all_generic_vendor_configuration(self):
        requests = []
        def handler(request):
            requests.append(request)
            response = completion()
            response["choices"][0]["message"]["reasoning_content"] = "must remain hidden"
            return httpx.Response(200, json=response)
        settings = Settings(api_key="synthetic-deepseek-only-key", provider_kind="deepseek",
            compatible_api_key="synthetic-compatible-only-key", model_options={"tools": "not forwarded"},
            preserve_reasoning_content=True, output_token_parameter="max_completion_tokens")
        provider = create_provider(settings, httpx.MockTransport(handler))
        self.assertIsInstance(provider, DeepSeekProvider)
        message, _ = provider.complete([], self.task, 1, 1)
        self.assertEqual(requests[0].headers["Authorization"], "Bearer synthetic-deepseek-only-key")
        self.assertEqual(str(requests[0].url), "https://api.deepseek.com/chat/completions")
        self.assertEqual(json.loads(requests[0].content)["tools"], TOOL_SCHEMAS)
        self.assertEqual(json.loads(requests[0].content)["thinking"], {"type": "disabled"})
        self.assertEqual(json.loads(requests[0].content)["max_tokens"], 4096)
        self.assertNotIn("max_completion_tokens", json.loads(requests[0].content))
        self.assertNotIn("reasoning_content", message)
        with self.assertRaises(ProviderError) as error:
            create_provider(replace(settings, provider_kind="do-not-echo-provider-config"))
        self.assertNotIn("do-not-echo", str(error.exception))

    def test_deepseek_payload_retains_its_exact_legacy_fields_and_endpoint_policy(self):
        settings = Settings(api_key="synthetic-deepseek-key")
        messages = initial_messages(self.task)
        provider = DeepSeekProvider(settings)
        expected = {"model": "deepseek-flash", "messages": messages, "tools": TOOL_SCHEMAS,
            "tool_choice": "auto", "max_tokens": 4096, "stream": False,
            "thinking": {"type": "disabled"}}
        self.assertEqual(json.dumps(provider.build_payload(messages)), json.dumps(expected))
        self.assertEqual(provider.label, "DeepSeek live model")
        with self.assertRaises(TypeError):
            DeepSeekProvider(settings, request_options={"thinking": {"type": "enabled"}})
        calls = []
        transport = httpx.MockTransport(lambda request: calls.append(request))
        for base_url in ("https://vendor.example.invalid/v1", "https://api.deepseek.com/v1",
                         "http://api.deepseek.com", "https://api.deepseek.com.attacker.invalid"):
            with self.subTest(base_url=base_url), self.assertRaises(ProviderError):
                DeepSeekProvider(replace(settings, base_url=base_url), transport).complete([], self.task, 1, 1)
        self.assertEqual(calls, [])

    def test_generic_defaults_do_not_send_deepseek_vendor_options(self):
        provider = OpenAICompatibleProvider(self.settings)
        self.assertNotIn("thinking", provider.build_payload([]))
        self.assertEqual(set(provider.build_payload([])),
                         {"model", "messages", "tools", "tool_choice", "max_tokens", "stream"})

    def test_modern_output_parameter_uses_same_fixed_cap_and_factory_profile(self):
        requests = []
        def handler(request):
            requests.append(json.loads(request.content))
            return httpx.Response(200, json=completion())
        settings = replace(self.settings, max_output_tokens=128, provider_kind="openai-compatible",
            compatible_api_key="synthetic-compatible-only-key", output_token_parameter="max_completion_tokens")
        provider = create_provider(settings, httpx.MockTransport(handler))
        provider.complete([], self.task, 1, 1)
        self.assertEqual(requests[0]["max_completion_tokens"], 128)
        self.assertNotIn("max_tokens", requests[0])
        self.assertNotIn("max_output_tokens", requests[0])
        self.assertEqual(requests[0]["tools"], TOOL_SCHEMAS)
        for value in ("max_output_tokens", "unrecognized", None, 1, []):
            with self.subTest(value=value), self.assertRaises(ProviderError) as error:
                OpenAICompatibleProvider(self.settings, output_token_parameter=value)
            self.assertNotIn("unrecognized", str(error.exception))

    def test_generic_normalizes_null_tool_calls_but_deepseek_retains_legacy_rejection(self):
        body = {"id": "no-tool-answer", "model": "second-vendor-model", "choices": [{
            "finish_reason": "stop", "message": {"role": "assistant", "content": "A plain answer.",
                                                   "tool_calls": None}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13}}
        transport = httpx.MockTransport(lambda request: httpx.Response(200, json=body))
        message, usage = OpenAICompatibleProvider(self.settings, transport).complete([], self.task, 1, 1)
        self.assertEqual(message, {"role": "assistant", "content": "A plain answer."})
        self.assertEqual(usage["total_tokens"], 13)
        with self.assertRaises(ProviderError) as error:
            DeepSeekProvider(Settings(api_key="synthetic-key"), transport).complete([], self.task, 1, 1)
        self.assertFalse(error.exception.uncertain)

    def test_factory_transport_environment_policy_is_vendor_specific(self):
        requests = []
        def handler(request):
            requests.append(request)
            return httpx.Response(200, json=completion())
        real_client = httpx.AsyncClient
        transport = httpx.MockTransport(handler)
        with patch("patchtrail.providers.httpx.AsyncClient", wraps=real_client) as clients:
            create_provider(Settings(api_key="synthetic-deepseek-key", provider_kind="deepseek"),
                            transport).complete([], self.task, 1, 1)
            compatible = replace(self.settings, provider_kind="openai-compatible",
                                 compatible_api_key="synthetic-compatible-key")
            create_provider(compatible, transport).complete([], self.task, 1, 1)
        self.assertEqual([call.kwargs["trust_env"] for call in clients.call_args_list], [True, False])
        self.assertTrue(all(call.kwargs["follow_redirects"] is False for call in clients.call_args_list))
        self.assertEqual([request.headers["Authorization"] for request in requests],
                         ["Bearer synthetic-deepseek-key", "Bearer synthetic-compatible-key"])

    def test_opted_in_reasoning_is_redacted_and_replayed_with_tool_result(self):
        observed = []
        escaped = "".join("\\u%04x" % ord(character) for character in self.settings.api_key)
        reasoning = '{"private":{"value":"' + escaped + '"}}'
        def handler(request):
            observed.append(json.loads(request.content))
            response = completion()
            response["choices"][0]["message"]["reasoning_content"] = reasoning
            return httpx.Response(200, json=response)
        provider = OpenAICompatibleProvider(self.settings, httpx.MockTransport(handler),
                                           preserve_reasoning_content=True)
        messages = initial_messages(self.task)
        message, _ = provider.complete(messages, self.task, 1, 2)
        self.assertEqual(json.loads(message["reasoning_content"]),
                         {"private": {"value": "[REDACTED]"}})
        messages.extend([message, {"role": "tool", "tool_call_id": "tool-second-01", "content": "source"}])
        provider.complete(messages, self.task, 2, 2)
        self.assertEqual(observed[1]["messages"][-2]["reasoning_content"], message["reasoning_content"])
        self.assertNotIn(self.settings.api_key, json.dumps(message))
        self.assertNotIn("reasoning_content", provider.last_completion_metadata)
        self.assertEqual(message["content"], None)

    def test_reasoning_preservation_is_optional_validated_and_never_changes_deepseek(self):
        for content in ("private reasoning", ["invalid reasoning"]):
            response = completion()
            response["choices"][0]["message"]["reasoning_content"] = content
            transport = httpx.MockTransport(lambda request: httpx.Response(200, json=response))
            generic, _ = OpenAICompatibleProvider(self.settings, transport).complete([], self.task, 1, 1)
            legacy, _ = DeepSeekProvider(Settings(api_key="synthetic-key"), transport).complete([], self.task, 1, 1)
            self.assertNotIn("reasoning_content", generic)
            self.assertNotIn("reasoning_content", legacy)
        provider = OpenAICompatibleProvider(self.settings, transport, preserve_reasoning_content=True)
        with self.assertRaises(ProviderError):
            provider.complete([], self.task, 1, 1)
        for value in (None, 1, "yes"):
            with self.subTest(value=value), self.assertRaises(ProviderError):
                OpenAICompatibleProvider(self.settings, preserve_reasoning_content=value)

    def test_vendor_options_cannot_replace_tools_mode_model_or_output_limit(self):
        for field in ("model", "messages", "tools", "tool_choice", "max_tokens",
                      "max_completion_tokens", "max_output_tokens", "stream"):
            with self.subTest(field=field), self.assertRaises(ProviderError):
                OpenAICompatibleProvider(self.settings, request_options={field: None})
        for options in ([], {"temperature": float("nan")}, {"temperature": float("inf")},
                        {"non-json": object()}, {1: "wrong key"}, {"long": "x" * 16001}):
            with self.subTest(options=type(options).__name__), self.assertRaises(ProviderError):
                OpenAICompatibleProvider(self.settings, request_options=options)
        options = {"thinking": {"type": "disabled"}}
        provider = OpenAICompatibleProvider(self.settings, request_options=options)
        options["thinking"]["type"] = "enabled"
        payload = provider.build_payload([])
        payload["thinking"]["type"] = "enabled"
        self.assertEqual(provider.build_payload([])["thinking"], {"type": "disabled"})

    def test_unsafe_endpoint_is_rejected_before_a_credential_can_be_dispatched(self):
        calls = []
        transport = httpx.MockTransport(lambda request: calls.append(request))
        for url in ("http://vendor.example.invalid/v1", "https://user:password@vendor.example.invalid/v1",
                    "https://vendor.example.invalid/v1?token=value", "https://vendor.example.invalid/v1#part",
                    "https://vendor.example.invalid/v1?", "https://vendor.example.invalid:0/v1",
                    "https://vendor.example.invalid:99999/v1", "file:///tmp/provider", " ",
                    "https://vendor.example.invalid/\n"):
            with self.subTest(url=url), self.assertRaises(ProviderError):
                OpenAICompatibleProvider(replace(self.settings, base_url=url), transport).complete([], self.task, 1, 1)
        self.assertEqual(calls, [])

    def test_explicit_loopback_http_is_available_for_local_compatible_servers(self):
        observed = []
        def handler(request):
            observed.append(str(request.url))
            return httpx.Response(200, json=completion())
        for base in ("http://127.0.0.1:18080/v1/", "http://localhost:18080/v1", "http://[::1]:18080/v1"):
            OpenAICompatibleProvider(replace(self.settings, base_url=base),
                httpx.MockTransport(handler)).complete([], self.task, 1, 1)
        self.assertEqual(len(observed), 3)
        self.assertTrue(all(url.endswith("/v1/chat/completions") for url in observed))

    def test_redirect_and_http_errors_do_not_echo_body_or_forward_auth(self):
        for status in (302, 401, 429, 500):
            calls = []
            def handler(request):
                calls.append(str(request.url))
                return httpx.Response(status, text=self.settings.api_key + " private body",
                                      headers={"Location": "https://different.example.invalid/v1"})
            provider = OpenAICompatibleProvider(self.settings, httpx.MockTransport(handler))
            with self.subTest(status=status), self.assertRaises(ProviderError) as error:
                provider.complete([], self.task, 1, 1)
            self.assertEqual(len(calls), 1)
            self.assertNotIn(self.settings.api_key, str(error.exception))
            self.assertNotIn("private body", str(error.exception))
            self.assertFalse(error.exception.uncertain)

    def test_network_failure_is_unknown_and_never_retried(self):
        calls = []
        def handler(request):
            calls.append(1)
            raise httpx.ReadTimeout(self.settings.api_key + " private endpoint", request=request)
        provider = OpenAICompatibleProvider(self.settings, httpx.MockTransport(handler))
        provider.last_completion_metadata = {"response_id": "stale"}
        with self.assertRaises(ProviderError) as error:
            provider.complete([], self.task, 1, 1)
        self.assertTrue(error.exception.uncertain)
        self.assertEqual(calls, [1])
        self.assertEqual(provider.last_completion_metadata, {})
        self.assertNotIn(self.settings.api_key, str(error.exception))
        self.assertNotIn("private endpoint", str(error.exception))

    def test_streamed_reads_have_a_whole_call_deadline(self):
        class SlowStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                await asyncio.sleep(0.3)
                yield json.dumps(completion()).encode()
        calls = []
        def handler(request):
            calls.append(1)
            return httpx.Response(200, stream=SlowStream())
        started = time.monotonic()
        with self.assertRaises(ProviderError) as error:
            OpenAICompatibleProvider(self.settings, httpx.MockTransport(handler)).complete([], self.task, 1, 0.05)
        self.assertTrue(error.exception.uncertain)
        self.assertEqual(calls, [1])
        self.assertLess(time.monotonic() - started, 0.25)

    def test_proxy_credentials_and_prior_cookies_are_not_forwarded(self):
        requests = []
        def handler(request):
            requests.append(dict(request.headers))
            return httpx.Response(200, json=completion(), headers={"Set-Cookie": "session=do-not-reuse"})
        real_client = httpx.AsyncClient
        with patch("patchtrail.providers.httpx.AsyncClient", wraps=real_client) as clients, \
                patch.dict("os.environ", {"HTTPS_PROXY": "http://proxy.invalid:1", "NETRC": "/nonexistent/private"}):
            provider = OpenAICompatibleProvider(self.settings, httpx.MockTransport(handler))
            provider.complete([], self.task, 1, 1)
            provider.complete([], self.task, 2, 1)
        self.assertTrue(all(call.kwargs["trust_env"] is False and
                            call.kwargs["follow_redirects"] is False for call in clients.call_args_list))
        self.assertTrue(all("cookie" not in headers for headers in requests))
        self.assertTrue(all(headers["authorization"] == "Bearer " + self.settings.api_key for headers in requests))

    def test_credential_is_rechecked_at_dispatch_after_initial_session_lookup(self):
        lookups, requests = [], []
        def credential():
            lookups.append(1)
            if len(lookups) > 1:
                raise ProviderError("Member session ended before dispatch")
            return "synthetic-member-key"
        provider = DeepSeekProvider(Settings(api_key=""), httpx.MockTransport(lambda r: requests.append(r)),
                                    credential_supplier=credential)
        with self.assertRaises(ProviderError):
            provider.complete([], self.task, 1, 1)
        self.assertEqual(lookups, [1, 1])
        self.assertEqual(requests, [])

    def test_generic_response_redacts_credentials_in_content_metadata_and_nested_tool_json(self):
        key = self.settings.api_key
        escaped = "".join("\\u%04x" % ord(character) for character in key)
        body = completion(model=key, content=key)
        body["id"] = key
        body["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = '{"path":"' + escaped + '"}'
        provider = OpenAICompatibleProvider(self.settings,
            httpx.MockTransport(lambda request: httpx.Response(200, json=body)))
        message, _ = provider.complete([], self.task, 1, 1)
        self.assertNotIn(key, json.dumps(message))
        self.assertNotIn(key, json.dumps(provider.last_completion_metadata))
        self.assertEqual(message["content"], "[REDACTED]")
        self.assertEqual(json.loads(message["tool_calls"][0]["function"]["arguments"]), {"path": "[REDACTED]"})


if __name__ == "__main__":
    unittest.main()
