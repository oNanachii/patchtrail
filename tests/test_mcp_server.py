"""MCP contracts through the real API with explicit nonexecuting test doubles."""
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import httpx
from mcp.server.mcpserver.exceptions import ToolError

from patchtrail.api import create_app
from patchtrail.config import Settings
from patchtrail.sandbox import CheckResult, SandboxUnavailable
from patchtrail.service import RunService
from patchtrail.tasks import CHECK_COMMAND, get_task


TASK = "quantity-shortfall"
BASE_URL = "http://127.0.0.1:8874"


class InspectionRunner:
    """Byte comparison only: never import fixtures or execute a host command."""
    def __init__(self):
        self.online, self.calls = True, 0

    def available(self):
        return self.online

    def run(self, workspace, command, run_id):
        if not self.online:
            raise SandboxUnavailable("Explicit test runner is unavailable")
        if command != CHECK_COMMAND:
            raise AssertionError("Only the fixed catalog command may reach this double")
        self.calls += 1
        task = get_task(TASK)
        fixed = (workspace / task.demo_path).read_text() == task.demo_content
        output = "Ran 4 tests in 0.001s\n\n" + ("OK\n" if fixed else "FAILED (failures=1)\n")
        return CheckResult(0 if fixed else 1, output, 1, False, False,
                           "explicit-mcp-byte-inspection-no-execution")


class ForbiddenLiveProvider:
    label = "Live provider forbidden in MCP tests"

    def complete(self, *args):
        raise AssertionError("MCP must never dispatch a paid model")


class RecordingASGITransport(httpx.AsyncBaseTransport):
    def __init__(self, app):
        self.inner = httpx.ASGITransport(app=app)
        self.requests = []

    async def handle_async_request(self, request):
        self.requests.append({"method": request.method, "path": request.url.path,
            "headers": dict(request.headers), "body": request.content})
        return await self.inner.handle_async_request(request)

    async def aclose(self):
        pass


class McpApiContractTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from patchtrail.mcp_server import PatchTrailClient
        temporary = TemporaryDirectory(prefix="patchtrail-mcp-api-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.settings = Settings(data_dir=self.root / "data", api_key="fake-unused-model-key",
                                 admin_token="fake-operator-token")
        self.runner = InspectionRunner()
        self.service = RunService(self.settings, runner=self.runner,
                                  provider=ForbiddenLiveProvider(), dispatch=False)
        self.addCleanup(self.service.close)
        with patch("patchtrail.api.load_local_env"):
            self.app = create_app(self.service)
        self.addCleanup(self.app.state.members.close)
        self.transport = RecordingASGITransport(self.app)
        self.client = PatchTrailClient(BASE_URL, transport=self.transport)

    async def test_scripted_run_stops_for_web_approval_then_actual_api_verifier_decides(self):
        started = await self.client.start_demo_run(TASK, "mcp-demo-contract")
        self.assertFalse(started["paid_model_called"])
        created = started["run"]
        self.assertEqual(created["mode"], "demo")
        self.assertIn("Scripted", created["provider_label"])
        run_id = created["id"]
        self.service.diagnose(run_id)
        report = await self.client.get_run(run_id)
        row = report["run"]
        self.assertEqual(row["state"], "awaiting_approval")
        self.assertTrue(row["proposal"]["diff"])
        self.assertIsNone(row["result"])
        self.assertNotIn("content", row["proposal"])
        self.assertNotIn("messages", row["checkpoint"])
        self.assertTrue(report["events"])
        cursor = report["events"][-1]["id"]
        tail = await self.client.get_run(run_id, after=cursor)
        self.assertEqual(tail["events"], [])
        self.assertEqual(tail["run"]["id"], run_id)
        self.assertEqual(report["review"], {"web_url": BASE_URL + "/#run=" + run_id, "run_id": run_id,
                                          "approval_available_via_mcp": False})
        self.assertEqual(self.runner.calls, 1)
        self.assertFalse(any("approve" in request["path"] for request in self.transport.requests))
        # This explicitly simulates the existing human-facing webpage request;
        # the MCP client has no corresponding mutation method or tool.
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url=BASE_URL) as web:
            approved = await web.post(f"/api/runs/{run_id}/approve", json={
                "proposal_digest": row["proposal"]["digest"], "expected_version": row["version"]})
        self.assertEqual(approved.status_code, 200)
        self.service.verify(run_id)
        complete = await self.client.get_run(run_id)
        self.assertEqual(complete["run"]["state"], "succeeded")
        self.assertTrue(complete["run"]["result"]["passed"])
        self.assertEqual(complete["run"]["result"]["runner"], "explicit-mcp-byte-inspection-no-execution")
        self.assertEqual(self.runner.calls, 2)
        self.assertTrue(any(event["kind"] == "verification_started" for event in complete["events"]))

    async def test_stable_request_key_reuses_one_demo_and_never_accepts_paid_mode(self):
        first = await self.client.start_demo_run(TASK, "mcp-stable-retry-key")
        repeated = await self.client.start_demo_run(TASK, "mcp-stable-retry-key")
        self.assertEqual(first["run"]["id"], repeated["run"]["id"])
        self.assertEqual(self.service.store.count_runs(), 1)
        for request in self.transport.requests:
            if request["method"] == "POST":
                self.assertEqual(json.loads(request["body"]), {"task_id": TASK, "mode": "demo"})
                self.assertEqual(request["headers"]["idempotency-key"], "mcp-stable-retry-key")
        with self.assertRaises(TypeError):
            await self.client.start_demo_run(TASK, "mcp-forbidden-mode", mode="live")
        self.assertEqual(self.runner.calls, 0)

    async def test_live_runs_stay_private_and_catalog_never_exposes_replacement_content(self):
        live = self.service.create(TASK, "live", "private-test-run")
        self.service.store.transition(live["id"], {"queued"}, "diagnosing", {
            "checkpoint": {"messages": [{"role": "assistant", "content": "PRIVATE_MCP_MARKER"}]}})
        tasks = await self.client.list_tasks()
        self.assertTrue(tasks["tasks"])
        self.assertNotIn("demo_content", json.dumps(tasks))
        self.assertNotIn("source_dir", json.dumps(tasks))
        runs = await self.client.list_runs()
        self.assertEqual(runs["runs"], [])
        with self.assertRaises((ToolError, ValueError)) as error:
            await self.client.get_run(live["id"])
        self.assertNotIn("PRIVATE_MCP_MARKER", str(error.exception))
        self.assertNotIn(self.settings.api_key, str(error.exception))
        self.assertNotIn(self.settings.admin_token, str(error.exception))

    async def test_disabled_public_demo_and_unavailable_runner_remain_denied(self):
        created = await self.client.start_demo_run(TASK, "mcp-before-disable")
        self.settings.public_demo = False
        for name, operation in (("list", self.client.list_runs),
                ("read", lambda: self.client.get_run(created["run"]["id"])),
                ("start", lambda: self.client.start_demo_run(TASK, "mcp-disabled-demo"))):
            with self.subTest(operation=name):
                with self.assertRaises((ToolError, ValueError)):
                    await operation()
        self.assertEqual(self.service.store.count_runs(), 1)
        self.settings.public_demo = True
        self.runner.online = False
        with self.assertRaises((ToolError, ValueError)) as error:
            await self.client.start_demo_run(TASK, "mcp-no-container")
        self.assertIn("service_unavailable", str(error.exception))
        self.assertEqual(self.service.store.count_runs(), 1)

    async def test_private_imported_tasks_cannot_be_started_or_listed(self):
        source = self.root / "private-source"
        source.mkdir()
        (source / "app.py").write_text("def value():\n    return 0\n")
        (source / "test_app.py").write_text("from app import value\ndef test_value():\n    assert value() == 1\n")
        metadata = self.service.catalog.import_project(source, {
            "schema_version": 1, "project_name": "mcp-private-import",
            "title": "Private MCP fixture", "description": "Repair value.",
            "files": ["app.py", "test_app.py"], "editable_files": ["app.py"],
            "check_profile": "pytest", "test_paths": ["test_app.py"],
            "tags": ["self-authored"], "provenance": "Inert authored MCP privacy fixture."})
        tasks = await self.client.list_tasks()
        self.assertNotIn(metadata["id"], [task["id"] for task in tasks["tasks"]])
        with self.assertRaises((ToolError, ValueError)):
            await self.client.start_demo_run(metadata["id"], "mcp-private-import-key")
        self.assertEqual(self.service.store.count_runs(), 0)

    async def test_idempotency_conflicts_and_unknown_ids_are_not_hidden_as_success(self):
        await self.client.start_demo_run(TASK, "mcp-conflicting-key")
        other_task = next(task["id"] for task in (await self.client.list_tasks())["tasks"]
                          if task["id"] != TASK)
        with self.assertRaises((ToolError, ValueError)):
            await self.client.start_demo_run(other_task, "mcp-conflicting-key")
        with self.assertRaises((ToolError, ValueError)):
            await self.client.get_run("f" * 32)
        self.assertEqual(self.service.store.count_runs(), 1)

    async def test_unsafe_path_and_cursor_inputs_never_reach_http_adapter(self):
        before = len(self.transport.requests)
        for run_id in ("../runs", "a/approve", "a?mode=live", "a#fragment", "", "a" * 1000):
            with self.subTest(run_id=run_id):
                with self.assertRaises((ToolError, ValueError)):
                    await self.client.get_run(run_id)
        for after in (-1, True, "0"):
            with self.subTest(after=after):
                with self.assertRaises((ToolError, ValueError)):
                    await self.client.get_run("a" * 32, after=after)
        for request_key in ("short", "a" * 129, "header\r\nInjected: yes", "非ASCII标记", "key with spaces"):
            with self.subTest(request_key=request_key):
                with self.assertRaises((ToolError, ValueError)):
                    await self.client.start_demo_run(TASK, request_key)
        self.assertEqual(len(self.transport.requests), before)

    async def test_client_does_not_forward_inherited_credentials_or_cookies(self):
        with patch.dict(os.environ, {"PATCHTRAIL_ADMIN_TOKEN": "INHERITED_ADMIN_SECRET",
                "DEEPSEEK_API_KEY": "INHERITED_MODEL_SECRET", "HTTP_PROXY": "http://proxy.invalid"}):
            await self.client.list_tasks()
        request = self.transport.requests[-1]
        for name in ("authorization", "cookie", "proxy-authorization"):
            self.assertNotIn(name, request["headers"])
        self.assertNotIn("INHERITED_", str(request))

    async def test_unknown_start_outcome_is_not_automatically_retried(self):
        from patchtrail.mcp_server import PatchTrailClient
        calls = []
        async def timeout(request):
            calls.append(request)
            raise httpx.ReadTimeout("PRIVATE_TRANSPORT_DETAIL", request=request)
        client = PatchTrailClient(BASE_URL, transport=httpx.MockTransport(timeout))
        with self.assertRaises((ToolError, ValueError)) as error:
            await client.start_demo_run(TASK, "mcp-unknown-outcome-key")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].headers["Idempotency-Key"], "mcp-unknown-outcome-key")
        self.assertNotIn("PRIVATE_TRANSPORT_DETAIL", str(error.exception))

    async def test_response_cookies_are_not_reused_as_authority(self):
        from patchtrail.mcp_server import PatchTrailClient
        requests = []
        def response(request):
            requests.append(request)
            return httpx.Response(200, json={"tasks": []},
                headers={"Set-Cookie": "session=UNTRUSTED_COOKIE; Path=/"})
        client = PatchTrailClient(BASE_URL, transport=httpx.MockTransport(response))
        await client.list_tasks()
        await client.list_tasks()
        self.assertEqual(len(requests), 2)
        self.assertTrue(all("cookie" not in request.headers for request in requests))

    async def test_redirect_is_refused_without_contacting_the_target(self):
        from patchtrail.mcp_server import PatchTrailClient
        requests = []
        def response(request):
            requests.append(request)
            return httpx.Response(302, headers={"Location": "https://example.invalid/redirect-target"})
        client = PatchTrailClient(BASE_URL, transport=httpx.MockTransport(response))
        with self.assertRaises(ToolError):
            await client.list_tasks()
        self.assertEqual(len(requests), 1)
        self.assertEqual(str(requests[0].url), BASE_URL + "/api/tasks")

    async def test_oversized_malformed_and_error_responses_fail_closed_without_echo(self):
        from patchtrail.mcp_server import PatchTrailClient, RESPONSE_LIMIT
        for response, expected in (
                (httpx.Response(200, content=b"x" * (RESPONSE_LIMIT + 1)), "response_too_large"),
                (httpx.Response(200, content=b"PRIVATE_INVALID_JSON"), "invalid_response"),
                (httpx.Response(503, json={"error": {"message": "PRIVATE_ERROR_DETAIL"}}), "service_unavailable")):
            with self.subTest(expected=expected):
                client = PatchTrailClient(BASE_URL, transport=httpx.MockTransport(lambda request: response))
                with self.assertRaises(ToolError) as error:
                    await client.list_tasks()
                self.assertIn(expected, str(error.exception))
                self.assertNotIn("PRIVATE_", str(error.exception))

    async def test_event_cursor_can_read_a_run_without_refetching_oversized_history(self):
        from patchtrail.mcp_server import PatchTrailClient, RESPONSE_LIMIT
        run_id = "a" * 32
        paths = []
        row = {"id": run_id, "mode": "demo", "state": "awaiting_approval", "result": None,
               "proposal": {"diff": "bounded public diff"}}
        def response(request):
            paths.append(request.url.path)
            if request.url.path.endswith("/report"):
                return httpx.Response(200, content=b'{"history":"' + b"x" * RESPONSE_LIMIT + b'"}')
            if request.url.path.endswith("/events"):
                self.assertEqual(request.url.params["after"], "5")
                return httpx.Response(200, json={"events": [{"id": 6, "kind": "state_changed"}]})
            return httpx.Response(200, json=row)
        client = PatchTrailClient(BASE_URL, transport=httpx.MockTransport(response))
        with self.assertRaisesRegex(ToolError, "response_too_large"):
            await client.get_run(run_id)
        paths.clear()
        bounded = await client.get_run(run_id, after=5)
        self.assertEqual(paths, [f"/api/runs/{run_id}", f"/api/runs/{run_id}/events"])
        self.assertEqual(bounded["run"], row)
        self.assertEqual(bounded["events"], [{"id": 6, "kind": "state_changed"}])
        self.assertEqual(bounded["review"]["web_url"], BASE_URL + "/#run=" + run_id)

    async def test_official_sdk_discovery_has_only_four_tools_and_no_approval_capability(self):
        from mcp import Client
        from patchtrail.mcp_server import create_server
        async with Client(create_server(self.client), mode="legacy") as caller:
            listing = await caller.list_tools()
            tools = {tool.name: tool for tool in listing.tools}
            self.assertEqual(set(tools), {"list_tasks", "start_demo_run", "get_run", "list_runs"})
            for name, tool in tools.items():
                fields = tool.input_schema.get("properties", {})
                self.assertFalse({"mode", "api_key", "token", "command", "approved", "proposal_digest"} & set(fields))
                self.assertEqual(tool.annotations.read_only_hint, name != "start_demo_run")
            result = await caller.call_tool("list_tasks", {})
            self.assertFalse(result.is_error)
            payload = result.structured_content or json.loads(next(item.text for item in result.content if item.type == "text"))
            self.assertTrue(payload["tasks"])
            before = len(self.transport.requests)
            denied = await caller.call_tool("approve_patch", {"run_id": "a" * 32})
            self.assertTrue(denied.is_error)
            self.assertEqual(len(self.transport.requests), before)

    async def test_official_sdk_arguments_cannot_turn_demo_into_paid_or_self_approved_run(self):
        from mcp import Client
        from patchtrail.mcp_server import create_server
        async with Client(create_server(self.client), mode="legacy") as caller:
            denied = await caller.call_tool("start_demo_run", {"task_id": TASK,
                "request_key": "mcp-protocol-forced-demo", "mode": "live", "approved": True})
            self.assertTrue(denied.is_error)
            self.assertEqual(self.transport.requests, [])
            for request in self.transport.requests:
                if request["method"] == "POST":
                    self.assertEqual(request["path"], "/api/runs")
                    self.assertEqual(json.loads(request["body"]), {"task_id": TASK, "mode": "demo"})
            rows = self.service.store.list_runs()
            self.assertTrue(all(row["mode"] == "demo" for row in rows))
            self.assertTrue(all(row["state"] != "succeeded" for row in rows))
            self.assertEqual(self.runner.calls, 0)


class McpConfigurationTests(unittest.TestCase):
    def test_only_explicit_local_api_origins_are_accepted(self):
        from patchtrail.mcp_server import PatchTrailClient
        for value in ("http://127.0.0.1:8874", "http://localhost:8874", "http://[::1]:8874"):
            with self.subTest(allowed=value):
                PatchTrailClient(value)
        for value in ("http://remote.invalid", "https://remote.invalid", "http://localhost.evil.invalid",
                "http://name:secret@127.0.0.1:8874", "http://127.0.0.1:8874/api",
                "http://127.0.0.1:8874?token=secret", "http://127.0.0.1:8874/#fragment",
                " http://localhost:8874", "http://localhost:8874\n"):
            with self.subTest(rejected=value):
                with self.assertRaises((ToolError, ValueError)):
                    PatchTrailClient(value)


if __name__ == "__main__":
    unittest.main()
