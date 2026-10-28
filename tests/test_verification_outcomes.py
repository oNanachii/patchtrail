from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from patchtrail.config import Settings
from patchtrail.sandbox import CheckResult
from patchtrail.service import RunService
from test_service import FakeRunner


class VerificationOutcomeTests(unittest.TestCase):
    def test_infrastructure_and_incomplete_checks_are_not_patch_failures(self):
        cases = [
            (CheckResult(125, "image is not known\n", 1, False, False, "podman"), "verification_unconfirmed"),
            (CheckResult(0, "Ran 0 tests in 0.001s\n\nOK\n", 1, False, False, "podman"), "verification_unconfirmed"),
            (CheckResult(None, "started\n", 20000, True, False, "podman"), "verification_unconfirmed"),
            (CheckResult(1, "Ran 4 tests in 0.001s\n\nFAILED (failures=1)\n", 1, False, False, "podman"), "verifier_failed"),
        ]
        with TemporaryDirectory(prefix="patchtrail-verdict-") as directory:
            runner = FakeRunner()
            service = RunService(Settings(data_dir=Path(directory), api_key="", admin_token=""), runner=runner, dispatch=False)
            try:
                for index, (result, reason) in enumerate(cases):
                    with self.subTest(reason=reason, exit_code=result.exit_code):
                        runner.force_result = None
                        row = service.create("quantity-shortfall", "demo", f"verdict-check-{index}")
                        service.diagnose(row["id"])
                        row = service.store.get_run(row["id"])
                        service.approve(row["id"], row["proposal"]["digest"], row["version"])
                        runner.force_result = result
                        service.verify(row["id"])
                        row = service.store.get_run(row["id"])
                        self.assertEqual(row["state"], "failed")
                        self.assertEqual(row["stop_reason"], reason)
                        self.assertEqual(row["result"]["runner"], "podman")
            finally:
                service.close()


if __name__ == "__main__":
    unittest.main()
