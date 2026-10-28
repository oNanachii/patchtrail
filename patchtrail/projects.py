"""Explicit operator-only source imports; no remote repositories or execution.

Snapshots contain only a reviewed manifest's allowlisted UTF-8 files. Catalog
lookups verify both the metadata identity and every source byte before use.
Filename exclusions are not a secret-content scanner: operators must inspect
the allowlisted source before committing it or sending it to a model.
"""
from collections import defaultdict
import ctypes
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import sys
import tempfile

from .tasks import TaskSpec
from .workspace import (MAX_FILE_BYTES, MAX_FILES, MAX_TREE_BYTES, WorkspaceError,
                        _canonical, _relative, _root, _sha, tree_hash)


MANIFEST_LIMIT = 96 * 1024
SCHEMA_FIELDS = {"schema_version", "project_name", "title", "description", "files",
                 "editable_files", "check_profile", "test_paths", "tags", "provenance"}
PROJECT_NAME = re.compile(r"[a-z0-9][a-z0-9-]{1,63}\Z")
SNAPSHOT_ID = re.compile(r"[a-f0-9]{64}\Z")
TASK_ID = re.compile(r"project-([a-f0-9]{56})\Z")
DENIED_PARTS = {".git", ".hg", ".svn", ".venv", "venv", "node_modules", "__pycache__",
                ".pytest_cache", ".mypy_cache", ".ruff_cache", "dist", "build", "artifacts",
                "var", "secrets", "credentials", "site-packages"}
TEXT_SUFFIXES = {".py", ".md", ".txt", ".json", ".toml", ".ini", ".cfg", ".yaml", ".yml", ".csv"}
DENIED_NAMES = {"credentials.json", "credential.json", "secrets.json", "secret.json", "id_rsa",
                "id_ed25519", "token.json", "tokens.json", "authorized_keys", "known_hosts"}
CONFIG_PY = {"conftest.py", "setup.py", "config.py", "settings.py"}
SAFE_PART = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}\Z")
CONFIG_VARIANT = re.compile(r"(?:(?:config|settings)(?:_[a-z0-9_-]+)?|[a-z0-9_-]+_(?:config|settings))\.py\Z")
CONFIG_DIRS = {"config", "configs", "configuration", "settings"}


def _publish_directory(source, destination):
    """Publish a complete snapshot without replacing even an empty old directory."""
    library = ctypes.CDLL(None, use_errno=True)
    if sys.platform.startswith("linux") and hasattr(library, "renameat2"):
        operation = library.renameat2
        operation.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
        operation.restype = ctypes.c_int
        result = operation(-100, os.fsencode(source), -100, os.fsencode(destination), 1)  # RENAME_NOREPLACE
    elif sys.platform == "darwin" and hasattr(library, "renamex_np"):
        operation = library.renamex_np
        operation.argtypes = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint)
        operation.restype = ctypes.c_int
        result = operation(os.fsencode(source), os.fsencode(destination), 4)  # RENAME_EXCL
    else:
        raise WorkspaceError("Atomic non-replacing snapshot publication is unavailable")
    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def _text(value, name, maximum):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or "\x00" in value:
        raise WorkspaceError(f"Invalid {name}")
    return value.strip()


def _safe_file_name(value):
    value = _relative(value)
    path = PurePosixPath(value)
    if any(not SAFE_PART.fullmatch(part) for part in path.parts):
        raise WorkspaceError("Import paths require portable ASCII letters, digits, underscores, dots or hyphens")
    if any(part.startswith(".") or part.casefold() in DENIED_PARTS for part in path.parts):
        raise WorkspaceError("Hidden, secret or generated paths cannot be imported")
    name = path.name.casefold()
    if name in DENIED_NAMES or name.startswith((".env", "secrets.", "credentials.")):
        raise WorkspaceError("Credential-bearing file names cannot be imported")
    if path.suffix.casefold() not in TEXT_SUFFIXES and name not in {"license", "notice", "readme"}:
        raise WorkspaceError("Only allowlisted text source and test files can be imported")
    return value


def _paths(values, name, minimum, maximum):
    if not isinstance(values, list) or not minimum <= len(values) <= maximum:
        raise WorkspaceError(f"Invalid {name} file count")
    result = [_safe_file_name(value) for value in values]
    if len(set(result)) != len(result):
        raise WorkspaceError(f"Duplicate {name} path")
    return sorted(result)


def normalize_manifest(manifest):
    """Validate the control schema without reading or executing a repository."""
    from .check_profiles import build_check_command
    if not isinstance(manifest, dict) or set(manifest) - SCHEMA_FIELDS:
        raise WorkspaceError("Unknown project manifest field")
    required = SCHEMA_FIELDS - {"tags", "provenance"}
    if not required <= set(manifest) or type(manifest.get("schema_version")) is not int or manifest["schema_version"] != 1:
        raise WorkspaceError("Project manifest schema_version 1 is required")
    project_name = _text(manifest["project_name"], "project_name", 64)
    if not PROJECT_NAME.fullmatch(project_name):
        raise WorkspaceError("project_name must be a lowercase slug of 2 to 64 characters")
    files = _paths(manifest["files"], "included", 2, MAX_FILES)
    editable = _paths(manifest["editable_files"], "editable", 1, 3)
    if not set(editable) <= set(files):
        raise WorkspaceError("Every editable file must be included")
    tests = manifest["test_paths"]
    if (not isinstance(tests, list) or not tests or not all(isinstance(value, str) for value in tests)
            or len(set(tests)) != len(tests)):
        raise WorkspaceError("A unique fixed test selection is required")
    try:
        build_check_command(manifest["check_profile"], tests)
    except (TypeError, ValueError) as exc:
        raise WorkspaceError("Invalid fixed test profile or paths") from exc
    test_targets = [value.split("::", 1)[0] for value in tests]
    for target in test_targets:
        if not any(path == target or path.startswith(target + "/") for path in files):
            raise WorkspaceError("Selected tests must exist in the included file inventory")
        if not any(path.endswith(".py") and (path == target or path.startswith(target + "/")) for path in files):
            raise WorkspaceError("Selected test paths must include Python test source")
    for path in editable:
        name = PurePosixPath(path).name.casefold()
        if (not path.endswith(".py") or name in CONFIG_PY or CONFIG_VARIANT.fullmatch(name)
                or any(part.casefold() in CONFIG_DIRS for part in PurePosixPath(path).parts[:-1])
                or name.startswith("test_")
                or name.endswith("_test.py") or any(part.casefold() in {"tests", "test"} for part in PurePosixPath(path).parts)
                or any(path == target or path.startswith(target + "/") for target in test_targets)):
            raise WorkspaceError("Tests and configuration cannot be editable")
    tags = manifest.get("tags", [])
    if not isinstance(tags, list) or len(tags) > 12:
        raise WorkspaceError("Invalid project tags")
    tags = sorted({_text(value, "tag", 40) for value in tags})
    provenance = manifest.get("provenance", "")
    if not isinstance(provenance, str) or len(provenance) > 2000 or "\x00" in provenance:
        raise WorkspaceError("provenance must be bounded source attribution text")
    return {"schema_version": 1, "project_name": project_name,
            "title": _text(manifest["title"], "title", 160),
            "description": _text(manifest["description"], "description", 4000),
            "files": files, "editable_files": editable,
            "check_profile": manifest["check_profile"], "test_paths": list(tests),
            "tags": tags, "provenance": provenance.strip()}


def load_manifest(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MANIFEST_LIMIT:
        raise WorkspaceError("Manifest must be a bounded regular JSON file")
    try:
        return normalize_manifest(json.loads(path.read_text(encoding="utf-8")))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise WorkspaceError("Manifest must contain UTF-8 JSON") from exc


def _source_file(root, relative):
    path = root
    for part in PurePosixPath(relative).parts:
        path = path / part
        if path.is_symlink():
            raise WorkspaceError("Source paths cannot contain symlinks")
    try:
        mode = path.stat().st_mode
    except OSError as exc:
        raise WorkspaceError("An allowlisted source file is missing") from exc
    if not stat.S_ISREG(mode) or path.stat().st_size > MAX_FILE_BYTES:
        raise WorkspaceError("Source files must be regular and at most 128 KiB")
    return path


def _inventory(source, manifest):
    root = _root(source)
    inventory, total = [], 0
    for relative in manifest["files"]:
        path = _source_file(root, relative)
        content = path.read_bytes()
        if len(content) > MAX_FILE_BYTES:
            raise WorkspaceError("Source changed beyond its file-size limit")
        try:
            content.decode("utf-8")
        except UnicodeError as exc:
            raise WorkspaceError("Source files must contain UTF-8 text") from exc
        if b"\x00" in content:
            raise WorkspaceError("Binary source files cannot be imported")
        total += len(content)
        if total > MAX_TREE_BYTES:
            raise WorkspaceError("Source snapshot exceeds 2 MiB")
        inventory.append({"path": relative, "bytes": len(content), "sha256": _sha(content)})
    return root, inventory


def _snapshot_file_inventory(source, manifest):
    """Unlike general workspace hashes, snapshots reject caches and extra files."""
    allowed = set(manifest["files"])
    allowed_dirs = {PurePosixPath(path).parent.as_posix() for path in allowed}
    allowed_dirs |= {parent.as_posix() for path in allowed for parent in PurePosixPath(path).parents}
    pending = [source]
    found = set()
    while pending:
        parent = pending.pop()
        for child in parent.iterdir():
            if child.is_symlink():
                raise WorkspaceError("Imported snapshot contains a symlink")
            relative = child.relative_to(source).as_posix()
            if child.is_dir():
                if relative not in allowed_dirs:
                    raise WorkspaceError("Imported snapshot contains an unexpected directory")
                pending.append(child)
            elif child.is_file() and relative in allowed:
                found.add(relative)
            else:
                raise WorkspaceError("Imported snapshot contains unexpected source files")
    if found != allowed:
        raise WorkspaceError("Imported snapshot inventory changed")


def preview_project(source, manifest):
    manifest = normalize_manifest(manifest)
    root, inventory = _inventory(source, manifest)
    # Re-read to reject a source that changes while the review inventory is made.
    if _inventory(root, manifest)[1] != inventory:
        raise WorkspaceError("Source changed while preparing the import preview")
    return {"manifest": manifest, "files": inventory, "file_count": len(inventory),
            "total_bytes": sum(row["bytes"] for row in inventory), "execution_performed": False,
            "scope": "explicit source allowlist; review it for private content before committing",
            "content_review_required": True,
            "credential_policy": "Filename exclusions only; manually inspect source for hardcoded credentials before commit/model use"}


class ProjectCatalog:
    def __init__(self, directory):
        self.directory = Path(directory)
        if self.directory.is_symlink():
            raise WorkspaceError("Project catalog cannot be a symlink")

    def import_project(self, source, manifest, *, expected_inventory=None):
        preview = preview_project(source, manifest)
        manifest = preview["manifest"]
        if expected_inventory is not None and preview["files"] != expected_inventory:
            raise WorkspaceError("Source differs from the reviewed import inventory")
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        base = _root(self.directory)
        staging = Path(tempfile.mkdtemp(prefix=".import-", dir=base))
        try:
            source_root = _root(source)
            snapshot = staging / "source"
            snapshot.mkdir(mode=0o755)
            for entry in preview["files"]:
                content = _source_file(source_root, entry["path"]).read_bytes()
                if len(content) != entry["bytes"] or _sha(content) != entry["sha256"]:
                    raise WorkspaceError("Source changed after import preview")
                target = snapshot / entry["path"]
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
                target.chmod(0o444)
            for directory in (path for path in snapshot.rglob("*") if path.is_dir()):
                directory.chmod(0o755)
            source_hash = tree_hash(snapshot)
            identity = {"manifest": manifest, "source_hash": source_hash}
            snapshot_id = _sha(_canonical(identity))
            task_id = "project-" + snapshot_id[:56]
            metadata = {"schema_version": 1, "snapshot_id": snapshot_id, "task_id": task_id,
                        **identity, "inventory": preview["files"]}
            (staging / "metadata.json").write_bytes(_canonical(metadata) + b"\n")
            (staging / "metadata.json").chmod(0o400)
            if _inventory(source_root, manifest)[1] != preview["files"]:
                raise WorkspaceError("Source changed during import publication")
            destination = base / snapshot_id
            try:
                _publish_directory(staging, destination)
                created = True
            except FileExistsError:
                if destination.is_symlink() or not destination.is_dir():
                    raise
                existing = self._load(destination)
                if existing != metadata:
                    raise WorkspaceError("Snapshot destination has different registered content")
                created = False
            loaded = self._load(destination)
            return {**self._metadata(loaded), "created": created}
        finally:
            if staging.exists():
                shutil.rmtree(staging)

    def _load(self, directory):
        directory = Path(directory)
        if not SNAPSHOT_ID.fullmatch(directory.name) or directory.is_symlink():
            raise WorkspaceError("Invalid snapshot location")
        record = directory / "metadata.json"
        if record.is_symlink() or not record.is_file() or record.stat().st_size > 256 * 1024:
            raise WorkspaceError("Snapshot metadata is unavailable or oversized")
        try:
            metadata = json.loads(record.read_text(encoding="utf-8"))
        except (ValueError, UnicodeError) as exc:
            raise WorkspaceError("Snapshot metadata is invalid") from exc
        if not isinstance(metadata, dict) or set(metadata) != {
                "schema_version", "snapshot_id", "task_id", "manifest", "source_hash", "inventory"}:
            raise WorkspaceError("Snapshot metadata schema changed")
        manifest = normalize_manifest(metadata["manifest"])
        if metadata["manifest"] != manifest:
            raise WorkspaceError("Snapshot manifest is not in its registered canonical form")
        identity = {"manifest": manifest, "source_hash": metadata["source_hash"]}
        digest = _sha(_canonical(identity))
        if (type(metadata["schema_version"]) is not int or metadata["schema_version"] != 1
                or metadata["snapshot_id"] != digest or directory.name != digest
                or metadata["task_id"] != "project-" + digest[:56]):
            raise WorkspaceError("Snapshot identity no longer matches its manifest")
        source = _root(directory / "source")
        _snapshot_file_inventory(source, manifest)
        if tree_hash(source) != metadata["source_hash"] or _inventory(source, manifest)[1] != metadata["inventory"]:
            raise WorkspaceError("Snapshot source bytes no longer match registration")
        return metadata

    def _records(self):
        if not self.directory.exists():
            return []
        base = _root(self.directory)
        records = []
        for path in sorted(base.iterdir()):
            if path.name.startswith(".import-") and path.is_dir() and not path.is_symlink():
                continue  # An unpublished concurrent import is never a catalog entry.
            if not SNAPSHOT_ID.fullmatch(path.name):
                raise WorkspaceError("Unexpected file in project catalog")
            records.append(self._load(path))
        return records

    @staticmethod
    def _metadata(record):
        manifest = record["manifest"]
        return {"id": record["task_id"], "title": manifest["title"], "description": manifest["description"],
                "editable_files": manifest["editable_files"], "tags": manifest["tags"], "private": True,
                "project_id": manifest["project_name"], "project_name": manifest["project_name"],
                "snapshot_id": record["snapshot_id"], "source_hash": record["source_hash"],
                "check_id": "tests", "check_profile": manifest["check_profile"],
                "test_paths": manifest["test_paths"], "file_count": len(manifest["files"]),
                "total_bytes": sum(row["bytes"] for row in record["inventory"]),
                "files": [dict(row) for row in record["inventory"]], "provenance": manifest["provenance"]}

    def get_task(self, task_id):
        from .check_profiles import build_check_command
        if not isinstance(task_id, str) or not TASK_ID.fullmatch(task_id):
            raise KeyError(task_id)
        for record in self._records():
            if record["task_id"] == task_id:
                manifest = record["manifest"]
                return TaskSpec(task_id, manifest["title"], manifest["description"],
                                self.directory.resolve() / record["snapshot_id"] / "source",
                                tuple(manifest["editable_files"]),
                                build_check_command(manifest["check_profile"], manifest["test_paths"]),
                                "", "", tuple(manifest["tags"]), private=True,
                                project_id=manifest["project_name"], snapshot_id=record["snapshot_id"],
                                check_profile=manifest["check_profile"], test_paths=tuple(manifest["test_paths"]),
                                source_hash=record["source_hash"])
        raise KeyError(task_id)

    def list_tasks(self):
        return [self._metadata(record) for record in self._records()]

    def list_projects(self):
        projects = defaultdict(list)
        for task in self.list_tasks():
            projects[task["project_id"]].append(task)
        return [{"id": project_id, "project_name": project_id, "private": True,
                 "tasks": tasks, "snapshot_count": len(tasks)} for project_id, tasks in sorted(projects.items())]
