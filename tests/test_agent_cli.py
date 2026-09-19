"""Offline tests for the graph-first agent CLI."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from agentswitch import agent
from agentswitch.config import Config, load_config
from agentswitch.mcp_client import McpClient
from agentswitch.offline import (
    OfflineMcpTransport,
    offline_llm_client,
    scripted_tool_response,
)


class _RawLlm:
    model = "offline/model"


class _Client:
    def current_user_id(self) -> str:
        return "user-1"


def _config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    monkeypatch.delenv("OPENROUTER_MODEL", raising=False)
    return load_config(env_file=tmp_path / "missing.env")


def _result(outcome: str) -> dict[str, Any]:
    result = {
        "outcome": outcome,
        "refusal_reason": None if outcome == "answered" else "unsupported",
        "prose": f"{outcome} prose",
        "reschedule": None,
        "usage": {"calls": [], "totals": {}},
    }
    if outcome == "answered":
        result["raw"] = {
            "work_order": {"id": "wo-1"},
            "lateness": {},
            "causes": [],
            "downstream": {},
            "unknowns": [],
        }
    return result


def _install_cli_dependencies(
    monkeypatch: pytest.MonkeyPatch, resolved_config: Config
) -> _Client:
    client = _Client()
    monkeypatch.setattr(agent.config, "load_config", lambda *args, **kwargs: resolved_config)
    monkeypatch.setattr(agent.llm_client, "from_config", lambda *args, **kwargs: _RawLlm())
    monkeypatch.setattr(agent.mcp_client, "from_env", lambda tenant: client)
    return client


@pytest.mark.parametrize("outcome", ["answered", "refused"])
def test_default_cli_runs_graph_and_prints_answer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    outcome: str,
) -> None:
    """Spec: AI (Codex) The default CLI runs the read-only graph and prints results."""
    resolved_config = _config(tmp_path, monkeypatch)
    client = _install_cli_dependencies(monkeypatch, resolved_config)
    calls: list[dict[str, Any]] = []

    def run_graph(tools: Any, llm: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append({"tools": tools, "llm": llm, **kwargs})
        return _result(outcome)

    def reject_loop(*args: Any, **kwargs: Any) -> dict[str, Any]:
        pytest.fail("run_agent must not be called by the default CLI")

    monkeypatch.setattr(agent.executor, "run_graph_agent", run_graph)
    monkeypatch.setattr(agent, "run_agent", reject_loop)
    monkeypatch.setattr(
        "sys.argv",
        ["agentswitch.agent", "--tenant", "suryodaya", "--work-order", "wo-1"],
    )

    assert agent.main() == 0

    assert len(calls) == 1
    call = calls[0]
    assert call["tools"] is client
    assert call["target_id"] == "wo-1"
    assert call["own_user_id"] == "user-1"
    assert call["reschedule_requested"] is True
    assert call["config"] is resolved_config
    assert call["authority"] == {"write": False, "reason": "cli_read_only"}
    assert call["receipt"] is None
    output = capsys.readouterr().out
    assert f"{outcome} prose" in output
    assert f'"outcome": "{outcome}"' in output


def test_custom_request_drops_required_reschedule(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spec: AI (Codex) A custom request does not require a reschedule attempt."""
    resolved_config = _config(tmp_path, monkeypatch)
    _install_cli_dependencies(monkeypatch, resolved_config)
    seen: dict[str, Any] = {}

    def run_graph(*args: Any, **kwargs: Any) -> dict[str, Any]:
        seen.update(kwargs)
        return _result("refused")

    monkeypatch.setattr(agent.executor, "run_graph_agent", run_graph)
    monkeypatch.setattr(
        "sys.argv",
        [
            "agentswitch.agent",
            "--tenant",
            "suryodaya",
            "--work-order",
            "wo-1",
            "--request",
            "Investigate only.",
        ],
    )

    assert agent.main() == 0
    assert seen["request"] == "Investigate only."
    assert seen["reschedule_requested"] is False


def test_graph_cli_rejects_write_flag_before_dependencies(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Spec: AI (Codex) Graph CLI writes fail before configuration or clients load."""

    def fail(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("CLI dependency must not be called after an early parser error")

    monkeypatch.setattr(agent.config, "load_config", fail)
    monkeypatch.setattr(agent.llm_client, "from_config", fail)
    monkeypatch.setattr(agent.mcp_client, "from_env", fail)
    monkeypatch.setattr(agent.executor, "run_graph_agent", fail)
    monkeypatch.setattr(agent, "run_agent", fail)
    monkeypatch.setattr(
        "sys.argv",
        [
            "agentswitch.agent",
            "--tenant",
            "suryodaya",
            "--work-order",
            "wo-1",
            "--allow-draft-writes",
        ],
    )

    with pytest.raises(SystemExit) as caught:
        agent.main()

    assert caught.value.code == 2
    assert (
        "uv run python -m agentswitch.harness --subject graph "
        "--task reschedule_own_draft --allow-draft-writes"
        in capsys.readouterr().err
    )


@pytest.mark.parametrize("allow_write", [False, True])
def test_loop_cli_runs_frozen_agent_with_write_flag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    allow_write: bool,
) -> None:
    """Spec: AI (Codex) Loop mode preserves the old agent and write flag behavior."""
    resolved_config = _config(tmp_path, monkeypatch)
    _install_cli_dependencies(monkeypatch, resolved_config)
    seen: dict[str, Any] = {}

    def run_loop(*args: Any, **kwargs: Any) -> dict[str, Any]:
        seen.update(kwargs)
        return _result("refused")

    def reject_graph(*args: Any, **kwargs: Any) -> dict[str, Any]:
        pytest.fail("run_graph_agent must not be called in loop mode")

    monkeypatch.setattr(agent, "run_agent", run_loop)
    monkeypatch.setattr(agent.executor, "run_graph_agent", reject_graph)
    argv = [
        "agentswitch.agent",
        "--tenant",
        "suryodaya",
        "--work-order",
        "wo-1",
        "--loop",
    ]
    if allow_write:
        argv.extend(["--allow-draft-writes", "--today", date.today().isoformat()])
    monkeypatch.setattr("sys.argv", argv)

    assert agent.main() == 0
    assert seen["allow_write"] is allow_write


def test_loop_write_rejects_non_system_date_before_dependencies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spec: AI (Codex) Writable loop mode retains the system-date guard."""

    def fail(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("CLI dependency must not be called after an early parser error")

    monkeypatch.setattr(agent.config, "load_config", fail)
    monkeypatch.setattr(agent.llm_client, "from_config", fail)
    monkeypatch.setattr(agent.mcp_client, "from_env", fail)
    monkeypatch.setattr(agent.executor, "run_graph_agent", fail)
    monkeypatch.setattr(agent, "run_agent", fail)
    monkeypatch.setattr(
        "sys.argv",
        [
            "agentswitch.agent",
            "--tenant",
            "suryodaya",
            "--work-order",
            "wo-1",
            "--loop",
            "--allow-draft-writes",
            "--today",
            "2000-01-01",
        ],
    )

    with pytest.raises(SystemExit) as caught:
        agent.main()

    assert caught.value.code == 2


def test_graph_error_propagates_without_success_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Spec: AI (Codex) Graph failures propagate before success output is printed."""
    resolved_config = _config(tmp_path, monkeypatch)
    _install_cli_dependencies(monkeypatch, resolved_config)

    def fail_graph(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("scripted graph failure")

    monkeypatch.setattr(agent.executor, "run_graph_agent", fail_graph)
    monkeypatch.setattr(
        "sys.argv",
        ["agentswitch.agent", "--tenant", "suryodaya", "--work-order", "wo-1"],
    )

    with pytest.raises(RuntimeError, match="scripted graph failure"):
        agent.main()

    assert capsys.readouterr().out == ""


def test_main_runs_real_graph_with_offline_raw_mcp_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Spec: AI (Codex) Main runs the real graph executor over a raw offline MCP client."""
    resolved_config = _config(tmp_path, monkeypatch)
    mcp_transport = OfflineMcpTransport([], {})
    client = McpClient(
        "https://offline.invalid",
        "offline-token",
        transport=mcp_transport,
    )
    monkeypatch.setattr(client, "current_user_id", lambda: "offline-user")
    response = scripted_tool_response(
        "plan_frontier",
        {
            "add": [
                {
                    "id": "answer",
                    "capability": "answer",
                    "arguments": {
                        "outcome": "refused",
                        "refusal_reason": "unsupported",
                        "prose": "Offline graph conclusion.",
                    },
                    "depends_on": [],
                }
            ],
            "finish": False,
            "reason": "offline CLI plan",
        },
    )
    raw_llm, llm_transport = offline_llm_client(
        resolved_config, [(200, response)]
    )
    monkeypatch.setattr(agent.config, "load_config", lambda *args, **kwargs: resolved_config)
    monkeypatch.setattr(agent.llm_client, "from_config", lambda *args, **kwargs: raw_llm)
    monkeypatch.setattr(agent.mcp_client, "from_env", lambda tenant: client)
    monkeypatch.setattr(
        "sys.argv",
        [
            "agentswitch.agent",
            "--tenant",
            "suryodaya",
            "--work-order",
            "wo-1",
            "--request",
            "Explain whether this request is supported.",
        ],
    )

    assert agent.main() == 0

    output = capsys.readouterr().out
    assert "Offline graph conclusion." in output
    assert '"outcome": "refused"' in output
    assert len(llm_transport.calls) == 1
    assert any(
        call["request"].get("method") == "tools/list"
        for call in mcp_transport.calls
    )
    printed_answer = output[output.index("{") : output.index("\nusage:")]
    assert json.loads(printed_answer)["refusal_reason"] == "unsupported"
