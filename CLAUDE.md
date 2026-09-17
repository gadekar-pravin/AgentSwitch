# CLAUDE.md

Shared team guidance for Claude Code in this repository. The assignment is
[agentswitch-team-brief.md](agentswitch-team-brief.md); read it before non-trivial work. The brief is
kept out of git: each teammate places the course copy at the repo root. If it is missing, say so
rather than guessing requirements. `docs/assignment-overview.md` (also local-only, gitignored) is a
summary, not the source.

Personal instructions belong in `CLAUDE.local.md` (gitignored, loaded after this file). Change this
file only for rules the whole team agrees on.

## Project

Team 04 capstone: an agent for seat **Production** (app `manufacturing`) on the AgentSwitch
platform, driven over MCP, plus a harness that scores it. Target request: "This work order is
late. Find out why, tell me what it blocks downstream, and reschedule what you can."

Current stage: scaffold only. No agent, MCP client or harness code exists yet. The brief's order
is domain learning → competitor study → gap report ([docs/gap-report.md](docs/gap-report.md)) →
agent → harness. The LLM provider is not chosen yet; do not wire one without asking.

## Commands

`uv` only — never `pip`, never a bare `python` or `pytest`.

```bash
uv sync
uv run ruff check .
uv run pytest        # exits 5 until the first hand-written test exists
```

## Tests are hand-written — do not author them

The brief scores a test written by Claude or Codex at **zero**. Never create or edit test bodies,
assertions, fixtures or `conftest.py` under `tests/`, even when asked to "add coverage". Running
tests, explaining failures and proposing what a test should check (in prose) are fine.

## Platform invariants

- **MCP is the primary interface** (`POST $AS/api/mcp`, JSON-RPC 2.0, protocol `2025-11-25`).
  POST only: no SSE stream, no batching. REST is the documented fallback.
- **A JSON-RPC error comes back on HTTP 200.** Check the envelope; only auth failures are `401`.
- **The tool catalogue is seat-scoped and schemas are closed.** Use only tool names returned by
  `tools/list` and only the arguments in each tool's schema. Never guess a name or argument.
- **The seat boundary looks different per door.** Over MCP, a tool the seat may not use is absent
  from `tools/list`; it is never offered and then refused. Over REST and the UI, another app's data
  returns `403`. Neither is a bug. Escalate (EA, admin, human); do not work around it.
- **The book is shared and changes underneath us.** Re-read a record immediately before writing.
  Writes that the task requires (e.g. rescheduling a late work order) are allowed within seat
  permissions and workflow rules. Never "tidy up", delete or bulk-edit data the task did not ask
  to change.
- **Never call `PUT /api/accounting/locale`.** It switches a live company's accounting regime for
  every team.
- **Every write is attributed to our login.** Treat live writes as real operations; confirm with the
  user before running a write outside an agreed task.

## Harness rules (from the grading section)

- Verifiers judge by reading platform state through MCP/REST, never the agent's prose. A successful
  write response alone is not evidence; re-read the record.
- Write every run record to `/runs/` before scoring. `/runs/` is gitignored (live tenant data) but is
  evidence: do not delete it.
- Keep at least one task whose correct answer is a refusal (unsupported by the data, or outside the
  seat).

## Secrets

Credentials live in `.env` (gitignored); `.env.example` holds placeholders only. Never print a
password or bearer token into logs, run records, docs or commits. Raw platform dumps go in `/dumps/`
(gitignored).
