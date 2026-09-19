"""Strict loading and recording of AgentSwitch runtime configuration."""

from __future__ import annotations

import hashlib
import json
import math
import os
import tomllib
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .mcp_client import _read_env_file

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "agentswitch.toml"


class ConfigError(ValueError):
    """The configuration file or one of its values is invalid."""


@dataclass(frozen=True)
class ModelConfig:
    agent: str
    reasoning_effort: str
    seed: int
    max_tokens: int
    timeout_seconds: int | float


@dataclass(frozen=True)
class PricingRate:
    input_usd_per_million: int | float
    output_usd_per_million: int | float


@dataclass(frozen=True)
class PricingConfig:
    default: PricingRate
    models: dict[str, PricingRate]


@dataclass(frozen=True)
class BudgetConfig:
    run_usd: int | float
    max_attempts_per_round: int
    max_attempts_per_run: int
    admission_safety_factor: int | float


@dataclass(frozen=True)
class Config:
    path: str
    models: ModelConfig
    pricing: PricingConfig
    budgets: BudgetConfig
    values: dict[str, Any]
    overrides: tuple[dict[str, str], ...]
    sha256: str

    def pricing_for(self, model: str | None = None) -> tuple[str, PricingRate]:
        """Return the selected pricing row name and rates for a model."""
        selected = self.models.agent if model is None else model
        if selected in self.pricing.models:
            return f"pricing.models.{selected}", self.pricing.models[selected]
        return "pricing.default", self.pricing.default

    def effective_record(self) -> dict[str, Any]:
        """Return the secret-free, JSON-serialisable effective configuration."""
        return {
            "path": self.path,
            "sha256": self.sha256,
            "values": deepcopy(self.values),
            "overrides": [dict(item) for item in self.overrides],
        }


def _require_keys(value: Any, expected: set[str], key: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{key} must be a table")
    missing = expected - value.keys()
    if missing:
        name = sorted(missing)[0]
        raise ConfigError(f"missing key {key}.{name}")
    unknown = value.keys() - expected
    if unknown:
        name = sorted(unknown)[0]
        raise ConfigError(f"unknown key {key}.{name}")
    return value


def _string(value: Any, key: str) -> str:
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{key} must be a non-empty string")
    return value


def _integer(value: Any, key: str, *, minimum: int | None = None) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ConfigError(f"{key} must be an integer")
    if minimum is not None and value < minimum:
        raise ConfigError(f"{key} must be at least {minimum}")
    return value


def _number(value: Any, key: str, *, minimum: float, inclusive: bool) -> int | float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ConfigError(f"{key} must be a number")
    if isinstance(value, float) and not math.isfinite(value):
        raise ConfigError(f"{key} must be finite")
    valid = value >= minimum if inclusive else value > minimum
    if not valid:
        comparison = "at least" if inclusive else "greater than"
        raise ConfigError(f"{key} must be {comparison} {minimum:g}")
    return value


def _pricing_rate(value: Any, key: str) -> PricingRate:
    row = _require_keys(
        value,
        {"input_usd_per_million", "output_usd_per_million"},
        key,
    )
    return PricingRate(
        input_usd_per_million=_number(
            row["input_usd_per_million"],
            f"{key}.input_usd_per_million",
            minimum=0,
            inclusive=True,
        ),
        output_usd_per_million=_number(
            row["output_usd_per_million"],
            f"{key}.output_usd_per_million",
            minimum=0,
            inclusive=True,
        ),
    )


def load_config(
    path: str | Path = DEFAULT_CONFIG_PATH,
    *,
    env_file: str | Path,
) -> Config:
    """Load a strict TOML configuration and apply the one supported override."""
    selected_path = Path(path)
    try:
        with selected_path.open("rb") as stream:
            raw = tomllib.load(stream)
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as error:
        raise ConfigError(f"cannot load config {selected_path}: {error}") from None

    root = _require_keys(raw, {"models", "pricing", "budgets"}, "config")
    models_raw = _require_keys(
        root["models"],
        {"agent", "reasoning_effort", "seed", "max_tokens", "timeout_seconds"},
        "models",
    )
    pricing_raw = _require_keys(root["pricing"], {"default", "models"}, "pricing")
    pricing_models_raw = pricing_raw["models"]
    if not isinstance(pricing_models_raw, dict):
        raise ConfigError("pricing.models must be a table")
    budgets_raw = _require_keys(
        root["budgets"],
        {
            "run_usd",
            "max_attempts_per_round",
            "max_attempts_per_run",
            "admission_safety_factor",
        },
        "budgets",
    )

    agent = _string(models_raw["agent"], "models.agent")
    overrides: list[dict[str, str]] = []
    if "OPENROUTER_MODEL" in os.environ:
        agent = _string(os.environ["OPENROUTER_MODEL"], "OPENROUTER_MODEL")
        overrides.append({"key": "models.agent", "source": "env"})
    else:
        try:
            file_values = _read_env_file(str(env_file))
        except (OSError, UnicodeError) as error:
            raise ConfigError(f"cannot load env file {env_file}: {error}") from None
        if "OPENROUTER_MODEL" in file_values:
            agent = _string(file_values["OPENROUTER_MODEL"], "OPENROUTER_MODEL")
            overrides.append({"key": "models.agent", "source": "env_file"})

    models = ModelConfig(
        agent=agent,
        reasoning_effort=_string(models_raw["reasoning_effort"], "models.reasoning_effort"),
        seed=_integer(models_raw["seed"], "models.seed"),
        max_tokens=_integer(models_raw["max_tokens"], "models.max_tokens", minimum=1),
        timeout_seconds=_number(
            models_raw["timeout_seconds"],
            "models.timeout_seconds",
            minimum=0,
            inclusive=False,
        ),
    )
    pricing = PricingConfig(
        default=_pricing_rate(pricing_raw["default"], "pricing.default"),
        models={
            model_id: _pricing_rate(row, f"pricing.models.{model_id}")
            for model_id, row in pricing_models_raw.items()
            if _string(model_id, "pricing.models model id")
        },
    )
    budgets = BudgetConfig(
        run_usd=_number(
            budgets_raw["run_usd"], "budgets.run_usd", minimum=0, inclusive=False
        ),
        max_attempts_per_round=_integer(
            budgets_raw["max_attempts_per_round"],
            "budgets.max_attempts_per_round",
            minimum=1,
        ),
        max_attempts_per_run=_integer(
            budgets_raw["max_attempts_per_run"],
            "budgets.max_attempts_per_run",
            minimum=1,
        ),
        admission_safety_factor=_number(
            budgets_raw["admission_safety_factor"],
            "budgets.admission_safety_factor",
            minimum=1,
            inclusive=True,
        ),
    )
    values = {
        "models": {
            "agent": models.agent,
            "reasoning_effort": models.reasoning_effort,
            "seed": models.seed,
            "max_tokens": models.max_tokens,
            "timeout_seconds": models.timeout_seconds,
        },
        "pricing": {
            "default": {
                "input_usd_per_million": pricing.default.input_usd_per_million,
                "output_usd_per_million": pricing.default.output_usd_per_million,
            },
            "models": {
                model_id: {
                    "input_usd_per_million": row.input_usd_per_million,
                    "output_usd_per_million": row.output_usd_per_million,
                }
                for model_id, row in pricing.models.items()
            },
        },
        "budgets": {
            "run_usd": budgets.run_usd,
            "max_attempts_per_round": budgets.max_attempts_per_round,
            "max_attempts_per_run": budgets.max_attempts_per_run,
            "admission_safety_factor": budgets.admission_safety_factor,
        },
    }
    canonical = json.dumps(values, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode()).hexdigest()
    return Config(
        path=str(selected_path),
        models=models,
        pricing=pricing,
        budgets=budgets,
        values=values,
        overrides=tuple(overrides),
        sha256=digest,
    )


__all__ = [
    "BudgetConfig",
    "Config",
    "ConfigError",
    "DEFAULT_CONFIG_PATH",
    "ModelConfig",
    "PricingConfig",
    "PricingRate",
    "load_config",
]
