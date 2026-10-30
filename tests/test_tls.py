"""Member secrets are refused before ASGI body receipt on insecure connections."""
import asyncio
import json

from fastapi.testclient import TestClient

from test_byok import ByokCase


class MemberTransportTests(ByokCase):
    def raw_insecure_request(self, host, client, headers=(), path="/api/session", method="POST"):
        received = 0
        sent = []
        async def receive():
            nonlocal received
            received += 1
            return {"type": "http.request", "body": b'{"api_key":"must-not-be-read"}', "more_body": False}
        async def send(message):
            sent.append(message)
        scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
                 "http_version": "1.1", "scheme": "http", "method": method,
                 "path": path, "raw_path": path.encode(), "root_path": "", "query_string": b"",
                 "headers": [(b"host", host.encode()), (b"content-type", b"application/json"), *headers],
                 "client": (client, 50000), "server": (host, 80)}
        asyncio.run(self.app(scope, receive, send))
        start = next(item for item in sent if item["type"] == "http.response.start")
        body = b"".join(item.get("body", b"") for item in sent if item["type"] == "http.response.body")
        return start["status"], json.loads(body), received

    def test_login_rejects_http_before_body_and_does_not_trust_forwarded_headers(self):
        for host, client in [("example.test", "203.0.113.42"), ("localhost", "203.0.113.42"),
                             ("example.test", "127.0.0.1"), ("testserver", "127.0.0.1")]:
            with self.subTest(host=host, client=client):
                status, result, received = self.raw_insecure_request(host, client, [
                    (b"x-forwarded-proto", b"https"), (b"x-forwarded-for", b"127.0.0.1")])
                self.assertEqual(status, 400)
                self.assertEqual(result["error"]["code"], "https_required")
                self.assertEqual(received, 0)
        self.assertEqual(self.app.state.members.sessions, {})
        self.assertEqual(self.calls, [])

    def test_explicit_real_loopback_development_can_login_and_read_session(self):
        member = self.member("loopback")
        for base_url, peer in [("http://localhost", "127.0.0.1"), ("http://127.0.0.1", "127.0.0.1"),
                               ("http://[::1]", "::1")]:
            with self.subTest(base_url=base_url):
                client = TestClient(self.app, base_url=base_url, client=(peer, 50000))
                self.addCleanup(client.close)
                response = client.post("/api/session", json={"invite_code": member["invite"], "api_key": member["key"]})
                self.assertEqual(response.status_code, 200, response.text)
                header = {"Authorization": "Bearer " + response.json()["session_token"]}
                self.assertEqual(client.get("/api/session", headers=header).status_code, 200)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.runner.calls, 0)

    def test_session_and_member_requests_require_https_without_consuming_body(self):
        member = self.member("protected")
        headers = [(b"authorization", ("Bearer " + member["token"]).encode())]
        for method, path in [("GET", "/api/session"), ("DELETE", "/api/session"),
                             ("GET", "/api/tasks"), ("POST", "/api/imports/preview"),
                             ("POST", "/api/tasks/quantity-shortfall/preflight"), ("POST", "/api/runs")]:
            with self.subTest(method=method, path=path):
                status, result, received = self.raw_insecure_request("example.test", "203.0.113.42", headers, path, method)
                self.assertEqual(status, 400)
                self.assertEqual(result["error"]["code"], "https_required")
                self.assertEqual(received, 0)
        # Rejected insecure logout must not mutate the authenticated HTTPS session.
        self.assertEqual(self.client.get("/api/session", headers=member["headers"]).status_code, 200)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.runner.calls, 0)

    def test_health_exposes_only_configured_run_budgets(self):
        response = self.client.get("/api/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["run_limits"], self.settings.limits)
        self.assertNotIn(self.settings.api_key, response.text)
        self.assertNotIn(self.settings.admin_token, response.text)
