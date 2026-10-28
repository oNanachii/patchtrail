"""Verify an official pinned Linux archive and install to an explicit new path.

Does not install/start services, change firewall/DNS, or replace the old proxy.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import tarfile
import tempfile
import urllib.request


def download(url, destination):
    request = urllib.request.Request(url, headers={"User-Agent": "PatchTrail-deploy/0.1"})
    with urllib.request.urlopen(request, timeout=90) as response, destination.open("wb") as output:
        total = 0
        while chunk := response.read(65536):
            total += len(chunk)
            if total > 64 * 1024 * 1024:
                raise ValueError("Official archive exceeded the download limit")
            output.write(chunk)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    if platform.system() != "Linux" or platform.machine() not in {"x86_64", "amd64"}:
        parser.error("This pinned archive is for Linux amd64")
    manifest = json.loads(Path(__file__).with_name("release.json").read_text())
    expected = manifest["asset_sha512"]
    if manifest["checksum_algorithm"] != "sha512" or not re.fullmatch(r"[a-f0-9]{128}", expected):
        raise ValueError("Unexpected pinned checksum")
    with tempfile.TemporaryDirectory(prefix="patchtrail-caddy-download-") as temporary:
        root = Path(temporary)
        checksums, archive = root / "checksums.txt", root / manifest["asset"]
        download(manifest["checksums_url"], checksums)
        matches = [line.split()[0].lower() for line in checksums.read_text().splitlines()
                   if len(line.split()) == 2 and line.split()[1].lstrip("*") == manifest["asset"]]
        if matches != [expected]:
            raise ValueError("Official checksum does not match the reviewed pinned value")
        download(manifest["asset_url"], archive)
        with archive.open("rb") as stream:
            actual = hashlib.file_digest(stream, "sha512").hexdigest()
        if actual != expected:
            raise ValueError("Official archive checksum failed")
        with tarfile.open(archive, "r:gz") as bundle:
            member = bundle.getmember("caddy")
            if not member.isfile() or member.size > 128 * 1024 * 1024:
                raise ValueError("Unexpected binary archive member")
            stream = bundle.extractfile(member)
            if stream is None:
                raise ValueError("Caddy binary missing")
            binary = stream.read()
        destination = args.destination.absolute()
        destination.parent.mkdir(parents=True, exist_ok=True)
        binary_sha256 = hashlib.sha256(binary).hexdigest()
        created = False
        if destination.exists() or destination.is_symlink():
            if destination.is_symlink() or not destination.is_file() or destination.read_bytes() != binary:
                raise FileExistsError("Existing destination differs; it was not overwritten")
        else:
            temporary_binary = None
            try:
                with tempfile.NamedTemporaryFile(prefix=".caddy-", dir=destination.parent, delete=False) as output:
                    temporary_binary = Path(output.name)
                    output.write(binary)
                    output.flush()
                    os.fsync(output.fileno())
                temporary_binary.chmod(0o755)
                os.link(temporary_binary, destination)
                created = True
            finally:
                if temporary_binary is not None:
                    temporary_binary.unlink(missing_ok=True)
        print(json.dumps({"release": manifest["release"], "archive_sha512": actual,
              "binary_sha256": binary_sha256, "destination": str(destination), "created": created,
              "official_checksum_verified": True, "signature_verified": False}, indent=2))


if __name__ == "__main__":
    main()
