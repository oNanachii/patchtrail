"""Verify effective rootless Podman isolation. Never installs or pulls anything.

Run as the dedicated service user with an already-present, reviewed image.
This starts one disposable probe container and removes only its own container.
It is an integration check, not a general sandbox certification.
"""

import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile
import uuid


PROBE = r'''
import errno,json,os,pathlib
def blocked(path):
    try:
        pathlib.Path(path).write_text("must not persist")
    except OSError as exc:
        return exc.errno in (errno.EROFS,errno.EACCES,errno.EPERM)
    return False
def text(path):
    return pathlib.Path(path).read_text().strip()
status={line.split(":",1)[0]:line.split(":",1)[1].strip() for line in text("/proc/self/status").splitlines() if ":" in line}
interfaces=[line.split(":",1)[0].strip() for line in text("/proc/net/dev").splitlines()[2:]]
mounts={line.split()[4]:line.split()[5].split(",") for line in text("/proc/self/mountinfo").splitlines()}
print(json.dumps({
    "uid":os.getuid(),
    "no_new_privileges":status.get("NoNewPrivs"),
    "effective_capabilities":status.get("CapEff"),
    "seccomp_mode":status.get("Seccomp"),
    "network_interfaces":interfaces,
    "memory_max":text("/sys/fs/cgroup/memory.max"),
    "pids_max":text("/sys/fs/cgroup/pids.max"),
    "cpu_max":text("/sys/fs/cgroup/cpu.max"),
    "root_write_blocked":blocked("/patchtrail-root-write-probe"),
    "workspace_write_blocked":blocked("/workspace/probe.txt"),
    "root_mount_read_only":"ro" in mounts.get("/",[]),
    "workspace_mount_read_only":"ro" in mounts.get("/workspace",[]),
    "temporary_mount_noexec":"noexec" in mounts.get("/tmp",[]),
    "workspace_read_ok":text("/workspace/probe.txt")=="read-only fixture",
    "operator_credentials_absent":not any(os.environ.get(k) for k in ("DEEPSEEK_API_KEY","PATCHTRAIL_ADMIN_TOKEN")),
}))
'''


def command(args, *, timeout=15):
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
    if result.returncode:
        raise RuntimeError(f"Container preflight command failed (exit {result.returncode}): "
                           + result.stderr[-2000:])
    return result.stdout


def verify(binary, image):
    if os.geteuid() == 0:
        raise RuntimeError("Run this check as the dedicated unprivileged service user")
    info = json.loads(command([binary, "info", "--format", "json"]))
    host = info.get("host", {})
    if not host.get("security", {}).get("rootless") or host.get("cgroupVersion") != "v2":
        raise RuntimeError("Rootless Podman and cgroup v2 are required")
    command([binary, "image", "exists", image])
    name = "patchtrail-preflight-" + uuid.uuid4().hex
    with tempfile.TemporaryDirectory(prefix="patchtrail-container-probe-") as temporary:
        workspace = Path(temporary)
        workspace.chmod(0o755)
        fixture = workspace / "probe.txt"
        fixture.write_text("read-only fixture", encoding="utf-8")
        fixture.chmod(0o644)
        arguments = [binary, "run", "--rm", "--name", name, "--pull=never",
                     "--network=none", "--read-only", "--user=65532:65532",
                     "--cap-drop=ALL", "--security-opt=no-new-privileges",
                     "--memory=256m", "--cpus=0.5", "--pids-limit=64",
                     "--tmpfs=/tmp:rw,noexec,nosuid,size=64m",
                     "--volume", f"{workspace}:/workspace:ro", "--workdir=/workspace",
                     image, "python", "-I", "-B", "-c", PROBE]
        try:
            result = json.loads(command(arguments, timeout=30))
        finally:
            try:
                subprocess.run([binary, "rm", "--force", name], capture_output=True,
                               text=True, timeout=5, check=False)
            except subprocess.TimeoutExpired:
                pass
    quota, period = result["cpu_max"].split()
    checks = {
        "non_root": result["uid"] > 0,
        "no_new_privileges": result["no_new_privileges"] == "1",
        "capabilities_dropped": int(result["effective_capabilities"], 16) == 0,
        "seccomp_enabled": result["seccomp_mode"] == "2",
        "no_external_interface": set(result["network_interfaces"]) <= {"lo"},
        "memory_limit_effective": result["memory_max"].isdigit() and int(result["memory_max"]) <= 256 * 1024**2,
        "pid_limit_effective": result["pids_max"].isdigit() and int(result["pids_max"]) <= 64,
        "cpu_limit_effective": quota != "max" and int(quota) <= int(period) // 2,
        "read_only_root": result["root_mount_read_only"] and result["root_write_blocked"],
        "read_only_workspace": result["workspace_mount_read_only"] and result["workspace_write_blocked"] and result["workspace_read_ok"],
        "temporary_mount_noexec": result["temporary_mount_noexec"],
        "credentials_absent": result["operator_credentials_absent"],
    }
    output = {"status": "passed" if all(checks.values()) else "failed",
              "runner": "rootless-podman", "image": image, "checks": checks,
              "observed": result, "scope": "one disposable integration probe; no host fallback"}
    print(json.dumps(output, indent=2))
    return all(checks.values())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", default="/usr/bin/podman")
    parser.add_argument("--image", required=True)
    arguments = parser.parse_args()
    try:
        passed = verify(arguments.binary, arguments.image)
    except (OSError, RuntimeError, ValueError, KeyError, subprocess.TimeoutExpired) as exc:
        print(json.dumps({"status": "failed", "error": str(exc), "host_fallback": False}))
        raise SystemExit(1)
    if not passed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
