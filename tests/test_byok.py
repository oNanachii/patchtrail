"""Invitation/session isolation with temporary identities and explicit no-network fakes."""
from concurrent.futures import ThreadPoolExecutor
import io
import json
from pathlib import Path
import stat
import threading
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
import zipfile

import httpx

from fastapi.testclient import TestClient

from patchtrail.api import create_app
from patchtrail.config import Settings
from patchtrail.providers import DeepSeekProvider, ProviderError, ScriptedProvider
from patchtrail.service import RunService
from patchtrail.tasks import get_task
from test_api import FakeRunner, NoNetworkProvider, TASK
from test_web_imports import payload


class ByokCase(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(prefix="patchtrail-byok-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.settings = Settings(data_dir=self.root / "data", api_key="legacy-test-key-not-real",
                                 admin_token="legacy-operator-token-not-an-invite")
        self.runner = FakeRunner()
        self.service = RunService(self.settings, runner=self.runner, provider=NoNetworkProvider(), dispatch=False)
        self.addCleanup(self.service.close)
        with patch("patchtrail.api.load_local_env"):
            self.app = create_app(self.service)
        self.addCleanup(self.app.state.members.close)
        self.client = TestClient(self.app, base_url="https://testserver")
        self.addCleanup(self.client.close)
        self.operator = {"Authorization": "Bearer " + self.settings.admin_token}
        self.calls = []
        calls = self.calls
        def no_network_complete(provider, messages, task, call_number, remaining_seconds):
            calls.append(call_number)
            return ScriptedProvider().complete(messages, task, call_number, remaining_seconds)
        self.addCleanup(patch.stopall)
        patch("patchtrail.providers.DeepSeekProvider.complete", new=no_network_complete).start()

    def member(self, name="one"):
        target = self.root / ("invitation-" + name + ".json")
        member_id = self.app.state.members.create(target)
        invitation = json.loads(target.read_text())
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        secret = "unit-byok-key-" + name + "-secret-marker"
        response = self.client.post("/api/session", json={"invite_code": invitation["invite_code"], "api_key": secret})
        self.assertEqual(response.status_code, 200, response.text)
        session = response.json()
        self.assertEqual(session["member_id"], member_id)
        self.assertNotIn(secret, response.text)
        self.assertNotEqual(session["session_token"], self.settings.admin_token)
        return {"id": member_id, "invite": invitation["invite_code"], "key": secret,
                "token": session["session_token"], "headers": {"Authorization": "Bearer " + session["session_token"]},
                "session": session}

    def preflight(self, member, task_id=TASK):
        response = self.client.post(f"/api/tasks/{task_id}/preflight", headers=member["headers"])
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def create_live(self, member, key="byok-run-request", preflight=None, task_id=TASK):
        flight = preflight if preflight is not None else self.preflight(member, task_id)
        return self.client.post("/api/runs", headers={**member["headers"], "Idempotency-Key": key},
                                json={"task_id": task_id, "mode": "live", "preflight_id": flight["preflight_id"]})

    def import_task(self, headers, title):
        preview = self.client.post("/api/imports/preview", headers=headers, json=payload(title))
        self.assertEqual(preview.status_code, 200, preview.text)
        committed = self.client.post("/api/imports/commit", headers=headers,
                                     json={"preview_id": preview.json()["preview_id"]})
        self.assertEqual(committed.status_code, 200, committed.text)
        return preview.json(), committed.json()["task"]


class ByokSecurityTests(ByokCase):
    def test_login_is_model_free_credentials_do_not_enter_data_or_responses(self):
        member = self.member()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.runner.calls, 0)
        status = self.client.get("/api/session", headers=member["headers"])
        self.assertEqual(status.status_code, 200)
        self.assertEqual(status.json()["role"], "member")
        for route in ("/api/session", "/api/health", "/api/tasks", "/api/runs", "/api/projects"):
            response = self.client.get(route, headers=member["headers"])
            self.assertNotIn(member["key"], response.text)
            self.assertNotIn(member["invite"], response.text)
            self.assertNotIn(member["token"], response.text)
        # Invitation delivery file is deliberately outside the application data directory.
        for path in self.settings.data_dir.rglob("*"):
            if path.is_file():
                contents = path.read_bytes()
                for value in (member["key"], member["invite"], member["token"]):
                    self.assertNotIn(value.encode(), contents, str(path))
        bad = self.client.post("/api/session", json={"invite_code": self.settings.admin_token, "api_key": member["key"]})
        self.assertEqual(bad.status_code, 401)
        endpoint = self.client.post("/api/session", json={"invite_code": member["invite"], "api_key": member["key"],
                                                        "base_url": "https://untrusted.invalid"})
        self.assertEqual(endpoint.status_code, 422)
        self.assertEqual(self.calls, [])

    def test_namespace_isolation_includes_list_import_preflight_export_and_mutation(self):
        alice, bob = self.member("alice"), self.member("bob")
        preview, task = self.import_task(alice["headers"], "Alice private source")
        legacy_preview, legacy_task = self.import_task(self.operator, "Legacy private source")
        live = self.create_live(alice).json()
        legacy = self.service.create(TASK, "live", "legacy-private-run")
        for owner in (bob, alice):
            for rid in ([live["id"], legacy["id"]] if owner is bob else [legacy["id"]]):
                for suffix in ("", "/events", "/report", "/patch.diff", "/files.zip"):
                    self.assertEqual(self.client.get(f"/api/runs/{rid}{suffix}", headers=owner["headers"]).status_code, 404)
                for suffix, body in (("cancel", {}), ("approve", {"proposal_digest": "a" * 64, "expected_version": 0}),
                                     ("resume", {"expected_version": 0})):
                    self.assertEqual(self.client.post(f"/api/runs/{rid}/{suffix}", headers=owner["headers"], json=body).status_code, 404)
        for owner, foreign_preview, foreign_task in ((bob, preview, task), (alice, legacy_preview, legacy_task)):
            for route in ("/api/tasks", "/api/projects", "/api/runs"):
                listing = self.client.get(route, headers=owner["headers"])
                self.assertNotIn(foreign_task["id"], listing.text)
                self.assertNotIn(legacy["id"], listing.text)
            self.assertEqual(self.client.get("/api/imports/" + foreign_preview["preview_id"], headers=owner["headers"]).status_code, 404)
            self.assertEqual(self.client.post("/api/imports/commit", headers=owner["headers"],
                                            json={"preview_id": foreign_preview["preview_id"]}).status_code, 404)
            self.assertEqual(self.client.post(f"/api/tasks/{foreign_task['id']}/preflight", headers=owner["headers"]).status_code, 404)
        self.assertNotIn(live["id"], self.client.get("/api/runs", headers=bob["headers"]).text)
        self.assertNotIn(live["id"], self.client.get("/api/runs", headers=self.operator).text)
        unknown = {"Authorization": "Bearer unknown-session-token"}
        for route in ("/api/tasks", "/api/runs", "/api/projects"):
            self.assertEqual(self.client.get(route, headers=unknown).status_code, 401)

    def test_logout_expiry_and_external_revocation_block_new_provider_requests(self):
        from patchtrail.members import MemberRegistry
        for action in ("logout", "expiry", "revoke"):
            with self.subTest(action=action):
                member = self.member(action)
                row = self.create_live(member, "run-before-" + action).json()
                if action == "logout":
                    self.assertEqual(self.client.delete("/api/session", headers=member["headers"]).status_code, 200)
                elif action == "expiry":
                    self.app.state.members.clock = lambda: member["session"]["expires_at"] + 1
                else:
                    # Another process/CLI updates the same registry, not the session object.
                    MemberRegistry(self.settings.data_dir).revoke(member["id"])
                before = len(self.calls)
                self.app.state.member_services[member["id"]].diagnose(row["id"])
                self.assertEqual(len(self.calls), before)
                self.assertEqual(self.client.get("/api/runs", headers=member["headers"]).status_code, 401)
                self.app.state.members.clock = __import__("time").time

    def test_external_revocation_cancels_waiting_and_interrupted_runs_without_erasing_history(self):
        from patchtrail.members import MemberRegistry
        member = self.member("revoked-waiting")
        flight = self.preflight(member)
        waiting = self.create_live(member, "revoke-waiting-record", flight).json()
        interrupted = self.create_live(member, "revoke-interrupted-record", flight).json()
        service = self.app.state.member_services[member["id"]]
        service.diagnose(waiting["id"])
        service.store.transition(interrupted["id"], {"queued"}, "diagnosing")
        service.store.transition(interrupted["id"], {"diagnosing"}, "interrupted", {"stop_reason": "test-restart"})
        original = {rid: service.store.get_run(rid) for rid in (waiting["id"], interrupted["id"])}
        events = {rid: service.store.events(rid) for rid in original}
        self.assertEqual(original[waiting["id"]]["state"], "awaiting_approval")
        self.assertEqual(service.pool.active_runs(), 2)
        MemberRegistry(self.settings.data_dir).revoke(member["id"])
        self.assertEqual(service.pool.active_runs(), 0)
        self.assertEqual(service.store.count_runs(), 2)
        for rid, before in original.items():
            after = service.store.get_run(rid)
            self.assertEqual(after["state"], "cancelled")
            self.assertEqual(after["stop_reason"], "invitation_revoked")
            self.assertEqual(after["proposal"], before["proposal"])
            self.assertEqual(after["checkpoint"], before["checkpoint"])
            self.assertEqual(after["version"], before["version"] + 1)
            history = service.store.events(rid)
            self.assertEqual(history[:-1], events[rid])
            self.assertEqual(history[-1]["payload"]["reason"], "invitation_revoked")
        self.assertEqual(self.client.get("/api/runs", headers=member["headers"]).status_code, 401)
        # Freed global capacity can be used by a different invitation.
        other = self.member("after-revocation")
        self.assertEqual(self.create_live(other, "new-member-after-revoke").status_code, 201)

    def test_twenty_total_run_quota_still_allows_exact_idempotent_readback(self):
        from patchtrail.members import digest
        member = self.member("lifetime-quota")
        flight = self.preflight(member)
        service = self.app.state.member_services[member["id"]]
        first = None
        for index in range(20):
            row = service.create(TASK, "live", "lifetime-run-" + str(index), flight["preflight_id"], digest(member["token"]))
            first = first or row
            service.cancel(row["id"])
        self.assertEqual(service.store.count_runs(), 20)
        self.assertEqual(service.store.count_runs(active=True), 0)
        overflow = self.create_live(member, "lifetime-run-overflow", flight)
        self.assertEqual(overflow.status_code, 429, overflow.text)
        self.assertEqual(overflow.json()["error"]["code"], "member_quota")
        retry = self.create_live(member, "lifetime-run-0", flight)
        self.assertEqual(retry.status_code, 201, retry.text)
        self.assertEqual(retry.json()["id"], first["id"])
        self.assertEqual(retry.json()["state"], "cancelled")
        self.assertEqual(service.store.count_runs(), 20)
        self.assertEqual(self.calls, [])

    def test_same_invitation_relogin_preserves_only_its_own_history(self):
        member = self.member()
        row = self.create_live(member).json()
        self.client.delete("/api/session", headers=member["headers"])
        login = self.client.post("/api/session", json={"invite_code": member["invite"], "api_key": member["key"]})
        self.assertEqual(login.status_code, 200)
        headers = {"Authorization": "Bearer " + login.json()["session_token"]}
        self.assertEqual(login.json()["member_id"], member["id"])
        self.assertIn(row["id"], self.client.get("/api/runs", headers=headers).text)

    def test_member_active_quota_and_global_capacity_cannot_be_bypassed_by_new_identities(self):
        members = [self.member("quota" + str(index)) for index in range(4)]
        for index, member in enumerate(members[:3]):
            flight = self.preflight(member)
            for number in range(2):
                response = self.create_live(member, f"quota-{index}-{number}", flight)
                self.assertEqual(response.status_code, 201, response.text)
            denied = self.create_live(member, f"quota-{index}-overflow", flight)
            self.assertEqual(denied.status_code, 429)
        flight = self.preflight(members[3])
        denied = self.create_live(members[3], "global-overflow", flight)
        self.assertEqual(denied.status_code, 429)
        self.assertEqual(sum(s.store.count_runs(active=True) for s in self.app.state.member_services.values()), 6)

    def test_preflight_and_member_diagnosis_share_one_bounded_execution_worker(self):
        self.service.dispatch = True
        alice, bob = self.member("worker-alice"), self.member("worker-bob")
        alice_service = self.app.state.member_services[alice["id"]]
        bob_service = self.app.state.member_services[bob["id"]]
        pool = self.service.pool
        self.assertIs(alice_service.pool, pool)
        self.assertIs(bob_service.pool, pool)
        flight = self.preflight(alice)
        provider_entered, queue_full, release = threading.Event(), threading.Event(), threading.Event()
        monitor = threading.Lock()
        current, peak, threads = [0], [0], set()
        original_runner, original_submit = self.runner.run, pool.submit
        def enter():
            with monitor:
                current[0] += 1
                peak[0] = max(peak[0], current[0])
                threads.add(threading.get_ident())
        def leave():
            with monitor:
                current[0] -= 1
        def tracked_runner(*args):
            enter()
            try:
                return original_runner(*args)
            finally:
                leave()
        def tracked_provider(provider, messages, task, call_number, remaining_seconds):
            enter()
            try:
                provider_entered.set()
                if not release.wait(5):
                    raise AssertionError("Test release did not arrive")
                return ScriptedProvider().complete(messages, task, call_number, remaining_seconds)
            finally:
                leave()
        def tracked_submit(operation):
            future = original_submit(operation)
            if pool.pending == pool.capacity:
                queue_full.set()
            return future
        def queued_preflight():
            return self.client.post(f"/api/tasks/{TASK}/preflight", headers=bob["headers"])
        with patch.object(self.runner, "run", side_effect=tracked_runner), patch.object(pool, "submit", side_effect=tracked_submit), \
                patch("patchtrail.providers.DeepSeekProvider.complete", new=tracked_provider), \
                ThreadPoolExecutor(max_workers=5) as requests:
            try:
                response = self.create_live(alice, "shared-worker-model", flight)
                self.assertEqual(response.status_code, 201, response.text)
                self.assertTrue(provider_entered.wait(3))
                waiting = [requests.submit(queued_preflight) for _ in range(5)]
                self.assertTrue(queue_full.wait(3))
                overflow = queued_preflight()
                self.assertEqual(overflow.status_code, 429, overflow.text)
                self.assertEqual(peak[0], 1)
                release.set()
                for future in waiting:
                    answer = future.result(timeout=5)
                    self.assertEqual(answer.status_code, 200, answer.text)
            finally:
                release.set()
        self.assertEqual(peak[0], 1)
        self.assertEqual(len(threads), 1)
        self.assertEqual(pool.pending, 0)

    def test_member_snapshot_quota_preserves_idempotent_commit(self):
        member = self.member()
        first = None
        for index in range(5):
            preview, task = self.import_task(member["headers"], "Quota snapshot " + str(index))
            first = first or preview
        preview = self.client.post("/api/imports/preview", headers=member["headers"], json=payload("Quota snapshot overflow")).json()
        denied = self.client.post("/api/imports/commit", headers=member["headers"], json={"preview_id": preview["preview_id"]})
        self.assertEqual(denied.status_code, 429)
        retry = self.client.post("/api/imports/commit", headers=member["headers"], json={"preview_id": first["preview_id"]})
        self.assertEqual(retry.status_code, 200, retry.text)
        self.assertFalse(retry.json()["created"])

    def test_verified_export_contains_real_replacement_files_and_rejects_tampering(self):
        member = self.member()
        row = self.create_live(member).json()
        rid = row["id"]
        service = self.app.state.member_services[member["id"]]
        self.assertNotEqual(self.client.get(f"/api/runs/{rid}/files.zip", headers=member["headers"]).status_code, 200)
        service.diagnose(rid)
        pending = service.store.get_run(rid)
        self.assertEqual(pending["state"], "awaiting_approval", pending)
        approval = self.client.post(f"/api/runs/{rid}/approve", headers=member["headers"],
                                   json={"proposal_digest": pending["proposal"]["digest"], "expected_version": pending["version"]})
        self.assertEqual(approval.status_code, 200)
        service.verify(rid)
        response = self.client.get(f"/api/runs/{rid}/files.zip", headers=member["headers"])
        self.assertEqual(response.status_code, 200, response.text if response.status_code != 200 else "")
        task = get_task(TASK)
        with zipfile.ZipFile(io.BytesIO(response.content)) as package:
            self.assertEqual(package.read(task.demo_path).decode(), task.demo_content)
            self.assertIn("PATCHTRAIL_APPLY.md", package.namelist())
            self.assertIn("patchtrail-manifest.json", package.namelist())
            for name in package.namelist():
                self.assertFalse(name.startswith("/") or ".." in Path(name).parts)
            self.assertNotIn(member["key"].encode(), b"".join(package.read(name) for name in package.namelist()))
        # Simulate database proposal corruption, not user source or frozen fixtures.
        proposal = service.store.get_run(rid)["proposal"]
        proposal["content"] = "malicious replacement not approved\n"
        with service.store._transaction() as connection:
            connection.execute("UPDATE runs SET proposal=? WHERE id=?", (json.dumps(proposal), rid))
        denied = self.client.get(f"/api/runs/{rid}/files.zip", headers=member["headers"])
        self.assertNotEqual(denied.status_code, 200)
        self.assertNotIn("malicious replacement", denied.text)


class ByokTransportTests(unittest.TestCase):
    def test_transport_rejects_arbitrary_endpoint_before_network_dispatch(self):
        calls = []
        settings = Settings(api_key="unit-private-transport-key", base_url="https://attacker.invalid")
        provider = DeepSeekProvider(settings, httpx.MockTransport(lambda request: calls.append(request)))
        with self.assertRaises(ProviderError):
            provider.complete([], get_task(TASK), 1, 1)
        self.assertEqual(calls, [])

    def test_transport_redacts_unicode_escaped_key_and_second_level_tool_json(self):
        key = "unit-private-transport-key"
        escaped = "".join("\\u%04x" % ord(character) for character in key)
        # The outer response uses escaped Unicode for content/metadata. Tool
        # arguments contain a second JSON document, decoded later by the service.
        arguments = '{"query":"' + escaped + '","nested":{"value":"' + escaped + '"}}'
        body = {"id": key, "model": key, "choices": [{"finish_reason": "tool_calls", "message": {
            "role": "assistant", "content": key, "tool_calls": [{"id": "fake-call", "type": "function",
                "function": {"name": "search_code", "arguments": arguments}}]}}]}
        raw = json.dumps(body).replace('"' + key + '"', '"' + escaped + '"').encode()
        self.assertNotIn(key.encode(), raw)
        provider = DeepSeekProvider(Settings(api_key=key), httpx.MockTransport(lambda request: httpx.Response(200, content=raw)))
        message, _ = provider.complete([], get_task(TASK), 1, 1)
        self.assertEqual(message["content"], "[REDACTED]")
        decoded_arguments = json.loads(message["tool_calls"][0]["function"]["arguments"])
        self.assertEqual(decoded_arguments, {"query": "[REDACTED]", "nested": {"value": "[REDACTED]"}})
        self.assertNotIn(key, json.dumps(message))
        self.assertNotIn(key, json.dumps(provider.last_completion_metadata))

    def test_transport_removes_echoed_key_before_returning_message_and_metadata(self):
        key = "unit-private-transport-key"
        calls = []
        def handler(request):
            calls.append(request.url.host)
            self.assertEqual(request.headers["Authorization"], "Bearer " + key)
            return httpx.Response(200, json={"id": key, "model": key, "usage": {}, "choices": [{
                "finish_reason": "tool_calls", "message": {"role": "assistant", "content": key,
                    "tool_calls": [{"id": "fake-tool", "type": "function", "function": {
                        "name": "search_code", "arguments": json.dumps({"query": key})}}]}}]})
        provider = DeepSeekProvider(Settings(api_key=key), httpx.MockTransport(handler))
        message, usage = provider.complete([], get_task(TASK), 1, 1)
        self.assertEqual(calls, ["api.deepseek.com"])
        self.assertNotIn(key, json.dumps(message))
        self.assertNotIn(key, json.dumps(provider.last_completion_metadata))
        self.assertIn("[REDACTED]", message["content"])


if __name__ == "__main__":
    unittest.main()
