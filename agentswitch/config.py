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
JUDGE_CRITERIA = (
    "addresses_task",
    "specific",
    "consistent",
    "complete",
    "meets_expectation",
)


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
    judge_usd: int | float
    max_attempts_per_round: int
    max_attempts_per_run: int
    admission_safety_factor: int | float


@dataclass(frozen=True)
class LimitsConfig:
    max_workers: int
    replan: str
    max_new_tasks: int
    max_nodes: int
    hard_repairs: int
    soft_repairs: int
    page_size: int
    projection_chars: int
    projection_total_chars: int


@dataclass(frozen=True)
class EvalsConfig:
    judge_model: str
    scale_max: int
    floor: int
    threshold: int | float
    weights: dict[str, int | float]


@dataclass(frozen=True)
class Config:
    path: str
    models: ModelConfig
    pricing: PricingConfig
    budgets: BudgetConfig
    limits: LimitsConfig
    evals: EvalsConfig
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

    root = _require_keys(
        raw, {"models", "pricing", "budgets", "limits", "evals"}, "config"
    )
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
            "judge_usd",
            "max_attempts_per_round",
            "max_attempts_per_run",
            "admission_safety_factor",
        },
        "budgets",
    )
    limits_raw = _require_keys(
        root["limits"],
        {
            "max_workers",
            "replan",
            "max_new_tasks",
            "max_nodes",
            "hard_repairs",
            "soft_repairs",
            "page_size",
            "projection_chars",
            "projection_total_chars",
        },
        "limits",
    )
    evals_raw = _require_keys(
        root["evals"],
        {"judge_model", "scale_max", "floor", "threshold", "weights"},
        "evals",
    )
    weights_raw = _require_keys(
        evals_raw["weights"], set(JUDGE_CRITERIA), "evals.weights"
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
        judge_usd=_number(
            budgets_raw["judge_usd"],
            "budgets.judge_usd",
            minimum=0,
            inclusive=False,
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
    max_workers = _integer(limits_raw["max_workers"], "limits.max_workers")
    if not 1 <= max_workers <= 8:
        raise ConfigError("limits.max_workers must be in the allowed range 1..8")
    replan = _string(limits_raw["replan"], "limits.replan")
    if replan not in {"frontier", "node"}:
        raise ConfigError('limits.replan must be one of "frontier" or "node"')
    max_new_tasks = _integer(
        limits_raw["max_new_tasks"], "limits.max_new_tasks", minimum=1
    )
    max_nodes = _integer(limits_raw["max_nodes"], "limits.max_nodes", minimum=1)
    if max_nodes < max_new_tasks:
        raise ConfigError("limits.max_nodes must be at least limits.max_new_tasks")
    projection_chars = _integer(
        limits_raw["projection_chars"], "limits.projection_chars", minimum=1
    )
    projection_total_chars = _integer(
        limits_raw["projection_total_chars"],
        "limits.projection_total_chars",
        minimum=1,
    )
    if projection_total_chars < projection_chars:
        raise ConfigError(
            "limits.projection_total_chars must be at least limits.projection_chars"
        )
    limits = LimitsConfig(
        max_workers=max_workers,
        replan=replan,
        max_new_tasks=max_new_tasks,
        max_nodes=max_nodes,
        hard_repairs=_integer(
            limits_raw["hard_repairs"], "limits.hard_repairs", minimum=0
        ),
        soft_repairs=_integer(
            limits_raw["soft_repairs"], "limits.soft_repairs", minimum=0
        ),
        page_size=_integer(limits_raw["page_size"], "limits.page_size", minimum=1),
        projection_chars=projection_chars,
        projection_total_chars=projection_total_chars,
    )
    scale_max = _integer(evals_raw["scale_max"], "evals.scale_max", minimum=1)
    floor = _integer(evals_raw["floor"], "evals.floor", minimum=0)
    if floor > scale_max:
        raise ConfigError("evals.floor must be at most evals.scale_max")
    threshold = _number(
        evals_raw["threshold"], "evals.threshold", minimum=0, inclusive=True
    )
    if threshold > scale_max:
        raise ConfigError("evals.threshold must be at most evals.scale_max")
    weights = {
        criterion: _number(
            weights_raw[criterion],
            f"evals.weights.{criterion}",
            minimum=0,
            inclusive=True,
        )
        for criterion in JUDGE_CRITERIA
    }
    if not any(weight > 0 for weight in weights.values()):
        raise ConfigError("evals.weights total must be greater than 0")
    evals = EvalsConfig(
        judge_model=_string(evals_raw["judge_model"], "evals.judge_model"),
        scale_max=scale_max,
        floor=floor,
        threshold=threshold,
        weights=weights,
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
            "judge_usd": budgets.judge_usd,
            "max_attempts_per_round": budgets.max_attempts_per_round,
            "max_attempts_per_run": budgets.max_attempts_per_run,
            "admission_safety_factor": budgets.admission_safety_factor,
        },
        "limits": {
            "max_workers": limits.max_workers,
            "replan": limits.replan,
            "max_new_tasks": limits.max_new_tasks,
            "max_nodes": limits.max_nodes,
            "hard_repairs": limits.hard_repairs,
            "soft_repairs": limits.soft_repairs,
            "page_size": limits.page_size,
            "projection_chars": limits.projection_chars,
            "projection_total_chars": limits.projection_total_chars,
        },
        "evals": {
            "judge_model": evals.judge_model,
            "scale_max": evals.scale_max,
            "floor": evals.floor,
            "threshold": evals.threshold,
            "weights": dict(evals.weights),
        },
    }
    canonical = json.dumps(values, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode()).hexdigest()
    return Config(
        path=str(selected_path),
        models=models,
        pricing=pricing,
        budgets=budgets,
        limits=limits,
        evals=evals,
        values=values,
        overrides=tuple(overrides),
        sha256=digest,
    )


__all__ = [
    "BudgetConfig",
    "Config",
    "ConfigError",
    "DEFAULT_CONFIG_PATH",
    "EvalsConfig",
    "JUDGE_CRITERIA",
    "LimitsConfig",
    "ModelConfig",
    "PricingConfig",
    "PricingRate",
    "load_config",
]
