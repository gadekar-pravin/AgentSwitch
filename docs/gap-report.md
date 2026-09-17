# Gap report — seat 04 Production

Draft, 2026-09-17. Evidence and labels: [competitor-study.md](competitor-study.md); our seat:
[domain-notes.md](domain-notes.md). Competitor points are documented unless marked *claimed*.

## Product studied

- **[Carbon](https://carbon.ms/)** (primary): open-core manufacturing ERP and MES (public repo created
  June 2024) with its own MCP server. The Production equivalent of Rillet.
- **[Fulcrum](https://fulcrumpro.com/)** (secondary): its MCP server (April 2026) surfaces at-risk
  jobs, updates due dates and priorities, and triggers scheduling runs.
- **Studied:** Carbon's docs, pricing and [source](https://github.com/crbnos/carbon) (commit
  `0bbb3b53`, including its MCP tool manifest); Fulcrum's MCP docs, REST spec and changelog. Nothing
  run: Carbon's trial excludes MCP, its scheduler and what-if are Enterprise code (trial coverage
  unverified), and Fulcrum is demo-only.

## 1. What do they do that we do not?

- **Finite-capacity scheduling.** Carbon places operations inside shifts, one per work centre, minus
  maintenance downtime. Our `finite_schedule` projects every submitted order to finish today;
  downtime appears not to reduce capacity (inferred).
- **Named late causes.** Carbon names the blocking jobs or late predecessor per operation. Ours gives
  `work_content_exceeds_due_date` for 50 of 51 late Suryodaya orders; blocking and downtime causes
  are empty.
- **Expedite what-if.** Carbon simulates a projected finish without saving. We have none.
- **Rescheduling with a replan.** Carbon changes due date or priority, then replans; Fulcrum triggers
  a scheduling run. Our `WorkOrder.update` is refused on `not_started` ("Cancel first"), cancel is
  admin-only, and no replan tool was found.
- **Downstream impact.** Carbon lists jobs made newly late by a replan; Fulcrum flags items not ready
  for what depends on them (*claimed*). We have no work-order-to-work-order link.
- **Shortfall across jobs.** Carbon computes each job's shortfall from shared stock in priority order
  (no reservation); our `check_stock_availability` checks one order alone.
- **Shop-floor progress and downtime** are readable in Carbon; ours refuse (bugs filed).

## 2. Which gaps can an agent close with the tools our seat already has?

| Gap | Verdict | Tools / evidence |
| --- | --- | --- |
| Late cause | **Agent, partly** (candidates, not proof) | `WorkOrder.get`, `MaterialRequest.list`, `SubcontractOrder.list`, `check_stock_availability`, `QualityInspection.list` |
| Machine or operator cause | **Filed bug** | `finite_schedule` causes empty; `JobCard`, `DowntimeEntry` refused |
| Capacity placement | **Platform: defect** | `finite_schedule` projects all orders to today |
| Expedite what-if | **Platform: missing** | no simulation tool |
| Sales orders at risk | **Agent**; Keystone **seat limit** | `WorkOrder.sales_order_id` → `SalesOrder.get` |
| Work orders potentially affected | **Agent, inferred**; true link **platform: missing** | `BOM.list` materials joined to open `WorkOrder.item_id` |
| Shortfall across jobs | **Agent, partly**; stock ledger **seat limit** | `check_stock_availability` per order; competing demand unverified |
| Reschedule | **Workflow rule** on `not_started` (not a filed bug); other states untested; replan **platform: missing** | `WorkOrder.update` refused; escalate a proposed date via `endpoint.agent_governance.escalations.raise` |
| Knock-on lateness after a change | **Platform: missing** (no replan); agent can only diff re-reads | `WorkOrder.list` before and after |

## 3. What can our agent do that their product cannot?

The agent is not built; these are design targets the harness will verify. Neither competitor documents
an end-to-end answer to "this work order is late".

- **Join causes the scheduler cannot see.** Carbon's scheduler ignores material; shortage is a separate
  tool. Our agent will rank stopped status, overdue materials and subcontracting, stock shortage and
  open inspections in one answer, citing the record behind each, as candidates rather than proof.
- **Carry a change to a verified result.** A Carbon MCP date change needs a second call to replan, and
  that replan recalculates every open job at the location. Our agent will re-read before and after any
  allowed change and report what else moved. The book moves without us: Suryodaya's late set went from
  49 to 51 in a day.
- **Decide within the seat's limits.** It will make only allowed moves and escalate the rest with a
  proposed date.
- **Refuse when the data is missing.** If downtime could be the cause, it will say it cannot read it.

Not claimed: better scheduling than a dedicated engine; these are "not demonstrated in the sources
studied", not "cannot".
