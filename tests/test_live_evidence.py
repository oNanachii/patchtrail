"""Offline live-path evidence regressions; no credentials or model service access."""
import copy
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import httpx

from patchtrail.config import Settings
from patchtrail.providers import DeepSeekProvider
from patchtrail.sandbox import CheckResult
from patchtrail.service import RunService, public_run
from patchtrail.tasks import CHECK_COMMAND, get_task


TASK_ID = "quantity-shortfall"
PROVIDER_CALL_ID = "provider-reused-id"


def tool_message(name, arguments, identity=PROVIDER_CALL_ID):
    return {"role": "assistant", "content": None, "tool_calls": [{
        "id": identity, "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }]}


def actions(task):
    return [
        ("read_file", {"path": task.demo_path}),
        ("run_check", {"check_id": "tests"}),
        ("propose_patch", {"path": task.demo_path, "content": task.demo_content,
                           "explanation": "Authored test replacement; not a real model result."}),
    ]


class EvidenceOnlyRunner:
    """Returns a known failing check without importing or executing workspace code."""
    def __init__(self):
        self.calls = []

    def available(self):
        return True

    def run(self, workspace, command, run_id):
        if command != CHECK_COMMAND:
            raise AssertionError("Unexpected command reached the explicit fake runner")
        self.calls.append(run_id)
        return CheckResult(1, "Ran 4 tests in 0.001s\n\nFAILED (failures=1)\n",
                           1, False, False, runner="explicit-evidence-only-fake")


class RepeatedIDProvider:
    label = "Explicit repeated-provider-ID fake"

    def __init__(self):
        self.histories = []

    def complete(self, messages, task, call_number, remaining_seconds):
        self.histories.append(copy.deepcopy(messages))
        name, arguments = actions(task)[call_number - 1]
        return tool_message(name, arguments), {"total_tokens": 10}


class LiveEvidenceTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(prefix="patchtrail-live-evidence-")
        self.addCleanup(temporary.cleanup)
        self.settings = Settings(data_dir=Path(temporary.name),
                                 api_key="explicit-evidence-unit-key-no-network",
                                 admin_token="explicit-evidence-unit-operator",
                                 model="unit-configured-model")
        self.runner = EvidenceOnlyRunner()

    def run_diagnosis(self, provider, key):
        service = RunService(self.settings, runner=self.runner, provider=provider, dispatch=False)
        self.addCleanup(service.close)
        row = service.create(TASK_ID, "live", key)
        service.diagnose(row["id"])
        return service, service.store.get_run(row["id"])

    def test_repeated_provider_ids_across_responses_keep_api_pairing_and_do_not_hang(self):
        provider = RepeatedIDProvider()
        service, row = self.run_diagnosis(provider, "repeated-live-evidence")
        self.assertEqual(row["state"], "awaiting_approval")
        self.assertEqual(row["checkpoint"]["calls"], 3)
        self.assertEqual(len(self.runner.calls), 1)
        self.assertFalse(row["checkpoint"].get("pending_model_request"))
        events = service.store.events(row["id"])
        for kind in ("tool_requested", "tool_result"):
            selected = [event for event in events if event["kind"] == kind]
            self.assertEqual([event["call_id"] for event in selected],
                             [f"{number}:{PROVIDER_CALL_ID}" for number in (1, 2, 3)])
            self.assertEqual([event["payload"]["provider_call_id"] for event in selected],
                             [PROVIDER_CALL_ID] * 3)
        # Internal event identity must never replace the provider's API protocol IDs.
        messages = row["checkpoint"]["messages"]
        assistant_calls = 0
        for index, message in enumerate(messages):
            if message["role"] == "assistant":
                assistant_calls += 1
                self.assertEqual(message["tool_calls"][0]["id"], PROVIDER_CALL_ID)
                self.assertEqual(messages[index + 1]["tool_call_id"], PROVIDER_CALL_ID)
        self.assertEqual(assistant_calls, 3)

    def test_text_only_failure_retains_bounded_response_and_full_content_hash(self):
        answer = "Specific final answer explaining why no patch was proposed. " + "x" * 2200

        class TextProvider:
            label = "Explicit text-only fake"

            def complete(self, *args):
                return {"role": "assistant", "content": answer}, {"total_tokens": 11}

        service, row = self.run_diagnosis(TextProvider(), "text-only-live-evidence")
        self.assertEqual((row["state"], row["stop_reason"]), ("failed", "no_patch_proposal"))
        self.assertEqual(len(self.runner.calls), 1)  # Fixed baseline runs before the model.
        responses = [event["payload"] for event in service.store.events(row["id"])
                     if event["kind"] == "model_response"]
        self.assertEqual(len(responses), 1)
        response = responses[0]
        self.assertEqual(response["content"], answer[:2000])
        self.assertTrue(response["content_truncated"])
        self.assertEqual(response["content_sha256"], hashlib.sha256(answer.encode()).hexdigest())
        self.assertEqual(response["tool_names"], [])
        self.assertEqual(row["checkpoint"]["messages"][-1]["content"], answer)
        self.assertEqual(row["checkpoint"]["usage"]["total_tokens"], 11)
        self.assertFalse(row["checkpoint"].get("pending_model_request"))

    def test_real_parser_records_only_response_metadata_not_sensitive_configuration(self):
        task = get_task(TASK_ID)
        requests = []

        def endpoint(request):
            # MockTransport handles every request locally; nothing reaches DeepSeek.
            requests.append(json.loads(request.content))
            index = len(requests) - 1
            name, arguments = actions(task)[index]
            return httpx.Response(200, json={
                "model": "unit-observed-model-version",
                "id": f"unit-response-{index + 1}",
                "choices": [{"finish_reason": "tool_calls", "message":
                             tool_message(name, arguments, f"metadata-call-{index + 1}")}],
                "usage": {"total_tokens": 7},
                # Unknown response fields must not be copied into recorded metadata.
                "api_key": self.settings.api_key,
                "operator_token": self.settings.admin_token,
            })

        provider = DeepSeekProvider(self.settings, transport=httpx.MockTransport(endpoint))
        service, row = self.run_diagnosis(provider, "metadata-live-evidence")
        self.assertEqual(row["state"], "awaiting_approval")
        self.assertEqual(row["checkpoint"]["usage"]["total_tokens"], 21)
        events = service.store.events(row["id"])
        responses = [event["payload"] for event in events if event["kind"] == "model_response"]
        self.assertEqual([response["response_id"] for response in responses],
                         ["unit-response-1", "unit-response-2", "unit-response-3"])
        self.assertTrue(all(response["reported_model"] == "unit-observed-model-version"
                            and response["finish_reason"] == "tool_calls" for response in responses))
        self.assertEqual(provider.last_completion_metadata, {
            "reported_model": "unit-observed-model-version", "response_id": "unit-response-3",
            "finish_reason": "tool_calls"})
        public = public_run(row)
        self.assertEqual(public["configured_model"], "unit-configured-model")
        self.assertEqual(public["observed_models"], ["unit-observed-model-version"])
        self.assertNotIn("messages", public["checkpoint"])
        self.assertNotIn("provider_metadata", public["checkpoint"])
        evidence = json.dumps({"row": row, "events": events, "public": public})
        self.assertNotIn(self.settings.api_key, evidence)
        self.assertNotIn(self.settings.admin_token, evidence)
        self.assertTrue(all(request["model"] == "unit-configured-model" for request in requests))


if __name__ == "__main__":
    unittest.main()
