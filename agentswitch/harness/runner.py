"""Run, persist, and independently score the read-only harness tasks."""

import json
import os
import subprocess
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from agentswitch.mcp_client import McpClient, TransportError, _read_env_file, login

from .recorder import (
    ReadOnlyTools,
    ScopedWriteTools,
    observations_from_calls,
    redact,
    write_exclusive,
)
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
from .write_verifiers import reschedule_valid, writes_in_scope

SCHEMA_VERSION = "1.0"
VALID_TENANTS = {"suryodaya", "keystone"}


class HarnessConfigurationError(Exception):
    """Harness configuration or the runs path is invalid."""


class HarnessLoginError(Exception):
    """Authentication or pre-task transport failed."""


class HarnessPersistenceError(Exception):
    """A required run or score record could not be persisted."""

    def __init__(
        self,
        message: str,
        *,
        failed_restores: list[dict[str, Any]] | None = None,
        restore_task_id: str | None = None,
        restore: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.failed_restores = failed_restores if failed_restores is not None else []
        if not self.failed_restores and restore_task_id is not None and restore is not None:
            self.failed_restores.append({"task_id": restore_task_id, "restore": restore})
        self.restore_task_id = restore_task_id
        self.restore = restore


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
    audit_names = {"no_writes", "writes_in_scope", "restore"}
    task_results = [result for result in verifiers if result["name"] not in audit_names]
    if task_results and all(result["verdict"] == "not_applicable" for result in task_results):
        return "not_applicable"
    return "pass"


def _simple_result(name: str, verdict: str, reason: str) -> dict[str, Any]:
    return {"name": name, "verdict": verdict, "reason": reason, "evidence": {}}


def _restore_succeeded(restore: dict[str, Any] | None) -> bool:
    return restore is None or restore.get("status") in {"restored", "not_needed"}


def _failed_restore(task_id: str, restore: dict[str, Any]) -> dict[str, Any]:
    return {"task_id": task_id, "restore": restore}


def _report_failed_restore(failure: dict[str, Any]) -> None:
    restore = failure["restore"]
    raw_path = restore.get("path")
    restore_file = Path(raw_path).name if isinstance(raw_path, str) and raw_path else "no restore file"
    print(
        f"RESTORE FAILED for {failure['task_id']}: {restore.get('reason')}; "
        f"see {restore_file}",
        file=sys.stderr,
    )


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


class _PinnedReader:
    """Serve one pre-action work order while delegating every other fresh read."""

    def __init__(self, fresh: FreshReader, target_id: Any, snapshot: dict[str, Any]) -> None:
        self.fresh = fresh
        self.target_id = target_id
        self.snapshot = snapshot

    def get(self, entity: str, identifier: Any) -> tuple[str, Any]:
        if entity == "WorkOrder" and identifier == self.target_id:
            return "ok", self.snapshot
        return self.fresh.get(entity, identifier)

    def list(self, entity: str, filters: dict[str, Any]) -> tuple[str, Any]:
        return self.fresh.list(entity, filters)

    def catalogue(self) -> tuple[str, Any]:
        return self.fresh.catalogue()


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
    actual_fresh = FreshReader(verify_tools)
    target_id = selection.get("target_id")
    pre_action_snapshot = run_record.get("pre_action_snapshot")
    fresh: Any = actual_fresh
    if task.get("writes") and isinstance(pre_action_snapshot, dict):
        fresh = _PinnedReader(actual_fresh, target_id, pre_action_snapshot)
    results: list[dict[str, Any]] = []

    status = selection.get("status")
    fixture = run_record.get("fixture")
    if status == "draft_writes_disabled":
        results.append(_simple_result("selection", "not_applicable", "draft writes not enabled"))
    elif status == "configuration_error":
        results.append(_simple_result("selection", "fail", selection.get("reason", "configuration_error")))
    elif status == "fixture_residue":
        results.append(
            _simple_result(
                "selection",
                "inconclusive",
                "fixture_residue: an owned draft already has past dates; a human must check it",
            )
        )
    elif isinstance(fixture, dict) and fixture.get("status") == "fixture_failed":
        results.append(_simple_result("fixture", "inconclusive", "fixture_failed"))
    elif status == "no_target":
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
                if task.get("reschedule"):
                    _run_verifier(
                        results,
                        "reschedule_valid",
                        lambda: reschedule_valid(
                            task,
                            claims,
                            target_id,
                            observations,
                            subject_calls,
                            actual_fresh,
                            today,
                            own_user_id=run_record.get("own_user_id"),
                            pre_action_snapshot=pre_action_snapshot,
                        ),
                        secrets,
                    )
    if "call_log" in run_record:
        if task.get("writes") and status == "selected" and not (
            isinstance(fixture, dict) and fixture.get("status") == "fixture_failed"
        ):
            _run_verifier(
                results,
                "writes_in_scope",
                lambda: writes_in_scope(subject_calls, target_id),
                secrets,
            )
        elif not task.get("writes"):
            _run_verifier(
                results,
                "no_writes",
                lambda: no_writes(subject_calls + verify_tools.calls, actual_fresh),
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


def _exact_iso_date(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return False
    return parsed.isoformat() == value


def _prepare_fixture(
    tools: ScopedWriteTools,
    *,
    target_id: str,
    own_user_id: str,
    today: date,
    path: Path,
    secrets: tuple[str, ...],
    cleanup: dict[str, Any],
) -> tuple[dict[str, Any], Path | None, dict[str, Any] | None]:
    tools.phase = "fixture"
    try:
        before = tools.call_tool("WorkOrder.get", {"id": target_id}).structured
    except Exception as error:
        return {"status": "fixture_failed", "reason": type(error).__name__}, None, None
    if not isinstance(before, dict):
        return {"status": "fixture_failed", "reason": "invalid_fixture_read"}, None, None
    mutation = {
        "id": target_id,
        "planned_start_date": (today - timedelta(days=10)).isoformat(),
        "planned_end_date": (today - timedelta(days=3)).isoformat(),
    }
    fixture_record = {
        "status": "prepared",
        "pre_fixture": before,
        "intended_mutation": mutation,
        "recorded_at": _utc_now().isoformat(),
    }
    fixture_path = write_exclusive(path, fixture_record, secrets)
    cleanup["fixture_path"] = fixture_path
    safe = (
        before.get("id") == target_id
        and before.get("status") == "draft"
        and isinstance(before.get("created_by"), str)
        and before.get("created_by") == own_user_id
        and all(
            _exact_iso_date(before.get(field))
            for field in ("planned_start_date", "planned_end_date")
        )
    )
    if not safe:
        return {"status": "fixture_failed", "reason": "fixture_not_safely_restorable"}, fixture_path, None
    cleanup["write_attempted"] = True
    try:
        tools.call_tool("WorkOrder.update", mutation, allow_write=True)
    except Exception as error:
        return {"status": "fixture_failed", "reason": type(error).__name__}, fixture_path, None
    try:
        snapshot = tools.call_tool("WorkOrder.get", {"id": target_id}).structured
    except Exception as error:
        return {"status": "fixture_failed", "reason": type(error).__name__}, fixture_path, None
    valid = (
        isinstance(snapshot, dict)
        and snapshot.get("status") == "draft"
        and snapshot.get("created_by") == own_user_id
        and snapshot.get("planned_start_date") == mutation["planned_start_date"]
        and snapshot.get("planned_end_date") == mutation["planned_end_date"]
    )
    if not valid:
        return {"status": "fixture_failed", "reason": "fixture_confirmation_mismatch"}, fixture_path, None
    return {"status": "ready", "path": fixture_path.name}, fixture_path, snapshot


def _restore_fixture(
    tools: ScopedWriteTools,
    *,
    fixture_path: Path,
    restore_path: Path,
    target_id: str,
    own_user_id: str,
    secrets: tuple[str, ...],
    write_attempted: bool,
) -> dict[str, Any]:
    with fixture_path.open(encoding="utf-8") as stream:
        persisted = json.load(stream)
    before = persisted.get("pre_fixture") if isinstance(persisted, dict) else None
    start_index = len(tools.calls)
    tools.phase = "restore"
    outcome: dict[str, Any] = {
        "status": "restore_failed",
        "reason": "pre_fixture_record_missing",
        "fixture_file": fixture_path.name,
        "recorded_at": _utc_now().isoformat(),
    }
    if not write_attempted:
        outcome.update({"status": "not_needed", "reason": "fixture_write_not_attempted"})
    elif isinstance(before, dict):
        try:
            current = tools.call_tool("WorkOrder.get", {"id": target_id}).structured
        except Exception as error:
            outcome["reason"] = f"restore_read_failed:{type(error).__name__}"
        else:
            original_dates = {
                "planned_start_date": before.get("planned_start_date"),
                "planned_end_date": before.get("planned_end_date"),
            }
            already_original = isinstance(current, dict) and all(
                current.get(field) == value for field, value in original_dates.items()
            )
            if already_original:
                outcome.update({"status": "restored", "reason": "already_original"})
            elif not all(_exact_iso_date(value) for value in original_dates.values()):
                outcome["reason"] = "original_dates_not_restorable"
            else:
                safe = (
                    isinstance(current, dict)
                    and current.get("status") == "draft"
                    and isinstance(current.get("created_by"), str)
                    and current.get("created_by") == own_user_id
                )
                if not safe:
                    outcome["reason"] = "restore_guard_failed"
                else:
                    outcome["reason"] = None
                    try:
                        tools.call_tool(
                            "WorkOrder.update",
                            {"id": target_id, **original_dates},
                            allow_write=True,
                        )
                    except Exception as error:
                        outcome["reason"] = f"restore_write_failed:{type(error).__name__}"
                    try:
                        confirmed = tools.call_tool("WorkOrder.get", {"id": target_id}).structured
                    except Exception as error:
                        outcome["reason"] = f"restore_confirmation_failed:{type(error).__name__}"
                    else:
                        restored = (
                            isinstance(confirmed, dict)
                            and confirmed.get("status") == "draft"
                            and confirmed.get("created_by") == own_user_id
                            and all(confirmed.get(field) == value for field, value in original_dates.items())
                        )
                        if restored:
                            outcome.update({"status": "restored", "reason": None})
                        elif not outcome.get("reason"):
                            outcome["reason"] = "restore_confirmation_mismatch"
    restore_calls = tools.calls[start_index:]
    outcome["reads"] = [
        {
            "outcome": call.get("outcome"),
            "record": call.get("structuredContent"),
            "error": call.get("error"),
        }
        for call in restore_calls
        if call.get("tool") == "WorkOrder.get"
    ]
    outcome["write_calls"] = [
        {
            "arguments": call.get("arguments"),
            "outcome": call.get("outcome"),
            "error": call.get("error"),
        }
        for call in restore_calls
        if call.get("write") is True
    ]
    persisted_path = write_exclusive(restore_path, outcome, secrets)
    outcome["path"] = persisted_path.name
    return outcome


def _run_tasks(
    tenant: str,
    *,
    failed_restores: list[dict[str, Any]],
    task_ids: list[str] | None = None,
    today: date | None = None,
    transport: Any = None,
    get_transport: Any = None,
    env_file: Path | None = None,
    runs_dir: Path | None = None,
    allow_draft_writes: bool = False,
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
    identity_error: Exception | None = None
    try:
        own_user_id = McpClient(
            base_url,
            token,
            transport=transport,
            get_transport=get_transport,
        ).current_user_id()
    except Exception as error:
        own_user_id = None
        identity_error = error

    for task in selected_tasks:
        task_started = _utc_now()
        stem = f"{task_started.strftime('%Y%m%dT%H%M%S%fZ')}_{normalized_tenant}_{task['id']}"
        client = McpClient(base_url, token, transport=transport, get_transport=get_transport)
        if task.get("writes") and allow_draft_writes and own_user_id is not None:
            tools: ReadOnlyTools = ScopedWriteTools(
                client,
                target_id=None,
                own_user_id=own_user_id,
                phase="select",
            )
        else:
            tools = ReadOnlyTools(client, phase="select")
        harness_errors: list[dict[str, Any]] = []
        subject_output: dict[str, Any] | None = None
        answer: dict[str, Any] | None = None
        label = SUBJECT_LABEL
        fixture: dict[str, Any] | None = None
        fixture_path: Path | None = None
        fixture_cleanup: dict[str, Any] = {
            "fixture_path": None,
            "write_attempted": False,
        }
        pre_action_snapshot: dict[str, Any] | None = None
        restore: dict[str, Any] | None = None
        score_record: dict[str, Any] | None = None
        run_path: Path | None = None
        persistence_error: Exception | None = None

        if task.get("writes") and not allow_draft_writes:
            selection = {
                "selector": task["selector"],
                "today": run_today.isoformat(),
                "selected_at": _utc_now().isoformat(),
                "status": "draft_writes_disabled",
                "complete": True,
                "target_id": None,
                "reason": "draft writes not enabled",
            }
        elif task.get("writes") and run_today != date.today():
            selection = {
                "selector": task["selector"],
                "today": run_today.isoformat(),
                "selected_at": _utc_now().isoformat(),
                "status": "configuration_error",
                "complete": False,
                "target_id": None,
                "reason": "draft writes require today to equal the system date",
            }
        elif task.get("writes") and identity_error is not None:
            selection = {
                "selector": task["selector"],
                "today": run_today.isoformat(),
                "selected_at": _utc_now().isoformat(),
                "status": "selection_error",
                "complete": False,
                "target_id": None,
                "reason": type(identity_error).__name__,
            }
            harness_errors.append(_error(identity_error, secrets, phase="identity"))
        else:
            try:
                selection = select_target(
                    task,
                    tools,
                    run_today,
                    own_user_id=own_user_id,
                )
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

        subject_elapsed_ms = 0.0
        try:
            if selection.get("status") == "selected" and task.get("writes"):
                assert isinstance(tools, ScopedWriteTools)
                assert isinstance(own_user_id, str)
                target_id = selection.get("target_id")
                tools.target_id = target_id
                fixture, fixture_path, pre_action_snapshot = _prepare_fixture(
                    tools,
                    target_id=target_id,
                    own_user_id=own_user_id,
                    today=run_today,
                    path=output_dir / f"{stem}.fixture.json",
                    secrets=secrets,
                    cleanup=fixture_cleanup,
                )
            ready = not task.get("writes") or (
                isinstance(fixture, dict) and fixture.get("status") == "ready"
            )
            if selection.get("status") == "selected" and ready:
                tools.phase = "subject"
                subject_started = time.perf_counter_ns()
                try:
                    subject_output = investigate_subject(
                        tools,
                        request=task["request"],
                        request_kind=task["request_kind"],
                        target_id=selection.get("target_id"),
                        today=run_today,
                        reschedule=task.get("reschedule", False),
                        own_user_id=own_user_id,
                    )
                    answer = subject_output.get("answer")
                    label = subject_output.get("label", SUBJECT_LABEL)
                except Exception as error:
                    harness_errors.append(_error(error, secrets, phase="subject"))
                subject_elapsed_ms = round(
                    (time.perf_counter_ns() - subject_started) / 1_000_000,
                    3,
                )

            run_record = {
                "schema_version": SCHEMA_VERSION,
                "run_file": f"{stem}.json",
                "task": public_task(task),
                "tenant": normalized_tenant,
                "today": run_today.isoformat(),
                "git": git_metadata,
                "selection": selection,
                "fixture": fixture,
                "pre_action_snapshot": pre_action_snapshot,
                "own_user_id": own_user_id,
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
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
            persistence_error = HarnessPersistenceError(
                f"Could not persist or reload task {task['id']}: {redact(str(error), secrets)}"
            )
        finally:
            cleanup_path = fixture_cleanup.get("fixture_path")
            restore_interruption: BaseException | None = None
            if isinstance(cleanup_path, Path) and isinstance(tools, ScopedWriteTools):
                fixture_path = cleanup_path
                try:
                    restore = _restore_fixture(
                        tools,
                        fixture_path=fixture_path,
                        restore_path=output_dir / f"{stem}.restore.json",
                        target_id=selection.get("target_id"),
                        own_user_id=own_user_id,
                        secrets=secrets,
                        write_attempted=fixture_cleanup.get("write_attempted") is True,
                    )
                except BaseException as error:
                    restore = {"status": "restore_failed", "reason": type(error).__name__}
                    if not isinstance(error, Exception):
                        restore_interruption = error
            if not _restore_succeeded(restore):
                assert restore is not None
                failure = _failed_restore(task["id"], restore)
                failed_restores.append(failure)
                _report_failed_restore(failure)
            if restore_interruption is not None:
                raise restore_interruption
        if persistence_error is not None:
            raise persistence_error
        assert score_record is not None and run_path is not None
        if restore is not None:
            restore_verdict = "pass" if _restore_succeeded(restore) else "fail"
            score_record["verifiers"].append(
                _simple_result(
                    "restore",
                    restore_verdict,
                    restore.get("reason") or "fixture dates restored",
                )
            )
            score_record["restore"] = restore
            score_record["verdict"] = _score_verdict(score_record["verifiers"])
        score_record["run_file"] = run_path.name
        try:
            score_path = write_exclusive(
                run_path.with_name(f"{run_path.stem}.score.json"),
                score_record,
                secrets,
            )
        except (OSError, TypeError, ValueError) as error:
            raise HarnessPersistenceError(
                f"Could not persist score for task {task['id']}: {redact(str(error), secrets)}",
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
                "restore": restore,
                "restore_failed": not _restore_succeeded(restore),
            }
        )
    return summaries


def run_tasks(
    tenant: str,
    *,
    task_ids: list[str] | None = None,
    today: date | None = None,
    transport: Any = None,
    get_transport: Any = None,
    env_file: Path | None = None,
    runs_dir: Path | None = None,
    allow_draft_writes: bool = False,
) -> list[dict[str, Any]]:
    """Run selected tasks and attach all failed restores to any propagated exception."""
    failed_restores: list[dict[str, Any]] = []
    try:
        return _run_tasks(
            tenant,
            failed_restores=failed_restores,
            task_ids=task_ids,
            today=today,
            transport=transport,
            get_transport=get_transport,
            env_file=env_file,
            runs_dir=runs_dir,
            allow_draft_writes=allow_draft_writes,
        )
    except BaseException as error:
        error.failed_restores = failed_restores
        if isinstance(error, HarnessPersistenceError) and failed_restores:
            latest = failed_restores[-1]
            error.restore_task_id = latest["task_id"]
            error.restore = latest["restore"]
        raise


__all__ = [
    "HarnessConfigurationError",
    "HarnessLoginError",
    "HarnessPersistenceError",
    "run_tasks",
]
