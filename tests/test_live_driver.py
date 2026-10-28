import unittest
from unittest.mock import patch

import httpx

from scripts.verify_live import request


class LiveDriverTests(unittest.TestCase):
    def test_read_reconciles_transient_storage_failure(self):
        calls = []
        def handler(req):
            calls.append(req.method)
            return httpx.Response(503, json={"error": {"code": "storage_busy"}}) if len(calls) == 1 else httpx.Response(200, json={"id": "same-run"})
        with httpx.Client(transport=httpx.MockTransport(handler), base_url="http://localhost") as client, patch("scripts.verify_live.time.sleep"):
            self.assertEqual(request(client, "GET", "/api/runs/existing")["id"], "same-run")
        self.assertEqual(calls, ["GET", "GET"])

    def test_write_failure_is_never_silently_retried(self):
        calls = []
        def handler(req):
            calls.append(req.method)
            return httpx.Response(503, text="do not expose unit-provider-secret")
        with httpx.Client(transport=httpx.MockTransport(handler), base_url="http://localhost") as client:
            with self.assertRaises(RuntimeError) as error:
                request(client, "POST", "/api/runs", json={"task_id": "x"})
        self.assertEqual(calls, ["POST"])
        self.assertNotIn("unit-provider-secret", str(error.exception))

    def test_network_read_retry_is_bounded(self):
        calls = []
        def handler(req):
            calls.append(1)
            raise httpx.ReadTimeout("private endpoint", request=req)
        with httpx.Client(transport=httpx.MockTransport(handler), base_url="http://localhost") as client, patch("scripts.verify_live.time.sleep"):
            with self.assertRaises(RuntimeError):
                request(client, "GET", "/api/runs/existing")
        self.assertEqual(len(calls), 3)


if __name__ == "__main__":
    unittest.main()
