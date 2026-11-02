"""Small explicit configuration; secrets never appear in public configuration."""
from dataclasses import dataclass, field
import os
import hashlib
import json
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


def _model_options():
    try:
        value = json.loads(os.getenv("PATCHTRAIL_MODEL_OPTIONS", "{}"))
    except (ValueError, TypeError):
        raise ValueError("PATCHTRAIL_MODEL_OPTIONS must be a JSON object") from None
    if not isinstance(value, dict):
        raise ValueError("PATCHTRAIL_MODEL_OPTIONS must be a JSON object")
    return value


@dataclass
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(os.getenv("PATCHTRAIL_DATA_DIR", str(ROOT / "var"))))
    api_key: str = field(default_factory=lambda: os.getenv("DEEPSEEK_API_KEY", ""), repr=False)
    provider_kind: str = field(default_factory=lambda: os.getenv("PATCHTRAIL_PROVIDER", "deepseek"))
    compatible_api_key: str = field(default_factory=lambda: os.getenv("PATCHTRAIL_COMPATIBLE_API_KEY", ""), repr=False)
    model_options: dict = field(default_factory=_model_options, repr=False)
    output_token_parameter: str = field(default_factory=lambda: os.getenv("PATCHTRAIL_OUTPUT_TOKEN_PARAMETER", "max_tokens"))
    preserve_reasoning_content: bool = field(default_factory=lambda: os.getenv("PATCHTRAIL_PRESERVE_REASONING", "false").lower() == "true")
    deepseek_member_model: str = field(default_factory=lambda: os.getenv("PATCHTRAIL_MEMBER_MODEL", "deepseek-flash"))
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
    def live_api_key(self):
        return self.compatible_api_key if self.provider_kind == "openai-compatible" else self.api_key

    @property
    def member_model(self):
        return self.model if self.provider_kind == "deepseek" else self.deepseek_member_model

    @property
    def limits(self):
        return {"max_calls": self.max_calls, "max_steps": self.max_steps,
                "max_seconds": self.max_seconds, "max_input_chars": self.max_input_chars,
                "max_output_tokens": self.max_output_tokens}
