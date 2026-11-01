"""Real SDK stdio subprocess + loopback API; task runner is an explicit byte fake."""
import asyncio
import json
from pathlib import Path
import socket
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from mcp import Client, StdioServerParameters
import uvicorn

from patchtrail.api import create_app
from patchtrail.config import Settings
from patchtrail.sandbox import CheckResult
from patchtrail.service import RunService
from patchtrail.tasks import CHECK_COMMAND


ROOT = Path(__file__).resolve().parents[1]


class FailingInspectionRunner:
    def __init__(self):
        self.checks = 0

    def available(self):
        return True

    def run(self, workspace, command, run_id):
        if command != CHECK_COMMAND:
            raise AssertionError("Only the fixed catalog check may reach the runner")
        self.checks += 1
        return CheckResult(1, "Ran 4 tests in 0.001s\n\nFAILED (failures=1)\n",
                           1, False, False, "explicit-mcp-stdio-fake-no-code-execution")


class NoModel:
    def complete(self, *args):
        raise AssertionError("MCP must never dispatch a paid provider")


def tool_data(result):
    if result.is_error:
        raise AssertionError(result.content)
    data = result.structured_content
    if data is None:
        data = json.loads(result.content[0].text)
    return data.get("result", data)


class MCPStdioTests(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_stdio_from_other_cwd_waits_for_human(self):
        await self.exercise("legacy")

    async def test_auto_negotiated_stdio_from_other_cwd_waits_for_human(self):
        await self.exercise("auto")

    async def exercise(self, mode):
        with TemporaryDirectory(prefix="patchtrail-mcp-stdio-") as temporary:
            runner = FailingInspectionRunner()
            service = RunService(Settings(data_dir=Path(temporary) / "runtime", api_key="",
                admin_token="", public_demo=True), runner=runner, provider=NoModel(), dispatch=False)
            with patch("patchtrail.api.load_local_env"):
                app = create_app(service)
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                listener.bind(("127.0.0.1", 0))
                port = listener.getsockname()[1]
                listener.listen(16)
                server = uvicorn.Server(uvicorn.Config(app, lifespan="off", log_level="error",
                                                       access_log=False))
                serving = asyncio.create_task(server.serve(sockets=[listener]))
                try:
                    for _ in range(100):
                        if server.started:
                            break
                        if serving.done():
                            await serving
                            self.fail("Local test API stopped before startup")
                        await asyncio.sleep(0.02)
                    self.assertTrue(server.started)
                    parameters = StdioServerParameters(command=sys.executable,
                        args=[str(ROOT / "scripts/serve_mcp.py"), "--base-url", f"http://127.0.0.1:{port}"],
                        cwd=temporary, env={"PYTHONDONTWRITEBYTECODE": "1"})
                    async with Client(parameters, mode=mode, read_timeout_seconds=10) as client:
                        tools = await client.list_tools()
                        self.assertEqual({tool.name for tool in tools.tools},
                                         {"list_tasks", "start_demo_run", "get_run", "list_runs"})
                        tasks = tool_data(await client.call_tool("list_tasks"))["tasks"]
                        self.assertIn("quantity-shortfall", {task["id"] for task in tasks})
                        started = tool_data(await client.call_tool("start_demo_run", {
                            "task_id": "quantity-shortfall", "request_key": "stdio-demo-request-01"}))
                        run_id = started["run"]["id"]
                        await asyncio.to_thread(service.diagnose, run_id)
                        report = tool_data(await client.call_tool("get_run", {"run_id": run_id}))
                        self.assertEqual(report["run"]["state"], "awaiting_approval")
                        self.assertTrue(report["run"]["proposal"]["diff"])
                        self.assertIsNone(report["run"]["result"])
                        self.assertTrue(report["events"])
                        self.assertEqual(report["review"]["web_url"], f"http://127.0.0.1:{port}/#run={run_id}")
                        self.assertFalse(report["review"]["approval_available_via_mcp"])
                        rows = tool_data(await client.call_tool("list_runs"))["runs"]
                        self.assertEqual([row["id"] for row in rows], [run_id])
                        self.assertEqual(runner.checks, 1)  # Baseline only, no approval or verification.
                    self.assertEqual(service.store.get_run(run_id)["state"], "awaiting_approval")
                finally:
                    server.should_exit = True
                    await asyncio.wait_for(serving, 5)
            finally:
                listener.close()
                app.state.members.close()
                service.close()


if __name__ == "__main__":
    unittest.main()
