"""Run, persist, and independently score the read-only harness tasks."""

import json
import os
import subprocess
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable

from agentswitch.mcp_client import McpClient, TransportError, _read_env_file, login

from .recorder import ReadOnlyTools, observations_from_calls, redact, write_exclusive
from .subjects import SUBJECT_LABEL, investigate_subject
from .tasks import TASKS, public_task, select_target, task_by_id
from .verifiers import (
    FreshReader,
    answered_task_refusal,
    causes_valid,
    downstream_complete,
    downstream_valid,
    expected_causes_present,
    lateness_correct,
    no_writes,
    refusal_valid,
    target_still_eligible,
    validate_answer,
    work_order_matches,
)

SCHEMA_VERSION = "1.0"
VALID_TENANTS = {"suryodaya", "keystone"}


class HarnessConfigurationError(Exception):
    """Harness configuration or the runs path is invalid."""


class HarnessLoginError(Exception):
    """Authentication or pre-task transport failed."""


class HarnessPersistenceError(Exception):
    """A required run or score record could not be persisted."""


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _git(repo_root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    command = ["git", "-C", str(repo_root), *arguments]
    try:
        return subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as error:
        return subprocess.CompletedProcess(command, 127, stdout="", stderr=str(error))


def _prepare_runs_dir(repo_root: Path, runs_dir: Path | None) -> Path:
    selected = (repo_root / "runs") if runs_dir is None else runs_dir
    resolved = selected.resolve()
    if resolved.is_relative_to(repo_root.resolve()):
        relative = resolved.relative_to(repo_root.resolve())
        ignored = _git(repo_root, "check-ignore", "-q", f"{relative.as_posix()}/")
        if ignored.returncode != 0:
            raise HarnessConfigurationError(
                f"Run directory {relative.as_posix()!r} is not ignored by git; refusing to continue"
            )
    try:
        resolved.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise HarnessConfigurationError(f"Cannot create the run directory: {error}") from None
    return resolved


def _settings(tenant: str, env_file: Path) -> tuple[str, str, str]:
    file_values: dict[str, str] | None = None

    def setting(name: str) -> str:
        nonlocal file_values
        if name in os.environ:
            value = os.environ[name]
        else:
            if file_values is None:
                file_values = _read_env_file(str(env_file))
            value = file_values.get(name, "")
        if not value:
            raise HarnessConfigurationError(f"Missing required configuration variable {name}")
        return value

    suffix = tenant.upper()
    return setting(f"AS_URL_{suffix}"), setting("AS_EMAIL"), setting(f"AS_PASSWORD_{suffix}")


def _git_metadata(repo_root: Path) -> dict[str, Any]:
    commit = _git(repo_root, "rev-parse", "HEAD")
    status = _git(repo_root, "status", "--porcelain")
    return {
        "commit_sha": commit.stdout.strip() if commit.returncode == 0 and commit.stdout.strip() else None,
        "dirty": bool(status.stdout) if status.returncode == 0 else None,
    }


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _error(error: Exception, secrets: tuple[str, ...], *, phase: str) -> dict[str, Any]:
    return {
        "phase": phase,
        "type": type(error).__name__,
        "is_transport": isinstance(error, TransportError),
        "message": redact(str(error), secrets),
    }


def _score_verdict(verifiers: list[dict[str, Any]]) -> str:
    if any(result["verdict"] == "fail" for result in verifiers):
        return "fail"
    if any(result["verdict"] == "inconclusive" for result in verifiers):
        return "inconclusive"
    task_results = [result for result in verifiers if result["name"] != "no_writes"]
    if task_results and all(result["verdict"] == "not_applicable" for result in task_results):
        return "not_applicable"
    return "pass"


def _simple_result(name: str, verdict: str, reason: str) -> dict[str, Any]:
    return {"name": name, "verdict": verdict, "reason": reason, "evidence": {}}


def _run_verifier(
    results: list[dict[str, Any]],
    name: str,
    function: Callable[[], dict[str, Any]],
    secrets: tuple[str, ...],
    *,
    exception_verdict: str = "inconclusive",
    exception_prefix: str = "verifier_error",
) -> None:
    try:
        results.append(function())
    except Exception as error:
        results.append(
            _simple_result(
                name,
                exception_verdict,
                f"{exception_prefix}: {redact(str(error), secrets)}",
            )
        )


def _score_run(
    run_record: dict[str, Any],
    *,
    base_url: str,
    token: str,
    transport: Any,
    secrets: tuple[str, ...],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    task = run_record["task"]
    selection = run_record["selection"]
    answer = run_record.get("answer")
    today = date.fromisoformat(run_record["today"])
    subject_calls = run_record.get("call_log", [])
    observations = observations_from_calls(subject_calls)
    verify_client = McpClient(base_url, token, transport=transport)
    verify_tools = ReadOnlyTools(verify_client, phase="verify")
    fresh = FreshReader(verify_tools)
    results: list[dict[str, Any]] = []

    status = selection.get("status")
    if status == "no_target":
        results.append(_simple_result("selection", "not_applicable", "no_target"))
    elif status == "selection_incomplete":
        results.append(_simple_result("selection", "inconclusive", "selection_incomplete"))
    elif status == "selection_error":
        results.append(_simple_result("selection", "inconclusive", "selection_error"))
    elif run_record.get("harness_errors"):
        subject_errors = [
            error
            for error in run_record["harness_errors"]
            if error.get("phase") == "subject"
        ]
        subject_read_failed = any(
            call.get("phase") == "subject"
            and "outcome" in call
            and call.get("outcome") not in {"ok", "refused_write"}
            for call in subject_calls
        )
        if (
            subject_errors
            and all(error.get("is_transport") is True for error in subject_errors)
            and subject_read_failed
        ):
            results.append(
                _simple_result(
                    "subject_execution",
                    "inconclusive",
                    "source_unavailable: subject read failed at transport level",
                )
            )
        else:
            results.append(_simple_result("subject_execution", "fail", "subject_error"))
    else:
        target_id = selection.get("target_id")
        contract_results: list[dict[str, Any]] = []
        _run_verifier(
            contract_results,
            "answer_contract",
            lambda: validate_answer(answer, target_id),
            secrets,
            exception_verdict="fail",
            exception_prefix="invalid_answer",
        )
        results.extend(contract_results)
        contract = contract_results[0]
        if contract["verdict"] == "pass":
            expected_outcome = task["expected"]["outcome"]
            if answer["outcome"] != expected_outcome:
                if expected_outcome == "answered" and answer["outcome"] == "refused":
                    _run_verifier(
                        results,
                        "task_outcome",
                        lambda: answered_task_refusal(answer, target_id, subject_calls, fresh),
                        secrets,
                    )
                else:
                    results.append(
                        _simple_result("task_outcome", "fail", "answer outcome does not match the task")
                    )
            elif answer["outcome"] == "refused":
                _run_verifier(
                    results,
                    "refusal_valid",
                    lambda: refusal_valid(task, answer, target_id, fresh),
                    secrets,
                )
            else:
                eligibility: list[dict[str, Any]] = []
                _run_verifier(
                    eligibility,
                    "target_still_eligible",
                    lambda: target_still_eligible(task, selection, observations, fresh, today),
                    secrets,
                )
                results.extend(eligibility)
                changed = bool(eligibility and eligibility[0]["verdict"] == "inconclusive")
                claims = answer["claims"]
                _run_verifier(
                    results,
                    "work_order_matches",
                    lambda: work_order_matches(claims, target_id, observations, fresh, today),
                    secrets,
                )
                _run_verifier(
                    results,
                    "lateness_correct",
                    lambda: lateness_correct(
                        task,
                        claims,
                        target_id,
                        observations,
                        fresh,
                        today,
                        target_changed=changed,
                    ),
                    secrets,
                )
                _run_verifier(
                    results,
                    "causes_valid",
                    lambda: causes_valid(claims, target_id, observations, fresh, today),
                    secrets,
                )
                if task["expected"].get("is_late") is True and not changed:
                    _run_verifier(
                        results,
                        "expected_causes_present",
                        lambda: expected_causes_present(
                            selection,
                            claims,
                            target_id,
                            observations,
                            subject_calls,
                            fresh,
                            today,
                        ),
                        secrets,
                    )
                _run_verifier(
                    results,
                    "downstream_valid",
                    lambda: downstream_valid(
                        claims,
                        target_id,
                        observations,
                        subject_calls,
                        fresh,
                        today,
                    ),
                    secrets,
                )
                _run_verifier(
                    results,
                    "downstream_complete",
                    lambda: downstream_complete(
                        claims,
                        target_id,
                        observations,
                        subject_calls,
                        fresh,
                    ),
                    secrets,
                )
    if "call_log" in run_record:
        _run_verifier(
            results,
            "no_writes",
            lambda: no_writes(subject_calls + verify_tools.calls, fresh),
            secrets,
        )
    score = {
        "schema_version": SCHEMA_VERSION,
        "task_id": task["id"],
        "tenant": run_record["tenant"],
        "run_file": run_record["run_file"],
        "scored_at": _utc_now().isoformat(),
        "verdict": _score_verdict(results),
        "verifiers": results,
        "verify_call_log": verify_tools.calls,
    }
    return score, verify_tools.calls


def run_tasks(
    tenant: str,
    *,
    task_ids: list[str] | None = None,
    today: date | None = None,
    transport: Any = None,
    env_file: Path | None = None,
    runs_dir: Path | None = None,
) -> list[dict[str, Any]]:
    """Run selected tasks, persisting each run before independently scoring it."""
    normalized_tenant = tenant.strip().lower()
    if normalized_tenant not in VALID_TENANTS:
        choices = ", ".join(sorted(VALID_TENANTS))
        raise HarnessConfigurationError(f"Invalid tenant {tenant!r}; expected one of: {choices}")
    requested_ids = list(task_ids) if task_ids else [task["id"] for task in TASKS]
    if len(requested_ids) != len(set(requested_ids)):
        raise HarnessConfigurationError("Task ids must not be repeated")
    try:
        selected_tasks = [task_by_id(task_id) for task_id in requested_ids]
    except KeyError as error:
        raise HarnessConfigurationError(f"Unknown task id: {error.args[0]}") from None

    repo_root = _repo_root()
    output_dir = _prepare_runs_dir(repo_root, runs_dir)
    selected_env_file = repo_root / ".env" if env_file is None else env_file
    base_url, email, password = _settings(normalized_tenant, selected_env_file)
    try:
        token = login(base_url, email, password, transport=transport)
    except Exception as error:
        message = redact(str(error), (password,))
        raise HarnessLoginError(f"Login failed: {message}") from None
    secrets = (token, password)
    password = ""
    run_today = today or date.today()
    git_metadata = _git_metadata(repo_root)
    summaries: list[dict[str, Any]] = []

    for task in selected_tasks:
        task_started = _utc_now()
        stem = f"{task_started.strftime('%Y%m%dT%H%M%S%fZ')}_{normalized_tenant}_{task['id']}"
        client = McpClient(base_url, token, transport=transport)
        tools = ReadOnlyTools(client, phase="select")
        harness_errors: list[dict[str, Any]] = []
        subject_output: dict[str, Any] | None = None
        answer: dict[str, Any] | None = None
        label = SUBJECT_LABEL
        try:
            selection = select_target(task, tools, run_today)
        except Exception as error:
            selection = {
                "selector": task["selector"],
                "today": run_today.isoformat(),
                "selected_at": _utc_now().isoformat(),
                "status": "selection_error",
                "complete": False,
                "target_id": None,
                "reason": type(error).__name__,
            }
            harness_errors.append(_error(error, secrets, phase="selection"))
        if selection.get("status") == "selected":
            tools.phase = "subject"
            subject_started = time.perf_counter_ns()
            try:
                subject_output = investigate_subject(
                    tools,
                    request=task["request"],
                    request_kind=task["request_kind"],
                    target_id=selection.get("target_id"),
                    today=run_today,
                )
                answer = subject_output.get("answer")
                label = subject_output.get("label", SUBJECT_LABEL)
            except Exception as error:
                harness_errors.append(_error(error, secrets, phase="subject"))
            subject_elapsed_ms = round((time.perf_counter_ns() - subject_started) / 1_000_000, 3)
        else:
            subject_elapsed_ms = 0.0

        run_record = {
            "schema_version": SCHEMA_VERSION,
            "run_file": f"{stem}.json",
            "task": public_task(task),
            "tenant": normalized_tenant,
            "today": run_today.isoformat(),
            "git": git_metadata,
            "selection": selection,
            "subject": {"name": "investigate_subject", "label": label},
            "request": task["request"],
            "answer": answer,
            "subject_output": subject_output,
            "call_log": tools.calls,
            "timings": {
                "started_at": task_started.isoformat(),
                "finished_at": _utc_now().isoformat(),
                "subject_elapsed_ms": subject_elapsed_ms,
            },
            "harness_errors": harness_errors,
        }
        try:
            run_path = write_exclusive(
                output_dir / f"{stem}.json",
                run_record,
                secrets,
                filename_field="run_file",
            )
            with run_path.open(encoding="utf-8") as stream:
                persisted_run = json.load(stream)
            score_record, _ = _score_run(
                persisted_run,
                base_url=base_url,
                token=token,
                transport=transport,
                secrets=secrets,
            )
            score_record["run_file"] = run_path.name
            score_path = write_exclusive(
                run_path.with_name(f"{run_path.stem}.score.json"),
                score_record,
                secrets,
            )
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise HarnessPersistenceError(
                f"Could not persist or reload task {task['id']}: {redact(str(error), secrets)}"
            ) from None
        summaries.append(
            {
                "task_id": task["id"],
                "run_path": str(run_path),
                "score_path": str(score_path),
                "verdict": score_record["verdict"],
                "verifiers": [
                    {
                        "name": result["name"],
                        "verdict": result["verdict"],
                        "reason": result["reason"],
                    }
                    for result in score_record["verifiers"]
                ],
                "subject_label": label,
                "routed_refusal": task["request_kind"] != "work_order_lateness",
            }
        )
    return summaries


__all__ = [
    "HarnessConfigurationError",
    "HarnessLoginError",
    "HarnessPersistenceError",
    "run_tasks",
]
