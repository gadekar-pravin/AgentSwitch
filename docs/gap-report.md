# Gap report — seat 04 Production

Draft, 2026-09-17. Evidence and labels: [competitor-study.md](competitor-study.md); our seat:
[domain-notes.md](domain-notes.md). Competitor points are documented unless marked *claimed*.

## Product studied

- **[Carbon](https://carbon.ms/)** (primary): open-core manufacturing ERP and MES, public since 2024,
  with its own MCP server. The Production equivalent of Rillet.
- **[Fulcrum](https://fulcrumpro.com/)** (secondary): its MCP server (April 2026) surfaces at-risk
  jobs, updates due dates and priorities, and triggers scheduling runs.
- **Studied:** Carbon's docs, pricing and [source](https://github.com/crbnos/carbon) (commit `0bbb3b53`,
  1,564 MCP tools); Fulcrum's MCP docs, REST spec and changelog. No hands-on trial or demo yet.

## 1. What do they do that we do not?

- **Finite-capacity scheduling.** Carbon places operations inside shifts, one per work centre, minus
  maintenance downtime, with a qualified operator. Our `finite_schedule` projects every order to
  finish today and ignores downtime.
- **Named late causes.** Carbon names the blocking jobs or late predecessor per operation. Ours gives
  `work_content_exceeds_due_date` for 50 of 51 late orders, with empty blocking and downtime causes.
- **Expedite what-if.** Carbon simulates a projected finish without saving. We have none.
- **Rescheduling as a supported write.** Carbon and Fulcrum change due date or priority, then replan.
  Our `WorkOrder.update` is refused on `not_started`, cancel is admin-only, and there is no replan.
- **Downstream impact.** Carbon lists jobs made newly late by a replan; Fulcrum flags items not ready
  for what depends on them (*claimed*). We have no work-order-to-work-order link.
- **Shortfall across jobs.** Carbon allocates stock to jobs by priority; our
  `check_stock_availability` checks one order alone.
- **Shop-floor progress and downtime.** Readable in Carbon; our `JobCard` and `DowntimeEntry` refuse
  (bugs filed).

## 2. Which gaps can an agent close with the tools our seat already has?

| Gap | Verdict | Tools / evidence |
| --- | --- | --- |
| Late cause | **Agent, partly** (candidates, not proof) | `WorkOrder.get`, `MaterialRequest.list`, `SubcontractOrder.list`, `check_stock_availability`, `QualityInspection.list` |
| Machine or operator cause | **Platform: defect / filed bug** | `finite_schedule` causes empty; `JobCard`, `DowntimeEntry` refused |
| Capacity placement, what-if | **Platform: defect** | `finite_schedule` ignores calendar and downtime |
| Blocked sales orders | **Agent**; Keystone **seat limit** | `WorkOrder.sales_order_id` → `SalesOrder.get` |
| Blocked work orders | **Agent, inferred**; true link **platform: missing** | `BOM.list` materials joined to open `WorkOrder.item_id` |
| Shortfall across jobs | **Agent, partly**; stock ledger **seat limit** | `check_stock_availability` per order, ranked by due date |
| Reschedule | **Platform: defect** (`not_started`); other states untested | `WorkOrder.update` refused; agent can `stop` / `resume` and escalate a date via `endpoint.agent_governance.escalations.raise` |
| Newly late after a change | **Agent** | Re-read `WorkOrder.list` before and after, and diff |

## 3. What can our agent do that their product cannot?

Both expose the pieces; neither documents an end-to-end answer to "this work order is late".

- **Join causes the scheduler cannot see.** Carbon's scheduler ignores material, so a shortage is never
  a late cause. Our agent ranks stopped status, overdue materials and subcontracting, stock shortage
  and open inspections in one answer, with the record behind each.
- **Carry a write to a verified result.** A Carbon MCP date change needs a second call to replan, and
  that replan moves every job at the site. Our agent re-reads before writing (the late set moved 49 →
  51 in a day), writes, re-reads, and reports what else moved.
- **Decide within the seat's limits.** It makes the moves allowed and escalates the rest with a
  proposed date.
- **Refuse when the data is missing.** If downtime is the likely cause, it says it cannot read it.

Not claimed: better scheduling than a dedicated engine; these are "not demonstrated in the sources
studied", not "cannot".
