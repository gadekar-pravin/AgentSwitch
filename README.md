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
  code pages every list to its end.
- The only write is `reschedule_work_order`, which runs `reschedule()`: planned dates on a draft
  created by our login. From the command line it writes only with `--allow-draft-writes`.
- If the answer misses a required read, the agent gets one repair message naming the exact calls
  still needed. Reads still missing after that become unknowns in the answer.
- Model calls go through OpenRouter with `data_collection: "deny"`. `OPENROUTER_API_KEY` and
  `OPENROUTER_MODEL` are set in `.env`.
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

## Harness

```bash
uv run python -m agentswitch.harness --tenant suryodaya --subject llm   # the LLM agent
uv run python -m agentswitch.harness --tenant suryodaya            # all tasks
uv run python -m agentswitch.harness --tenant keystone --task refuse_not_found
uv run python -m agentswitch.harness --tenant suryodaya --task reschedule_own_draft --allow-draft-writes
```

- Six tasks: three late work orders (oldest late, late with a sales order, late with an open
  material request, subcontract order or job card), one completed order (must be answered "not
  late"), and two refusals (a work order that does not exist; a stock-ledger request outside the
  seat). Targets are picked from live data at run time, so no record ids are committed.
- A seventh task, `reschedule_own_draft`, checks the one write the agent may make: new planned
  dates on a draft work order created by our own login. It runs only with `--allow-draft-writes`
  and scores `not_applicable` otherwise. With the flag, the harness saves the draft to
  `runs/<...>.fixture.json`, moves its dates into the past, lets the agent reschedule it, scores the
  result by re-reading the record, then writes the original dates back and records that in
  `runs/<...>.restore.json`. If the restore does not end with the original dates, it prints
  `RESTORE FAILED` and exits 5: check that draft by hand. Run one write task at a time.
- Each task writes `runs/<time>_<tenant>_<task>.json` first, then reads it back from disk and
  writes `<same name>.score.json`. Verifiers re-read the platform over MCP; they never read prose.
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
