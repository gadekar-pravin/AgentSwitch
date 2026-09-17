# Competitor study — manufacturing

The study plan and its evidence log. It feeds the one-page [gap report](gap-report.md) (§8 steps 2–3
of the brief). This repo is public, so competitor names and public competitor links are fine here.
Do not add AgentSwitch URLs, logins or live record data. Enter only dummy data in a competitor trial.

Labels: **observed** = seen in a trial or a running copy; **documented** = in the vendor's technical
docs, changelog or source code; **claimed** = marketing page, blog, pricing, FAQ or company page;
**unverified** = not checked yet. Give every entry the date it was checked.

Status (2026-09-17): the plan is agreed and nothing has been studied hands-on yet. The screen below
combines our web screen, a second screen by another AI search agent, and our own check of the load-bearing
claims on the vendors' pages and Carbon's source. The Carbon agent surface has been studied from source
([below](#carbon-agent-surface-from-source)) and Fulcrum's from its public docs and REST spec
([below](#fulcrum-agent-surface-from-docs)); nothing was run.

## Picks

| Role | Product | Why |
| --- | --- | --- |
| Primary | **Carbon** ([carbon.ms](https://carbon.ms/), source [crbnos/carbon](https://github.com/crbnos/carbon)) | A young open-core ERP and MES (public repo created 2024-06). It ships its own MCP server over manufacturing data, the closest match to the brief's Rillet example. It is the easiest product to inspect: public scheduling docs, a public MCP tool list in its source, and a 30-day trial for the UI. The trial does not include MCP (see below). |
| Secondary, docs only | **Fulcrum** ([fulcrumpro.com](https://fulcrumpro.com/), [MCP capabilities](https://developers.fulcrumpro.com/mcp/capabilities)) | Its MCP capabilities match our request most directly: at-risk jobs, update priorities and due dates, trigger scheduling runs, schedule health. It dates from 2015 and has no self-serve trial, so study only the public docs. |

**Fallback rule:** Fulcrum is harder to inspect than Carbon, so it never becomes primary. If Carbon's
MCP cannot be reached (the trial is Starter, which rejects API keys), study Carbon's agent surface
from its public source and docs, and keep Fulcrum as the docs-only secondary. Revisit the pick only
if Carbon's scheduling or its MCP write tools turn out thin in the docs or source.

Not picked (screened 2026-09-17):

- **SkyPlanner APS:** finite-capacity scheduler with an AI engine (Arcturus) and a REST API; no MCP
  found. It optimises schedules but is not built around an agent driving it.
- **DOSS:** AI-native operations ERP (founded 2022) with a chat copilot. It focuses on inventory; no
  MCP found; demo only.
- **Tulip:** ships an MCP server (June 2025) over stations, machines and tables. It is a frontline
  operations platform, not a scheduler.
- **Katana:** first-party hosted MCP that can read, create and update manufacturing orders, BOMs and
  operation rows (no deletes), and a free plan. Its MCP overview says nothing about scheduling,
  priority or deadlines, so it has no documented agent-driven rescheduling
  ([MCP overview](https://support.katanamrp.com/en/articles/15502865-katana-mcp-overview), documented).
- **First Resonance ION:** manufacturing plans and BOM demand; MCP in beta; demo-led access. Its
  Autoplan "is currently an infinite capacity model"
  ([Autoplan](https://manual.firstresonance.io/plans-and-autoplan/autoplan), documented).
- **MRPeasy:** finite-capacity scheduling in the UI, but its MCP is reported read-only (GET endpoints
  only), so it cannot reschedule (second screen, unverified by us).
- **Tangle:** AI-native manufacturing ERP (Milo assistant). Its platform page mentions APIs and
  webhooks but no MCP or technical docs, so its scheduling claims are marketing only (claimed).
- **ERPNext:** AgentSwitch's manufacturing entities mirror it (WorkOrder, JobCard, `docstatus`).
  Use it for calibration, not as "the best product" (inferred).

## Screen results (checked 2026-09-17)

| Claim | Product | Source | Label |
| --- | --- | --- | --- |
| Hosted MCP server at `<host>/api/mcp`, three meta-tools `search_tools`, `describe_tool`, `call_tool`; API-key or browser auth; a key scoped to View only is read-only | Carbon | [MCP docs](https://docs.carbon.ms/mcp) | documented |
| MCP and API need the Business plan on Carbon Cloud ("Starter keys are rejected with 403"); self-hosted MCP needs an Enterprise licence | Carbon | [MCP docs](https://docs.carbon.ms/mcp) | documented |
| Pricing: Starter $40/user/month (no API); Business $100/user/month, 5-user minimum (API, webhooks, integrations); Enterprise custom. 30-day free trial, no sales call, on Starter | Carbon | [pricing](https://carbon.ms/pricing) | claimed |
| The full MCP tool list is public: `tool-manifest.digest.json` lists 1,564 tools (798 read, 533 write, 233 destructive) with classification and permission per tool | Carbon | [source](https://github.com/crbnos/carbon), `apps/erp/app/routes/api+/mcp+/lib/tool-manifest.digest.json` on `main` | documented |
| Scheduling and request-relevant tools in that list. Write: `production_scheduleJob`, `production_updateJobOperationDueDate`, `production_calculateJobPriority`, `production_notifyScheduleInputsChanged`. Read: `production_getJobExpediteForecast`, `production_getUnscheduledJobs`, `production_getJobMaterialShortfallByItem`, `production_getJobMaterialSupplyJobLines`, `production_getCapacityReservationsByJob`, `production_getMaintenanceDowntimeForResources`, `resources_getWorkCentersListWithBlockingStatus` | Carbon | same file | documented (names only; behaviour untested) |
| Tool counts differ between pages (1,374 on the homepage; 1,564 in the docs and source; other pages say 1,200+, 1,400+, 1,482). Do not quote a count in the report | Carbon | [homepage](https://carbon.ms/), [MCP tools](https://docs.carbon.ms/mcp/tools) | documented |
| Finite-capacity scheduler: one operation at a time per work centre, inside shift hours; maintenance windows subtract downtime; operations need a qualified operator on shift; forward placement after predecessors | Carbon | [scheduling reference](https://docs.carbon.ms/docs/reference/scheduling) | documented |
| Lateness and explanations: operations projected past the due date show amber; unschedulable operations get an "Unschedulable" chip with notes on why; schedule notes explain waits | Carbon | same page | documented |
| Replanning: edits to due dates, shifts, work-centre hours, qualifications or process requirements trigger a replan after about 30 seconds; status changes reschedule immediately; planners drag jobs on a Priorities board | Carbon | same page | documented |
| The raw Data API (for example job-operation `dueDate`, `workCenterId`, `priority`, `manuallyScheduled`) does not recalculate: "Nothing here validates, recalculates, or posts". Rescheduling goes through the service tools above, not table writes | Carbon | [job-operation Data API](https://docs.carbon.ms/api-reference/production/job-operation) | documented |
| Open core: Community edition (ERP + MES) is AGPLv3 and self-hostable; Enterprise features (`packages/ee`) are commercial. Which features are Enterprise-only is not listed | Carbon | [licensing](https://docs.carbon.ms/docs/platform/licensing) | documented |
| $4.3M seed round | Carbon | search result pointing to a founder's LinkedIn post dated 2025-09-05 (not opened); the second screen found no reliable source | unverified — do not use |
| MCP server announced 2026-04-07 | Fulcrum | [product update](https://fulcrumpro.com/product-update/fulcrum-mcp-server-connect-ai-tools-directly-to-your-shop-data) | documented |
| MCP capability groups: Jobs ("surface at-risk jobs, change statuses, update priorities and due dates"); Sales Orders ("linked jobs"); Work Orders; Equipment ("check backlogs"); Scheduling & Capacity ("identify bottlenecks, trigger scheduling runs"); Dashboards ("schedule health", "demand planning"). The tool list "is actively evolving"; no public tool names or schemas | Fulcrum | [MCP capabilities](https://developers.fulcrumpro.com/mcp/capabilities) | documented |
| No self-serve free trial ("guided demos"); pricing by company size and scope, not per user | Fulcrum | [FAQ](https://fulcrumpro.com/faq) | claimed |
| Founded 2015 | Fulcrum | [company page](https://fulcrumpro.com/grow) | claimed |

## Carbon agent surface (from source)

Read on 2026-09-17 from [crbnos/carbon](https://github.com/crbnos/carbon) at commit `0bbb3b53`, with
the tool classification and permission from `tool-manifest.digest.json`. Everything here is
**documented (source), not run**. File paths are relative to the repo root.

### Our request as Carbon tool calls

| Step | MCP tools (classification, permission) | What the source shows |
| --- | --- | --- |
| Find the late job | `production_getJobs`, `production_getJob` (read, `production:view`); `production_getUnscheduledJobs` (read) | A job carries `dueDate`, `deadlineType`, `priority`, `projectedCompletionAt` and a schedule-outdated stamp. |
| Why is it late | `production_getJobOperationsForTimeline` (read); `production_getJobExpediteForecast` (read, what-if, persists nothing); `production_getCapacityReservationsByJob`, `production_getMaintenanceDowntimeForResources`, `resources_getWorkCentersListWithBlockingStatus` (read) | Every operation stores `hasConflict` and a `conflictReason` sentence. The scheduler classifies each late placement as one of: queued behind other jobs on the machine (naming them), behind this job's own operations, machine wait, operator queue (naming the jobs), no qualified operator on shift, waiting for assigned people, inherited delay from a named predecessor, no runway before the due date, or outside processing (`packages/ee/src/planning/scheduling/conflict-messages.ts`). The expedite forecast returns a projected completion and a cause sentence, including the first operation behind its target. |
| Material cause | `production_getJobMaterialShortfallByItem`, `production_getJobMaterialsWithQuantityOnHand`, `production_getJobPurchaseOrderLines` (read) | Shortfall allocates on-hand stock first, then incoming purchase and production orders, across active jobs at the location by priority. Material is **not** a scheduling constraint: the finite scheduler gates only on work centre and operator, so a shortage never appears as a late cause. The agent has to join the two. |
| What it blocks downstream | `production_getJobMethodTree` (read); `production_getJobMaterialSupplyJobLines` (read); `sales_getSalesOrder`, `sales_getSalesOrderLines` (read, `sales:view`) | Inside a job, make-to-order sub-assemblies are a method tree with operation dependencies, so a late sub-assembly shows up as "inherited delay" on its parent. Across jobs, `getJobMaterialSupplyJobLines` returns only the item and status of active jobs making a material, with no allocation to a specific consuming job (no pegging found). A job links to its sales order line. A replan lists **newly late** jobs, and the replan wave sends a "jobs projected late" notification to each assignee. |
| Reschedule | `production_updateJob` (write: `dueDate`, `priority`, `deadlineType`); `production_updateJobOperationDueDate` (write: pins an operation's need-by date); `production_calculateJobPriority` (write); `production_scheduleJob` (write, re-checks `production:update`); `production_notifyScheduleInputsChanged` (write) | Placement is always forward, as soon as possible: **due dates are targets, never placement constraints**. So moving a date changes lateness flags and priority order, not where work is placed. The placement moves through priority, work centre, shifts or people, then a replan. |
| Re-read | `production_getJob`, `production_getJobOperationsForTimeline` (read) | The replan writes new `startDate`, `projectedCompletionAt`, conflict flags and capacity reservations, and clears the stale stamp. |

### Findings

- **An MCP write does not replan by itself.** The UI route that pins an operation due date calls
  `updateJobOperationDueDate` and then `notifyScheduleInputsChanged`
  (`apps/erp/app/routes/x+/job+/methods+/operation.due-date.tsx`). The service functions exposed over
  MCP do only the update (`updateJob` also recalculates priority). An agent must then call
  `production_scheduleJob` (regenerates the whole location now) or
  `production_notifyScheduleInputsChanged` (marks jobs stale; a replan wave follows after a 30-second
  debounce). Otherwise the schedule stays stale until the nightly replan at 01:00
  (`.claude/rules/scheduling-data-structures.md`).
- **Replanning is location-wide.** `scheduleJob` regenerates every open job at the job's location, not
  just one job. One agent call can move many other jobs.
- **Permission checks are per tool, not central.** A comment in `production.mcp.server.ts` says the MCP
  executor "performs no per-tool permission check"; sensitive tools such as `scheduleJob` re-apply the
  gate inline. `mcp-blocked-tools.ts` blocks the raw schedule trigger and some tenant-level tools.
- **The scheduler, MRP and expedite what-if are Enterprise code.** They live in `packages/ee/src/planning`
  (`runLocationSchedule`, `runExpediteWhatIf`, `runMrp`). Licensing says Community mode "ships without
  EE features", so a self-hosted Community copy most likely has no finite scheduler (inferred; not
  run). Whether the Starter cloud trial includes it is unverified.
- **Cross-job dependency is weak.** `job.parentJobId` exists in the service layer, but we did not find
  it used for scheduling or impact. Downstream impact across jobs comes from "newly late" after a replan
  and from sales order links, not from explicit pegging.

### What this means for the gap report (inferred, to confirm in the UI run)

- **Q1 candidates, what Carbon has that we lack:**
  - A per-operation late cause naming the blocking jobs or the late predecessor. Our `finite_schedule`
    gives one generic cause code with empty `causes[].downtime` and `causes[].blocking`.
  - A simulate-only expedite forecast.
  - Forward finite placement over shifts, maintenance downtime and operator qualifications.
  - A list of jobs made newly late by a replan, pushed to assignees.
  - Writable due dates and priority with a replan.
- **Shared weakness:** neither system pegs a sub-assembly's supply to a specific consuming order.
  Carbon links sub-assemblies inside one job; we infer them across work orders from BOM materials.
- **Q3 angle:** Carbon exposes the parts, but the agent must still join schedule causes with material
  shortfall, trigger a replan after a write, accept that the replan is location-wide, and re-read.
  That is the multi-step orchestration our agent does (not demonstrated end to end in Carbon's docs).

### Open questions for the hands-on run

- Does the Starter trial run the Enterprise scheduler (Forecast page, amber flags, expedite dialog)?
- After a job due-date change in the UI, how long until the placement and conflict flags update?
- What does the expedite dialog show for a job blocked by a material shortage but not by capacity?

## Fulcrum agent surface (from docs)

Read on 2026-09-17 from the public developer hub: [MCP introduction](https://developers.fulcrumpro.com/mcp),
[getting started](https://developers.fulcrumpro.com/mcp/getting-started),
[MCP tools](https://developers.fulcrumpro.com/mcp/capabilities),
[permissions and safety](https://developers.fulcrumpro.com/mcp/permissions-and-safety), and the REST
spec (`https://api.fulcrumpro.com/swagger/v1/swagger.json`, "Fulcrum PublicAPI v1", 376 paths). Also
product pages and changelog entries named below. Nothing was run.

Fulcrum publishes MCP **capability groups, not tool names or schemas** ("actively evolving"). The table
therefore names capabilities, not calls.

### Our request as Fulcrum capabilities

| Step | MCP capability (write actions in bold) | What the docs show |
| --- | --- | --- |
| Find the late job | Jobs: search, view details, "surface at-risk jobs"; Dashboards: "schedule health" | The REST job record carries `productionDueDate`, `earliestStartDate`, `scheduledStartUtc` / `scheduledEndUtc` and the first scheduled `originalScheduledStartUtc` / `originalScheduledEndUtc`, so slip against the first schedule is readable (documented). How "at risk" is computed is not documented. |
| Why is it late | Equipment: "view schedules, check backlogs"; Scheduling & Capacity: "check capacity utilization, identify bottlenecks"; Work Orders: "view operations"; Items & Inventory: on-hand; Purchase Orders: "receiving status" | No per-job cause or explanation found. An agent assembles the cause from bottlenecks, equipment backlog, operation progress and material. AutoSchedule is said to factor shifts, equipment, operations and "material availability" ([production scheduling](https://fulcrumpro.com/manufacturing-software/production-scheduling), claimed). REST `/api/inventory/availableByItem` returns on hand plus incoming supply minus demand (documented). |
| What it blocks downstream | Sales Orders: "view fulfillment progress and linked jobs"; Dashboards: "demand planning" | A job links to `salesOrderId` and `salesOrderLineItemId` (REST, documented). Demand planning has a "Timing Issue" KPI for "an item isn't expected to be ready on time for something that depends on it", and flags "jobs that won't be done in time for the orders that rely on them" ([demand planning](https://fulcrumpro.com/manufacturing-software/demand-planning), claimed). What-if scenarios "see how downstream operations/jobs are impacted" (production scheduling page, claimed). |
| Reschedule | Jobs: **change statuses, update priorities and due dates**; Scheduling & Capacity: **trigger scheduling runs** | Writable job fields over REST are `name`, `priority` (low / moderate / high), `quantityToMake`, `productionDueDate`, `earliestStartDate`, `notes` (documented). AutoSchedule re-sequences on the next run; operations scheduled in the past move to the present, and running operations get priority on their equipment. Drag-and-drop locks an operation to a date and switches that behaviour off ([changelog, 2023-11-27](https://fulcrumpro.com/product-update/autoschedule-enhancement-to-improve-scheduling-accuracy-and-priority-visibility-in-job-tracker), documented). |
| Re-read | Jobs: view details; Dashboards: schedule health | `scheduledEndUtc` after a scheduling run, compared with `productionDueDate`. |

### Findings

- **The scheduler is MCP-only.** The public REST spec has no scheduling, capacity or dashboard
  endpoint (0 of 376 paths). Running the scheduler, bottlenecks and schedule health exist only in the
  MCP capabilities (documented by absence).
- **Guardrails are the user's permissions plus previews.** "You can only view or modify data that your
  Fulcrum user account has access to", and "all status changes that could have side effects are
  previewed before executing", with blockers and consequences shown before confirming (documented).
  Connecting needs a "Connect OAuth" or "Create MCP API Key" permission. Audit logging, rate limits and
  whether a scheduling run is previewed are not documented.
- **Due dates are an input to AutoSchedule, not a pin.** Changing `productionDueDate` or priority takes
  effect on the next scheduling run, which the agent must trigger (inferred from the capability list and
  the 2023 changelog). Whether a run covers the whole shop or one job is not documented.
- **Downstream tracing is stronger than Carbon's on paper.** Demand planning claims supply-to-demand
  timing issues across dependent items and orders; Carbon has only inside-job sub-assembly delays and
  "newly late" lists. Fulcrum's claim is marketing-level; no API or MCP detail backs it.
- **Built-in agents are not shipped.** The product page lists Archie agents as "Coming soon"
  (claimed). The MCP server is the agent surface today; it is available to all customers, per the
  [MCP release](https://fulcrumpro.com/product-update/fulcrum-mcp-server-connect-ai-tools-directly-to-your-shop-data)
  (documented).
- **Not the same product:** `help.fulcrumapp.com` is Fulcrum field-inspection software, not Fulcrum Pro.
  Do not cite it.

### Carbon and Fulcrum side by side (for the gap report)

| Step | Carbon (source, documented) | Fulcrum (docs; mostly claimed below the capability level) |
| --- | --- | --- |
| Why late | Per-operation cause naming blocking jobs or the late predecessor; expedite what-if | Bottlenecks, backlog, schedule health; no per-job cause found |
| Material in the schedule | Not a scheduling constraint; separate shortfall tool | Listed as an AutoSchedule input (claimed) |
| Downstream | Sub-assembly delay inside a job; newly-late jobs after replan; sales order link | Sales order linked jobs; demand-planning timing issues across dependents (claimed) |
| Reschedule | Due date, priority, work centre, then an explicit replan call (location-wide) | Priority, due date, earliest start, then trigger a scheduling run |
| Agent guardrails | Per-tool permission metadata; some tools re-check; blocked-tool list | User permissions; previews for side-effect status changes |
| Inspectable detail | Every tool name, classification and permission, plus source | Capability groups only; REST spec for data fields |

### Open questions (demo only)

- How is "at risk" defined, and does the schedule-health dashboard name a cause per job?
- Does a triggered scheduling run cover the whole shop, and is it previewed?
- Does the Timing Issue KPI peg a specific sub-assembly job to the order that consumes it?

## Method (about one team-day)

1. **Screen (1 h).** Read Carbon's scheduling reference and MCP docs, and the `describe_tool` schemas
   or service code behind the tools above. Decide whether Carbon's scheduling and MCP writes are thin
   (fallback rule).
2. **Primary (4–5 h), in two parts.**
   - **UI scenario (Starter trial or self-hosted Community copy, dummy data only):**
     - a sub-assembly job that is late;
     - two jobs that consume that sub-assembly;
     - a component shortage;
     - a work centre in maintenance.

     Record what Carbon shows for the cause, the downstream impact and the reschedule, and how many
     clicks each step takes. Smallest useful check: change one due date or shift, wait for the replan,
     and re-read the schedule to confirm the placement moved.
   - **Agent surface (no paid plan needed):** from the public tool list and source, write down which
     tool calls an agent would make for each of the four steps, with each tool's classification and
     permission. Mark it "from source, not run".
   - A live MCP run needs Business ($100/user/month, 5-user minimum) or an Enterprise self-hosted
     licence. That is a team decision, not part of the default plan.
3. **Secondary (1 h).** Read Fulcrum's MCP capabilities, getting-started and Permissions & Safety
   pages, then its scheduling docs, for the same four steps.
4. **Write-up (2 h).** Fill the comparison table below, then the gap report.

For each claim, record the link, the date checked, a screenshot or video timestamp, the pricing tier
it needs, and its label. The study is deep enough when each checklist row has a source or reads "not
documented", and the gap report has 3–5 defensible gaps.

**Human steps:** trial sign-up, self-hosting, any paid plan, demo booking and any contact with a
vendor. A teammate does these; the AI does not.

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

- Picking a product we cannot inspect turns the report into marketing paraphrase. Apply the fallback
  rule early.
- An announced feature is not a shipped one. Look for a changelog entry, docs page or source.
- A tool name is not behaviour. Tools read from source stay "not run" until a live call.
- Vendor counts and dates move quickly; record the date and the exact wording.
- A permission refusal or seat boundary filed as a platform gap makes Q2 wrong.
- Screenshots of our own platform must not show record ids or tenant URLs.
