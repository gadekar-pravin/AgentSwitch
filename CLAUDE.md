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

- **This file** holds rules the whole team depends on: test authorship, platform invariants,
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
  especially the test-authorship rule.

## Project

Team 04 capstone: an agent for seat **Production** (app `manufacturing`) on the AgentSwitch
platform, driven over MCP, plus a harness that scores it. Target request: "This work order is
late. Find out why, tell me what it blocks downstream, and reschedule what you can."

Current stage: the MCP client, read-only investigation, guarded reschedule, LLM agent loop and
harness exist; [README.md](README.md) lists each with its live-check status. The only tests are
AI-originated (`Spec: AI`). The LLM agent calls OpenRouter (model in `config/agentswitch.toml`,
currently `z-ai/glm-5.3-flash`; `OPENROUTER_MODEL` overrides it); do not change or add a provider
without asking. The instructor says the platform's own LLM will be integrated with our pipeline
once the harness is ready (Release 1 note, 2026-09-17).

Next: [docs/harness-architecture-plan.md](docs/harness-architecture-plan.md) ports the agent and
harness to the S17Code architecture in phases. The team agreed it on 2026-09-19; phases 1, 2,
2b and 3 are done. Follow the plan's phase order.

## Commands

`uv` only — never `pip`, never a bare `python` or `pytest`.

```bash
uv sync
uv run ruff check .
uv run pytest
```

## Tests: AI writes the code; what counts is who specified the test

The brief says: "Tests you wrote by hand. Ten points a test … A test written by Claude or Codex
scores zero." The team reads this by who **planned and specified** the test, not who typed it:

- **Human-specified test (scores):** a teammate decided the test and specified what it checks, its
  inputs and the expected result. AI then writes the code.
- **AI-originated test (scores zero):** a test Claude or Codex decided to add on its own while
  developing code.

All test code is written by AI (Claude, Codex), including fixtures and `conftest.py`. Rules:

- Every test function's docstring states its origin: `Spec: human (<name>)` or `Spec: AI (<tool>)`.
- Implement a human-specified test exactly as specified. If the spec looks wrong or cannot be
  implemented as written, ask its author; do not change what it checks.
- Never mark a test `Spec: human` unless a named teammate specified it. A test list that AI drafted
  (for example in a plan) is AI-originated until a teammate adopts and owns each spec.
- Fix the code, not the test: do not weaken an assertion to make a failing test pass.

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
- **The in-app agent is live on our login (Release 1, 2026-09-17).** A teammate may chat with it in
  the UI (Agent → New chat; reopening an old chat can fail) to compare answers. It acts as our login,
  so its writes are ours: apply the same write rules. Never call it from our agent or harness. Each
  agent has 1M tokens a day and the workspace shares 10M; do not spend them on idle chats.
- **Bug reports:** list ours with `GET $AS/api/bug-report/mine`. Do not call `BugReport.list`; it
  leaked other teams' reports before Release 1, and we have not confirmed a fix. A `No
  AGENTSWITCH_GITHUB_TOKEN configured` note in a bug-report response is expected, not an error.
  Fix status is on the Bug Board, not in `/mine` (still `new` after Release 1). Reports filed after
  14:35 on 2026-09-17 go into the next release.

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
