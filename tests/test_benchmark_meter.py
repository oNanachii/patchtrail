import asyncio
import json
from pathlib import Path
import tempfile
import unittest

import httpx

from scripts.benchmark_meter import (CapExceeded, Meter, MeterStopped, estimate_request,
                                     tariff_rates)


def payload():
    return {"model": "deepseek-flash", "messages": [{"role": "user", "content": "repair"}],
            "max_tokens": 4096, "stream": False, "thinking": {"type": "disabled"}}


def completion(**extra):
    return {"model": "deepseek-flash", "choices": [{"finish_reason": "stop", "message": {
        "role": "assistant", "content": "fixed"}}], "usage": {"prompt_tokens": 1000,
        "prompt_cache_hit_tokens": 200, "completion_tokens": 100, "total_tokens": 1100}, **extra}


class BenchmarkMeterTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "meter.json"

    def test_reservation_is_saved_before_dispatch_and_cost_counts_cache(self):
        def handler(request):
            journal = json.loads(self.path.read_text())
            self.assertEqual(journal["calls"][0]["status"], "pending")
            self.assertEqual(request.headers["Authorization"], "Bearer secret-key")
            return httpx.Response(200, json=completion())
        meter = Meter(self.path, "secret-key", transport=httpx.MockTransport(handler))
        self.assertEqual(meter.send(payload(), 2, {"task": "example"})["model"], "deepseek-flash")
        self.assertAlmostEqual(meter.summary()["conservative_peak_yuan"], 0.002408)
        self.assertEqual(meter.calls[0]["usage"]["prompt_cache_miss_tokens"], 800)
        self.assertEqual(meter.summary()["unknown_calls"], 0)
        self.assertNotIn("secret-key", self.path.read_text())

    def test_cap_prevents_network_and_two_instances_share_ledger(self):
        calls = []
        def handler(request):
            calls.append(1)
            return httpx.Response(200, json=completion())
        # One reservation fits; the second no longer fits after actual peak cost.
        cap = estimate_request(payload())["reservation_peak_yuan"] + 0.001
        first = Meter(self.path, "secret", cap_yuan=cap, transport=httpx.MockTransport(handler))
        second = Meter(self.path, "secret", cap_yuan=cap, transport=httpx.MockTransport(handler))
        first.send(payload(), 2, {})
        with self.assertRaises(CapExceeded):
            second.send(payload(), 2, {})
        restarted = Meter(self.path, "secret", cap_yuan=cap, transport=httpx.MockTransport(handler))
        with self.assertRaises(CapExceeded):
            restarted.send(payload(), 2, {})
        self.assertEqual(len(calls), 1)

    def test_unknown_outcome_stops_after_restart_without_retry(self):
        calls = []
        def handler(request):
            calls.append(1)
            raise httpx.ReadTimeout("secret private endpoint", request=request)
        meter = Meter(self.path, "secret", transport=httpx.MockTransport(handler))
        with self.assertRaises(MeterStopped) as error:
            meter.send(payload(), 2, {})
        self.assertTrue(error.exception.uncertain)
        self.assertNotIn("secret private", str(error.exception))
        restarted = Meter(self.path, "secret", transport=httpx.MockTransport(handler))
        with self.assertRaises(MeterStopped):
            restarted.send(payload(), 2, {})
        self.assertEqual(len(calls), 1)
        self.assertGreater(restarted.summary()["unresolved_reserved_yuan"], 0)

    def test_missing_usage_and_http_errors_preserve_reservation(self):
        for mode in ("usage", "http"):
            with self.subTest(mode=mode):
                path = self.path.with_name(mode + ".json")
                calls = []
                def handler(request):
                    calls.append(1)
                    return httpx.Response(503) if mode == "http" else httpx.Response(200, json={})
                meter = Meter(path, "secret", transport=httpx.MockTransport(handler))
                with self.assertRaises(MeterStopped):
                    meter.send(payload(), 2, {})
                self.assertEqual(meter.summary()["unknown_calls"], 1)
                self.assertEqual(len(calls), 1)

    def test_nested_unicode_secret_redacted_and_length_response_still_metered(self):
        encoded = "".join("\\u%04x" % ord(char) for char in "secret-key")
        body = completion()
        body["choices"][0]["finish_reason"] = "length"
        body["choices"][0]["message"]["tool_calls"] = [{"function": {
            "arguments": '{"content":"' + encoded + '"}'}}]
        meter = Meter(self.path, "secret-key", transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=body)))
        result = meter.send(payload(), 2, {})
        self.assertEqual(meter.summary()["confirmed_calls"], 1)
        self.assertIn("[REDACTED]", result["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"])
        self.assertNotIn(encoded, self.path.read_text())

    def test_price_windows_and_estimate_not_claimed_as_tokenizer(self):
        self.assertEqual(tariff_rates("2026-11-02T01:00:00+00:00")["output"], "8")
        self.assertEqual(tariff_rates("2026-11-01T01:00:00+00:00")["output"], "4")
        estimate = estimate_request(payload())
        self.assertGreater(estimate["conservative_input_tokens"], estimate["estimated_input_tokens"])
        self.assertIn("not tokenizer", estimate["estimate_method"])
        with self.assertRaises(ValueError):
            estimate_request({"max_tokens": 0})

    def test_whole_call_deadline_keeps_unknown_reservation(self):
        calls = []
        async def handler(request):
            calls.append(1)
            await asyncio.sleep(1)
            return httpx.Response(200, json=completion())
        meter = Meter(self.path, "secret", transport=httpx.MockTransport(handler))
        with self.assertRaises(MeterStopped):
            meter.send(payload(), 0.01, {})
        self.assertEqual(len(calls), 1)
        self.assertTrue(meter.stopped)
        self.assertEqual(meter.calls[0]["status"], "unknown")


if __name__ == "__main__":
    unittest.main()
