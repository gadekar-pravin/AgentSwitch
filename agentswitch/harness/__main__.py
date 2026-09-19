"""Command-line entry point for the read-only harness."""

import argparse
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any

from agentswitch.config import DEFAULT_CONFIG_PATH

from .runner import (
    HarnessConfigurationError,
    HarnessLoginError,
    HarnessPersistenceError,
    run_tasks,
)
from .tasks import TASKS


def _date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError("expected YYYY-MM-DD") from None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the AgentSwitch evaluation harness")
    parser.add_argument("--tenant", required=True, choices=("suryodaya", "keystone"))
    parser.add_argument(
        "--subject",
        choices=("deterministic", "llm"),
        default="deterministic",
        help="subject implementation to evaluate",
    )
    parser.add_argument(
        "--task",
        action="append",
        choices=tuple(task["id"] for task in TASKS),
        help="task to run; repeat for multiple tasks (default: all)",
    )
    parser.add_argument("--today", type=_date, default=date.today(), help="evaluation date (YYYY-MM-DD)")
    parser.add_argument(
        "--allow-draft-writes",
        action="store_true",
        help="enable the owned-draft reschedule fixture and scoped subject write",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help="runtime TOML configuration path",
    )
    return parser


def _exception_failed_restores(error: BaseException) -> list[dict[str, Any]]:
    failures = getattr(error, "failed_restores", [])
    return failures if isinstance(failures, list) else []


def _restore_failure_line(failure: dict[str, Any]) -> str:
    restore = failure.get("restore")
    if not isinstance(restore, dict):
        restore = {}
    raw_path = restore.get("path")
    restore_file = Path(raw_path).name if isinstance(raw_path, str) and raw_path else "no restore file"
    return (
        f"RESTORE FAILED for {failure.get('task_id')}: {restore.get('reason')}; "
        f"see {restore_file}"
    )


def _report_failed_restores(failures: list[dict[str, Any]]) -> None:
    for failure in failures:
        print(_restore_failure_line(failure))


def main() -> int:
    args = _parser().parse_args()
    try:
        summaries = run_tasks(
            args.tenant,
            task_ids=args.task,
            today=args.today,
            allow_draft_writes=args.allow_draft_writes,
            subject=args.subject,
            config_file=args.config,
        )
    except HarnessConfigurationError as error:
        failures = _exception_failed_restores(error)
        _report_failed_restores(failures)
        print(f"configuration error: {error}")
        return 5 if failures else 2
    except HarnessLoginError as error:
        failures = _exception_failed_restores(error)
        _report_failed_restores(failures)
        print(f"login/transport error: {error}")
        return 5 if failures else 3
    except HarnessPersistenceError as error:
        failures = _exception_failed_restores(error)
        _report_failed_restores(failures)
        print(f"persistence error: {error}")
        return 5 if failures else 4
    except KeyboardInterrupt as error:
        failures = _exception_failed_restores(error)
        _report_failed_restores(failures)
        return 5 if failures else 130
    except BaseException as error:
        failures = _exception_failed_restores(error)
        if failures:
            _report_failed_restores(failures)
            return 5
        raise
    counts = Counter(summary["verdict"] for summary in summaries)
    for summary in summaries:
        details = [
            f"{result['name']}: {result['reason']}"
            for result in summary["verifiers"]
            if result["verdict"] in {"fail", "inconclusive"}
        ]
        label = summary["subject_label"]
        if summary["routed_refusal"]:
            label += " (deterministic adapter scope refusal; no LLM or boundary reasoning exercised)"
        suffix = f"; {'; '.join(details)}" if details else ""
        print(f"{summary['task_id']}: {summary['verdict']} [{label}]{suffix}")
        if summary.get("restore_failed"):
            print(
                _restore_failure_line(
                    {"task_id": summary["task_id"], "restore": summary.get("restore")}
                )
            )
    print(
        "counts: "
        f"pass={counts['pass']} fail={counts['fail']} "
        f"inconclusive={counts['inconclusive']} not_applicable={counts['not_applicable']}"
    )
    return 5 if any(summary.get("restore_failed") for summary in summaries) else 0


if __name__ == "__main__":
    raise SystemExit(main())
