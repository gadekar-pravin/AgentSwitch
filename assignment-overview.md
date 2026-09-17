# Assignment overview

This is a plain-language summary of `agentswitch-team-brief.md` for team 04. The brief is the source
of truth and is stored locally at the repository root. Section references (§) point to the brief.

## Goal

Build an AI agent for the **Production** seat (`manufacturing`) on the AgentSwitch platform. The
agent must investigate a late work order, explain its downstream impact, and reschedule what it can.
Build a harness that verifies the result from platform state, not from the agent's answer.

Before building the agent, study a strong manufacturing product and write a one-page gap report.

## Platform context

- AgentSwitch has two businesses: Suryodaya Precision Works (India) and Keystone Precision Works
  LLC (US). Start with Suryodaya; use Keystone when a US comparison is useful (§1).
- Team 04 can access work orders, BOMs, routings, job cards, the shared `crm` app, and the agent's
  workspace (§2).
- Other apps are outside our seat. Their tools are absent from MCP `tools/list`, their REST data
  returns `403`, and they do not appear in the UI. Escalate cross-app needs to an EA, admin, or human
  instead of working around the boundary (§3, §4, §6).
- The platform is live and shared. Records may change between reads. Re-read a record immediately
  before writing, and change only what the task requires (§3).
- New `AgentMemory`, `AgentMessage`, and `AgentSkill` records are private to the team. Seed records
  are shared (§3).

## Deliverables

1. **One-page gap report, due in week one.** Compare AgentSwitch with a manufacturing product we
   found and researched ourselves. Answer:
   - What does the competitor do that AgentSwitch does not?
   - Which gaps can our agent close with existing tools, and which require platform changes?
   - What can our agent do that the competitor cannot?
2. **Production agent.** Answer the assigned request using current platform data.
3. **Harness.** Use our own agent loop and task set. Verifiers must re-read platform state instead of
   trusting the agent's prose or a successful write response. Save every run under `/runs/` before
   scoring it.
4. **At least one refusal task.** Include a request that the available data cannot support or the
   Production seat cannot perform. The correct result must be a refusal, not a guess.
5. **Hand-written tests.** The brief awards 10 points per test and 100 points per real AgentSwitch
   bug. Tests written by Claude or Codex receive zero, so AI tools must not create or edit tests.

## Required order of work (§8)

1. **Learn the domain.** Inspect real manufacturing records in the UI. Read `/api/schemas` to learn
   fields, relationships, workflow states, and valid transitions.
2. **Study a leading manufacturing product.** Prefer an AI-native product. Review its trial, demos,
   documentation, changelog, and pricing.
3. **Write the gap report.** Name specific missing features and separate agent-solvable gaps from
   gaps that need new platform data or APIs.
4. **Build the agent, then the harness.** The agent performs the work; the harness proves the
   outcome.

The Ledger-versus-Rillet example in the brief shows the expected research depth. We must choose and
study our own manufacturing competitor.

## Assigned request

> "This work order is late. Find out why, tell me what it blocks downstream, and reschedule what you
> can."

The exact fields and relationships must come from the schemas and live records. The agent will
likely need to:

1. Compare planned and actual progress and identify evidence for the delay.
2. Trace downstream dependencies as far as the Production seat allows. Escalate blocked cross-app
   checks instead of guessing.
3. Decide what can be rescheduled, re-read each target, make only valid changes, and verify each
   change by reading the record again.
4. Report what changed, what could not change, and why.

Rescheduling is a real write to shared data and is attributed to our login.

## Build rules

- Use MCP as the primary interface: JSON-RPC 2.0 over `POST $AS/api/mcp`, protocol `2025-11-25`.
  There is no SSE stream or batching. REST is the fallback; the UI is for learning and inspection
  (§4, §6).
- Use only tool names returned by `tools/list` and only arguments defined by each tool's closed JSON
  Schema. Never guess tool names, fields, or transition arguments (§6).
- Inspect the JSON-RPC envelope for errors. Tool errors still return HTTP `200`; authentication
  failures return HTTP `401` (§6).
- Re-read records before writes. Do not delete, bulk-edit, or tidy unrelated data (§3).
- Never call `PUT /api/accounting/locale`; it changes a shared company's accounting regime.
- Build and run our own agent locally with our model keys. Do not drive AgentSwitch's built-in
  Agents (instructor note, 2026-09-17).
- Keep credentials in `.env`. Never write passwords or tokens to logs, run records, documentation,
  or commits (§9).

## Bug reports

Report reproducible platform bugs with the action, expected result, actual result, page, seat, and
job ID. Use the in-app button or `POST $AS/api/bug-report`; the limit is 20 reports per hour (§10).

Use `GET $AS/api/bug-report/mine` to view our reports. Do not use `BugReport.list`; it currently
exposes other teams' reports. A response saying `No AGENTSWITCH_GITHUB_TOKEN configured` is expected
and does not mean the report failed (instructor note, 2026-09-17).

Do not report documented behavior such as cross-app `403` responses, seat-scoped missing tools, or
other teams changing shared data.

## Open questions

The brief does not define:

- how to package and submit the agent, harness, run records, or gap report;
- the exact verifier interface expected by the platform;
- the precise definitions of "late" and "downstream" for the assigned request;
- whether the final agent must support both businesses;
- whether "hand-written tests" means unit tests, harness tasks, or both; or
- deadlines for the agent, harness, and tests.

Confirm these points with the instructor or team channel.

## Next steps

1. ~~Verify both logins with `GET /api/auth/me`.~~ Completed on 2026-09-17; see
   [docs/domain-notes.md](docs/domain-notes.md).
2. Inspect real manufacturing records and document findings in
   [docs/domain-notes.md](docs/domain-notes.md).
3. Choose the competitor and write [docs/gap-report.md](docs/gap-report.md), due in week one.
4. Build the MCP client, then retrieve the Production seat's schemas and tool catalogue.
