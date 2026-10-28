from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from patchtrail.api import create_app
from patchtrail.config import Settings
from patchtrail.service import RunService
from test_api import FakeRunner, NoNetworkProvider


class StorageBusyApiTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(prefix="patchtrail-busy-api-")
        self.addCleanup(temporary.cleanup)
        settings = Settings(data_dir=Path(temporary.name), api_key="", admin_token="unit-operator")
        self.service = RunService(settings, runner=FakeRunner(), provider=NoNetworkProvider(), dispatch=False)
        self.addCleanup(self.service.close)
        with patch("patchtrail.api.load_local_env"):
            self.client = TestClient(create_app(self.service))
        self.addCleanup(self.client.close)
        self.row = self.service.create("quantity-shortfall", "demo", "unit-busy-request")

    def test_actual_exclusive_sqlite_lock_returns_retryable503_then_same_run(self):
        connect = self.service.store._connect
        def short_wait_connection():
            connection = connect()
            connection.execute("PRAGMA busy_timeout=5")
            return connection
        blocker = sqlite3.connect(self.service.store.database, isolation_level=None)
        try:
            blocker.execute("BEGIN EXCLUSIVE")
            with patch.object(self.service.store, "_connect", short_wait_connection):
                response = self.client.get("/api/runs/" + self.row["id"])
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.json()["error"]["code"], "storage_busy")
            self.assertEqual(response.headers["Retry-After"], "1")
        finally:
            blocker.rollback()
            blocker.close()
        recovered = self.client.get("/api/runs/" + self.row["id"])
        self.assertEqual(recovered.status_code, 200)
        self.assertEqual(recovered.json()["id"], self.row["id"])
        self.assertEqual(recovered.json()["state"], "queued")

    def test_other_storage_errors_do_not_leak_internal_exception_text(self):
        with patch.object(self.service.store, "get_run", side_effect=sqlite3.OperationalError("private-schema-path")):
            response = self.client.get("/api/runs/" + self.row["id"])
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()["error"]["code"], "storage_error")
        self.assertNotIn("private-schema-path", response.text)


if __name__ == "__main__":
    unittest.main()
