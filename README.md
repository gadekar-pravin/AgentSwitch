2# AgentSwitch — Team 04, Production

Capstone project for the EAG V3 Agentic AI course. We build an agent for the
**Production** seat (app `manufacturing`: work orders, BOMs, routings, job
cards) that drives the AgentSwitch platform over MCP, plus a harness that proves
it works. The assignment is in [agentswitch-team-brief.md](agentswitch-team-brief.md); a plain-language
summary is in [docs/assignment-overview.md](docs/assignment-overview.md).

The request our agent must handle:

> "This work order is late. Find out why, tell me what it blocks downstream, and reschedule what you can."

## Deliverables

| Deliverable | Where | Status |
| --- | --- | --- |
| Gap report (week one) | [docs/gap-report.md](docs/gap-report.md) | not started |
| Domain notes | [docs/domain-notes.md](docs/domain-notes.md) | not started |
| Agent (MCP client + loop) | _not created yet_ | after domain learning |
| Harness (tasks, DB-reading verifiers, run records, ≥1 refusal task) | _not created yet_ | after the agent |
| Hand-written tests (team members only; AI-written tests score zero) | `tests/` (create when writing the first test) | none yet |

Order follows the brief: learn the domain, study a leading product, write the
gap report, then build the agent and harness.

## Setup

Requires [uv](https://docs.astral.sh/uv/).

```bash
uv sync
cp .env.example .env    # then fill in the passwords from the team channel
```

## Platforms

| Tenant | Business | URL |
| --- | --- | --- |
| `suryodaya` | Suryodaya Precision Works (India, GST) | <Suryodaya URL from the brief> |
| `keystone` | Keystone Precision Works LLC (US, Sales & Use Tax) | <Keystone URL from the brief> |

Start on Suryodaya. Same email on both; the password differs per business.

Quick manual check (from the brief §5–6):

```bash
set -a; source .env; set +a    # loads AS_EMAIL and the passwords into this shell
export AS=<Suryodaya URL from the brief>
export TOKEN=$(curl -s -X POST "$AS/api/auth/login" -H 'Content-Type: application/json' \
  -d "{\"email\":\"$AS_EMAIL\",\"password\":\"$AS_PASSWORD_SURYODAYA\"}" \
  | python3 -c 'import sys,json; print(json.load(sys.stdin)["token"])')
curl -s "$AS/api/auth/me" -H "Authorization: Bearer $TOKEN"
```

Reading material on the live instance (login required): `$AS/docs`, `$AS/redoc`,
`$AS/api/schemas`, `$AS/api/agent/tools`.

## Checks

```bash
uv run ruff check .
uv run pytest
```
