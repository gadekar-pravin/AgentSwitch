# CLAUDE.md

Shared team guidance for Claude Code in this repository. The assignment is
[agentswitch-team-brief.md](agentswitch-team-brief.md); read it before non-trivial work. The brief is
kept out of git: it is a saved copy of the AgentSwitch brief lesson on the course site, and each
teammate places that copy at the repo root. Do not download the lesson again. If it is missing, say so
rather than guessing requirements; [assignment-overview.md](assignment-overview.md) is a
summary, not the source.

## Shared and personal instructions

Claude Code loads these in order ([memory docs](https://code.claude.com/docs/en/memory.md)):

| Order | File | Location | Shared? |
| --- | --- | --- | --- |
| 1 | `~/.claude/CLAUDE.md` | each person's home directory | No; applies to all of that person's projects |
| 2 | `CLAUDE.md` (this file) | repo root, committed | Yes |
| 3 | `CLAUDE.local.md` | repo root, gitignored | No; each teammate's own copy |

- **This file** holds rules the whole team depends on: no AI-written tests, platform invariants,
  harness rules, secrets. Change it only by a commit the team agrees on; pull before editing and keep
  edits small.
- **`CLAUDE.local.md`** holds personal, project-specific preferences, e.g. which part of the project
  you own, a default tenant, reply style. It is never committed, so it cannot conflict.
- **`~/.claude/CLAUDE.md`** holds personal habits for every project.
- All three become context; a later file is not a guaranteed override. If `CLAUDE.local.md`
  contradicts this file, follow this file and point out the conflict. Personal files should add
  preferences, not relax team rules.
- **Codex reads `AGENTS.md`, not these files.** Make `AGENTS.md` a local symlink to this file:
  `ln -s CLAUDE.md AGENTS.md` at the repo root. It is gitignored, so this file stays the only source;
  never edit `AGENTS.md` as a separate copy. Codex has no `CLAUDE.local.md`; personal Codex
  preferences go in `~/.codex/AGENTS.md`. For any other tool, give it this file's team rules,
  especially the no-AI-tests rule.

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
  from `tools/list`; it is never offered and then refused. Over REST, another app's data returns
  `403`; the UI navigation does not show other apps. None of these is a bug. Escalate (EA, admin, human); do not work around it.
- **The book is shared and changes underneath us.** Re-read a record immediately before writing.
  Writes that the task requires (e.g. rescheduling a late work order) are allowed within seat
  permissions and workflow rules. Never "tidy up", delete or bulk-edit data the task did not ask
  to change.
- **Never call `PUT /api/accounting/locale`.** It switches a live company's accounting regime for
  every team.
- **Every write is attributed to our login.** Treat live writes as real operations; confirm with the
  user before running a write outside an agreed task.
- **Do not drive the platform's built-in Agents.** Our agent calls the MCP tools and APIs directly
  (instructor's note, 2026-09-17). Submitted pipelines will replace the app's default agents.
- **Bug reports:** list ours with `GET $AS/api/bug-report/mine`. Do not call `BugReport.list`; it
  currently leaks other teams' reports (known, fix due next release). A `No
  AGENTSWITCH_GITHUB_TOKEN configured` note in a bug-report response is expected, not an error.

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
