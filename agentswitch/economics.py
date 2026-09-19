"""Budget admission, retries, and cost accounting for model calls."""

from __future__ import annotations

import time
from copy import deepcopy
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Any

from .config import Config
from .llm_client import OpenRouterError, OpenRouterRetryable

CHARS_PER_TOKEN = 2.0
_MICRO_PER_USD = Decimal(1_000_000)


class BudgetExceeded(Exception):
    """A model attempt was refused by a configured budget or attempt ceiling."""

    def __init__(
        self,
        *,
        reason: str,
        estimate_micro: int,
        remaining_micro: int,
        round: int,
        attempt: int,
    ) -> None:
        self.reason = reason
        self.estimate_micro = estimate_micro
        self.remaining_micro = remaining_micro
        self.round = round
        self.attempt = attempt
        super().__init__(
            f"model attempt refused: {reason} "
            f"(estimate_micro={estimate_micro}, remaining_micro={remaining_micro}, "
            f"round={round}, attempt={attempt})"
        )

    def details(self) -> dict[str, Any]:
        return {
            "reason": self.reason,
            "estimate_micro": self.estimate_micro,
            "remaining_micro": self.remaining_micro,
            "round": self.round,
            "attempt": self.attempt,
        }


def _decimal(value: int | float) -> Decimal:
    return Decimal(str(value))


def _ceil(value: Decimal) -> int:
    return int(value.to_integral_value(rounding=ROUND_CEILING))


def _valid_count(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _reasoning_tokens(usage: dict[str, Any]) -> int | None:
    direct = _valid_count(usage.get("reasoning_tokens"))
    if direct is not None:
        return direct
    details = usage.get("completion_tokens_details")
    if isinstance(details, dict):
        return _valid_count(details.get("reasoning_tokens"))
    return None


class MeteredClient:
    """Apply budget policy and retry accounting around a single-attempt client."""

    def __init__(self, client: Any, config: Config, *, sleep: Any = time.sleep) -> None:
        self._client = client
        self._config = config
        self._sleep = sleep
        self.model = client.model
        self.max_tokens = config.models.max_tokens
        self._pricing_row, self._pricing = config.pricing_for(self.model)
        self._budget_micro = int(
            (_decimal(config.budgets.run_usd) * _MICRO_PER_USD).to_integral_value(
                rounding=ROUND_FLOOR
            )
        )
        self._spent_micro = 0
        self._rounds = 0
        self._attempts = 0
        self._entries: list[dict[str, Any]] = []
        self._refusal: dict[str, Any] | None = None

    def redaction_secret(self) -> str:
        return self._client.redaction_secret()

    def _estimate(self, body: bytes) -> tuple[int, int]:
        factor = _decimal(self._config.budgets.admission_safety_factor)
        estimated_input_tokens = _ceil(
            Decimal(len(body)) / _decimal(CHARS_PER_TOKEN) * factor
        )
        estimate_micro = _ceil(
            Decimal(estimated_input_tokens)
            * _decimal(self._pricing.input_usd_per_million)
            + Decimal(self.max_tokens)
            * _decimal(self._pricing.output_usd_per_million)
        )
        return estimated_input_tokens, estimate_micro

    def _remaining(self) -> int:
        return self._budget_micro - self._spent_micro

    def _refuse(
        self,
        *,
        reason: str,
        round_number: int,
        attempt: int,
        estimated_input_tokens: int,
        estimate_micro: int,
    ) -> None:
        error = BudgetExceeded(
            reason=reason,
            estimate_micro=estimate_micro,
            remaining_micro=self._remaining(),
            round=round_number,
            attempt=attempt,
        )
        self._refusal = error.details()
        self._entries.append(
            {
                "round": round_number,
                "attempt": attempt,
                "status": "refused",
                "outcome": "error",
                "error_type": type(error).__name__,
                "model": {"requested": self.model, "returned": None},
                "finish_reason": None,
                "prompt_tokens": None,
                "completion_tokens": None,
                "reasoning_tokens": None,
                "estimated_input_tokens": estimated_input_tokens,
                "estimate_micro": estimate_micro,
                "provider_cost_micro": None,
                "config_cost_micro": None,
                "difference_micro": None,
                "charged_micro": 0,
                "overrun_micro": 0,
                "remaining_micro_after": self._remaining(),
            }
        )
        raise error

    def _admit(
        self,
        *,
        round_number: int,
        attempt: int,
        estimated_input_tokens: int,
        estimate_micro: int,
    ) -> None:
        if self._attempts >= self._config.budgets.max_attempts_per_run:
            self._refuse(
                reason="attempts_per_run",
                round_number=round_number,
                attempt=attempt,
                estimated_input_tokens=estimated_input_tokens,
                estimate_micro=estimate_micro,
            )
        if estimate_micro > self._remaining():
            self._refuse(
                reason="budget",
                round_number=round_number,
                attempt=attempt,
                estimated_input_tokens=estimated_input_tokens,
                estimate_micro=estimate_micro,
            )

    def _would_admit(self, estimate_micro: int) -> bool:
        return (
            self._attempts < self._config.budgets.max_attempts_per_run
            and estimate_micro <= self._remaining()
        )

    def _charge(
        self,
        *,
        round_number: int,
        attempt: int,
        estimated_input_tokens: int,
        estimate_micro: int,
        response: dict[str, Any] | None,
        error: Exception | None,
    ) -> None:
        raw_usage = response.get("usage") if response is not None else getattr(error, "usage", None)
        usage = raw_usage if isinstance(raw_usage, dict) else {}
        prompt_tokens = _valid_count(usage.get("prompt_tokens"))
        completion_tokens = _valid_count(usage.get("completion_tokens"))
        reasoning_tokens = _reasoning_tokens(usage)

        config_cost_micro: int | None = None
        if prompt_tokens is not None and completion_tokens is not None:
            config_cost_micro = _ceil(
                Decimal(prompt_tokens) * _decimal(self._pricing.input_usd_per_million)
                + Decimal(completion_tokens) * _decimal(self._pricing.output_usd_per_million)
            )

        provider_cost_micro: int | None = None
        cost = usage.get("cost")
        if isinstance(cost, (int, float)) and not isinstance(cost, bool):
            decimal_cost = Decimal(str(cost))
            if decimal_cost.is_finite() and decimal_cost >= 0:
                provider_cost_micro = _ceil(decimal_cost * _MICRO_PER_USD)

        if provider_cost_micro is not None:
            charged_micro = provider_cost_micro
            status = "charged"
        elif config_cost_micro is not None:
            charged_micro = config_cost_micro
            status = "charged"
        else:
            charged_micro = estimate_micro
            status = "charged_reservation"
        difference_micro = (
            provider_cost_micro - config_cost_micro
            if provider_cost_micro is not None and config_cost_micro is not None
            else None
        )
        self._spent_micro += charged_micro
        self._entries.append(
            {
                "round": round_number,
                "attempt": attempt,
                "status": status,
                "outcome": (
                    "ok"
                    if error is None
                    else "retryable_error"
                    if isinstance(error, OpenRouterRetryable)
                    else "error"
                ),
                "error_type": type(error).__name__ if error is not None else None,
                "model": {
                    "requested": self.model,
                    "returned": response.get("model") if response is not None else None,
                },
                "finish_reason": response.get("finish_reason") if response is not None else None,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "reasoning_tokens": reasoning_tokens,
                "estimated_input_tokens": estimated_input_tokens,
                "estimate_micro": estimate_micro,
                "provider_cost_micro": provider_cost_micro,
                "config_cost_micro": config_cost_micro,
                "difference_micro": difference_micro,
                "charged_micro": charged_micro,
                "overrun_micro": max(0, charged_micro - estimate_micro),
                "remaining_micro_after": self._remaining(),
            }
        )

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: Any = None,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._rounds += 1
        round_number = self._rounds
        body = self._client.build_body(
            messages,
            tools=tools,
            tool_choice=tool_choice,
            extra=extra,
        )
        estimated_input_tokens, estimate_micro = self._estimate(body)

        for attempt in range(1, self._config.budgets.max_attempts_per_round + 1):
            self._admit(
                round_number=round_number,
                attempt=attempt,
                estimated_input_tokens=estimated_input_tokens,
                estimate_micro=estimate_micro,
            )
            self._attempts += 1
            response: dict[str, Any] | None = None
            error: Exception | None = None
            try:
                response = self._client.chat(
                    messages,
                    tools=tools,
                    tool_choice=tool_choice,
                    extra=extra,
                )
            except Exception as caught:
                error = caught
            self._charge(
                round_number=round_number,
                attempt=attempt,
                estimated_input_tokens=estimated_input_tokens,
                estimate_micro=estimate_micro,
                response=response,
                error=error,
            )
            if error is None:
                assert response is not None
                return response
            if not isinstance(error, OpenRouterRetryable):
                raise error
            if attempt == self._config.budgets.max_attempts_per_round:
                raise OpenRouterError(str(error)) from None
            if self._would_admit(estimate_micro):
                self._sleep(2 ** (attempt - 1))

        raise AssertionError("retry loop terminated unexpectedly")

    def ledger(self) -> dict[str, Any]:
        """Return a detached, JSON-serialisable accounting snapshot."""
        return {
            "entries": deepcopy(self._entries),
            "summary": {
                "budget_micro": self._budget_micro,
                "spent_micro": self._spent_micro,
                "remaining_micro": self._remaining(),
                "rounds": self._rounds,
                "attempts": self._attempts,
                "refused": self._refusal is not None,
                "refusal": deepcopy(self._refusal),
                "pricing_row": self._pricing_row,
                "chars_per_token": CHARS_PER_TOKEN,
                "max_attempts_per_round": self._config.budgets.max_attempts_per_round,
                "max_attempts_per_run": self._config.budgets.max_attempts_per_run,
            },
        }


__all__ = ["BudgetExceeded", "CHARS_PER_TOKEN", "MeteredClient"]
