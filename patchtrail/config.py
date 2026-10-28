"""Small explicit configuration; secrets never appear in public configuration."""
from dataclasses import dataclass, field
import os
import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def runtime_code_hash():
    digest = hashlib.sha256()
    for path in sorted((ROOT / "patchtrail").glob("*.py")):
        digest.update(path.name.encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
    return digest.hexdigest()


def load_local_env(path: Path = ROOT / ".env") -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip().replace("_", "").isalnum():
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


@dataclass
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(os.getenv("PATCHTRAIL_DATA_DIR", str(ROOT / "var"))))
    api_key: str = field(default_factory=lambda: os.getenv("DEEPSEEK_API_KEY", ""), repr=False)
    model: str = field(default_factory=lambda: os.getenv("PATCHTRAIL_MODEL", "deepseek-flash"))
    base_url: str = field(default_factory=lambda: os.getenv("PATCHTRAIL_MODEL_BASE_URL", "https://api.deepseek.com"))
    admin_token: str = field(default_factory=lambda: os.getenv("PATCHTRAIL_ADMIN_TOKEN", ""), repr=False)
    container_binary: str = field(default_factory=lambda: os.getenv("PATCHTRAIL_CONTAINER_BIN", "docker"))
    container_image: str = field(default_factory=lambda: os.getenv("PATCHTRAIL_CONTAINER_IMAGE", "python:3.12-slim"))
    pytest_image: str = field(default_factory=lambda: os.getenv("PATCHTRAIL_PYTEST_IMAGE", ""))
    public_demo: bool = field(default_factory=lambda: os.getenv("PATCHTRAIL_PUBLIC_DEMO", "true").lower() == "true")
    max_calls: int = 8
    max_steps: int = 12
    max_seconds: int = 120
    max_input_chars: int = 64000
    max_output_tokens: int = 4096

    @property
    def limits(self):
        return {"max_calls": self.max_calls, "max_steps": self.max_steps,
                "max_seconds": self.max_seconds, "max_input_chars": self.max_input_chars,
                "max_output_tokens": self.max_output_tokens}
