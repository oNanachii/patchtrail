"""No paid dispatch: exercise the official-profile ledger with mock transports."""
import asyncio
import copy
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import httpx

from scripts.benchmark_meter import CapExceeded, MeterStopped, _json
from scripts.zhipu_benchmark_meter import (ENDPOINT, MAX_RATES, PRICING, ZhipuMeter,
                                          estimate_request, normalize_usage, tariff_rates)


SECRET = "zhipu-unit-secret"


def payload(**updates):
    return {"model": "glm-4.7", "messages": [{"role": "user", "content": "repair"}],
            "max_tokens": 4096, "stream": False, "thinking": {"type": "disabled"}, **updates}


def completion(*, prompt=1000, output=100, cached=400, reported=True):
    usage = {"prompt_tokens": prompt, "completion_tokens": output, "total_tokens": prompt + output}
    if reported:
        usage["prompt_tokens_details"] = {"cached_tokens": cached}
    return {"id": "zhipu-test-response", "model": "glm-4.7", "choices": [{
        "finish_reason": "stop", "message": {"role": "assistant", "content": "fixed"}}], "usage": usage}


class ZhipuBenchmarkMeterTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(prefix="patchtrail-zhipu-meter-")
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "calls.json"

    def test_pending_reservation_precedes_dispatch_and_prices_reported_cache(self):
        def handler(request):
            journal = json.loads(self.path.read_text())
            self.assertEqual(journal["calls"][0]["status"], "pending")
            self.assertEqual(journal["endpoint"], ENDPOINT)
            self.assertEqual(journal["pricing"], PRICING)
            self.assertNotIn(SECRET, self.path.read_text())
            self.assertEqual(request.headers["Authorization"], "Bearer " + SECRET)
            self.assertEqual(str(request.url), ENDPOINT)
            return httpx.Response(200, json=completion())
        meter = ZhipuMeter(self.path, SECRET, transport=httpx.MockTransport(handler))
        meter.send(payload(), 2, {"task": "fixture"})
        self.assertEqual(meter.calls[0]["usage"]["prompt_cache_miss_tokens"], 600)
        self.assertEqual(meter.calls[0]["tariff_rates_yuan_per_million"],
                         {"cache_hit": "0.4", "cache_miss": "2", "output": "8"})
        self.assertAlmostEqual(meter.summary()["tariff_yuan"], 0.00216)
        self.assertAlmostEqual(meter.summary()["conservative_peak_yuan"], 0.0056)
        self.assertTrue(meter.calls[0]["cache_usage_reported"])
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_tier_boundaries_and_cache_shapes_are_explicit(self):
        for prompt, output, expected in [(32767, 199, ("0.4", "2", "8")),
                                         (32767, 200, ("0.6", "3", "14")),
                                         (32768, 199, ("0.8", "4", "16")),
                                         (32768, 200, ("0.8", "4", "16"))]:
            with self.subTest(prompt=prompt, output=output):
                usage = normalize_usage(completion(prompt=prompt, output=output))
                rates = tariff_rates(usage)
                self.assertEqual(tuple(rates[key] for key in ("cache_hit", "cache_miss", "output")), expected)
        body = completion()
        body["usage"]["prompt_cache_hit_tokens"] = 400
        self.assertEqual(normalize_usage(body)["prompt_cache_hit_tokens"], 400)
        del body["usage"]["prompt_tokens_details"]
        self.assertEqual(normalize_usage(body)["prompt_cache_hit_tokens"], 400)

    def test_missing_cache_usage_receives_no_assumed_discount(self):
        meter = ZhipuMeter(self.path, SECRET, transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=completion(reported=False))))
        meter.send(payload(), 2, {})
        self.assertFalse(meter.calls[0]["cache_usage_reported"])
        self.assertEqual(meter.calls[0]["usage"]["prompt_cache_hit_tokens"], 0)
        self.assertAlmostEqual(meter.summary()["tariff_yuan"], 0.0028)
        for details in (None, {}, {"cached_tokens": None}):
            with self.subTest(details=details):
                body = completion(reported=False)
                body["usage"].update(prompt_tokens_details=details, prompt_cache_hit_tokens=None,
                                     prompt_cache_miss_tokens=None)
                self.assertEqual(normalize_usage(body)["prompt_cache_hit_tokens"], 0)

    def test_invalid_or_conflicting_usage_is_never_priced_as_zero(self):
        modifications = [{"total_tokens": 1}, {"prompt_tokens": True}, {"completion_tokens": -1},
                         {"prompt_cache_hit_tokens": 300}, {"prompt_tokens_details": []},
                         {"prompt_tokens_details": {"cached_tokens": True}},
                         {"prompt_tokens_details": {"cached_tokens": 1001}},
                         {"prompt_cache_miss_tokens": 1000}]
        for update in modifications:
            with self.subTest(update=update):
                body = completion()
                body["usage"].update(update)
                with self.assertRaises(ValueError):
                    normalize_usage(body)

    def test_cap_and_cross_instance_locking_refuse_additional_dispatch(self):
        requests = []
        def handler(request):
            requests.append(request)
            return httpx.Response(200, json=completion())
        cap = estimate_request(payload())["reservation_peak_yuan"] + 0.001
        first = ZhipuMeter(self.path, SECRET, cap_yuan=cap, transport=httpx.MockTransport(handler))
        second = ZhipuMeter(self.path, SECRET, cap_yuan=cap, transport=httpx.MockTransport(handler))
        first.send(payload(), 2, {})
        with self.assertRaises(CapExceeded):
            second.send(payload(), 2, {})
        restarted = ZhipuMeter(self.path, SECRET, cap_yuan=cap, transport=httpx.MockTransport(handler))
        with self.assertRaises(CapExceeded):
            restarted.send(payload(), 2, {})
        self.assertEqual(len(requests), 1)

    def test_timeout_http_and_incomplete_usage_preserve_reservation_and_never_retry(self):
        for failure in ("timeout", "http", "usage", "oversize", "json"):
            with self.subTest(failure=failure):
                path = self.path.with_name(failure + ".json")
                requests = []
                def handler(request):
                    requests.append(1)
                    if failure == "timeout":
                        raise httpx.ReadTimeout(SECRET + " private detail", request=request)
                    if failure == "http":
                        return httpx.Response(503, text=SECRET + " private detail")
                    if failure == "oversize":
                        return httpx.Response(200, content=b"x" * 256001)
                    if failure == "json":
                        return httpx.Response(200, content=b"not-json")
                    return httpx.Response(200, json={})
                meter = ZhipuMeter(path, SECRET, transport=httpx.MockTransport(handler))
                with self.assertRaises(MeterStopped) as caught:
                    meter.send(payload(), 2, {})
                self.assertTrue(caught.exception.uncertain)
                self.assertNotIn(SECRET, str(caught.exception))
                self.assertEqual(meter.summary()["unknown_calls"], 1)
                self.assertGreater(meter.summary()["unresolved_reserved_yuan"], 0)
                restarted = ZhipuMeter(path, SECRET, transport=httpx.MockTransport(handler))
                with self.assertRaises(MeterStopped):
                    restarted.send(payload(), 2, {})
                self.assertEqual(len(requests), 1)

    def test_nested_unicode_key_echo_is_redacted_from_result_and_journal(self):
        encoded = "".join("\\u%04x" % ord(character) for character in SECRET)
        body = completion()
        body["choices"][0]["message"] = {"role": "assistant", "content": SECRET,
            "tool_calls": [{"function": {"arguments": '{"content":"' + encoded + '"}'}}]}
        meter = ZhipuMeter(self.path, SECRET, transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=body)))
        result = meter.send(payload(), 2, {"diagnostic": SECRET})
        self.assertNotIn(SECRET, self.path.read_text())
        self.assertNotIn(encoded, self.path.read_text())
        self.assertEqual(result["choices"][0]["message"]["content"], "[REDACTED]")
        self.assertIn("[REDACTED]", result["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"])

    def test_key_in_model_input_is_rejected_before_any_dispatch(self):
        requests = []
        meter = ZhipuMeter(self.path, SECRET, transport=httpx.MockTransport(
            lambda request: requests.append(request)))
        encoded = "".join("\\u%04x" % ord(character) for character in SECRET)
        for text in (SECRET, '{"content":"' + encoded + '"}'):
            with self.subTest(encoded=text != SECRET), self.assertRaises(ValueError):
                meter.send(payload(messages=[{"role": "user", "content": text}]), 2, {})
        self.assertEqual(requests, [])
        self.assertFalse(self.path.exists())

    def test_configuration_fingerprint_rejects_mutated_endpoint_price_model_or_cap(self):
        meter = ZhipuMeter(self.path, SECRET, transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=completion())))
        meter.send(payload(), 2, {})
        original = json.loads(self.path.read_text())
        for mutation in ("endpoint", "price", "model", "cap", "fingerprint"):
            with self.subTest(mutation=mutation):
                ledger = copy.deepcopy(original)
                if mutation == "endpoint":
                    ledger["endpoint"] = "https://example.invalid/chat/completions"
                elif mutation == "price":
                    ledger["pricing"]["tiers"][0]["cache_miss"] = "0"
                elif mutation == "model":
                    ledger["model"] = "another-model"
                elif mutation == "cap":
                    ledger["cap_yuan"] = 31
                else:
                    ledger["configuration_sha256"] = "0" * 64
                self.path.write_text(json.dumps(ledger))
                with self.assertRaises(ValueError):
                    ZhipuMeter(self.path, SECRET)

    def test_dispatched_payload_snapshot_cannot_drift_with_later_messages(self):
        meter = ZhipuMeter(self.path, SECRET, transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=completion())))
        request = payload()
        original = copy.deepcopy(request)
        meter.send(request, 2, {})
        request["messages"].append({"role": "tool", "content": "later observation"})
        meter.send(payload(), 2, {})
        saved = json.loads(self.path.read_text())["calls"][0]
        self.assertEqual(saved["request"], original)
        self.assertEqual(saved["request_sha256"], hashlib.sha256(_json(original).encode()).hexdigest())

    def test_whole_request_deadline_stops_without_releasing_unknown_reservation(self):
        requests = []
        async def handler(request):
            requests.append(1)
            await asyncio.sleep(1)
            return httpx.Response(200, json=completion())
        meter = ZhipuMeter(self.path, SECRET, transport=httpx.MockTransport(handler))
        with self.assertRaises(MeterStopped):
            meter.send(payload(), 0.01, {})
        self.assertEqual(len(requests), 1)
        self.assertEqual(meter.calls[0]["status"], "unknown")
        self.assertTrue(meter.stopped)

    def test_invalid_caps_models_or_request_profiles_never_dispatch(self):
        for cap in (0, -1, 30.01, float("nan"), float("inf")):
            with self.subTest(cap=cap), self.assertRaises(ValueError):
                ZhipuMeter(self.path, SECRET, cap_yuan=cap)
        with self.assertRaises(ValueError):
            ZhipuMeter(self.path, SECRET, model="glm-5")
        for update in ({"max_tokens": 0}, {"max_tokens": 4097}, {"model": "glm-5"},
                       {"thinking": {"type": "enabled"}}, {"stream": True}):
            with self.subTest(update=update), self.assertRaises(ValueError):
                estimate_request(payload(**update))
        estimate = estimate_request(payload())
        self.assertGreater(estimate["conservative_input_tokens"], estimate["estimated_input_tokens"])
        self.assertIn("not tokenizer", estimate["estimate_method"])
        self.assertEqual(MAX_RATES, {"cache_hit": "0.8", "cache_miss": "4", "output": "16"})


if __name__ == "__main__":
    unittest.main()
