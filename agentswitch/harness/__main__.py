"""Command-line entry point for the read-only harness."""

import argparse
from collections import Counter
from datetime import date

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
    parser = argparse.ArgumentParser(description="Run the AgentSwitch read-only evaluation harness")
    parser.add_argument("--tenant", required=True, choices=("suryodaya", "keystone"))
    parser.add_argument(
        "--task",
        action="append",
        choices=tuple(task["id"] for task in TASKS),
        help="task to run; repeat for multiple tasks (default: all)",
    )
    parser.add_argument("--today", type=_date, default=date.today(), help="evaluation date (YYYY-MM-DD)")
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        summaries = run_tasks(args.tenant, task_ids=args.task, today=args.today)
    except HarnessConfigurationError as error:
        print(f"configuration error: {error}")
        return 2
    except HarnessLoginError as error:
        print(f"login/transport error: {error}")
        return 3
    except HarnessPersistenceError as error:
        print(f"persistence error: {error}")
        return 4
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
    print(
        "counts: "
        f"pass={counts['pass']} fail={counts['fail']} "
        f"inconclusive={counts['inconclusive']} not_applicable={counts['not_applicable']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
