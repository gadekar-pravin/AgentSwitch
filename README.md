# AgentSwitch — Team 04, Production

Capstone project for the EAG V3 Agentic AI course. We build an agent for the
**Production** seat (app `manufacturing`: work orders, BOMs, routings, job
cards) that drives the AgentSwitch platform over MCP, plus a harness that proves
it works. The assignment is the course brief `agentswitch-team-brief.md` (kept out of git); a plain-language
summary is in [assignment-overview.md](assignment-overview.md).

The request our agent must handle:

> "This work order is late. Find out why, tell me what it blocks downstream, and reschedule what you can."

## Deliverables

| Deliverable | Where | Status |
| --- | --- | --- |
| Gap report (week one) | [docs/gap-report.md](docs/gap-report.md) | draft, advisor-reviewed (2026-09-17); not yet agreed by the team |
| Domain learning plan (Step 1) | [docs/domain-learning-plan.md](docs/domain-learning-plan.md) | done (2026-09-17); draft date test skipped |
| Domain notes | [docs/domain-notes.md](docs/domain-notes.md) | Step 1 done (2026-09-17): tools, refusals, UI walk, links and actions, known unknowns for the agent; Release 1 changes re-checked the same day |
| Competitor study (feeds the gap report) | [docs/competitor-study.md](docs/competitor-study.md) | Carbon (source, docs) and Fulcrum (docs) studied; no hands-on trial or demo yet |
| MCP client | [agentswitch/mcp_client.py](agentswitch/mcp_client.py) | done (2026-09-18); read-only live check on Suryodaya; no hand-written tests yet |
| Investigation steps (read-only: lateness, cause candidates, downstream) | [agentswitch/investigate.py](agentswitch/investigate.py) | done (2026-09-18); read-only live checks on both tenants; no hand-written tests yet |
| Reschedule step | [agentswitch/reschedule.py](agentswitch/reschedule.py) | done (2026-09-18); writes only a draft created by our login (planned dates only), escalates everything else with a proposed date; live write check on our own Suryodaya draft, restored |
| Agent (LLM loop) | [agentswitch/agent.py](agentswitch/agent.py), [agentswitch/llm_client.py](agentswitch/llm_client.py) | done (2026-09-18); model `z-ai/glm-5.3-flash` via OpenRouter; 4/4 of the tested read-only tasks on Suryodaya (one run); write task and Keystone not yet run with it; no hand-written tests yet |
| Harness (tasks, DB-reading verifiers, run records, ≥1 refusal task) | [agentswitch/harness/](agentswitch/harness/) | done (2026-09-18); scores the LLM agent (`--subject llm`) or the deterministic `investigate()` + `reschedule()` adapter (default); deterministic: 6/6 read-only tasks on both tenants, write task passed live on Suryodaya; no hand-written tests yet |
| Human-specified tests (a teammate specifies each test; AI writes the code; AI-originated tests score zero) | `tests/` (create when writing the first test) | none yet |

Order follows the brief: learn the domain, study a leading product, write the
gap report, then build the agent and harness.

## Setup

Requires [uv](https://docs.astral.sh/uv/).

```bash
uv sync
cp .env.example .env    # then fill in the passwords from the team channel
```

Not in git, set up locally by each teammate:

- `agentswitch-team-brief.md` — copy the course brief to the repo root (it is gitignored).
- `CLAUDE.local.md` — optional personal Claude Code instructions. Claude Code loads it after the
  shared [CLAUDE.md](CLAUDE.md), which holds the team rules (including: every test states whether a human or AI specified it).
- `AGENTS.md` — for Codex, a symlink to the shared rules: `ln -s CLAUDE.md AGENTS.md`.

## Platforms

| Tenant | Business | URL variable in `.env` |
| --- | --- | --- |
| `suryodaya` | Suryodaya Precision Works (India, GST) | `AS_URL_SURYODAYA` |
| `keystone` | Keystone Precision Works LLC (US, Sales & Use Tax) | `AS_URL_KEYSTONE` |

The URLs are in the course brief; they are kept out of this repo. Start on Suryodaya. Same email on
both; the password differs per business.

Quick manual check (from the brief §5–6):

```bash
set -a; source .env; set +a    # loads the URLs, AS_EMAIL and the passwords into this shell
export AS="$AS_URL_SURYODAYA"
export TOKEN=$(curl -s -X POST "$AS/api/auth/login" -H 'Content-Type: application/json' \
  -d "{\"email\":\"$AS_EMAIL\",\"password\":\"$AS_PASSWORD_SURYODAYA\"}" \
  | python3 -c 'import sys,json; print(json.load(sys.stdin)["token"])')
curl -s "$AS/api/auth/me" -H "Authorization: Bearer $TOKEN"
```

Reading material on the live instance (login required): `$AS/docs`, `$AS/redoc`,
`$AS/api/schemas`, `$AS/api/agent/tools`.

Bug reports and fixes: [live tracker](https://claude.ai/artifact/6LvLawFFUXGoRHUQKbPg9h); the reports we filed are
listed in [docs/domain-notes.md](docs/domain-notes.md#bug-reports-filed).

## Agent

The agent answers one request about one work order. The model chooses which records to read and
whether to refuse, reschedule or finish. Code builds the scored answer (lateness, causes,
downstream) from the records the model actually read, using the same rules as `investigate()`.

- The model sees 14 read-only tools from the seat's `tools/list`, plus two local actions:
  `reschedule_work_order` and `finish`. Each list tool offers only the filters that tool needs, and
  code pages every list to its end. The menu comes from the capability registry in
  [agentswitch/capabilities.py](agentswitch/capabilities.py).
- Code checks every tool call's arguments against that tool's schema before calling MCP: unknown or
  missing keys, types, enums, lengths and ranges, plus the list-filter rules (no empty or `:placeholder`
  values, `*_id` filters are strings). A rejected call gets an `invalid_params` reply and is not sent.
  Values are never trimmed or defaulted. A seat tool whose schema uses a construct the check does not
  support is left off the menu and listed under `manifest.dropped` in the run record.
- The only write is `reschedule_work_order`, which runs `reschedule()`: planned dates on a draft
  created by our login. From the command line it writes only with `--allow-draft-writes`.
- If the answer misses a required read, the agent gets one repair message naming the exact calls
  still needed. Reads still missing after that become unknowns in the answer.
- Model calls go through OpenRouter with `data_collection: "deny"`. `OPENROUTER_API_KEY` is set in
  `.env`. The model, `max_tokens`, prices and the per-run budget are in
  [config/agentswitch.toml](config/agentswitch.toml); `OPENROUTER_MODEL` in the environment or `.env`
  overrides the model. Use `--config <path>` for another file.
- Every model attempt is admitted against the budget ($0.25 per task run), then charged from
  OpenRouter's reported cost. A call the budget cannot cover is refused before it is sent, and the
  run fails. Retries count as attempts.
- GLM is served by a third-party host (Parasail), not Z.ai. Live tenant data in tool results goes to
  that host.

```bash
uv run python -m agentswitch.agent --tenant suryodaya --work-order <id>
```

Model comparison, 2026-09-18, Suryodaya, one run each. The four read-only tasks were two refusals,
the completed order and the late order with causes:

| Model | Passed | Late-order cost |
| --- | --- | --- |
| `z-ai/glm-5.3-flash` | 4/4 | $0.042 |
| `deepseek/deepseek-v4.1-flash` | 4/4 | $0.061 |
| `openai/gpt-5.6-luna` | 3/4 (fills every filter with placeholders) | $0.035 |

`google/gemini-3.8-flash` passed 4/4 in an earlier run at $0.25 per late order and was dropped on
cost.

Graph agent (`--subject graph`) against the old loop, phase 4 exit runs (2026-09-19, GLM-5.3-flash,
code at `6f4e740`): 3 runs per task on each tenant, 36/36 pass, the same verdicts as the deterministic
subject (36/36). Costs are OpenRouter's reported cost; the old-loop column is one Suryodaya run from
the same day (the old loop has not been run on Keystone).

| Task | Old loop, Suryodaya | Graph, Suryodaya (median / max) | Graph, Keystone (median / max) |
| --- | --- | --- | --- |
| late_open_oldest | $0.036 | $0.005 / $0.008 | $0.008 / $0.019 |
| late_with_sales_order | $0.032 | $0.008 / $0.015 | $0.011 / $0.014 |
| late_with_cause | $0.027 | $0.005 / $0.005 | $0.009 / $0.010 |
| not_late_completed | $0.012 | $0.006 / $0.006 | $0.006 / $0.008 |
| refuse_not_found | $0.009 | $0.002 / $0.003 | $0.004 / $0.006 |
| refuse_outside_seat | $0.0003 | $0.0004 / $0.0004 | $0.0004 / $0.0004 |

- All 36 graph runs cost $0.21 together; a six-task pass costs about $0.03, against $0.12 for the old
  loop on Suryodaya.
- Planner replies were well-formed in 122 of 124 rounds; runs used 1 to 7 rounds. Across the 36 runs
  the planner needed 13 hard repairs and 6 soft repairs; the most any single run
  used was 2 hard and 1 soft, against limits of 5 and 2.
- A task takes about 1 to 2.5 minutes (median). Most of the time is the model; the MCP reads of a
  late order take about 30 seconds.
- Two fixes came from the first round of exit runs: OpenRouter's `finish_reason: error` is now retried
  within the round (it had failed one run), and `answer` no longer takes dependencies (naming the
  failed target read had cost refusals up to 4 hard repairs).

Phase 5 exit runs (2026-09-19, `max_workers = 4`): 3 runs of the six read-only tasks per tenant, 36/36
pass, and `deterministic` 12/12 the same day. Reads overlapped in 30 of the 36 runs, up to four at
once; `refuse_outside_seat` makes a single read. The 36 runs cost $0.24. Median task time fell only
from 79 s to 75 s against one worker, because the model, not MCP, takes most of the time.

## Harness

```bash
uv run python -m agentswitch.harness --tenant suryodaya --subject llm   # the LLM agent (old loop)
uv run python -m agentswitch.harness --tenant suryodaya --subject graph # the graph agent
uv run python -m agentswitch.harness --tenant suryodaya            # all tasks
uv run python -m agentswitch.harness --tenant keystone --task refuse_not_found
uv run python -m agentswitch.harness --tenant suryodaya --task reschedule_own_draft --allow-draft-writes
uv run python -m agentswitch.harness --tenant suryodaya --subject graph --judge   # plus the rubric judge
```

- Tasks are in [agentswitch/harness/tasks.jsonl](agentswitch/harness/tasks.jsonl), one per line; each
  line's `selector` must be one the code knows. Six tasks: three late work orders (oldest late, late with a sales order, late with an open
  material request, subcontract order or job card), one completed order (must be answered "not
  late"), and two refusals (a work order that does not exist; a stock-ledger request outside the
  seat). Targets are picked from live data at run time, so no record ids are committed.
- Each run record holds the effective config and its hash (`config`) and, for `--subject llm` and
  `--subject graph`, the cost ledger (`economics`), also when the run fails. Records are schema
  2.0; graph runs add the journal, the final graph, accepted and rejected patches, the manifest and
  the write authority under `subject_output.agent`.
- Graph runs get six extra audit checks, computed from the persisted record without the agent's
  own graph code: `journal_consistent` (replaying the journal gives the recorded graph),
  `capabilities_registered`, `limits_respected`, `terminal_last`, `write_after_target_read` and
  `single_subject_write`. They can fail a task but never turn a `not_applicable` task into a pass.
- Graph runs read up to `limits.max_workers` records at once (config default 4). Each MCP call in
  the record carries the graph node that made it and its start and end order. To check a set of
  saved graph records for correct attribution and overlapping reads (read-only; prints no record
  contents):

  ```bash
  uv run python -m agentswitch.harness.concurrency_check runs/
  ```

- `--subject graph` writes only on `reschedule_own_draft` with `--allow-draft-writes`, after the
  fixture is ready. Before the update it writes `runs/<...>.action.json` (the receipt); if that
  fails, nothing is sent. The run record shows the authority (`subject_output.agent.authority`)
  and the receipt (`action_receipt`).
- A seventh task, `reschedule_own_draft`, checks the one write the agent may make: new planned
  dates on a draft work order created by our own login. It runs only with `--allow-draft-writes`
  and scores `not_applicable` otherwise. With the flag, the harness saves the draft to
  `runs/<...>.fixture.json`, moves its dates into the past, lets the agent reschedule it, scores the
  result by re-reading the record, then writes the original dates back and records that in
  `runs/<...>.restore.json`. If the restore does not end with the original dates, it prints
  `RESTORE FAILED` and exits 5: check that draft by hand. Run one write task at a time.
- Each task writes `runs/<time>_<tenant>_<task>.json` first, then reads it back from disk and
  writes `<same name>.score.json`. Verifiers re-read the platform over MCP; they never read prose.
- After the score, each task writes `<same name>.spans.json`: a span tree (run → phase → planner
  round → model attempt or graph node → MCP call) with times, tokens and cost, built from the saved
  run record. It holds no arguments, results, prompts or prose. Restore calls happen after the run
  record is saved, so they are not in it.
- `--judge` adds `<same name>.judge.json`: a rubric judge scores the answer prose (on topic,
  specific, consistent with the answer's claims, complete, meets the task's `expectation`) as
  `resolved`, `unresolved` or `judge_failed`. It is advisory: it never changes a verdict or the
  exit code. It uses `[evals]` and `budgets.judge_usd` in the config, the same model as the agent
  (the file says `self_judging`), and makes no call when there is no prose (`deterministic` answers
  are `unresolved`, reason `no_prose`). Its live check is pending.
- Verdicts are `pass`, `fail`, `inconclusive` (the shared book changed, or a read failed) and
  `not_applicable` (no matching target). `inconclusive` is never counted as a pass.
- Outside that task the harness only calls read-only tools, and every task's write attempts are
  audited. It refuses to run if `runs/` is not git-ignored. It takes about 4 minutes per tenant.
- The default subject is `investigate()` plus `reschedule()` behind an adapter. Its outside-seat
  refusal is routing, not model judgement, and the run summary says so. With `--subject llm`, every
  refusal is the model's own decision. The run record also keeps the model transcript, token usage
  and cost, including for failed runs.

Known scoring limits. These need the book to change during a run, or a list longer than one page.
None has been seen on either tenant.

- A list longer than 1,000 rows is read in pages. If rows are added or removed between pages, the
  harness does not notice, so target selection or a check can miss a row. Tenant lists are about
  100 rows, so everything is read in one page.
- If the platform returns an empty or duplicate page before the reported total, the harness
  scores the subject's incomplete scan as `fail`, not `inconclusive`.
- A work order that was closed when the subject read it, but open at verification, scores `pass`
  when the answer leaves it out. The answer was right when the subject read the data.
- If a BOM gains the target item between the subject's read and verification, an open work order on
  that BOM scores `fail` when the answer leaves it out, although the subject could not have seen it.

## Checks

```bash
uv run ruff check .
uv run pytest
```
