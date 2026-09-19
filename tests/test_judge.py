"""Offline tests for the advisory rubric judge and harness sidecar."""

from __future__ import annotations

import json
import sys
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from agentswitch import judge
from agentswitch.config import JUDGE_CRITERIA, Config, load_config
from agentswitch.harness import __main__ as harness_main
from agentswitch.harness import runner
from agentswitch.llm_client import OpenRouterClient
from agentswitch.offline import (
    OfflineMcpTransport,
    ScriptedLlmTransport,
    offline_llm_client,
    scripted_tool_response,
)

TODAY = date(2026, 9, 19)


def _config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    monkeypatch.delenv("OPENROUTER_MODEL", raising=False)
    return load_config(env_file=tmp_path / "missing.env")


def _answer() -> dict[str, Any]:
    return {
        "outcome": "answered",
        "refusal_reason": None,
        "claims": [{"record": "WO-100", "late_days": 3}],
        "prose": "WO-100 is three days late as of 2026-09-19.",
    }


def _scored_arguments(**scores: int) -> dict[str, Any]:
    selected = {criterion: scores.get(criterion, 2) for criterion in JUDGE_CRITERIA}
    return {
        **selected,
        "rationale": {criterion: f"{criterion} rationale" for criterion in JUDGE_CRITERIA},
    }


def _judge_client(
    config: Config,
    responses: list[tuple[int, dict[str, Any] | bytes]],
) -> tuple[OpenRouterClient, ScriptedLlmTransport]:
    transport = ScriptedLlmTransport(responses)
    return (
        OpenRouterClient(
            "offline-judge-key",
            config.evals.judge_model,
            reasoning_effort=config.models.reasoning_effort,
            seed=config.models.seed,
            max_tokens=config.models.max_tokens,
            timeout=float(config.models.timeout_seconds),
            transport=transport,
        ),
        transport,
    )


def _judge_result(
    config: Config,
    arguments: dict[str, Any],
) -> tuple[dict[str, Any], ScriptedLlmTransport]:
    raw, transport = _judge_client(
        config,
        [(200, scripted_tool_response("score_answer", arguments))],
    )
    result = judge.judge_answer(
        request="Why is WO-100 late?",
        expectation="Names the work order and lateness.",
        today=TODAY,
        answer=_answer(),
        config=config,
        make_client=lambda: raw,
    )
    return result, transport


def _outside_seat_task() -> dict[str, Any]:
    return {
        "id": "refuse_outside_seat",
        "request": "Show the stock ledger for this item in every warehouse.",
        "request_kind": "stock_ledger",
        "selector": "none",
        "expected": {
            "outcome": "refused",
            "refusal_reason": ["outside_seat", "unsupported"],
        },
        "brief_refusal": True,
        "reschedule": False,
        "writes": False,
        "expectation": "Refuses because the request is outside the seat.",
    }


class _HarnessTransport:
    def __init__(self) -> None:
        self.mcp = OfflineMcpTransport([], {})

    def __call__(
        self,
        url: str,
        body: bytes,
        headers: dict[str, str],
        timeout: float,
    ) -> tuple[int, bytes]:
        if url.endswith("/api/auth/login"):
            return 200, b'{"token":"offline-token"}'
        return self.mcp(url, body, headers, timeout)


def _get_transport(
    url: str, headers: dict[str, str], timeout: float
) -> tuple[int, bytes]:
    del url, headers, timeout
    return 200, b'{"id":"offline-user"}'


def _env_file(tmp_path: Path) -> Path:
    path = tmp_path / "offline.env"
    path.write_text(
        "AS_URL_SURYODAYA=https://offline.invalid\n"
        "AS_EMAIL=offline@example.invalid\n"
        "AS_PASSWORD_SURYODAYA=offline-password\n",
        encoding="utf-8",
    )
    return path


def _graph_answer_response() -> tuple[int, dict[str, Any]]:
    return (
        200,
        scripted_tool_response(
            "plan_frontier",
            {
                "add": [
                    {
                        "id": "answer",
                        "capability": "answer",
                        "arguments": {
                            "outcome": "refused",
                            "refusal_reason": "outside_seat",
                            "prose": "The stock ledger is outside the Production seat.",
                        },
                        "depends_on": [],
                    }
                ],
                "finish": False,
                "reason": "The request is outside the seat.",
            },
        ),
    )


def _plain_response() -> dict[str, Any]:
    return {
        "choices": [
            {
                "message": {"role": "assistant", "content": "plain text only"},
                "finish_reason": "stop",
            }
        ],
        "model": "offline/judge",
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "cost": 0.0},
    }


def test_valid_scores_resolve_and_compute_weighted_mean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spec: AI (Codex) Valid forced-function scores resolve using Python's weighted mean."""
    config = _config(tmp_path, monkeypatch)
    result, transport = _judge_result(config, _scored_arguments())

    assert result["status"] == "resolved"
    assert result["reason"] == "meets_rubric"
    assert result["weighted_mean"] == 2.0
    assert result["ledger"]["summary"]["attempts"] == 1
    assert len(transport.calls) == 1


@pytest.mark.parametrize(
    ("scores", "reason"),
    [
        ({"addresses_task": 0}, "below_floor"),
        ({criterion: 1 for criterion in JUDGE_CRITERIA}, "below_threshold"),
    ],
)
def test_valid_scores_can_be_unresolved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scores: dict[str, int],
    reason: str,
) -> None:
    """Spec: AI (Codex) Floor failures take precedence and threshold failures remain distinct."""
    config = _config(tmp_path, monkeypatch)
    result, _ = _judge_result(config, _scored_arguments(**scores))

    assert result["status"] == "unresolved"
    assert result["reason"] == reason


@pytest.mark.parametrize("answer", [None, {"prose": None}])
def test_empty_prose_does_not_build_a_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    answer: Any,
) -> None:
    """Spec: AI (Codex) Missing and null prose are unresolved without constructing a client."""
    config = _config(tmp_path, monkeypatch)

    def fail_client() -> None:
        pytest.fail("empty prose must not construct the judge client")

    result = judge.judge_answer(
        request="request",
        expectation="expectation",
        today=TODAY,
        answer=answer,
        config=config,
        make_client=fail_client,
    )

    assert result["status"] == "unresolved"
    assert result["reason"] == "no_prose"
    assert result["called"] is False
    assert result["ledger"] is None


def test_tiny_judge_budget_refuses_before_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spec: AI (Codex) Judge admission failure records budget without reaching transport."""
    config = _config(tmp_path, monkeypatch)
    config = replace(config, budgets=replace(config.budgets, judge_usd=0.0000001))
    raw, transport = _judge_client(
        config,
        [(200, scripted_tool_response("score_answer", _scored_arguments()))],
    )

    result = judge.judge_answer(
        request="request",
        expectation="expectation",
        today=TODAY,
        answer=_answer(),
        config=config,
        make_client=lambda: raw,
    )

    assert result["status"] == "judge_failed"
    assert result["reason"] == "budget"
    assert result["called"] is True
    assert result["ledger"]["summary"]["attempts"] == 0
    assert result["ledger"]["summary"]["refused"] is True
    assert transport.calls == []


def test_client_construction_failure_is_safe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spec: AI (Codex) Client construction errors record only their type and do not escape."""
    config = _config(tmp_path, monkeypatch)

    def fail_client() -> None:
        raise ValueError("secret diagnostic must not be recorded")

    result = judge.judge_answer(
        request="request",
        expectation="expectation",
        today=TODAY,
        answer=_answer(),
        config=config,
        make_client=fail_client,
    )

    assert result["status"] == "judge_failed"
    assert result["reason"] == "client_unavailable"
    assert result["called"] is False
    assert result["error_type"] == "ValueError"
    assert "secret diagnostic" not in json.dumps(result)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.update(extra=1),
        lambda value: value.pop("complete"),
        lambda value: value.update(complete=True),
        lambda value: value.update(complete=3),
    ],
    ids=("extra-key", "missing-criterion", "bool-score", "out-of-range"),
)
def test_malformed_judge_arguments_are_unparseable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutate: Any,
) -> None:
    """Spec: AI (Codex) Extra, missing, boolean, and out-of-range score data fail strict parsing."""
    config = _config(tmp_path, monkeypatch)
    arguments = _scored_arguments()
    mutate(arguments)
    result, _ = _judge_result(config, arguments)

    assert result["status"] == "judge_failed"
    assert result["reason"] == "unparseable"
    assert result["scores"] == {criterion: None for criterion in JUDGE_CRITERIA}


def test_judge_request_is_minimal_and_denies_provider_collection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spec: AI (Codex) Judge payload excludes verifier data and preserves provider privacy policy."""
    config = _config(tmp_path, monkeypatch)
    result, transport = _judge_result(config, _scored_arguments())
    body = transport.calls[0]["body"]
    payload = json.loads(body["messages"][1]["content"])

    assert result["status"] == "resolved"
    assert set(payload) == {
        "request",
        "expectation",
        "today",
        "outcome",
        "refusal_reason",
        "claims",
        "prose",
    }
    assert "verifier" not in json.dumps(payload).lower()
    assert "verdict" not in json.dumps(payload).lower()
    assert "journal" not in json.dumps(payload).lower()
    assert "call_log" not in json.dumps(payload).lower()
    assert body["provider"]["data_collection"] == "deny"
    assert body["tool_choice"]["function"]["name"] == "score_answer"
    parameters = body["tools"][0]["function"]["parameters"]
    assert parameters["additionalProperties"] is False
    assert set(parameters["required"]) == {*JUDGE_CRITERIA, "rationale"}


def test_cli_unparseable_judge_is_advisory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spec: AI (Codex) Test 12: unparseable judging preserves CLI exit, verdict, and verifiers."""
    config = _config(tmp_path, monkeypatch)
    task = _outside_seat_task()
    summaries: list[dict[str, Any]] = []

    def client_factory(*args: Any, **kwargs: Any) -> OpenRouterClient:
        if kwargs.get("model") == config.evals.judge_model:
            raw, _ = _judge_client(config, [(200, _plain_response())])
            return raw
        raw, _ = offline_llm_client(config, [_graph_answer_response()])
        return raw

    monkeypatch.setattr(runner.llm_client, "from_config", client_factory)
    monkeypatch.setattr(harness_main, "load_tasks", lambda: (task,))
    real_run_tasks = runner.run_tasks

    def offline_run_tasks(tenant: str, **kwargs: Any) -> list[dict[str, Any]]:
        kwargs.update(
            transport=_HarnessTransport(),
            get_transport=_get_transport,
            env_file=_env_file(tmp_path),
            runs_dir=tmp_path / ("with-judge" if kwargs["judge"] else "without-judge"),
        )
        result = real_run_tasks(tenant, **kwargs)
        summaries.extend(result)
        return result

    monkeypatch.setattr(harness_main, "run_tasks", offline_run_tasks)
    base_argv = [
        "agentswitch.harness",
        "--tenant",
        "suryodaya",
        "--subject",
        "graph",
        "--task",
        task["id"],
        "--today",
        TODAY.isoformat(),
    ]
    monkeypatch.setattr(sys, "argv", base_argv)
    without_exit = harness_main.main()
    monkeypatch.setattr(sys, "argv", [*base_argv, "--judge"])
    with_exit = harness_main.main()

    without_summary, with_summary = summaries
    without_score = json.loads(Path(without_summary["score_path"]).read_text())
    with_score = json.loads(Path(with_summary["score_path"]).read_text())
    judge_record = json.loads(Path(with_summary["judge"]["path"]).read_text())
    with_run = json.loads(Path(with_summary["run_path"]).read_text())

    assert without_exit == with_exit == 0
    assert without_score["verdict"] == with_score["verdict"] == "pass"
    assert without_score["verifiers"] == with_score["verifiers"]
    assert without_summary["judge"] is None
    assert judge_record["status"] == "judge_failed"
    assert judge_record["reason"] == "unparseable"
    assert judge_record["called"] is True
    assert judge_record["ledger"]["summary"]["attempts"] == 1
    assert Path(with_summary["score_path"]).exists()
    assert Path(with_summary["spans_path"]).exists()
    assert with_run["economics"]["summary"]["attempts"] == 1


def test_judge_write_failure_is_advisory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Spec: AI (Codex) A judge sidecar write failure preserves score and CLI exit status."""
    config = _config(tmp_path, monkeypatch)
    raw, _ = _judge_client(
        config,
        [(200, scripted_tool_response("score_answer", _scored_arguments()))],
    )
    monkeypatch.setattr(runner.llm_client, "from_config", lambda *args, **kwargs: raw)
    original_write = runner.write_exclusive

    def fail_judge(path: Path, *args: Any, **kwargs: Any) -> Path:
        if path.name.endswith(".judge.json"):
            raise OSError("do not print this message")
        return original_write(path, *args, **kwargs)

    monkeypatch.setattr(runner, "write_exclusive", fail_judge)
    task = _outside_seat_task()
    monkeypatch.setattr(harness_main, "load_tasks", lambda: (task,))
    real_run_tasks = runner.run_tasks
    captured_summaries: list[dict[str, Any]] = []

    def offline_run_tasks(tenant: str, **kwargs: Any) -> list[dict[str, Any]]:
        kwargs.update(
            transport=_HarnessTransport(),
            get_transport=_get_transport,
            env_file=_env_file(tmp_path),
            runs_dir=tmp_path / "failed-judge-write",
        )
        result = real_run_tasks(tenant, **kwargs)
        captured_summaries.extend(result)
        return result

    monkeypatch.setattr(harness_main, "run_tasks", offline_run_tasks)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "agentswitch.harness",
            "--tenant",
            "suryodaya",
            "--task",
            task["id"],
            "--today",
            TODAY.isoformat(),
            "--judge",
        ],
    )

    assert harness_main.main() == 0
    output = capsys.readouterr()
    summary = captured_summaries[0]
    score = json.loads(Path(summary["score_path"]).read_text())
    assert output.err == "JUDGE FILE FAILED for refuse_outside_seat: OSError\n"
    assert "do not print this message" not in output.err
    assert summary["judge"] == {
        "status": "judge_failed",
        "reason": "write_failed",
        "path": None,
    }
    assert score["verdict"] == "pass"
    assert list((tmp_path / "failed-judge-write").glob("*.judge.json")) == []


def test_without_judge_never_builds_client_or_sidecar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spec: AI (Codex) Disabled judging builds no judge client and writes no sidecar."""
    config = _config(tmp_path, monkeypatch)
    del config

    def fail_client(*args: Any, **kwargs: Any) -> None:
        pytest.fail("judge client must not be built without --judge")

    monkeypatch.setattr(runner.llm_client, "from_config", fail_client)
    runs_dir = tmp_path / "no-judge"
    summary = runner.run_tasks(
        "suryodaya",
        tasks=(_outside_seat_task(),),
        today=TODAY,
        transport=_HarnessTransport(),
        get_transport=_get_transport,
        env_file=_env_file(tmp_path),
        runs_dir=runs_dir,
        subject="deterministic",
        judge=False,
    )[0]

    assert summary["judge"] is None
    assert list(runs_dir.glob("*.judge.json")) == []
