"""No-retry DeepSeek transport with a durable, conservative CNY spending ledger.

Prices were checked on 2026-11-01 at the official pricing page. The request
reservation uses peak cache-miss prices, UTF-8 payload bytes plus 4096 tokens
for framing, and the complete requested output allowance. Confirmed usage is
also charged at peak prices for the cap, even during off-peak periods.
"""
import asyncio
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
import time
from zoneinfo import ZoneInfo

import httpx

from patchtrail.providers import ProviderError


PRICING_URL = "https://api-docs.deepseek.com/zh-cn/quick_start/pricing/"
ENDPOINT = "https://api.deepseek.com/chat/completions"
PEAK_RATES = {"cache_hit": "0.04", "cache_miss": "2", "output": "8"}
OFFPEAK_RATES = {"cache_hit": "0.02", "cache_miss": "1", "output": "4"}
MILLION = Decimal(1000000)


class MeterStopped(ProviderError):
    """Further dispatch is forbidden until an operator reconciles the ledger."""


class CapExceeded(ProviderError):
    """A request was refused before dispatch because its reservation exceeds cap."""


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _decimal(value):
    return Decimal(str(value))


def estimate_request(payload):
    """Return a dry-run heuristic and deliberately generous cost reservation.

    The heuristic is not a tokenizer result. The reservation assumes no more
    than one input token per UTF-8 payload byte plus 4096 framing tokens.
    """
    maximum = payload.get("max_tokens")
    if type(maximum) is not int or not 1 <= maximum <= 4096:
        raise ValueError("Benchmark requests require max_tokens in 1..4096")
    encoded = _json(payload).encode("utf-8")
    bound = len(encoded) + 4096
    reserve = (Decimal(bound) * Decimal(PEAK_RATES["cache_miss"])
               + Decimal(maximum) * Decimal(PEAK_RATES["output"])) / MILLION
    return {"estimated_input_tokens": math.ceil(len(encoded) / 4),
            "estimate_method": "UTF-8 JSON bytes / 4; heuristic, not tokenizer",
            "request_utf8_bytes": len(encoded),
            "conservative_input_tokens": bound, "max_output_tokens": maximum,
            "reservation_peak_yuan": float(reserve)}


def _redact(value, secret):
    if isinstance(value, str):
        cleaned = value.replace(secret, "[REDACTED]") if secret else value
        # Tool arguments contain a second JSON layer, possibly with the key
        # represented as Unicode escapes. Never leave that layer unredacted.
        try:
            parsed = json.loads(cleaned)
        except (TypeError, ValueError, RecursionError):
            return cleaned
        if isinstance(parsed, (dict, list)):
            redacted = _redact(parsed, secret)
            if redacted != parsed:
                return json.dumps(redacted, ensure_ascii=False)
        return cleaned
    if isinstance(value, list):
        return [_redact(item, secret) for item in value]
    if isinstance(value, dict):
        return {_redact(key, secret): _redact(item, secret) for key, item in value.items()}
    return value


def tariff_rates(started_at):
    """Weekday holidays conservatively use peak rates; weekends are off-peak."""
    beijing = datetime.fromisoformat(started_at).astimezone(ZoneInfo("Asia/Shanghai"))
    peak = beijing.weekday() < 5 and (9 <= beijing.hour < 12 or 14 <= beijing.hour < 18)
    return dict(PEAK_RATES if peak else OFFPEAK_RATES)


def _cost(usage, rates):
    hit = usage["prompt_cache_hit_tokens"]
    miss = usage["prompt_tokens"] - hit
    return ((Decimal(hit) * Decimal(rates["cache_hit"]))
            + (Decimal(miss) * Decimal(rates["cache_miss"]))
            + (Decimal(usage["completion_tokens"]) * Decimal(rates["output"]))) / MILLION


def _usage(body):
    usage = body.get("usage") if isinstance(body, dict) else None
    if not isinstance(usage, dict):
        raise ValueError("Missing usage")
    keys = ("prompt_tokens", "completion_tokens", "total_tokens", "prompt_cache_hit_tokens")
    if any(type(usage.get(key)) is not int or usage[key] < 0 for key in keys):
        raise ValueError("Incomplete usage")
    if (usage["total_tokens"] != usage["prompt_tokens"] + usage["completion_tokens"]
            or usage["prompt_cache_hit_tokens"] > usage["prompt_tokens"]):
        raise ValueError("Inconsistent usage")
    clean = {key: usage[key] for key in keys}
    clean["prompt_cache_miss_tokens"] = usage["prompt_tokens"] - usage["prompt_cache_hit_tokens"]
    return clean


class Meter:
    def __init__(self, path: Path, api_key: str, model="deepseek-flash", cap_yuan=8, *, transport=None):
        if model != "deepseek-flash":
            raise ValueError("Only the verified deepseek-flash price schedule is supported")
        if not api_key:
            raise ValueError("DeepSeek API key is unavailable")
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.api_key = api_key
        self.model = model
        self.cap = _decimal(cap_yuan)
        if not self.cap.is_finite() or not 0 < self.cap <= 8:
            raise ValueError("Benchmark spending cap must be in (0, 8] CNY")
        self.transport = transport
        self.calls = []
        self.stopped = False
        self.stop_reason = None
        with self._lock():
            self._load()

    @contextmanager
    def _lock(self):
        lock = self.path.with_suffix(self.path.suffix + ".lock")
        with lock.open("a+b") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def _load(self):
        if self.path.exists():
            ledger = json.loads(self.path.read_text(encoding="utf-8"))
            if (ledger.get("schema_version") != 1 or ledger.get("model") != self.model
                    or _decimal(ledger["cap_yuan"]) != self.cap):
                raise ValueError("Existing benchmark ledger configuration differs")
            self.calls = ledger["calls"]
            self.stop_reason = ledger.get("stop_reason")
            self.stopped = bool(self.stop_reason) or any(call["status"] != "confirmed" for call in self.calls)

    def _save(self):
        value = {"schema_version": 1, "model": self.model, "cap_yuan": float(self.cap),
                 "pricing_url": PRICING_URL, "pricing_checked_date": "2026-11-01",
                 "cost_is_tariff_estimate": True,
                 "stop_reason": self.stop_reason,
                 "holiday_policy": "Weekday holidays conservatively charged at peak rates",
                 "calls": self.calls, "summary": self.summary()}
        fd, temporary = tempfile.mkstemp(prefix=".benchmark-meter-", dir=self.path.parent)
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

    def summary(self):
        confirmed = [call for call in self.calls if call["status"] == "confirmed"]
        unknown = [call for call in self.calls if call["status"] != "confirmed"]
        peak = sum((_decimal(call["conservative_peak_yuan"]) for call in confirmed), Decimal(0))
        reserved = sum((_decimal(call["estimate"]["reservation_peak_yuan"]) for call in unknown), Decimal(0))
        return {"dispatched_calls": len(self.calls), "confirmed_calls": len(confirmed),
                "unknown_calls": len(unknown), "tariff_yuan": float(sum(
                    (_decimal(call["tariff_yuan"]) for call in confirmed), Decimal(0))),
                "conservative_peak_yuan": float(peak), "unresolved_reserved_yuan": float(reserved),
                "cap_liability_yuan": float(peak + reserved), "cap_yuan": float(self.cap),
                "stopped": self.stopped, "cost_is_tariff_estimate": True}

    def send(self, payload, remaining_seconds, context):
        if payload.get("model") != self.model:
            raise ValueError("Request model differs from the metered model")
        if not math.isfinite(remaining_seconds) or remaining_seconds <= 0:
            raise ValueError("A positive finite request deadline is required")
        estimate = estimate_request(payload)
        with self._lock():
            self._load()
            if self.stopped:
                if self.stop_reason == "cost_cap":
                    raise CapExceeded("Benchmark stopped at the CNY cap; request not sent")
                raise MeterStopped("Benchmark ledger contains an unresolved request; no retry", uncertain=True)
            liability = _decimal(self.summary()["cap_liability_yuan"])
            if liability + _decimal(estimate["reservation_peak_yuan"]) > self.cap:
                self.stopped = True
                self.stop_reason = "cost_cap"
                self._save()
                raise CapExceeded("Benchmark cost reservation would exceed the CNY cap; request not sent")
            started = time.monotonic()
            record = {"index": len(self.calls) + 1, "context": _redact(context, self.api_key),
                      "started_at_utc": datetime.now(timezone.utc).isoformat(),
                      "status": "pending", "estimate": estimate,
                      "request": _redact(payload, self.api_key),
                      "request_sha256": hashlib.sha256(_json(payload).encode()).hexdigest()}
            self.calls.append(record)
            self._save()  # The dispatch identity and reservation survive a crash.
            try:
                async def perform():
                    async with asyncio.timeout(remaining_seconds):
                        timeout = httpx.Timeout(min(30.0, remaining_seconds),
                                                connect=min(5.0, remaining_seconds))
                        transport = self.transport or httpx.AsyncHTTPTransport(retries=0)
                        async with httpx.AsyncClient(timeout=timeout, transport=transport,
                                                     trust_env=False, follow_redirects=False) as client:
                            async with client.stream("POST", ENDPOINT, json=payload,
                                                     headers={"Authorization": "Bearer " + self.api_key}) as response:
                                record["http_status"] = response.status_code
                                if response.status_code != 200:
                                    raise ValueError("Non-success HTTP response")
                                parts, size = [], 0
                                async for part in response.aiter_bytes():
                                    size += len(part)
                                    if size > 256000:
                                        raise ValueError("Response-size limit")
                                    parts.append(part)
                                return json.loads(b"".join(parts))
                body = asyncio.run(perform())
                record["response"] = _redact(body, self.api_key)
                usage = _usage(body)
                peak_cost = _cost(usage, PEAK_RATES)
                if peak_cost > _decimal(estimate["reservation_peak_yuan"]):
                    # Do not dispatch again if the documented reservation
                    # assumption was violated by provider token accounting.
                    record["reservation_exceeded"] = True
                    raise ValueError("Provider usage exceeded reservation")
                record.update(status="confirmed", usage=usage,
                              tariff_rates_yuan_per_million=tariff_rates(record["started_at_utc"]),
                              tariff_yuan=float(_cost(usage, tariff_rates(record["started_at_utc"]))),
                              conservative_peak_yuan=float(peak_cost))
                record["duration_ms"] = int((time.monotonic() - started) * 1000)
                self._save()
                return record["response"]
            except (httpx.HTTPError, TimeoutError, ValueError, RecursionError, OSError):
                record["status"] = "unknown"
                record["duration_ms"] = int((time.monotonic() - started) * 1000)
                record["error"] = "Request outcome or usage could not be confirmed; no automatic retry"
                self.stopped = True
                self.stop_reason = "unknown_outcome"
                self._save()
                raise MeterStopped(record["error"], uncertain=True) from None
