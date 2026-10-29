"""Bounded operator imports with temporary files and no model or code execution."""
from concurrent.futures import ThreadPoolExecutor
import copy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from patchtrail.api import create_app
from patchtrail.config import Settings
from patchtrail.projects import ProjectCatalog
from patchtrail.service import RunService
from patchtrail.web_imports import IMPORT_BODY_LIMIT, WebImportError, WebImportStore


class NeverExecute:
    def available(self):
        return False

    def run(self, *args):
        raise AssertionError("Web import must never execute code")


class NeverCallModel:
    label = "No network test provider"

    def complete(self, *args):
        raise AssertionError("Web import must never call a model")


def payload(title="Owned browser source"):
    manifest = {"schema_version": 1, "project_name": "owned-browser", "title": title,
                "description": "Fix the bounded test without changing the tests.",
                "files": ["app.py", "tests/__init__.py", "tests/test_app.py"],
                "editable_files": ["app.py"], "check_profile": "unittest", "test_paths": ["tests"]}
    files = [{"path": "app.py", "content": "raise RuntimeError('SOURCE-PRIVATE-SENTINEL must not execute')\n"},
             {"path": "tests/__init__.py", "content": ""},
             {"path": "tests/test_app.py", "content": "import app\n"}]
    return {"manifest": manifest, "files": files}


class WebImportHttpTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory(prefix="patchtrail-web-import-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        settings = Settings(data_dir=self.root, api_key="", admin_token="web-import-operator")
        self.service = RunService(settings, runner=NeverExecute(), provider=NeverCallModel(), dispatch=False)
        self.addCleanup(self.service.close)
        with patch("patchtrail.api.load_local_env"):
            self.app = create_app(self.service)
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)
        self.auth = {"Authorization": "Bearer web-import-operator"}

    def preview(self, body=None):
        return self.client.post("/api/imports/preview", json=body or payload(), headers=self.auth)

    def test_authentication_precedes_body_consumption_and_size_errors(self):
        def unconsumable():
            raise AssertionError("Unauthenticated source body was consumed")
            yield b""
        for authorization in (None, "Bearer wrong-token"):
            headers = {"Content-Type": "application/json", "Content-Length": str(IMPORT_BODY_LIMIT + 1)}
            if authorization:
                headers["Authorization"] = authorization
            response = self.client.post("/api/imports/preview", content=unconsumable(), headers=headers)
            self.assertEqual(response.status_code, 401)
            self.assertEqual(response.headers["cache-control"], "no-store")
            self.assertIn("script-src 'self'", response.headers["content-security-policy"])
        self.assertFalse((self.root / "web-imports").exists())
        for route in ("/api/imports/commit", "/api/imports/upload-" + "a" * 64):
            response = self.client.post(route, json={}) if route.endswith("commit") else self.client.get(route)
            self.assertEqual(response.status_code, 401)

    def test_preview_commit_retry_and_status_are_private_and_do_not_execute(self):
        first = self.preview()
        self.assertEqual(first.status_code, 200, first.text)
        result = first.json()
        self.assertRegex(result["preview_id"], r"^upload-[a-f0-9]{64}$")
        self.assertFalse(result["execution_performed"])
        self.assertFalse(result["model_called"])
        self.assertEqual(result["file_count"], 3)
        self.assertNotIn("SOURCE-PRIVATE-SENTINEL", first.text)
        self.assertNotIn(str(self.root), first.text)
        same = self.preview({"manifest": payload()["manifest"], "files": list(reversed(payload()["files"]))})
        self.assertEqual(same.json()["preview_id"], result["preview_id"])
        self.assertFalse(same.json()["created"])
        body = {"preview_id": result["preview_id"]}
        commit = self.client.post("/api/imports/commit", json=body, headers=self.auth)
        retry = self.client.post("/api/imports/commit", json=body, headers=self.auth)
        self.assertEqual(commit.status_code, 200, commit.text)
        self.assertTrue(commit.json()["created"])
        self.assertFalse(retry.json()["created"])
        self.assertEqual(commit.json()["task"]["id"], retry.json()["task"]["id"])
        self.assertTrue(commit.json()["task"]["private"])
        status = self.client.get("/api/imports/" + result["preview_id"], headers=self.auth).json()
        self.assertEqual(status["committed_task"]["id"], commit.json()["task"]["id"])
        self.assertEqual(self.client.get("/api/imports/" + result["preview_id"]).status_code, 401)
        self.assertNotIn(commit.json()["task"]["id"], self.client.get("/api/tasks").text)
        self.assertEqual(self.service.store.list_runs(), [])

    def test_large_preview_is_bounded_but_other_control_posts_stay_small(self):
        body = payload()
        body["files"][0]["content"] = "#" + "a" * 6000
        self.assertEqual(self.preview(body).status_code, 200)
        headers = {**self.auth, "Content-Type": "application/json", "Content-Length": "1"}
        large = self.client.post("/api/imports/preview", content=b"x" * (IMPORT_BODY_LIMIT + 1), headers=headers)
        self.assertEqual(large.status_code, 413)
        control = self.client.post("/api/imports/commit", content=b"x" * 5000, headers={**self.auth, "Content-Type": "application/json"})
        self.assertEqual(control.status_code, 413)
        self.assertEqual(self.client.post("/api/runs", content=b"x" * 5000).status_code, 413)

    def test_compressed_or_non_json_import_is_rejected_without_staging(self):
        response = self.client.post("/api/imports/preview", content=b"not gzip", headers={**self.auth, "Content-Type": "application/json", "Content-Encoding": "gzip"})
        self.assertEqual(response.status_code, 415)
        response = self.client.post("/api/imports/preview", content=b"<xml/>", headers={**self.auth, "Content-Type": "application/xml"})
        self.assertEqual(response.status_code, 415)
        self.assertFalse((self.root / "web-imports").exists())

    def test_invalid_source_contracts_are_controlled_and_do_not_publish(self):
        cases = []
        wrong = payload(); wrong["files"][0]["path"] = "../escape.py"; cases.append(wrong)
        wrong = payload(); wrong["manifest"]["files"][0] = ".env"; wrong["files"][0]["path"] = ".env"; cases.append(wrong)
        wrong = payload(); wrong["files"].append(copy.deepcopy(wrong["files"][0])); cases.append(wrong)
        wrong = payload(); wrong["files"][0]["content"] = "a" * (128 * 1024 + 1); cases.append(wrong)
        wrong = payload(); wrong["files"][0]["content"] = "\ud800"; cases.append(wrong)
        wrong = payload(); wrong["files"][0]["content"] = "nul\0source"; cases.append(wrong)
        wrong = payload(); wrong["manifest"]["editable_files"] = ["tests/test_app.py"]; cases.append(wrong)
        wrong = payload(); wrong["manifest"]["command"] = "echo private"; cases.append(wrong)
        wrong = payload(); wrong["files"].pop(); cases.append(wrong)
        wrong = payload(); wrong["manifest"]["files"].append("app.py/nested.py"); wrong["files"].append({"path": "app.py/nested.py", "content": "pass"}); cases.append(wrong)
        wrong = payload(); deep = "/".join(["nested"] * 18) + "/app.py"; wrong["manifest"]["files"][0] = deep; wrong["manifest"]["editable_files"] = [deep]; wrong["files"][0]["path"] = deep; cases.append(wrong)
        for body in cases:
            with self.subTest(paths=body["manifest"]["files"]):
                # surrogate JSON must be escaped rather than encoded by the client transport.
                response = self.client.post("/api/imports/preview", content=json.dumps(body), headers={**self.auth, "Content-Type": "application/json"})
                self.assertIn(response.status_code, (400, 413, 422), response.text)
                self.assertNotIn("SOURCE-PRIVATE-SENTINEL", response.text)
                self.assertNotIn(str(self.root), response.text)
        directory = self.root / "web-imports"
        if directory.exists():
            self.assertEqual([p for p in directory.iterdir() if not p.name.startswith(".")], [])

    def test_source_drift_is_refused_before_commit(self):
        preview = self.preview().json()
        path = self.root / "web-imports" / preview["preview_id"] / "source/app.py"
        path.chmod(0o600)
        path.write_text("changed after preview\n")
        response = self.client.post("/api/imports/commit", json={"preview_id": preview["preview_id"]}, headers=self.auth)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "import_conflict")
        self.assertFalse(self.service.catalog.directory.exists())

    def test_get_unknown_or_invalid_identity_does_not_expose_paths(self):
        response = self.client.get("/api/imports/upload-" + "a" * 64, headers=self.auth)
        self.assertEqual(response.status_code, 404)
        response = self.client.get("/api/imports/not-valid", headers=self.auth)
        self.assertEqual(response.status_code, 400)
        self.assertNotIn(str(self.root), response.text)


class PreviewLifecycleTests(unittest.TestCase):
    def test_expiry_cleanup_and_same_payload_repreview(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            now = [1000]
            store = WebImportStore(root / "previews", ProjectCatalog(root / "projects"), clock=lambda: now[0])
            first = store.preview(**payload())
            now[0] += 24 * 60 * 60
            with self.assertRaises(WebImportError) as error:
                store.commit(first["preview_id"])
            self.assertEqual(error.exception.status, 410)
            self.assertFalse((root / "previews" / first["preview_id"]).exists())
            again = store.preview(**payload())
            self.assertEqual(first["preview_id"], again["preview_id"])
            self.assertNotEqual(first["expires_at"], again["expires_at"])

    def test_quota_is_serialized_across_store_instances_and_retries_still_work(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            stores = [WebImportStore(root / "previews", ProjectCatalog(root / "projects")) for _ in range(2)]
            def publish(index):
                try:
                    return stores[index].preview(**payload("Distinct title " + str(index)))
                except WebImportError as exc:
                    return exc
            with patch("patchtrail.web_imports.MAX_PREVIEWS", 1):
                with ThreadPoolExecutor(max_workers=2) as pool:
                    outcomes = list(pool.map(publish, range(2)))
                successes = [value for value in outcomes if isinstance(value, dict)]
                errors = [value for value in outcomes if isinstance(value, WebImportError)]
                self.assertEqual(len(successes), 1)
                self.assertEqual(len(errors), 1)
                self.assertEqual(errors[0].status, 429)
                winner = successes[0]
                same = stores[0].preview(winner["manifest"], payload()["files"])
                self.assertEqual(same["preview_id"], winner["preview_id"])
                self.assertFalse(same["created"])

    def test_disk_footprint_and_abandoned_staging_are_bounded(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = WebImportStore(root / "previews", ProjectCatalog(root / "projects"))
            with patch("patchtrail.web_imports.MAX_PREVIEW_BYTES", 1):
                with self.assertRaises(WebImportError) as error:
                    store.preview(**payload())
            self.assertEqual(error.exception.status, 429)
            self.assertEqual(list((root / "previews").glob(".preview-*")), [])
            abandoned = root / "previews/.preview-crashed"
            abandoned.mkdir(); (abandoned / "partial").write_text("unpublished")
            store.preview(**payload())
            self.assertFalse(abandoned.exists())


if __name__ == "__main__":
    unittest.main()
