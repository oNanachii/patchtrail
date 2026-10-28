"""Construct the two supported test commands; manifests never contain shell argv."""
from pathlib import PurePosixPath
import re


_PYTHON = ("python", "-I", "-B", "-m")
_PYTEST_PREFIX = _PYTHON + (
    "pytest", "-c", "/dev/null", "--rootdir=/workspace", "--import-mode=importlib",
    "-o", "pythonpath=/workspace", "-p", "no:cacheprovider", "--color=no",
    "--tb=short", "--basetemp=/tmp/patchtrail-pytest", "-v",
)
_PART = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}\Z")
_NODE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}\Z")


def _test_path(value: str, *, nodes: bool = False) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 512:
        raise ValueError("A bounded relative test path is required")
    pieces = value.split("::")
    path = pieces[0]
    parts = path.split("/")
    if (not path or PurePosixPath(path).as_posix() != path
            or any(not _PART.fullmatch(part) or part in {".", ".."} for part in parts)):
        raise ValueError("Test paths must be normalized relative paths without options")
    if len(pieces) > 1:
        if (not nodes or not path.endswith(".py") or len(pieces) > 3
                or any(not _NODE.fullmatch(part) for part in pieces[1:])):
            raise ValueError("Only pytest file::class::method node selectors are supported")
    elif PurePosixPath(path).suffix and not path.endswith(".py"):
        raise ValueError("Tests must select a directory or Python file")
    return value


def build_check_command(profile: str, test_paths: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    """Return one fixed profile with only bounded test paths varying.

    unittest supports one directory or test_*.py file; pytest supports up to
    eight directories/files or non-parameterized node selectors. Parameterized
    tests may still run when selecting their file or parent directory.
    """
    if not isinstance(test_paths, (list, tuple)) or not 1 <= len(test_paths) <= 8:
        raise ValueError("Select between one and eight test paths")
    if any(not isinstance(path, str) for path in test_paths):
        raise ValueError("Test selectors must be strings")
    if len(set(test_paths)) != len(test_paths):
        raise ValueError("Repeated test selectors are not allowed")
    if profile == "unittest":
        if len(test_paths) != 1:
            raise ValueError("The unittest profile selects one test directory or file")
        path = _test_path(test_paths[0])
        if path.endswith(".py"):
            parsed = PurePosixPath(path)
            if not parsed.name.startswith("test_"):
                raise ValueError("unittest file selection requires a test_*.py filename")
            return _PYTHON + ("unittest", "discover", "-s", str(parsed.parent),
                              "-t", ".", "-p", parsed.name, "-v")
        return _PYTHON + ("unittest", "discover", "-s", path, "-t", ".", "-v")
    if profile == "pytest":
        paths = tuple(_test_path(path, nodes=True) for path in test_paths)
        return _PYTEST_PREFIX + paths
    raise ValueError("Test profile must be unittest or pytest")


def parse_check_command(command: tuple[str, ...]) -> tuple[str, tuple[str, ...]]:
    """Reject every argv except an exact command this module can construct."""
    if not isinstance(command, tuple) or any(not isinstance(part, str) for part in command):
        raise ValueError("A fixed profile command tuple is required")
    if command[:len(_PYTEST_PREFIX)] == _PYTEST_PREFIX:
        paths = command[len(_PYTEST_PREFIX):]
        if command == build_check_command("pytest", paths):
            return "pytest", paths
    prefix = _PYTHON + ("unittest", "discover", "-s")
    if command[:len(prefix)] == prefix:
        rest = command[len(prefix):]
        if len(rest) == 4 and rest[1:] == ("-t", ".", "-v"):
            paths = (rest[0],)
        elif len(rest) == 6 and rest[1:3] == ("-t", ".") and rest[3] == "-p" and rest[5] == "-v":
            paths = ((PurePosixPath(rest[0]) / rest[4]).as_posix(),)
        else:
            raise ValueError("Only the fixed unittest profile is allowed")
        if command == build_check_command("unittest", paths):
            return "unittest", paths
    raise ValueError("Only fixed unittest or pytest profile commands are allowed")
