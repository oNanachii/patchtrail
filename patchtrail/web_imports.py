"""Private, bounded browser-source previews. Importing never runs code or a model."""
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tempfile
import threading
import time

from .projects import (MANIFEST_LIMIT, ProjectCatalog, normalize_manifest, preview_project,
                       _publish_directory, _snapshot_file_inventory)
from .workspace import (MAX_FILES, MAX_FILE_BYTES, MAX_TREE_BYTES, WorkspaceError,
                        _canonical, _root, _sha, tree_hash)

IMPORT_BODY_LIMIT = 4 * 1024 * 1024
PREVIEW_ID = re.compile(r"upload-[a-f0-9]{64}\Z")
PREVIEW_TTL = 24 * 60 * 60
MAX_PREVIEWS = 32
MAX_PREVIEW_BYTES = 68 * 1024 * 1024  # 64 MiB source plus bounded manifests/inventories.
METADATA_LIMIT = 512 * 1024
_FIELDS = {"schema_version", "preview_id", "manifest", "files", "source_hash", "created_at", "expires_at"}


class WebImportError(ValueError):
    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code, self.status = code, status


def _utc(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


def _identity(record):
    return {key: record[key] for key in ("schema_version", "manifest", "files", "source_hash")}


class WebImportStore:
    def __init__(self, directory, catalog: ProjectCatalog, *, clock=time.time):
        self.directory = Path(directory)
        self.catalog, self.clock = catalog, clock
        self.thread_lock = threading.RLock()

    @contextmanager
    def _locked(self):
        with self.thread_lock:
            if self.directory.is_symlink():
                raise WebImportError("import_conflict", "Import preview storage is unavailable", 409)
            self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            self.directory.chmod(0o700)
            base = _root(self.directory)
            fd = os.open(base / ".web-import.lock", os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise WebImportError("import_conflict", "Import preview storage is unavailable", 409)
                deadline = time.monotonic() + 5
                while True:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            raise WebImportError("import_capacity", "Another import is publishing; retry the same preview", 429)
                        time.sleep(.01)
                yield base
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)

    @staticmethod
    def _validated_input(manifest, files):
        try:
            manifest = normalize_manifest(manifest)
            if len(_canonical(manifest)) > MANIFEST_LIMIT:
                raise WorkspaceError("Project manifest exceeds its size limit")
            if not isinstance(files, list) or not 2 <= len(files) <= MAX_FILES:
                raise WorkspaceError("Select between two and 200 source/test files")
            payload, total = {}, 0
            for item in files:
                if not isinstance(item, dict) or set(item) != {"path", "content"}:
                    raise WorkspaceError("Each source file requires only path and content")
                path, content = item["path"], item["content"]
                if not isinstance(path, str) or path in payload or not isinstance(content, str):
                    raise WorkspaceError("Source paths must be unique and file content must be text")
                if path not in manifest["files"]:
                    raise WorkspaceError("Uploaded source files must exactly match the reviewed manifest")
                if len(PurePosixPath(path).parts) > 17:
                    raise WorkspaceError("Source directory nesting is too deep")
                data = content.encode("utf-8")
                if len(data) > MAX_FILE_BYTES:
                    raise WorkspaceError("Source files must be at most 128 KiB")
                if b"\0" in data:
                    raise WorkspaceError("Binary source files cannot be imported")
                total += len(data)
                if total > MAX_TREE_BYTES:
                    raise WorkspaceError("Source snapshot exceeds 2 MiB")
                payload[path] = data
            if set(payload) != set(manifest["files"]):
                raise WorkspaceError("Uploaded source files must exactly match the reviewed manifest")
            if len({path.casefold() for path in payload}) != len(payload):
                raise WorkspaceError("Source paths must not collide when case is ignored")
            for path in payload:
                if any(parent.as_posix() in payload for parent in PurePosixPath(path).parents):
                    raise WorkspaceError("A source file cannot also be a parent directory")
            return manifest, payload
        except (WorkspaceError, UnicodeError, TypeError, ValueError):
            # Never reflect submitted contents or local filesystem details in a failure.
            raise WebImportError("import_invalid", "Source files, paths, sizes or test manifest do not match the import contract") from None

    def _metadata(self, path):
        try:
            if path.is_symlink() or not path.is_dir() or not PREVIEW_ID.fullmatch(path.name):
                raise ValueError()
            target = path / "metadata.json"
            if target.is_symlink() or not target.is_file() or target.stat().st_size > METADATA_LIMIT:
                raise ValueError()
            record = json.loads(target.read_text(encoding="utf-8"))
            if not isinstance(record, dict) or set(record) != _FIELDS:
                raise ValueError()
            manifest = normalize_manifest(record["manifest"])
            if manifest != record["manifest"] or len(_canonical(manifest)) > MANIFEST_LIMIT:
                raise ValueError()
            if type(record["schema_version"]) is not int or record["schema_version"] != 1:
                raise ValueError()
            if (type(record["created_at"]) is not int or type(record["expires_at"]) is not int
                    or record["created_at"] < 0 or record["expires_at"] - record["created_at"] != PREVIEW_TTL):
                raise ValueError()
            rows = record["files"]
            if not isinstance(rows, list) or not 2 <= len(rows) <= MAX_FILES:
                raise ValueError()
            for row in rows:
                if (not isinstance(row, dict) or set(row) != {"path", "bytes", "sha256"}
                        or type(row["bytes"]) is not int or not 0 <= row["bytes"] <= MAX_FILE_BYTES
                        or not isinstance(row["sha256"], str) or not re.fullmatch(r"[a-f0-9]{64}", row["sha256"])):
                    raise ValueError()
            if ([row["path"] for row in rows] != manifest["files"]
                    or sum(row["bytes"] for row in rows) > MAX_TREE_BYTES
                    or not isinstance(record["source_hash"], str)
                    or not re.fullmatch(r"[a-f0-9]{64}", record["source_hash"])):
                raise ValueError()
            expected = "upload-" + _sha(_canonical(_identity(record)))
            if record["preview_id"] != expected or path.name != expected:
                raise ValueError()
            return record
        except (OSError, ValueError, TypeError, KeyError, WorkspaceError):
            raise WebImportError("import_conflict", "Import preview metadata no longer matches; preview the files again", 409) from None

    def _load(self, base, preview_id, now):
        if not isinstance(preview_id, str) or not PREVIEW_ID.fullmatch(preview_id):
            raise WebImportError("import_invalid", "Invalid import preview identity")
        path = base / preview_id
        if not path.exists() and not path.is_symlink():
            raise WebImportError("import_not_found", "Import preview was not found; preview the files again", 404)
        record = self._metadata(path)
        if now >= record["expires_at"]:
            shutil.rmtree(path)
            raise WebImportError("import_expired", "Import preview expired; preview the files again", 410)
        try:
            source = _root(path / "source")
            _snapshot_file_inventory(source, record["manifest"])
            actual = preview_project(source, record["manifest"])
            if actual["files"] != record["files"] or tree_hash(source) != record["source_hash"]:
                raise ValueError()
        except (OSError, ValueError, WorkspaceError):
            raise WebImportError("import_conflict", "Import preview source changed; preview the files again", 409) from None
        return record, source

    def _prune(self, base, now, skip=None):
        records = []
        for path in sorted(base.iterdir()):
            if path.name == ".web-import.lock":
                continue
            if path.name.startswith(".preview-") and path.is_dir() and not path.is_symlink():
                # All producers hold this same lock. A leftover staging directory is abandoned.
                shutil.rmtree(path)
                continue
            if path.name == skip:
                continue
            record = self._metadata(path)
            if now >= record["expires_at"]:
                shutil.rmtree(path)
            else:
                records.append(record)
        return records

    def _committed_task(self, record):
        identity = {"manifest": record["manifest"], "source_hash": record["source_hash"]}
        snapshot = _sha(_canonical(identity))
        destination = self.catalog.directory / snapshot
        if not destination.exists() and not destination.is_symlink():
            return None
        return self.catalog._metadata(self.catalog._load(destination))

    def _response(self, record, *, created=False):
        response = {"preview_id": record["preview_id"], "manifest": record["manifest"],
                    "files": record["files"], "file_count": len(record["files"]),
                    "total_bytes": sum(row["bytes"] for row in record["files"]),
                    "created_at": _utc(record["created_at"]), "expires_at": _utc(record["expires_at"]),
                    "created": created, "execution_performed": False, "model_called": False}
        task = self._committed_task(record)
        if task is not None:
            response["committed_task"] = task
        return response

    def preview(self, manifest, files):
        manifest, payload = self._validated_input(manifest, files)
        with self._locked() as base:
            now = int(self.clock())
            existing = self._prune(base, now)
            staging = Path(tempfile.mkdtemp(prefix=".preview-", dir=base))
            try:
                source = staging / "source"
                source.mkdir(mode=0o755)
                for relative, content in payload.items():
                    target = source / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with target.open("xb") as stream:
                        stream.write(content)
                        stream.flush()
                        os.fsync(stream.fileno())
                    target.chmod(0o444)
                checked = preview_project(source, manifest)
                identity = {"schema_version": 1, "manifest": manifest, "files": checked["files"],
                            "source_hash": tree_hash(source)}
                preview_id = "upload-" + _sha(_canonical(identity))
                destination = base / preview_id
                if destination.exists() or destination.is_symlink():
                    record, _ = self._load(base, preview_id, now)
                    if _identity(record) != identity:
                        raise WebImportError("import_conflict", "Import preview identity collision", 409)
                    return self._response(record)
                footprint = sum(row["bytes"] for row in checked["files"]) + len(_canonical(identity))
                used = sum(sum(row["bytes"] for row in record["files"]) + len(_canonical(_identity(record))) for record in existing)
                if len(existing) >= MAX_PREVIEWS or used + footprint > MAX_PREVIEW_BYTES:
                    raise WebImportError("import_capacity", "Import preview storage is full; expired previews are cleared after 24 hours", 429)
                record = {**identity, "preview_id": preview_id, "created_at": now, "expires_at": now + PREVIEW_TTL}
                with (staging / "metadata.json").open("xb") as stream:
                    stream.write(_canonical(record) + b"\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                (staging / "metadata.json").chmod(0o400)
                _publish_directory(staging, destination)
                return self._response(record, created=True)
            except WebImportError:
                raise
            except (OSError, ValueError, TypeError, WorkspaceError):
                raise WebImportError("import_invalid", "Import files could not be staged under the bounded source contract") from None
            finally:
                if staging.exists():
                    shutil.rmtree(staging)

    def get(self, preview_id):
        with self._locked() as base:
            now = int(self.clock())
            record, _ = self._load(base, preview_id, now)
            self._prune(base, now, skip=preview_id)
            return self._response(record)

    def commit(self, preview_id):
        with self._locked() as base:
            now = int(self.clock())
            record, source = self._load(base, preview_id, now)
            self._prune(base, now, skip=preview_id)
            try:
                task = self.catalog.import_project(source, record["manifest"], expected_inventory=record["files"])
            except (OSError, ValueError, WorkspaceError):
                raise WebImportError("import_conflict", "The reviewed import could not be committed; inspect this preview before retrying", 409) from None
            created = task.pop("created")
            return {"task": task, "created": created, "preview_id": preview_id,
                    "execution_performed": False, "model_called": False}
