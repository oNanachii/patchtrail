"""Bounded fixture workspaces and exact, approval-bound single-file proposals."""
from contextlib import contextmanager
import difflib
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tempfile
import time

from .tasks import TaskSpec


MAX_FILES = 128
MAX_FILE_BYTES = 256 * 1024
MAX_TREE_BYTES = 2 * 1024 * 1024
MAX_PATCH_BYTES = 16 * 1024
MAX_READ_CHARACTERS = 20000
RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,95}\Z")


class WorkspaceError(ValueError):
    """A requested path, fixture or proposal violates the workspace contract."""


def _canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def _sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _root(directory: Path) -> Path:
    directory = Path(directory)
    if directory.is_symlink():
        raise WorkspaceError("Workspace roots cannot be symlinks")
    try:
        resolved = directory.resolve(strict=True)
    except OSError as exc:
        raise WorkspaceError("Workspace is unavailable") from exc
    if not resolved.is_dir():
        raise WorkspaceError("Workspace must be a directory")
    return resolved


def _files(directory: Path) -> list[Path]:
    root = _root(directory)
    found, pending, total = [], [(root, 0)], 0
    while pending:
        parent, depth = pending.pop()
        if depth > 16:
            raise WorkspaceError("Workspace directory nesting is too deep")
        for child in sorted(parent.iterdir()):
            if child.is_symlink():
                raise WorkspaceError("Symlinks are not allowed in workspaces")
            if child.name == "__pycache__" or child.suffix == ".pyc":
                continue
            mode = child.stat().st_mode
            if stat.S_ISDIR(mode):
                pending.append((child, depth + 1))
            elif stat.S_ISREG(mode):
                size = child.stat().st_size
                total += size
                if size > MAX_FILE_BYTES or total > MAX_TREE_BYTES:
                    raise WorkspaceError("Workspace exceeds its file or total size limit")
                found.append(child)
                if len(found) > MAX_FILES:
                    raise WorkspaceError("Workspace contains too many files")
            else:
                raise WorkspaceError("Only regular files and directories are allowed")
    return sorted(found, key=lambda path: path.relative_to(root).as_posix())


def _hash_files(root: Path, files: list[Path]) -> str:
    digest = hashlib.sha256()
    for path in files:
        relative = path.relative_to(root).as_posix().encode("utf-8")
        content = path.read_bytes()
        if len(content) > MAX_FILE_BYTES:
            raise WorkspaceError("Workspace file changed beyond its size limit")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def tree_hash(directory: Path) -> str:
    root = _root(directory)
    return _hash_files(root, _files(root))


def _relative(path: str) -> str:
    if not isinstance(path, str) or not path or "\x00" in path or "\\" in path:
        raise WorkspaceError("A normalized relative POSIX path is required")
    parsed = PurePosixPath(path)
    if (parsed.is_absolute() or any(part in {"", ".", ".."} for part in path.split("/"))
            or parsed.as_posix() != path or len(path) > 512):
        raise WorkspaceError("Path must stay inside the workspace")
    return parsed.as_posix()


def _path(workspace: Path, path: str) -> Path:
    root = _root(workspace)
    relative = _relative(path)
    current = root
    for part in PurePosixPath(relative).parts:
        current = current / part
        if current.is_symlink():
            raise WorkspaceError("Symlink paths are not allowed")
    try:
        resolved = current.resolve(strict=True)
    except OSError as exc:
        raise WorkspaceError("Requested file does not exist") from exc
    if not resolved.is_relative_to(root) or not resolved.is_file():
        raise WorkspaceError("Requested path is not a workspace file")
    if resolved.stat().st_size > MAX_FILE_BYTES:
        raise WorkspaceError("Requested file exceeds its size limit")
    return resolved


@contextmanager
def _creation_lock(base: Path, run_id: str):
    lock_path = base / ("." + run_id + ".workspace.lock")
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    deadline = time.monotonic() + 5
    try:
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise WorkspaceError("Workspace creation is already in progress")
                time.sleep(0.01)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _verify_existing(target: Path, task: TaskSpec) -> Path:
    root = _root(target)
    source_root = _root(task.source_dir)
    source_files = {path.relative_to(source_root).as_posix(): path for path in _files(source_root)}
    target_files = {path.relative_to(root).as_posix(): path for path in _files(root)}
    if set(source_files) != set(target_files):
        raise WorkspaceError("Existing workspace does not have the fixture's file inventory")
    for relative, source in source_files.items():
        if relative not in task.editable_files and source.read_bytes() != target_files[relative].read_bytes():
            raise WorkspaceError("Existing workspace has changed immutable fixture files")
    return root


def prepare_workspace(base: Path, run_id: str, task: TaskSpec) -> Path:
    if not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id):
        raise WorkspaceError("Invalid run ID")
    base = Path(base)
    if base.is_symlink():
        raise WorkspaceError("Workspace base cannot be a symlink")
    base.mkdir(parents=True, exist_ok=True)
    base = _root(base)
    source = _root(task.source_dir)
    source_hash = tree_hash(source)
    target = base / run_id
    with _creation_lock(base, run_id):
        if target.exists() or target.is_symlink():
            return _verify_existing(target, task)
        staging = Path(tempfile.mkdtemp(prefix="." + run_id + "-", dir=base))
        try:
            for original in _files(source):
                relative = original.relative_to(source)
                destination = staging / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(original.read_bytes())
                destination.chmod(0o644)
            for directory in (path for path in staging.rglob("*") if path.is_dir()):
                directory.chmod(0o755)
            staging.chmod(0o755)
            if tree_hash(staging) != source_hash or tree_hash(source) != source_hash:
                raise WorkspaceError("Fixture changed while copying")
            if target.exists() or target.is_symlink():
                raise WorkspaceError("Workspace destination appeared while copying")
            staging.rename(target)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
    return target


def read_file(workspace: Path, path: str, start: int = 1, end: int = 200) -> dict:
    if type(start) is not int or type(end) is not int or start < 1 or end < start or end - start >= 400:
        raise WorkspaceError("Read range must contain between 1 and 400 lines")
    target = _path(workspace, path)
    content = target.read_bytes()
    try:
        lines = content.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise WorkspaceError("Only UTF-8 text files can be read") from exc
    rendered, count, truncated = [], 0, False
    for number in range(start, min(end, len(lines)) + 1):
        line = f"{number}: {lines[number - 1]}"
        remaining = MAX_READ_CHARACTERS - count
        if remaining <= 0:
            truncated = True
            break
        if len(line) > remaining:
            rendered.append(line[:remaining])
            truncated = True
            break
        rendered.append(line)
        count += len(line) + 1
    return {"path": _relative(path), "text": "\n".join(rendered),
            "content_sha256": _sha(content), "start": start, "end": min(end, len(lines)),
            "truncated": truncated}


def search_code(workspace: Path, query: str) -> dict:
    if not isinstance(query, str) or not query or len(query) > 200 or "\x00" in query:
        raise WorkspaceError("Search query must contain 1 to 200 characters")
    root = _root(workspace)
    hits = []
    for target in _files(root):
        try:
            lines = target.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            continue
        for number, line in enumerate(lines, 1):
            if query in line:
                if len(hits) >= 30:
                    return {"query": query, "hits": hits, "truncated": True}
                hits.append({"path": target.relative_to(root).as_posix(), "line": number, "text": line[:500]})
    return {"query": query, "hits": hits, "truncated": False}


def build_proposal(task: TaskSpec, workspace: Path, path: str, content: str) -> dict:
    path = _relative(path)
    if path not in task.editable_files:
        raise WorkspaceError("Only the task's editable source file can be proposed")
    if not isinstance(content, str):
        raise WorkspaceError("Replacement content must be text")
    try:
        replacement = content.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise WorkspaceError("Replacement content must be valid UTF-8") from exc
    if len(replacement) > MAX_PATCH_BYTES:
        raise WorkspaceError("Replacement content exceeds 16 KiB")
    _verify_existing(workspace, task)
    old = _path(workspace, path).read_bytes()
    if old == replacement:
        raise WorkspaceError("Proposal makes no change")
    try:
        old_text = old.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise WorkspaceError("Editable source must be UTF-8 text") from exc
    diff = "".join(difflib.unified_diff(old_text.splitlines(keepends=True), content.splitlines(keepends=True),
                                       fromfile="a/" + path, tofile="b/" + path))
    payload = {"task_id": task.id, "path": path, "content": content,
               "old_sha256": _sha(old), "new_sha256": _sha(replacement),
               "base_workspace_hash": tree_hash(workspace), "source_hash": tree_hash(task.source_dir),
               "diff": diff}
    return {"digest": _sha(_canonical(payload)), **payload}


def apply_proposal(task: TaskSpec, workspace: Path, proposal: dict) -> None:
    if not isinstance(proposal, dict):
        raise WorkspaceError("Proposal must be an object")
    fields = {"digest", "task_id", "path", "content", "old_sha256", "new_sha256",
              "base_workspace_hash", "source_hash", "diff"}
    if set(proposal) != fields or proposal["task_id"] != task.id:
        raise WorkspaceError("Proposal identity or fields do not match the task")
    expected = build_proposal(task, workspace, proposal["path"], proposal["content"])
    if proposal != expected:
        raise WorkspaceError("Proposal digest, source or workspace no longer matches approval")
    target = _path(workspace, proposal["path"])
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(prefix=".patchtrail-", suffix=".tmp", dir=target.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(proposal["content"].encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(0o644)
        root = _root(workspace)
        # Exclude only this exact staging path, never a name pattern a caller could imitate.
        current = _hash_files(root, [path for path in _files(root) if path != temporary])
        if current != proposal["base_workspace_hash"]:
            raise WorkspaceError("Workspace changed before proposal publication")
        os.replace(temporary, target)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
