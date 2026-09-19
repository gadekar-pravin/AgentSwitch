"""Offline tests for phase 2 configuration and model-call economics."""

from __future__ import annotations

import json
from datetime import date
from urllib.error import URLError

import pytest

from agentswitch import agent
from agentswitch.agent import AgentError, run_agent
from agentswitch.config import ConfigError, load_config
from agentswitch.economics import BudgetExceeded, MeteredClient
from agentswitch.llm_client import OpenRouterClient, OpenRouterError, from_config


def _toml(
    *,
    model: str = "test/model",
    run_usd: str = "0.01",
    judge_usd: str = "0.005",
    max_tokens: int = 10,
    attempts_per_round: int = 3,
    attempts_per_run: int = 10,
    input_price: str = "1.0",
    output_price: str = "1.0",
) -> str:
    return f'''[models]
agent = "{model}"
reasoning_effort = "medium"
seed = 17
max_tokens = {max_tokens}
timeout_seconds = 4.0

[pricing.default]
input_usd_per_million = 2.0
output_usd_per_million = 3.0

[pricing.models."test/model"]
input_usd_per_million = {input_price}
output_usd_per_million = {output_price}

[budgets]
run_usd = {run_usd}
judge_usd = {judge_usd}
max_attempts_per_round = {attempts_per_round}
max_attempts_per_run = {attempts_per_run}
admission_safety_factor = 1.0

[limits]
max_workers = 1
replan = "frontier"
max_new_tasks = 4
max_nodes = 8
hard_repairs = 2
soft_repairs = 1
page_size = 100
projection_chars = 1000
projection_total_chars = 4000

[evals]
judge_model = "judge/model"
scale_max = 2
floor = 1
threshold = 1.5

[evals.weights]
addresses_task = 1
specific = 1
consistent = 1
complete = 1
meets_expectation = 1
'''


def _config(tmp_path, monkeypatch, **changes):
    monkeypatch.delenv("OPENROUTER_MODEL", raising=False)
    path = tmp_path / "config.toml"
    path.write_text(_toml(**changes), encoding="utf-8")
    return load_config(path, env_file=tmp_path / "missing.env")


def _success(*, usage=None, model: str = "returned/model") -> bytes:
    response = {
        "choices": [
            {
                "message": {"role": "assistant", "content": "done"},
                "finish_reason": "stop",
            }
        ],
        "model": model,
    }
    if usage is not None:
        response["usage"] = usage
    return json.dumps(response).encode()


def _choice_error(*, usage=None) -> bytes:
    response = {
        "choices": [{"finish_reason": "error", "error": {"message": "provider failed"}}]
    }
    if usage is not None:
        response["usage"] = usage
    return json.dumps(response).encode()


class ScriptedTransport:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def __call__(self, url, body, headers, timeout):
        self.calls.append((url, body, headers, timeout))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _raw(config, transport):
    return OpenRouterClient(
        "secret-key",
        config.models.agent,
        reasoning_effort=config.models.reasoning_effort,
        seed=config.models.seed,
        max_tokens=config.models.max_tokens,
        timeout=float(config.models.timeout_seconds),
        transport=transport,
    )


def test_retry_429_then_success_is_metered(tmp_path, monkeypatch):
    """Spec: AI (Codex) A 429 then success charges both attempts and sleeps once."""
    config = _config(tmp_path, monkeypatch)
    transport = ScriptedTransport(
        [
            (429, b'{"error":"busy"}'),
            (200, _success(usage={"cost": 0.000004})),
        ]
    )
    sleeps = []
    client = MeteredClient(_raw(config, transport), config, sleep=sleeps.append)

    result = client.chat([{"role": "user", "content": "hello"}])

    assert result["message"]["content"] == "done"
    assert sleeps == [1]
    entries = client.ledger()["entries"]
    assert [entry["status"] for entry in entries] == ["charged_reservation", "charged"]
    assert [entry["outcome"] for entry in entries] == ["retryable_error", "ok"]


def test_choice_error_then_success_retries_and_charges_both_attempts(
    tmp_path, monkeypatch
):
    """Spec: AI (Codex) A provider choice error is charged and retried in one round."""
    config = _config(tmp_path, monkeypatch)
    transport = ScriptedTransport(
        [
            (200, _choice_error(usage={"cost": 0.000004})),
            (200, _success(usage={"cost": 0.000005})),
        ]
    )
    client = MeteredClient(_raw(config, transport), config, sleep=lambda _: None)

    result = client.chat([{"role": "user", "content": "hello"}])

    assert result["message"]["content"] == "done"
    entries = client.ledger()["entries"]
    assert [(entry["round"], entry["attempt"]) for entry in entries] == [(1, 1), (1, 2)]
    assert [entry["status"] for entry in entries] == ["charged", "charged"]
    assert [entry["outcome"] for entry in entries] == ["retryable_error", "ok"]
    assert [entry["charged_micro"] for entry in entries] == [4, 5]


def test_choice_errors_exhaust_per_round_attempts(tmp_path, monkeypatch):
    """Spec: AI (Codex) Choice errors propagate after every permitted attempt is charged."""
    config = _config(tmp_path, monkeypatch)
    transport = ScriptedTransport(
        [(200, _choice_error(usage={"cost": 0.000004})) for _ in range(3)]
    )
    client = MeteredClient(_raw(config, transport), config, sleep=lambda _: None)

    with pytest.raises(OpenRouterError, match="OpenRouter choice error"):
        client.chat([{"role": "user", "content": "hello"}])

    entries = client.ledger()["entries"]
    assert [(entry["round"], entry["attempt"]) for entry in entries] == [
        (1, 1),
        (1, 2),
        (1, 3),
    ]
    assert all(entry["status"] == "charged" for entry in entries)
    assert all(entry["outcome"] == "retryable_error" for entry in entries)


def test_three_network_failures_exhaust_retries(tmp_path, monkeypatch):
    """Spec: AI (Codex) Three URL failures retain reservations and preserve network wording."""
    config = _config(tmp_path, monkeypatch)
    transport = ScriptedTransport([URLError("down"), URLError("down"), URLError("down")])
    sleeps = []
    client = MeteredClient(_raw(config, transport), config, sleep=sleeps.append)

    with pytest.raises(OpenRouterError, match=r"OpenRouter network failure \(URLError\)") as caught:
        client.chat([{"role": "user", "content": "hello"}])

    assert type(caught.value) is OpenRouterError
    assert sleeps == [1, 2]
    entries = client.ledger()["entries"]
    assert len(entries) == 3
    assert all(entry["status"] == "charged_reservation" for entry in entries)


def test_budget_refuses_before_transport(tmp_path, monkeypatch):
    """Spec: AI (Codex) An unaffordable estimate is refused before transport dispatch."""
    config = _config(tmp_path, monkeypatch, run_usd="0.000001")
    transport = ScriptedTransport([(200, _success())])
    client = MeteredClient(_raw(config, transport), config, sleep=lambda _: None)

    with pytest.raises(BudgetExceeded) as caught:
        client.chat([{"role": "user", "content": "hello"}])

    assert caught.value.reason == "budget"
    assert transport.calls == []
    entries = client.ledger()["entries"]
    assert len(entries) == 1
    assert entries[0]["status"] == "refused"


def test_explicit_budget_overrides_run_budget(tmp_path, monkeypatch):
    """Spec: AI (Codex) An explicit model-call budget replaces the configured run budget."""
    config = _config(tmp_path, monkeypatch, run_usd="0.01")
    transport = ScriptedTransport([(200, _success())])
    client = MeteredClient(
        _raw(config, transport),
        config,
        budget_usd=0.000001,
        sleep=lambda _: None,
    )

    with pytest.raises(BudgetExceeded):
        client.chat([{"role": "user", "content": "hello"}])

    assert client.ledger()["summary"]["budget_micro"] == 1
    assert transport.calls == []


def test_attempt_timestamps_use_injected_clock(tmp_path, monkeypatch):
    """Spec: AI (Codex) A provider attempt records ordered injected monotonic timestamps."""
    config = _config(tmp_path, monkeypatch)
    transport = ScriptedTransport([(200, _success())])
    readings = iter((101, 109))
    client = MeteredClient(
        _raw(config, transport),
        config,
        sleep=lambda _: None,
        clock=lambda: next(readings),
    )

    client.chat([{"role": "user", "content": "hello"}])

    entry = client.ledger()["entries"][0]
    assert (entry["started_ns"], entry["finished_ns"]) == (101, 109)


def test_refused_attempt_has_one_timestamp_reading(tmp_path, monkeypatch):
    """Spec: AI (Codex) A refused admission records one timestamp as both boundaries."""
    config = _config(tmp_path, monkeypatch, run_usd="0.000001")
    transport = ScriptedTransport([(200, _success())])
    readings = []

    def clock():
        readings.append(311)
        return readings[-1]

    client = MeteredClient(
        _raw(config, transport), config, sleep=lambda _: None, clock=clock
    )

    with pytest.raises(BudgetExceeded):
        client.chat([{"role": "user", "content": "hello"}])

    entry = client.ledger()["entries"][0]
    assert readings == [311]
    assert entry["started_ns"] == entry["finished_ns"] == 311


def test_provider_overrun_causes_next_round_refusal(tmp_path, monkeypatch):
    """Spec: AI (Codex) Provider cost above reservation records overrun and reduces admission."""
    config = _config(
        tmp_path,
        monkeypatch,
        run_usd="0.0005",
        input_price="0.1",
        output_price="0.1",
    )
    transport = ScriptedTransport([(200, _success(usage={"cost": 0.00049}))])
    client = MeteredClient(_raw(config, transport), config, sleep=lambda _: None)

    client.chat([{"role": "user", "content": "hello"}])
    with pytest.raises(BudgetExceeded) as caught:
        client.chat([{"role": "user", "content": "hello"}])

    entries = client.ledger()["entries"]
    assert entries[0]["overrun_micro"] > 0
    assert caught.value.reason == "budget"
    assert entries[1]["status"] == "refused"
    assert len(transport.calls) == 1


def test_success_without_usage_keeps_reservation(tmp_path, monkeypatch):
    """Spec: AI (Codex) A successful response without usage keeps the full reservation charged."""
    config = _config(tmp_path, monkeypatch)
    transport = ScriptedTransport([(200, _success())])
    client = MeteredClient(_raw(config, transport), config, sleep=lambda _: None)

    client.chat([{"role": "user", "content": "hello"}])

    entry = client.ledger()["entries"][0]
    assert entry["status"] == "charged_reservation"
    assert entry["provider_cost_micro"] is None
    assert entry["difference_micro"] is None


def test_error_response_usage_is_charged(tmp_path, monkeypatch):
    """Spec: AI (Codex) Parsed request errors expose usage so provider cost is charged."""
    config = _config(tmp_path, monkeypatch)
    body = json.dumps({"error": {"message": "bad"}, "usage": {"cost": 0.000007}}).encode()
    transport = ScriptedTransport([(200, body)])
    client = MeteredClient(_raw(config, transport), config, sleep=lambda _: None)

    with pytest.raises(OpenRouterError, match="OpenRouter request error"):
        client.chat([{"role": "user", "content": "hello"}])

    entry = client.ledger()["entries"][0]
    assert entry["status"] == "charged"
    assert entry["provider_cost_micro"] == 7
    assert entry["charged_micro"] == 7


@pytest.mark.parametrize(
    ("old", "new", "key"),
    [
        ('max_tokens = 10\n', "", "models.max_tokens"),
        ('max_tokens = 10\n', 'max_tokens = 10\nextra = 1\n', "models.extra"),
        ('seed = 17\n', 'seed = "17"\n', "models.seed"),
        ('seed = 17\n', "seed = true\n", "models.seed"),
    ],
)
def test_config_rejects_invalid_shape_or_type(tmp_path, monkeypatch, old, new, key):
    """Spec: AI (Codex) Missing, unknown, string, and bool values fail strict validation."""
    monkeypatch.delenv("OPENROUTER_MODEL", raising=False)
    path = tmp_path / "bad.toml"
    path.write_text(_toml().replace(old, new), encoding="utf-8")

    with pytest.raises(ConfigError, match=key):
        load_config(path, env_file=tmp_path / "missing.env")


def test_config_override_default_pricing_and_stable_hash(tmp_path, monkeypatch):
    """Spec: AI (Codex) Env model override is recorded, uses default price, and hashes stably."""
    path = tmp_path / "config.toml"
    path.write_text(_toml(), encoding="utf-8")
    monkeypatch.setenv("OPENROUTER_MODEL", "unknown/model")

    first = load_config(path, env_file=tmp_path / "missing.env")
    second = load_config(path, env_file=tmp_path / "missing.env")

    assert first.models.agent == "unknown/model"
    assert first.overrides == ({"key": "models.agent", "source": "env"},)
    assert first.pricing_for() == ("pricing.default", first.pricing.default)
    assert first.sha256 == second.sha256
    assert first.evals.judge_model == "judge/model"


@pytest.mark.parametrize(
    ("old", "new", "match"),
    [
        ('judge_model = "judge/model"\n', "", "evals.judge_model"),
        (
            "meets_expectation = 1\n",
            "meets_expectation = 1\nunknown = 1\n",
            "evals.weights.unknown",
        ),
        ("floor = 1\n", "floor = 3\n", "evals.floor"),
    ],
)
def test_config_rejects_invalid_evals(tmp_path, monkeypatch, old, new, match):
    """Spec: AI (Codex) Eval keys are required and closed, with floor bounded by scale."""
    monkeypatch.delenv("OPENROUTER_MODEL", raising=False)
    path = tmp_path / "bad.toml"
    path.write_text(_toml().replace(old, new), encoding="utf-8")

    with pytest.raises(ConfigError, match=match):
        load_config(path, env_file=tmp_path / "missing.env")


def test_from_config_model_override_does_not_change_agent_config(tmp_path, monkeypatch):
    """Spec: AI (Codex) A client-only model override leaves the configured agent unchanged."""
    config = _config(tmp_path, monkeypatch)
    monkeypatch.setenv("OPENROUTER_API_KEY", "offline-key")

    client = from_config(config, model=config.evals.judge_model)

    assert client.model == "judge/model"
    assert config.models.agent == "test/model"


def test_empty_model_override_is_invalid(tmp_path, monkeypatch):
    """Spec: AI (Codex) A present but empty process override is a configuration error."""
    path = tmp_path / "config.toml"
    path.write_text(_toml(), encoding="utf-8")
    monkeypatch.setenv("OPENROUTER_MODEL", "")

    with pytest.raises(ConfigError, match="OPENROUTER_MODEL"):
        load_config(path, env_file=tmp_path / "missing.env")


@pytest.mark.parametrize(
    ("old", "new", "match"),
    [
        ('\n[limits]\n', '\n[not_limits]\n', "config.limits"),
        ('max_workers = 1\n', 'max_workers = 1\nextra = 1\n', "limits.extra"),
        ('page_size = 100\n', 'page_size = true\n', "limits.page_size"),
        ('replan = "frontier"\n', 'replan = "later"\n', "limits.replan"),
        ('max_workers = 1\n', 'max_workers = 0\n', r"limits.max_workers.*1\.\.8"),
        ('max_workers = 1\n', 'max_workers = 9\n', r"limits.max_workers.*1\.\.8"),
    ],
)
def test_config_rejects_invalid_limits(tmp_path, monkeypatch, old, new, match):
    """Spec: AI (Codex) Limits are required, closed, typed, enumerated, and phase-safe."""
    monkeypatch.delenv("OPENROUTER_MODEL", raising=False)
    path = tmp_path / "bad.toml"
    path.write_text(_toml().replace(old, new), encoding="utf-8")

    with pytest.raises(ConfigError, match=match):
        load_config(path, env_file=tmp_path / "missing.env")


def test_config_accepts_four_workers(tmp_path, monkeypatch):
    """Spec: AI (Codex). Four concurrent workers are within the supported range."""
    monkeypatch.delenv("OPENROUTER_MODEL", raising=False)
    path = tmp_path / "config.toml"
    path.write_text(
        _toml().replace("max_workers = 1", "max_workers = 4"),
        encoding="utf-8",
    )

    assert load_config(
        path, env_file=tmp_path / "missing.env"
    ).limits.max_workers == 4


def test_payload_fixed_settings_cannot_be_overridden(tmp_path, monkeypatch):
    """Spec: AI (Codex) Payload policy and configured model settings override extra values."""
    config = _config(tmp_path, monkeypatch, max_tokens=123)
    transport = ScriptedTransport([(200, _success())])
    client = _raw(config, transport)
    messages = [{"role": "user", "content": "hello"}]

    body = client.build_body(
        messages,
        extra={
            "model": "wrong",
            "messages": [],
            "provider": {},
            "reasoning": {"effort": "wrong"},
            "seed": 999,
            "max_tokens": 999,
            "temperature": 2,
        },
    )
    payload = json.loads(body)

    assert payload["model"] == "test/model"
    assert payload["messages"] == messages
    assert payload["provider"] == {"data_collection": "deny", "require_parameters": True}
    assert payload["reasoning"] == {"effort": "medium"}
    assert payload["seed"] == 17
    assert payload["max_tokens"] == 123
    assert "temperature" not in payload


class MinimalTools:
    def list_tools(self):
        return []


class BudgetFailingLlm:
    model = "fake/model"

    def chat(self, messages, **kwargs):
        raise BudgetExceeded(
            reason="budget",
            estimate_micro=2,
            remaining_micro=1,
            round=1,
            attempt=1,
        )


def test_run_agent_wraps_budget_refusal_as_agent_error():
    """Spec: AI (Codex) Budget refusal becomes AgentError rather than an agent refusal answer."""
    with pytest.raises(AgentError) as caught:
        run_agent(
            MinimalTools(),
            BudgetFailingLlm(),
            request="investigate",
            target_id="wo-1",
            today=date(2026, 9, 19),
            own_user_id=None,
            allow_write=False,
        )

    assert caught.value.original_type == "BudgetExceeded"


def test_main_missing_config_exits_before_login(tmp_path, monkeypatch):
    """Spec: AI (Codex) A missing CLI config exits with status 2 before login."""
    missing_config = tmp_path / "missing.toml"
    monkeypatch.setattr(
        "sys.argv",
        [
            "agentswitch.agent",
            "--tenant",
            "suryodaya",
            "--work-order",
            "wo-1",
            "--config",
            str(missing_config),
        ],
    )

    def fail_if_called(_tenant):
        pytest.fail("mcp_client.from_env must not be called for an invalid config")

    monkeypatch.setattr(agent.mcp_client, "from_env", fail_if_called)

    with pytest.raises(SystemExit) as caught:
        agent.main()

    assert caught.value.code == 2
