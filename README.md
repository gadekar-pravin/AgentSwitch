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
| Agent (reschedule step + LLM loop) | _not created yet_ | next; LLM provider not chosen; a `draft` accepts a date update (tested 2026-09-18) |
| Harness (tasks, DB-reading verifiers, run records, ≥1 refusal task) | [agentswitch/harness/](agentswitch/harness/) | read-only skeleton done (2026-09-18); scores the `investigate()` adapter until the LLM agent exists; 6/6 on both tenants; no hand-written tests yet |
| Hand-written tests (team members only; AI-written tests score zero) | `tests/` (create when writing the first test) | none yet |

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
  shared [CLAUDE.md](CLAUDE.md), which holds the team rules (including: AI must not write tests).
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

## Harness

```bash
uv run python -m agentswitch.harness --tenant suryodaya            # all tasks
uv run python -m agentswitch.harness --tenant keystone --task refuse_not_found
```

- Six tasks: three late work orders (oldest late, late with a sales order, late with an open
  material request, subcontract order or job card), one completed order (must be answered "not
  late"), and two refusals (a work order that does not exist; a stock-ledger request outside the
  seat). Targets are picked from live data at run time, so no record ids are committed.
- Each task writes `runs/<time>_<tenant>_<task>.json` first, then reads it back from disk and
  writes `<same name>.score.json`. Verifiers re-read the platform over MCP; they never read prose.
- Verdicts are `pass`, `fail`, `inconclusive` (the shared book changed, or a read failed) and
  `not_applicable` (no matching target). `inconclusive` is never counted as a pass.
- The harness only calls read-only tools and refuses to run if `runs/` is not git-ignored. It
  takes about 4 minutes per tenant.
- The subject today is `investigate()` behind an adapter; its outside-seat refusal is routing, not
  model judgement, and the run summary says so.

## Checks

```bash
uv run ruff check .
uv run pytest
```
