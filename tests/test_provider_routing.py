"""Operator providers and DeepSeek-only BYOK use real routes and mock transports."""
from dataclasses import replace
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
import httpx

from patchtrail.api import create_app
from patchtrail.config import Settings
from patchtrail.members import MemberRegistry, digest
from patchtrail.providers import (DeepSeekProvider, OpenAICompatibleProvider, ScriptedProvider,
                                 create_provider)
from patchtrail.sandbox import CheckResult
from patchtrail.service import RunService
from patchtrail.tasks import CHECK_COMMAND, get_task


TASK = "quantity-shortfall"


class RoutingInspectionRunner:
    """Compare curated bytes only; no fixture imports, subprocesses or host execution."""
    def __init__(self):
        self.calls = 0

    def available(self):
        return True

    def run(self, workspace, command, run_id):
        if command != CHECK_COMMAND:
            raise AssertionError("Only the fixed catalog check may reach the runner")
        self.calls += 1
        task = get_task(TASK)
        fixed = (workspace / task.demo_path).read_text() == task.demo_content
        output = "Ran 4 tests in 0.001s\n\n" + ("OK\n" if fixed else "FAILED (failures=1)\n")
        return CheckResult(0 if fixed else 1, output, 1, False, False,
                           "explicit-provider-routing-byte-inspection-no-execution")


class ProviderRoutingTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(prefix="patchtrail-provider-routing-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.counter = 0

    def settings(self, **changes):
        self.counter += 1
        return replace(Settings(data_dir=self.root / f"data-{self.counter}",
            api_key="synthetic-deepseek-operator-key", compatible_api_key="synthetic-compatible-operator-key",
            admin_token="synthetic-routing-operator-token", provider_kind="deepseek",
            model="deepseek-flash", base_url="https://api.deepseek.com", model_options={},
            preserve_reasoning_content=False, deepseek_member_model="deepseek-flash"), **changes)

    def app(self, settings, transport=None):
        runner = RoutingInspectionRunner()
        with patch("patchtrail.service.create_provider", side_effect=lambda configured: create_provider(configured, transport)):
            service = RunService(settings, runner=runner, dispatch=False)
        self.addCleanup(service.close)
        with patch("patchtrail.api.load_local_env"):
            app = create_app(service)
        self.addCleanup(app.state.members.close)
        self.addCleanup(lambda: [member.close() for member in app.state.member_services.values()])
        client = TestClient(app, base_url="https://testserver")
        self.addCleanup(client.close)
        return service, app, client, runner

    def invite(self, app, client, suffix="member"):
        self.counter += 1
        path = self.root / f"invitation-{self.counter}.json"
        member_id = app.state.members.create(path)
        invitation = json.loads(path.read_text())
        key = "synthetic-deepseek-member-key-" + suffix
        response = client.post("/api/session", json={"invite_code": invitation["invite_code"], "api_key": key})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn(key, response.text)
        session = response.json()
        return member_id, key, session, {"Authorization": "Bearer " + session["session_token"]}

    def test_default_deepseek_and_generic_operator_both_keep_public_demo_scripted(self):
        for provider_kind, model, base_url in (
            ("deepseek", "deepseek-flash", "https://api.deepseek.com"),
            ("openai-compatible", "glm-unit-model", "https://vendor.example.invalid/v4"),
        ):
            with self.subTest(provider_kind=provider_kind):
                settings = self.settings(provider_kind=provider_kind, model=model, base_url=base_url,
                                         api_key="", compatible_api_key="")
                service, _, client, runner = self.app(settings)
                self.assertIsInstance(service.provider,
                    DeepSeekProvider if provider_kind == "deepseek" else OpenAICompatibleProvider)
                with patch.object(service.provider, "complete", side_effect=AssertionError("Paid provider forbidden")):
                    response = client.post("/api/runs", headers={"Idempotency-Key": "routing-public-demo"},
                                           json={"task_id": TASK, "mode": "demo"})
                    self.assertEqual(response.status_code, 201, response.text)
                    run_id = response.json()["id"]
                    service.diagnose(run_id)
                report = client.get(f"/api/runs/{run_id}/report")
                self.assertEqual(report.status_code, 200)
                self.assertEqual(report.json()["run"]["state"], "awaiting_approval")
                self.assertEqual(report.json()["run"]["configured_model"], "scripted")
                self.assertEqual(report.json()["run"]["provider_label"], ScriptedProvider.label)
                self.assertEqual(runner.calls, 1)

    def test_generic_operator_uses_only_compatible_key_and_records_no_credentials(self):
        settings = self.settings(provider_kind="openai-compatible", model="glm-unit-model",
            base_url="https://vendor.example.invalid/v4", model_options={"thinking": {"type": "disabled"}})
        requests = []
        def handler(request):
            requests.append(request)
            body = json.loads(request.content)
            self.assertEqual(str(request.url), "https://vendor.example.invalid/v4/chat/completions")
            self.assertEqual(request.headers["Authorization"], "Bearer " + settings.compatible_api_key)
            self.assertEqual(body["model"], "glm-unit-model")
            self.assertEqual(body["thinking"], {"type": "disabled"})
            self.assertNotIn(settings.api_key, request.content.decode())
            message, _ = ScriptedProvider().complete(body["messages"], get_task(TASK), len(requests), 1)
            message["content"] = settings.compatible_api_key
            return httpx.Response(200, json={"id": "routing-response-" + str(len(requests)),
                "model": "glm-unit-reported", "choices": [{"finish_reason": "tool_calls", "message": message}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}})
        service, _, client, runner = self.app(settings, httpx.MockTransport(handler))
        headers = {"Authorization": "Bearer " + settings.admin_token, "Idempotency-Key": "routing-compatible-live"}
        response = client.post("/api/runs", headers=headers, json={"task_id": TASK, "mode": "live"})
        self.assertEqual(response.status_code, 201, response.text)
        run_id = response.json()["id"]
        service.diagnose(run_id)
        report = client.get(f"/api/runs/{run_id}/report", headers=headers)
        self.assertEqual(report.status_code, 200, report.text)
        self.assertEqual(report.json()["run"]["state"], "awaiting_approval")
        self.assertEqual(report.json()["run"]["configured_model"], "glm-unit-model")
        self.assertEqual(report.json()["run"]["provider_label"], OpenAICompatibleProvider.label)
        self.assertEqual(report.json()["run"]["observed_models"], ["glm-unit-reported"])
        self.assertEqual(len(requests), 3)
        self.assertEqual(runner.calls, 1)
        self.assertEqual(client.get(f"/api/runs/{run_id}/report").status_code, 401)
        for route in ("/api/health", "/api/tasks", "/api/runs", f"/api/runs/{run_id}/report",
                      f"/api/runs/{run_id}/events"):
            response = client.get(route, headers=headers)
            for key in (settings.api_key, settings.compatible_api_key, settings.admin_token):
                self.assertNotIn(key, response.text)
        for path in settings.data_dir.rglob("*"):
            if path.is_file():
                for key in (settings.api_key, settings.compatible_api_key, settings.admin_token):
                    self.assertNotIn(key.encode(), path.read_bytes(), str(path))

    def test_generic_key_missing_never_falls_back_to_deepseek_or_starts_live_run(self):
        settings = self.settings(provider_kind="openai-compatible", model="glm-unit-model",
                                base_url="https://vendor.example.invalid/v4", compatible_api_key="")
        requests = []
        service, _, client, runner = self.app(settings, httpx.MockTransport(lambda request: requests.append(request)))
        response = client.post("/api/runs", headers={"Authorization": "Bearer " + settings.admin_token,
            "Idempotency-Key": "routing-key-missing"}, json={"task_id": TASK, "mode": "live"})
        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(response.json()["error"]["code"], "model_not_configured")
        self.assertEqual(service.store.count_runs(), 0)
        self.assertEqual(requests, [])
        self.assertEqual(runner.calls, 0)
        self.assertFalse(client.get("/api/health").json()["model_configured"])

    def test_health_uses_active_compatible_key_even_without_deepseek_operator_key(self):
        settings = self.settings(provider_kind="openai-compatible", api_key="", model="glm-unit-model",
                                base_url="https://vendor.example.invalid/v4")
        _, _, client, _ = self.app(settings)
        self.assertTrue(client.get("/api/health").json()["model_configured"])

    def test_generic_operator_member_login_is_deepseek_only_and_clears_vendor_profile(self):
        settings = self.settings(provider_kind="openai-compatible", model="glm-unit-model",
            base_url="https://vendor.example.invalid/v4", model_options={"thinking": {"type": "enabled"}, "temperature": 0.2},
            preserve_reasoning_content=True)
        _, app, client, _ = self.app(settings)
        member_id, key, session, headers = self.invite(app, client)
        self.assertEqual(session["model"], "deepseek-flash")
        current = app.state.member_services[member_id]
        self.assertIsInstance(current.provider, DeepSeekProvider)
        self.assertEqual(current.settings.provider_kind, "deepseek")
        self.assertEqual(current.settings.base_url, "https://api.deepseek.com")
        self.assertEqual(current.settings.model, "deepseek-flash")
        self.assertEqual(current.settings.api_key, "")
        self.assertEqual(current.settings.compatible_api_key, "")
        self.assertEqual(current.settings.model_options, {})
        self.assertFalse(current.settings.preserve_reasoning_content)
        status = client.get("/api/session", headers=headers)
        self.assertEqual(status.status_code, 200)
        self.assertEqual(status.json()["model"], "deepseek-flash")
        rejected = client.post("/api/session", json={"invite_code": "synthetic-invalid-invitation",
            "api_key": key, "model": "glm-unit-model", "base_url": settings.base_url})
        self.assertEqual(rejected.status_code, 422)
        for route in ("/api/session", "/api/health", "/api/tasks", "/api/runs"):
            response = client.get(route, headers=headers)
            for secret in (key, settings.api_key, settings.compatible_api_key, settings.admin_token):
                self.assertNotIn(secret, response.text)

    def test_direct_member_registry_provider_pins_endpoint_key_and_deepseek_model(self):
        for provider_kind, operator_model, separate_member_model, expected_model in (
            ("openai-compatible", "glm-unit-model", "deepseek-flash", "deepseek-flash"),
            ("openai-compatible", "glm-unit-model", "deepseek-member-custom", "deepseek-member-custom"),
            ("deepseek", "deepseek-legacy-custom", "ignored-separate-model", "deepseek-legacy-custom"),
        ):
            with self.subTest(provider_kind=provider_kind, expected_model=expected_model):
                settings = self.settings(provider_kind=provider_kind, model=operator_model,
                    base_url="https://vendor.example.invalid/v4", deepseek_member_model=separate_member_model,
                    model_options={"temperature": 0.2}, preserve_reasoning_content=True)
                registry = MemberRegistry(settings.data_dir)
                self.addCleanup(registry.close)
                delivery = self.root / f"registry-invite-{self.counter}.json"
                registry.create(delivery)
                key = "synthetic-direct-member-only-key"
                session = registry.login(json.loads(delivery.read_text())["invite_code"], key, expected_model)
                requests = []
                def handler(request):
                    requests.append(request)
                    self.assertEqual(str(request.url), "https://api.deepseek.com/chat/completions")
                    self.assertEqual(request.headers["Authorization"], "Bearer " + key)
                    body = json.loads(request.content)
                    self.assertEqual(body["model"], expected_model)
                    self.assertEqual(body["thinking"], {"type": "disabled"})
                    self.assertNotIn("temperature", body)
                    self.assertNotIn(settings.api_key, request.content.decode())
                    self.assertNotIn(settings.compatible_api_key, request.content.decode())
                    return httpx.Response(200, json={"model": expected_model, "id": key, "choices": [{
                        "finish_reason": "stop", "message": {"role": "assistant", "content": key,
                            "reasoning_content": "must not be retained"}}]})
                provider = registry.provider(digest(session["session_token"]), settings, httpx.MockTransport(handler))
                message, _ = provider.complete([], get_task(TASK), 1, 1)
                self.assertEqual(len(requests), 1)
                self.assertEqual(provider.settings.compatible_api_key, "")
                self.assertEqual(provider.settings.model_options, {})
                self.assertEqual(provider.settings.provider_kind, "deepseek")
                self.assertFalse(provider.preserve_reasoning_content)
                self.assertEqual(message["content"], "[REDACTED]")
                self.assertNotIn("reasoning_content", message)
                self.assertNotIn(key, json.dumps(provider.last_completion_metadata))

    def test_legacy_deepseek_operator_member_custom_model_is_preserved(self):
        settings = self.settings(model="deepseek-legacy-custom", deepseek_member_model="ignored-separate-model")
        _, app, client, _ = self.app(settings)
        member_id, _, session, headers = self.invite(app, client, "legacy")
        self.assertEqual(session["model"], "deepseek-legacy-custom")
        self.assertEqual(app.state.member_services[member_id].settings.model, "deepseek-legacy-custom")
        self.assertEqual(client.get("/api/session", headers=headers).json()["model"], "deepseek-legacy-custom")

    def test_model_options_configuration_errors_do_not_echo_environment_contents(self):
        for raw in ('{"sensitive":"do-not-echo"', '["do-not-echo"]'):
            with self.subTest(raw_type=raw[:1]), patch.dict(os.environ, {"PATCHTRAIL_MODEL_OPTIONS": raw}):
                with self.assertRaises(ValueError) as error:
                    Settings()
                self.assertNotIn("do-not-echo", str(error.exception))
        with patch.dict(os.environ, {"PATCHTRAIL_MODEL_OPTIONS": '{"temperature":0.2}'}):
            self.assertEqual(Settings().model_options, {"temperature": 0.2})


if __name__ == "__main__":
    unittest.main()
