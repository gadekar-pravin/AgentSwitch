# Assignment overview — what we must build

A plain-language reading of the course brief `agentswitch-team-brief.md` for team 04. The brief is
not in git; each teammate keeps the course copy at the repo root.
Section references (§) point to the brief. Where this document interprets rather than quotes, it
says so.

## In one paragraph

Build an AI agent that does real production-planning work on a live, shared ERP-style platform,
plus a test harness that proves the agent's answers are right. Before building, study the best
competing product in manufacturing and write a one-page gap report.

## The setup

- The school runs a business platform in two copies (§1): **Suryodaya Precision Works** (India)
  and **Keystone Precision Works LLC** (US). Their URLs are in the brief and the team's local
  `.env`. Same software; 424 entity types, real workflow states, permissions and data. Same email
  on both, a different password for each.
- Team 04 owns the **Production** seat (§2): the `manufacturing` app — work orders, BOMs, routings,
  job cards. Every seat also has `agent` (the agent's own workspace) and `crm` (the shared customer
  spine).
- Other teams' apps are off-limits (§3, §4, §6). Over MCP, tools the seat may not use are absent from
  `tools/list`; over REST, another app's data returns `403`; the UI navigation simply does not show
  other apps. None of these is a bug. Cross-app data is obtained by asking an EA, an admin or a
  human, and that escalation is part of the exercise.
- Data changes underneath the agent (§3, grading). Teams sharing a seat share a book. Team 04 is the
  only Production seat, but `crm` is held by every seat and other teams' agents act on the platform
  at the same time. Treat any row as changeable. This is deliberate.
- The agent's own `AgentMemory`, `AgentMessage` and `AgentSkill` writes are private to the team;
  pre-existing seed rows are shared (§3).

## What is graded

| # | Deliverable | What it must contain |
| --- | --- | --- |
| 1 | **Gap report** (due week one) | One page, three questions, answered with specifics, against a product we found ourselves. |
| 2 | **The agent** | Answers our seat's questions against live data that other teams are changing. |
| 3 | **The harness** | Our own loop; a task set with verifiers that read the database, not the agent's prose; every run written to disk before anything is scored. |
| 4 | **At least one refusal task** | A request the data cannot support, or the seat is not permitted to do. The agent must say so. Inventing a confident answer fails the task. |
| 5 | **Hand-written tests** | 10 points per test; 100 points per real AgentSwitch bug found. A test written by Claude or Codex scores zero. |

## The order of work (§8)

1. **Learn the domain.** Use the web UI to look at real work orders, BOMs, routings and job cards.
   Read `/api/schemas` for their fields and workflow states. Most objects move through a state
   machine, and the transitions carry the rules.
2. **Find the best product in our domain.** Finding it is our job. Prefer AI-native products built
   in the last three years over old products with a chatbot added. Study it seriously: trial, demos,
   docs, changelog, pricing page. A day is not too much.
3. **Write the gap report.** Three questions:
   - What do they do that we do not? Name concrete features.
   - Which of those gaps can an agent close with the tools our seat already has? Which need new
     tables or endpoints (platform work, not ours)?
   - What can our agent do that their product cannot? For example: hold a goal across twenty steps,
     re-read state that changed underneath it, and decide.
4. **Build the agent, then the harness.** The agent answers the seat's questions; the harness proves
   that it does.

The brief's worked example (Ledger seat vs Rillet) shows the expected depth.

## Our request

> "This work order is late. Find out why, tell me what it blocks downstream, and reschedule what you can."

The brief says each request has "several steps, a judgement call, and a state change in the middle",
and that "an agent that answers 'list my invoices' is a demo."

The breakdown below is **our interpretation**. The concrete fields and links are unknown until we
read the schemas.

| Part | Kind of work | What the agent likely has to do |
| --- | --- | --- |
| **Why is it late?** | Investigation | Compare planned and actual dates; look for causes such as missing BOM materials, a blocked or slow job card, a routing or workstation bottleneck, or a stuck workflow state. |
| **What does it block downstream?** | Dependency tracing | Follow links from the work order to whatever depends on it (for example parent work orders, sales orders, deliveries). Where a link leads into another team's app, say so and escalate instead of guessing. |
| **Reschedule what you can** | Judgement and a write | Decide what can move; re-read each record immediately before changing it; change it only through allowed fields or workflow transitions; report what could not be rescheduled and why. |

The reschedule is a real write on shared data, attributed to our login (§9).

## Rules that shape the build

- **MCP is the primary interface** (§4, §6): JSON-RPC 2.0 over `POST $AS/api/mcp`, protocol
  `2025-11-25`, no SSE stream, no batching. REST is the documented fallback; the UI is for seeing and
  learning.
- **Use only tools returned by `tools/list`, with only the arguments in their schemas** (§6). Tool
  names follow the data model: `<Entity>.list`, `.get`, `.create`, `.update`, one tool per workflow
  transition, plus the app's own endpoints. Schemas are closed: an extra argument is rejected.
- **A JSON-RPC error comes back on HTTP 200** (§6). Only authentication fails at the HTTP layer
  (`401`). Check the response envelope.
- **Re-read before acting; do not "tidy up" data we did not create** (§3).
- **Think before switching the accounting locale** (§1). It is a real, audited operation on a book
  other teams use.
- **The agent runs on our machine, with our own model keys, driving the platform over MCP** (§8).
  "Your own harness, not a wrapper around ours."
- **Report platform bugs with ids** (§10): what we did, what we expected, what happened, and the
  page, agent seat and job id. File them with the in-app "Report a problem" button or
  `POST $AS/api/bug-report`; the limit is 20 per hour. Some behaviours are known and not worth
  reporting (§10).
- **Our passwords are ours alone** (§9). Every write is attributed to whoever is signed in.

## Open questions (not answered by the brief)

- **How to submit.** The brief does not say whether code, run records and the gap report go to a
  repo, a form or a demo.
- **What "read the database" means for verifiers.** We only have MCP and REST, so re-reading records
  through them is the likely meaning. Not confirmed.
- **What "late" and "downstream" mean for this task.** Schemas show fields and links but not which
  date defines lateness or how far downstream to trace. Inspect schemas, real records and workflow
  rules; clarify what remains.
- **Whether both businesses are required.** The brief says start on Suryodaya and move to Keystone
  when we want the US contrast, but does not say the agent must handle both.
- **What counts as a "test you wrote by hand".** The brief gives 10 points a test but does not say
  whether that means unit tests, harness tasks with verifiers, or both.
- **Deadlines beyond week one.** Only the gap report has a date (week one); the brief gives none for
  the agent, harness or tests.

Ask the instructor or check the team channel.

## Next steps

1. Get our two logins from the team channel (§9). Log in as in §5, then confirm roles and
   `allowed_apps` with an authenticated `GET /api/auth/me`. (This repo keeps the passwords in a
   gitignored `.env`; that is our choice, not a brief requirement.)
2. Spend time in the UI on real manufacturing records; record findings in
   [domain-notes.md](domain-notes.md).
3. Pick the competitor product and start [gap-report.md](gap-report.md).
4. Then build the MCP client and pull the seat's schemas and tool list.
