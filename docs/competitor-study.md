# Competitor study — manufacturing

The study plan and its evidence log. It feeds the one-page [gap report](gap-report.md) (§8 steps 2–3
of the brief). This repo is public, so competitor names and public competitor links are fine here.
Do not add AgentSwitch URLs, logins or live record data. Enter only dummy data in a competitor trial.

Labels: **observed** = seen in a trial or a running copy; **documented** = in the vendor's own docs or
changelog; **claimed** = marketing page or blog only; **unverified** = not checked yet. Give every
entry the date it was checked.

Status (2026-09-17): the plan is agreed and nothing has been studied yet. The web screen below is
a first pass, not the study itself.

## Picks

| Role | Product | Why |
| --- | --- | --- |
| Primary | **Carbon** ([carbon.ms](https://carbon.ms/), source [crbnos/carbon](https://github.com/crbnos/carbon)) | A young product with an open-source ERP, MES and QMS on one database. It ships its own MCP server over manufacturing data, the closest match to the brief's Rillet example. It is the easiest product to inspect: 30-day trial, self-hosting with demo data, readable source. |
| Secondary, docs only | **Fulcrum** ([fulcrumpro.com](https://fulcrumpro.com/), [MCP docs](https://developers.fulcrumpro.com/mcp)) | Its MCP tools are the closest match to our request: overdue and at-risk jobs, schedule health and capacity, update priorities, run the scheduler. Older (founded 2015) and sold by demo, so study only the public docs. |

**Swap rule:** if the first hour shows that Carbon's scheduling or its MCP write tools are thin,
make Fulcrum primary.

Not picked (web screen, 2026-09-17):

- **SkyPlanner APS:** finite-capacity scheduler with an AI engine (Arcturus) and a REST API; no MCP
  found. It optimises schedules but is not built around an agent driving it.
- **DOSS:** AI-native operations ERP (founded 2022) with a chat copilot. It focuses on inventory; no
  MCP found; demo only.
- **Tulip:** ships an MCP server (June 2025) over stations, machines and tables. It is a frontline
  operations platform, not a scheduler.
- **ERPNext:** AgentSwitch's manufacturing entities mirror it (WorkOrder, JobCard, `docstatus`).
  Use it for calibration, not as "the best product" (inferred).

## First screen (unverified until the study confirms it)

| Claim | Product | Source (checked 2026-09-17) | Label |
| --- | --- | --- | --- |
| Hosted MCP server; read and write on inventory, work orders, purchase orders, quotes, quality | Carbon | [MCP for manufacturing](https://carbon.ms/learn/mcp-for-manufacturing) | claimed |
| Built-in MCP server exposing 1,374 operations across 15 modules through three discovery tools | Carbon | search-result excerpt from carbon.ms (page not opened); same pitch in the [GitHub README](https://github.com/crbnos/carbon) | claimed |
| An agent can "reschedule a job" through MCP | Carbon | search-result excerpt from carbon.ms (page not opened) | claimed |
| Finite-capacity scheduling against machine calendars; moving a job updates material needs and due dates | Carbon | [What is production planning](https://carbon.ms/learn/what-is-production-planning) (published 2026-08-16) | claimed |
| Capacity planning, MRP, job operations, traceability, nested BOMs | Carbon | [GitHub README](https://github.com/crbnos/carbon) (AGPL-3.0) | documented |
| Docs navigation has a Production section but no visible scheduling or MCP page | Carbon | [docs.carbon.ms](https://docs.carbon.ms/docs) | observed on the web |
| 30-day free trial | Carbon | the two carbon.ms blog pages above (they offer it) | claimed |
| $4.3M seed round | Carbon | search result pointing to a founder's LinkedIn post dated 2025-09-05 (not opened) | unverified |
| MCP server announced 2026-04-07: look up jobs, sales orders, work orders, equipment, NCRs; dashboards for schedule health and capacity; approve POs, move jobs through workflows, update priorities, run the scheduler | Fulcrum | [product update](https://fulcrumpro.com/product-update/fulcrum-mcp-server-connect-ai-tools-directly-to-your-shop-data), [MCP docs](https://developers.fulcrumpro.com/mcp) | documented |
| Founded 2015; custom pricing by shop revenue and integrations | Fulcrum | review-site listings | unverified |

## Method (about one team-day)

1. **Screen (1 h).** Check the unverified rows above. For Carbon, find the scheduling and MCP tool docs
   or read the source. Decide whether the swap rule applies.
2. **Primary (4–5 h).** Read the docs, changelog (last 12–18 months), API and MCP tool list, and the
   pricing page. Then run one scenario in a trial or self-hosted copy, using dummy data:
   - a sub-assembly work order that is late;
   - two work orders that consume that sub-assembly;
   - a component shortage;
   - a machine that is down.

   Record what the product shows for the cause, the downstream impact and the reschedule, and how
   many clicks or tool calls each step takes.
3. **Secondary (1 h).** Read Fulcrum's MCP Tools and Permissions & Safety pages, then the scheduling
   docs, for the same four steps.
4. **Write-up (2 h).** Fill the comparison table below, then the gap report.

For each claim, record the link, the date checked, a screenshot or video timestamp, the pricing tier
it needs, and its label. The study is deep enough when each checklist row has a source or reads "not
documented", and the gap report has 3–5 defensible gaps.

**Human steps:** trial sign-up, self-hosting, demo booking and any contact with a vendor. A teammate
does these; the AI does not.

## Checklist

For each product, how does it handle:

- **Finite capacity:** scheduling with calendars and shifts.
- **Downtime:** reducing capacity when a machine is down.
- **Operation progress:** job cards and shop-floor capture.
- **Delay cause:** explaining why a job is late.
- **Material shortage:** availability, and replenishment dates.
- **Order dependency:** sub-assembly to parent order, and order to sales order (pegging).
- **Downstream impact:** propagating a delay to later orders.
- **Rescheduling:** what-if, drag to reschedule, auto re-plan, and the workflow rules on changing dates.
- **Agent surface:** natural-language assistant, API, MCP tools (reads and writes), permissions and
  audit.

## Comparison table (fill during the study)

Verdict for each gap:

- **agent** — our agent can close it with the seat's tools; name the tools.
- **platform: missing** — needs a new table or endpoint.
- **platform: defect** — the capability exists but is wrong.
- **seat limit / filed bug** — not a feature gap; say which.

| Feature | Competitor evidence (link, date, label) | Our seat today (see [domain notes](domain-notes.md)) | Verdict | Tools |
| --- | --- | --- | --- | --- |
| | | | | |

Starting hypotheses from the domain notes (inferred, to be confirmed against the competitor):

- **Delay cause:** agent. Combine overdue MaterialRequests, overdue SubcontractOrders,
  `status = stopped`, `check_stock_availability` and draft QualityInspections. These are candidate
  causes, not proven ones.
- **Downstream impact:** partly agent, through `sales_order_id` → SalesOrder (Suryodaya) and BOM
  materials matching. A shared material does not prove a block. A true work-order dependency link is
  platform: missing.
- **Capacity-aware new dates:** platform: defect. `finite_schedule` projects every order to finish
  today and does not apply downtime.
- **Job-card progress, downtime reasons, ECO holds:** filed bug (the tools are listed but refuse), not a
  feature gap.
- **Re-dating work orders:** refused on `not_started`, and cancel is admin-only. Draft, in_progress
  and stopped are untested, so do not call this a gap until tested.
- **Stock history, purchasing, SalesOrder on Keystone:** seat limit; escalate.

## Q3 guardrails

- Claim only what we can show: the agent holds the whole request across many steps, re-reads records
  that changed underneath it, and refuses or escalates when the seat cannot act.
- Write "not demonstrated in the sources studied", not "cannot". The competitors ship agents and MCP
  servers too.
- Do not claim better scheduling than a dedicated scheduling engine.

## Risks

- Picking a product we cannot inspect turns the report into marketing paraphrase. Apply the swap rule
  early.
- An announced feature is not a shipped one. Look for a changelog entry or docs page.
- A permission refusal or seat boundary filed as a platform gap makes Q2 wrong.
- Screenshots of our own platform must not show record ids or tenant URLs.
