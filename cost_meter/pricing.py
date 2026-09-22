"""Prices: loaded from a dated, sourced prices.json. No price lives in the code.

A model missing from prices.json is a blocking error that names it. A cost is never
silently zero.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, Iterable, Mapping, Optional

# Token categories, in the order they are reported everywhere.
TOKEN_FIELDS = ("input", "cache_write_5m", "cache_write_1h", "cache_read", "output")

# Keys of the cache multipliers in prices.json, applied to the model's input price.
CACHE_MULTIPLIER_FIELDS = ("cache_write_5m", "cache_write_1h", "cache_read")

# usage.speed values: absent on older lines, "standard", or "fast" (fast mode).
STANDARD_SPEEDS = (None, "standard")
FAST_SPEED = "fast"

# usage.inference_geo values billed at standard rates (global routing, or not reported).
STANDARD_GEOS = (None, "not_available", "global")

DEFAULT_PRICES_PATH = Path(__file__).resolve().parent.parent / "prices.json"

PER_TOKENS = 1_000_000


class PricingError(Exception):
    """prices.json cannot price a request: malformed file, unknown speed or geography."""


class UnknownModelError(PricingError):
    """One or more models have no entry in prices.json."""

    def __init__(self, models: Mapping[str, int], prices_path: str):
        self.models = dict(models)
        listed = ", ".join(f"{m} ({n} requests)" for m, n in sorted(self.models.items()))
        super().__init__(
            f"unknown model(s) in {prices_path}: {listed}. "
            "Add them from the provider's official pricing page; no cost is computed without a price."
        )


def _number(value, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise PricingError(f"{where}: expected a non-negative number, got {value!r}")
    return float(value)


class Prices:
    def __init__(self, data: dict, path: str):
        self.path = path
        for key in ("currency", "retrieved", "sources", "cache_multipliers", "models"):
            if key not in data:
                raise PricingError(f"{path}: missing key {key!r}")
        self.currency = data["currency"]
        self.retrieved = data["retrieved"]
        self.sources = data["sources"]
        self.cache_multipliers = {
            k: _number(data["cache_multipliers"].get(k), f"{path}: cache_multipliers.{k}")
            for k in CACHE_MULTIPLIER_FIELDS
        }
        self.geo_multipliers = {
            k: _number(v, f"{path}: inference_geo_multipliers.{k}")
            for k, v in data.get("inference_geo_multipliers", {}).items()
        }
        self.models: Dict[str, dict] = {}
        for model, entry in data["models"].items():
            where = f"{path}: models.{model}"
            parsed = {
                "input": _number(entry.get("input"), where + ".input"),
                "output": _number(entry.get("output"), where + ".output"),
                "cache_multipliers": dict(self.cache_multipliers),
                "fast": None,
            }
            for k, v in entry.get("cache_multipliers", {}).items():
                if k not in CACHE_MULTIPLIER_FIELDS:
                    raise PricingError(f"{where}.cache_multipliers: unknown key {k!r}")
                parsed["cache_multipliers"][k] = _number(v, f"{where}.cache_multipliers.{k}")
            if "fast" in entry:
                parsed["fast"] = {
                    "input": _number(entry["fast"].get("input"), where + ".fast.input"),
                    "output": _number(entry["fast"].get("output"), where + ".fast.output"),
                }
            self.models[model] = parsed
        self.aliases = dict(data.get("aliases", {}))
        for alias, target in self.aliases.items():
            if target not in self.models:
                raise PricingError(f"{path}: alias {alias!r} points to unlisted model {target!r}")

    def resolve(self, model: str) -> Optional[dict]:
        return self.models.get(self.aliases.get(model, model))

    def check_models(self, counts: Mapping[str, int]) -> None:
        """Raise UnknownModelError naming every model of `counts` that has no price."""
        unknown = {m: n for m, n in counts.items() if self.resolve(m) is None}
        if unknown:
            raise UnknownModelError(unknown, self.path)

    def cost(self, model: str, tokens: Mapping[str, int], speed: Optional[str] = None,
             inference_geo: Optional[str] = None) -> float:
        entry = self.resolve(model)
        if entry is None:
            raise UnknownModelError({model: 1}, self.path)
        if speed in STANDARD_SPEEDS:
            base_in, base_out = entry["input"], entry["output"]
        elif speed == FAST_SPEED:
            if entry["fast"] is None:
                raise PricingError(f"{self.path}: no fast-mode price for {model}")
            base_in, base_out = entry["fast"]["input"], entry["fast"]["output"]
        else:
            raise PricingError(f"unknown usage.speed {speed!r} for {model}")
        if inference_geo in STANDARD_GEOS:
            geo = 1.0
        elif inference_geo in self.geo_multipliers:
            geo = self.geo_multipliers[inference_geo]
        else:
            raise PricingError(f"{self.path}: no multiplier for inference_geo {inference_geo!r}")
        mult = entry["cache_multipliers"]
        usd = (
            tokens["input"] * base_in
            + tokens["cache_write_5m"] * base_in * mult["cache_write_5m"]
            + tokens["cache_write_1h"] * base_in * mult["cache_write_1h"]
            + tokens["cache_read"] * base_in * mult["cache_read"]
            + tokens["output"] * base_out
        )
        return usd * geo / PER_TOKENS


def load_prices(path: Optional[str] = None) -> Prices:
    p = Path(path) if path else DEFAULT_PRICES_PATH
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise PricingError(f"prices file not found: {p}") from None
    except json.JSONDecodeError as exc:
        raise PricingError(f"{p}: invalid JSON ({exc})") from None
    return Prices(data, str(p))


def empty_tokens() -> Dict[str, int]:
    return {k: 0 for k in TOKEN_FIELDS}


def add_tokens(into: Dict[str, int], other: Mapping[str, int]) -> None:
    for k in TOKEN_FIELDS:
        into[k] += other[k]


def total_tokens(tokens: Mapping[str, int], fields: Iterable[str] = TOKEN_FIELDS) -> int:
    return sum(tokens[k] for k in fields)
