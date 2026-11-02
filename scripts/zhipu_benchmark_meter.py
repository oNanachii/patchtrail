"""Durable, no-retry CNY ledger for the official GLM-4.7 comparison.

This does not change the historical DeepSeek ledger. Reservations use the
highest published GLM-4.7 rates, the complete UTF-8 request bytes plus framing
allowance, and maximum requested output. Actual usage pricing follows the
published length tiers; unreported cache hits receive no assumed discount.
"""
import asyncio
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
import time

import httpx

from scripts.benchmark_meter import CapExceeded, Meter, MeterStopped, _cost, _decimal, _json, _redact


MODEL = "glm-4.7"
ENDPOINT = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
MAXIMUM_CAP_YUAN = Decimal(30)
MILLION = Decimal(1000000)
PROMPT_TIER_THRESHOLD = 32768
SHORT_OUTPUT_THRESHOLD = 200
MAX_RATES = {"cache_hit": "0.8", "cache_miss": "4", "output": "16"}
PRICING = {
    "source": "https://docs.bigmodel.cn/cn/guide/start/pricing",
    "checked_at": "2026-11-01",
    "currency": "CNY",
    "units": "per million tokens",
    "prompt_threshold_tokens": PROMPT_TIER_THRESHOLD,
    "short_output_threshold_tokens": SHORT_OUTPUT_THRESHOLD,
    "threshold_interpretation": "32K=32768 input tokens; 0.2K=200 output tokens",
    "tiers": [
        {"prompt_lt": PROMPT_TIER_THRESHOLD, "completion_lt": SHORT_OUTPUT_THRESHOLD,
         "cache_hit": "0.4", "cache_miss": "2", "output": "8"},
        {"prompt_lt": PROMPT_TIER_THRESHOLD, "completion_ge": SHORT_OUTPUT_THRESHOLD,
         "cache_hit": "0.6", "cache_miss": "3", "output": "14"},
        {"prompt_ge": PROMPT_TIER_THRESHOLD,
         "cache_hit": "0.8", "cache_miss": "4", "output": "16"},
    ],
    "reservation_rates": MAX_RATES,
    "confirmed_budget_accounting": "All input at maximum cache-miss rate; no cache discount for cap liability",
    "cache_storage": "Currently free; no explicit cache storage created",
    "missing_cache_usage": "Treat as zero cache hits; tariff is a conservative usage estimate",
}


def estimate_request(payload):
    """Return a heuristic estimate and explicit highest-rate reservation."""
    if not isinstance(payload, dict) or payload.get("model") != MODEL:
        raise ValueError("Only the verified GLM-4.7 price schedule is supported")
    maximum = payload.get("max_tokens")
    if type(maximum) is not int or not 1 <= maximum <= 4096:
        raise ValueError("Comparison requests require max_tokens in 1..4096")
    if payload.get("stream") is not False or payload.get("thinking") != {"type": "disabled"}:
        raise ValueError("Comparison requests require nonstreaming disabled-thinking mode")
    encoded = _json(payload).encode("utf-8")
    bound = len(encoded) + 4096
    reservation = (Decimal(bound) * Decimal(MAX_RATES["cache_miss"])
                   + Decimal(maximum) * Decimal(MAX_RATES["output"])) / MILLION
    return {"estimated_input_tokens": math.ceil(len(encoded) / 4),
            "estimate_method": "UTF-8 JSON bytes / 4; heuristic, not tokenizer",
            "request_utf8_bytes": len(encoded), "conservative_input_tokens": bound,
            "max_output_tokens": maximum, "reservation_peak_yuan": float(reservation)}


def normalize_usage(body):
    """Validate usage and accept either documented compatible cache-hit shape."""
    raw = body.get("usage") if isinstance(body, dict) else None
    keys = ("prompt_tokens", "completion_tokens", "total_tokens")
    if not isinstance(raw, dict) or any(type(raw.get(k)) is not int or raw[k] < 0 for k in keys):
        raise ValueError("Missing or incomplete token usage")
    if raw["total_tokens"] != raw["prompt_tokens"] + raw["completion_tokens"]:
        raise ValueError("Inconsistent total token usage")
    candidates = []
    if raw.get("prompt_cache_hit_tokens") is not None:
        candidates.append(raw["prompt_cache_hit_tokens"])
    details = raw.get("prompt_tokens_details")
    if details is not None:
        if not isinstance(details, dict):
            raise ValueError("Invalid prompt token details")
        if details.get("cached_tokens") is not None:
            candidates.append(details["cached_tokens"])
    if any(type(hit) is not int or not 0 <= hit <= raw["prompt_tokens"] for hit in candidates):
        raise ValueError("Invalid cached token usage")
    if len(set(candidates)) > 1:
        raise ValueError("Conflicting cached token usage")
    hit = candidates[0] if candidates else 0
    miss = raw["prompt_tokens"] - hit
    if raw.get("prompt_cache_miss_tokens") is not None and (
            type(raw["prompt_cache_miss_tokens"]) is not int or raw["prompt_cache_miss_tokens"] != miss):
        raise ValueError("Conflicting cache-miss token usage")
    return {**{key: raw[key] for key in keys}, "prompt_cache_hit_tokens": hit,
            "prompt_cache_miss_tokens": miss}


def tariff_rates(usage):
    """Choose the documented tier from confirmed complete usage."""
    if usage["prompt_tokens"] >= PROMPT_TIER_THRESHOLD:
        return dict(MAX_RATES)
    if usage["completion_tokens"] >= SHORT_OUTPUT_THRESHOLD:
        return {"cache_hit": "0.6", "cache_miss": "3", "output": "14"}
    return {"cache_hit": "0.4", "cache_miss": "2", "output": "8"}


class ZhipuMeter(Meter):
    """Reuse historical locking and summary arithmetic, with a separate profile."""

    def __init__(self, path: Path, api_key: str, model=MODEL, cap_yuan=30, *, transport=None):
        if model != MODEL:
            raise ValueError("Only the verified GLM-4.7 price schedule is supported")
        if not isinstance(api_key, str) or not api_key:
            raise ValueError("Zhipu API key is unavailable")
        self.cap = _decimal(cap_yuan)
        if not self.cap.is_finite() or not 0 < self.cap <= MAXIMUM_CAP_YUAN:
            raise ValueError("Comparison cap must be in (0, 30] CNY")
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.api_key, self.model, self.transport = api_key, model, transport
        self.calls, self.stopped, self.stop_reason = [], False, None
        self.configuration = {"model": self.model, "endpoint": ENDPOINT,
                              "cap_yuan": str(self.cap.normalize()), "pricing": json.loads(_json(PRICING)),
                              "thinking": {"type": "disabled"}, "maximum_output_tokens": 4096}
        self.configuration_sha256 = hashlib.sha256(_json(self.configuration).encode()).hexdigest()
        with self._lock():
            self._load()

    def _load(self):
        if not self.path.exists():
            return
        ledger = json.loads(self.path.read_text(encoding="utf-8"))
        if (ledger.get("schema_version") != 1 or ledger.get("model") != self.model
                or ledger.get("endpoint") != ENDPOINT or _decimal(ledger["cap_yuan"]) != self.cap
                or ledger.get("configuration_sha256") != self.configuration_sha256
                or ledger.get("configuration") != self.configuration
                or ledger.get("pricing") != self.configuration["pricing"]
                or ledger.get("maximum_cap_yuan") != 30):
            raise ValueError("Existing Zhipu comparison ledger configuration differs")
        self.calls = ledger["calls"]
        self.stop_reason = ledger.get("stop_reason")
        self.stopped = bool(self.stop_reason) or any(call["status"] != "confirmed" for call in self.calls)

    def _save(self):
        value = {"schema_version": 1, "model": self.model, "endpoint": ENDPOINT,
                 "cap_yuan": float(self.cap), "maximum_cap_yuan": float(MAXIMUM_CAP_YUAN),
                 "pricing": self.configuration["pricing"], "pricing_url": PRICING["source"],
                 "pricing_checked_date": PRICING["checked_at"], "cost_is_tariff_estimate": True,
                 "configuration": self.configuration, "configuration_sha256": self.configuration_sha256,
                 "stop_reason": self.stop_reason, "calls": self.calls, "summary": self.summary()}
        fd, temporary = tempfile.mkstemp(prefix=".zhipu-comparison-meter-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(value, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
            directory = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def send(self, payload, remaining_seconds, context):
        if not math.isfinite(remaining_seconds) or remaining_seconds <= 0:
            raise ValueError("A positive finite request deadline is required")
        estimate = estimate_request(payload)
        clean_payload = _redact(payload, self.api_key)
        if clean_payload != payload:
            raise ValueError("API keys must never appear in model input")
        with self._lock():
            self._load()
            if self.stopped:
                if self.stop_reason == "cost_cap":
                    raise CapExceeded("Comparison stopped at the CNY cap; request not sent")
                raise MeterStopped("Comparison ledger contains an unresolved request; no retry", uncertain=True)
            liability = _decimal(self.summary()["cap_liability_yuan"])
            if liability + _decimal(estimate["reservation_peak_yuan"]) > self.cap:
                self.stopped, self.stop_reason = True, "cost_cap"
                self._save()
                raise CapExceeded("Comparison reservation would exceed the CNY cap; request not sent")
            started = time.monotonic()
            record = {"index": len(self.calls) + 1, "context": _redact(context, self.api_key),
                      "started_at_utc": datetime.now(timezone.utc).isoformat(), "status": "pending",
                      "estimate": estimate, "request": clean_payload,
                      "request_sha256": hashlib.sha256(_json(payload).encode()).hexdigest()}
            self.calls.append(record)
            self._save()
            try:
                async def perform():
                    async with asyncio.timeout(remaining_seconds):
                        timeout = httpx.Timeout(min(30.0, remaining_seconds), connect=min(5.0, remaining_seconds))
                        transport = self.transport or httpx.AsyncHTTPTransport(retries=0)
                        async with httpx.AsyncClient(timeout=timeout, transport=transport,
                                trust_env=False, follow_redirects=False) as client:
                            async with client.stream("POST", ENDPOINT, json=payload,
                                    headers={"Authorization": "Bearer " + self.api_key}) as response:
                                record["http_status"] = response.status_code
                                if response.status_code != 200:
                                    raise ValueError("Non-success HTTP response")
                                parts, size = [], 0
                                async for chunk in response.aiter_bytes():
                                    size += len(chunk)
                                    if size > 256000:
                                        raise ValueError("Response-size limit")
                                    parts.append(chunk)
                                return json.loads(b"".join(parts))
                body = asyncio.run(perform())
                record["response"] = _redact(body, self.api_key)
                usage = normalize_usage(body)
                peak = (Decimal(usage["prompt_tokens"]) * Decimal(MAX_RATES["cache_miss"])
                        + Decimal(usage["completion_tokens"]) * Decimal(MAX_RATES["output"])) / MILLION
                if (usage["completion_tokens"] > estimate["max_output_tokens"]
                        or usage["prompt_tokens"] > estimate["conservative_input_tokens"]
                        or peak > _decimal(estimate["reservation_peak_yuan"])):
                    record["reservation_exceeded"] = True
                    raise ValueError("Provider usage exceeded the reserved bounds")
                rates = tariff_rates(usage)
                raw_usage = body["usage"]
                details = raw_usage.get("prompt_tokens_details") or {}
                cache_reported = (raw_usage.get("prompt_cache_hit_tokens") is not None or
                                  details.get("cached_tokens") is not None)
                record.update(status="confirmed", usage=usage, cache_usage_reported=cache_reported,
                              tariff_rates_yuan_per_million=rates,
                              tariff_yuan=float(_cost(usage, rates)), conservative_peak_yuan=float(peak))
                record["duration_ms"] = int((time.monotonic() - started) * 1000)
                self._save()
                return record["response"]
            except (httpx.HTTPError, TimeoutError, ValueError, RecursionError, OSError):
                record["status"] = "unknown"
                record["duration_ms"] = int((time.monotonic() - started) * 1000)
                record["error"] = "Request outcome or usage could not be confirmed; no automatic retry"
                self.stopped, self.stop_reason = True, "unknown_outcome"
                self._save()
                raise MeterStopped(record["error"], uncertain=True) from None
