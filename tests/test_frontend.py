from pathlib import Path
import shutil
import subprocess
import unittest


class FrontendStateTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node is required for the frontend state contract")
    def test_lost_create_response_and_reload_keep_same_idempotency_key(self):
        script = Path(__file__).with_name("frontend_retry.cjs")
        result = subprocess.run([shutil.which("node"), str(script)], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('"status":"passed"', result.stdout)


if __name__ == "__main__":
    unittest.main()
