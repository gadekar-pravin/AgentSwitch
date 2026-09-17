# Domain notes — manufacturing

Findings from the web UI, `/api/schemas` and `tools/list`. Record what was
observed, on which tenant (Suryodaya or Keystone) and when. Keep raw dumps in
`/dumps/` (gitignored); summarise them here. This repo is public: record counts and field
names, not record ids, customer names or other live data.

Labels: **observed** = seen in a response; **inferred** = our reading of schemas or data, not
confirmed; **reported** = found by a teammate and filed in a team bug report (see
[Bug reports filed](#bug-reports-filed)), not re-run for these notes; **untested** = not called yet.

## Identity

Checked 2026-09-17 with `GET /api/auth/me`, login `team04`:

| Tenant | Roles | allowed_apps |
| --- | --- | --- |
| Suryodaya | `manufacturing_user`, `user`, `viewer`, `agent_user` | `manufacturing`, `agent`, `crm` |
| Keystone | `manufacturing_user`, `user`, `agent_user` | `manufacturing`, `agent`, `crm` |

Keystone has no `viewer` role, and that does change the tool list (see below).

## Dumps

Pulled 2026-09-17, read-only (`initialize`, `tools/list`, `GET /api/schemas`, and `<Entity>.list`
tools marked `readOnlyHint: true`). Files in `/dumps/`:

- `<tenant>-tools-list.json`: the seat's full `tools/list`.
- `<tenant>-schemas.json`: all 424 entity schemas (identical on both tenants).
- `<tenant>-<Entity>.json`: every row of WorkOrder, BOM, Routing, Operation, Workstation,
  MaterialRequest, SubcontractOrder, ProductionPlan, QualityInspection, Batch, Item.

## MCP behaviour (observed, Suryodaya)

- `initialize` returns protocol `2025-11-25`, `capabilities.tools.listChanged: false`. No
  `Mcp-Session-Id` header.
- `notifications/initialized` returns `202` with an empty body.
- Unknown method: HTTP 200, JSON-RPC error `-32601`. No bearer: HTTP `401`. `GET /api/mcp`: `405`.
- `tools/list` returns everything in one page (no `nextCursor`), about 330 KB on Suryodaya.
- Each tool has `name`, `description`, `inputSchema` (closed: `additionalProperties: false`),
  `outputSchema`, `annotations` (`readOnlyHint`, `destructiveHint`, `idempotentHint`) and
  `_meta.agentswitch` (`permission`; transitions also carry `action`, `source_state`,
  `target_state`; endpoints carry `method` and `path`).
- `tools/call` success: `result` has `content` (one text item), `structuredContent` and
  `isError: false`. List tools return `structuredContent = {data, total, limit, offset}`.
  `limit: 1000` is honoured.
- Permission denied comes back as JSON-RPC error `-32001` with `data.code: "permission_denied"`.
- List rows carry `_display` / `_<field>_display` labels, `_permissions`, `docstatus`, `number`,
  audit fields, and child tables (BOM `materials` and `operations` are included in list results).

## MCP tools available to the seat

- Count from `tools/list`: **296 on Suryodaya, 292 on Keystone**. Keystone lacks
  `SalesOrder.list/.get` and `CRMPreferences.list/.get`. The schemas give `viewer` read on
  SalesOrder, so the missing `viewer` role explains it (inferred).
- Annotations: 152 read-only, 138 write, 6 destructive (all `.delete` on address-book, contact
  group, user preference and trusted-sender entities; none in manufacturing).
- Entities by domain: core 26, agent 21, manufacturing 17, sales 2 (Item, SalesOrder), crm 1,
  plus 35 `endpoint.*` tools.
- No Warehouse, stock ledger, StockEntry, PurchaseOrder or Employee tools. Stock is visible only
  through `endpoint.manufacturing.check_stock_availability` (tested, see below).

Tools relevant to "late work order":

| Purpose | Tools |
| --- | --- |
| Read | `WorkOrder.list/.get`, `BOM.*`, `Routing.*`, `Operation.*`, `Workstation.*`, `MaterialRequest.*`, `SubcontractOrder.*`, `ProductionPlan.*`, `QualityInspection.*`, `Batch.*`, `SerialNumber.*`, `Item.*`, `SalesOrder.list/.get` (Suryodaya only) |
| Listed but refused (see below) | `JobCard.*`, `DowntimeEntry.*`, `EngineeringChangeOrder.*` |
| Reschedule / state change | `WorkOrder.update` (planned dates, priority), `WorkOrder.submit`, `.start_production`, `.stop`, `.resume`, `.complete`; five `WorkOrder.cancel.<from>.cancelled` |
| App endpoints | `endpoint.manufacturing.finite_schedule` (GET, arg `horizon_days`), `.genealogy` (GET, arg `code`) and `.check_stock_availability` (POST, args `work_order_id`, `bom_id`, `qty`) — all three called, see [App endpoints](#app-endpoints-observed-2026-09-17); `.generate_production_plan`, `.create_work_orders_from_plan`, `.create_material_requests`, `.work_instructions.acknowledge` (the rest untested) |
| Escalation | `endpoint.agent_governance.escalations.raise` / `.assignees` / `.update`, `AgentEscalation.*` |

### Listed tools that refuse (observed, both tenants)

`JobCard.list`, `DowntimeEntry.list` and `EngineeringChangeOrder.list` appear in `tools/list` but
return `-32001 Permission denied`, both with no arguments and with `limit` / `offset`. REST
`GET /api/<Entity>` returns `403` `row_scope_denied` for all three (re-checked 2026-09-17). The schemas grant `manufacturing_user` read on all three, and they are
manufacturing entities, not another app's data. §6 says a tool the seat may not use is absent,
not refused, so this looks like a platform bug. Filed: JobCard and DowntimeEntry on 2026-09-16,
EngineeringChangeOrder as a follow-up on 2026-09-17. It also removes the most direct "why is it
late" sources: job-card progress, downtime reasons and engineering-change holds.

### Schema traps for an agent (observed in `inputSchema`)

- `WorkOrder.list` and `JobCard.list` advertise create-time defaults on filter fields
  (`status` = `draft` / `open`, `priority` = `medium`, `qty` = 1, ...). The server does not apply
  them: `WorkOrder.list` without `status` returned every status. An agent or client that fills
  schema defaults would silently filter to drafts. Send only the filters you mean.
- `SalesOrder.list` has `date` defaulting to `"today"` (same risk).
- `WorkOrder.update` exposes `status`, `produced_qty`, `actual_*` and costs, also with defaults.
  Filling defaults on update could reset real values; changing `status` by update might bypass
  the workflow (untested; do not try on live data without agreement).
- Admin-only transitions are listed for our `manufacturing_user` seat: WorkOrder cancel ×5,
  MaterialRequest cancel ×2, ProductionPlan cancel ×2, SubcontractOrder cancel ×2 (11 tools). A
  WorkOrder cancel returned `-32602 ... cannot perform transition 'Cancel' (requires 'admin')`
  (reported, 2026-09-16). An agent must not offer these as options.

## Entities and workflow states

From `/api/schemas`. Counts are rows on 2026-09-17 (Suryodaya / Keystone).

| Entity | Rows | Key fields | States and transitions | Notes |
| --- | --- | --- | --- | --- |
| WorkOrder | 123 / 28 | `item_id`*, `bom_id`, `sales_order_id`, `production_plan_id`, `qty`*, `produced_qty`, `planned_start_date`, `planned_end_date`, `actual_start_date`, `actual_end_date`, `priority`, `approval_status`, `status` | draft →Submit→ not_started →Start Production→ in_progress →Complete→ completed; in_progress ⇄ stopped (Stop / Resume); Cancel from any state (admin) | submittable (`docstatus` 0 in draft, 1 after). Dates are `date`, not datetime. |
| JobCard | refused | `work_order_id`*, `operation_id`, `workstation_id`, `employee_id`, `for_qty`, `completed_qty`, `planned_start/end`, `started_at`, `completed_at`, `time_in_mins`, `actual_time_in_mins`, `sequence`, `materials_consumed[]` | open →Start→ in_progress →Complete→ completed; Cancel (manufacturing_user) | The per-operation progress record. |
| BOM | 100 / 11 | `item_id`*, `routing_id`, `is_subassembly`, `parent_bom_id`, `materials[]` (`item_id`, `qty`, `lead_time_days`, `is_critical`, `preferred_vendor_id`, `source_warehouse_id`), `operations[]` (`operation_id`, `workstation_id`, `time_in_mins`, `sequence`) | none (record) | Suryodaya: 26 sub-assembly BOMs, none with `parent_bom_id` set. |
| Routing | 100 / 0 | `item_id`, `operations[]` (as BOM) | none | 63 Suryodaya BOMs link a routing. |
| Workstation | 14 / 6 | `capacity`, `working_hours_per_day`, `hour_rate`, `status` (active / under_maintenance / decommissioned) | none | Suryodaya: 1 under maintenance, 2 decommissioned. |
| MaterialRequest | 108 / 17 | `work_order_id`, `production_plan_id`, `required_by_date`, `items[]` (`qty`, `current_stock`, `shortage`) | draft → submitted → partially_ordered / ordered → received; Cancel (admin) | Suryodaya: 71 linked to a work order. |
| SubcontractOrder | 100 / 6 | `work_order_id`, `vendor_id`*, `expected_delivery_date`, `actual_delivery_date` | draft → submitted → materials_sent → in_progress → received → (quality_check →) completed | Suryodaya: 94 linked to a work order, 95 still draft. |
| ProductionPlan | 100 / 1 | `from_date`, `to_date`, `items[]` (`sales_order_id`, `planned_qty`, `pending_qty`) | draft → submitted → in_progress → completed | No work order has `production_plan_id` set on either tenant. |
| QualityInspection | 119 / 9 | `reference_type` (WorkOrder, StockEntry, PurchaseOrder, SubcontractOrder), `reference_id`, `overall_result` | draft → in_progress → completed | All reference a WorkOrder; 22 / 9 still draft. |
| DowntimeEntry | refused | `workstation_id`*, `work_order_id`, `job_card_id`, `from_time`, `to_time`, `downtime_mins`, `reason` (breakdown, material_shortage, quality_issue, operator_unavailable, ...) | none | Direct cause data if readable. |
| EngineeringChangeOrder | refused | `bom_id`*, `effectivity_date`, `affected_work_orders[]` (`action`: continue_old / switch_to_new / scrap_and_restart) | draft → submitted → under_review → approved → implemented (review / approve / reject are admin) | Could explain a held work order. |
| SalesOrder | Suryodaya only | `delivery_date`, `expected_shipment_date`, `delivered_status`, `items[]` | draft → confirmed → partially_delivered → delivered | Read-only for our seat. |

## WorkOrder links and actions

From `/api/schemas` (identical on both tenants), `tools/list` and the 2026-09-17 dumps; plan
[step 2](domain-learning-plan.md). Child-table links sit under `children.<table>.shape` in the schema,
not under `fields`, so a scan of `fields` alone misses them.

### Links

Evidence: **schema** = a link field exists; **data** = counts from the dumps (Suryodaya / Keystone).

| Link | Direction | Our seat can read | Data | Use for the agent |
| --- | --- | --- | --- | --- |
| `WorkOrder.item_id` → Item | out | yes | all rows | what it makes |
| `WorkOrder.bom_id` → BOM (`materials[]`, `operations[]`) | out | yes | 123 / 28 (all) | materials, lead times, operations |
| `WorkOrder.sales_order_id` → SalesOrder | out | Suryodaya only (MCP); UI refuses ("App 'accounting' is not enabled") | 65 / 12 | customer delivery at risk |
| `WorkOrder.production_plan_id` → ProductionPlan | out | yes | 0 / 0 | none today |
| `WorkOrder.project_id` → Project | out | no tool | 0 / 12 | escalate if needed |
| `WorkOrder.design_file_id` → DesignFile | out | no tool | 0 / 12 | none |
| `WorkOrder.*_warehouse_id` → Warehouse | out | no tool | target 123 / 28 | none |
| `MaterialRequest.work_order_id` | in | yes | 71 / 4 | material cause |
| `SubcontractOrder.work_order_id` | in | yes | 94 / 0 | subcontract cause |
| `QualityInspection.reference_type = WorkOrder`, `reference_id` | in (polymorphic) | yes | 119 / 9, all WorkOrder | quality cause |
| `Batch.work_order_id`, `SerialNumber.work_order_id` | in | yes | batches 100 / 6 (serial numbers not dumped) | producing order of a lot |
| `JobCard.work_order_id` | in | refused | — | progress (blocked) |
| `DowntimeEntry.work_order_id` | in | refused | — | downtime cause (blocked) |
| `EngineeringChangeOrder.affected_work_orders[].work_order_id` | in (child) | refused | — | ECO hold (blocked) |
| `SubcontractOrder.supplied_materials[].batch_id` → `Batch.work_order_id` | order → order, indirect | yes | Suryodaya 93 lines where the batch's producing order differs from the subcontract's order; Keystone 0 | **not usable** (see below) |
| `JobCard.materials_consumed[].batch_id` → `Batch.work_order_id` | order → order, indirect | refused | — | consumption (blocked) |

No field links one WorkOrder directly to another. `BOM.parent_bom_id` is never set.

**The batch supply link does not hold up in the data (Suryodaya, observed in dumps).** Of the 93
cross-order supplied-material lines: all 93 subcontract orders are `draft` (nothing sent); in 93 the
line's `item_id` differs from the batch's item; in 93 the batch's item is not a material in the
receiving order's BOM; 27 batches carry a different item from their producing order; and 15 batches
belong to orders still `draft` or `not_started`. Batches look randomly wired to orders in the seed
data. Treat this path as a schema capability only; do not present it as downstream evidence.

### WorkOrder actions

All transition tools take only `id` (no reason or date argument). "Schema role" is from `flow`;
"tool permission" is `_meta.agentswitch.permission`.

| From | Action (tool) | To | Schema role | Observed | UI (2026-09-17) |
| --- | --- | --- | --- | --- | --- |
| — | `WorkOrder.create` | draft | `manufacturing_user` (create) | team04 created drafts (2026-09-16) | New button |
| draft | `WorkOrder.update` (dates, priority, …) | draft | write | untested (planned test skipped) | no Edit control |
| draft | Submit (`WorkOrder.submit`) | not_started | `manufacturing_user` | worked: a team04-created order is `not_started`, updated by team04 on 2026-09-16 (inferred from audit fields) | Submit button |
| not_started | `WorkOrder.update` | — | write | refused: "Cannot modify WorkOrder in 'not_started' status… Cancel first" (reported) | no Edit control |
| not_started | Start Production (`.start_production`) | in_progress | `manufacturing_user` | untested; may create job cards (`auto_create_job_cards` on, Suryodaya) | button |
| in_progress | `WorkOrder.update` | — | write | untested | no Edit control |
| in_progress | Complete (`.complete`) | completed | `manufacturing_user` | untested; may trigger an inspection and auto close (Suryodaya preferences) | button |
| in_progress | Stop (`.stop`) | stopped | `manufacturing_user` | untested; no reason field | button |
| stopped | `WorkOrder.update` | — | write | untested | no Edit control |
| stopped | Resume (`.resume`) | in_progress | `manufacturing_user` | untested | button |
| any but cancelled | Cancel (`.cancel.<from>.cancelled`, 5 tools) | cancelled | `admin`; tool says `submit` | refused: requires `admin` (reported) | button shown |

No manufacturing flow has a transition `condition` or `auto` rule (some other entities do, e.g.
Invoice "Mark Overdue" with `today() > due_date`), so the `not_started` update refusal is a server
rule, not a flow guard (inferred).

### Other WorkOrder fields

| Field | Suryodaya | Keystone | Notes |
| --- | --- | --- | --- |
| `approval_status` (`not_required`, `pending_approval`, `approved`, `rejected`) | all `not_required` | 12 `approved` (4 not_started, 6 in_progress, 2 stopped), 16 `not_required` | plain select, no flow; no ApprovalRequest tools for our seat; not shown in the UI. Effect on transitions unknown. |
| `production_strategy` | 68 make_to_order, 55 make_to_stock | 20 / 8 | shown in the UI header |
| `quality_inspection_required` | 69 of 123 (33 of the late orders) | 23 of 28 | with `require_quality_inspection` on, may gate completion (untested) |
| `project_id`, `design_file_id` | 0 | 12 each | no tools for Project or DesignFile |

### Preferences (read over MCP, 2026-09-17)

- **ManufacturingPreferences:** Suryodaya 1 record: `auto_create_job_cards` 1, `auto_consume_materials`
  0, `allow_over_production` 0 (tolerance 10), `backflush_materials` 0, `require_quality_inspection` 1,
  `auto_close_wo_on_completion` 1, costing `standard`. Keystone: no record.
- **QualityPreferences:** Suryodaya 1 record: `auto_inspection_on_receive` 1,
  `auto_inspection_on_complete` 1, `require_inspector_approval` 0, sampling `100_percent`,
  `reject_action` `hold`, `track_non_conformances` 1. Keystone: no record.
- Our seat has create and update on both (so they are shared settings we must not change). Whether the
  server enforces them on transitions is untested.

## What "late" and "downstream" mean in the data

- **Late (observed for submitted orders; inferred for drafts):** a submitted work order
  (`not_started`, `in_progress`, `stopped`) whose `planned_end_date` is before today. The UI's own
  "Late production orders" list uses this rule: its "Due" date is `planned_end_date`, and it counts
  51 on Suryodaya and 7 on Keystone, matching `finite_schedule` (see [UI walk](#ui-walk-observed-2026-09-17)).
  Drafts past planned end are not counted by the UI. A second signal we may use: `draft` /
  `not_started` with `planned_start_date` before today.
  On 2026-09-17: Suryodaya 57 of 123 are past planned end (draft 6, not_started 34,
  in_progress 13, stopped 4); Keystone 7 of 28. No completed work order finished after its
  planned end on either tenant, so history gives no examples of "late but done".
- **Candidate causes we can read (inferred):**
  - `status = stopped` (4 on Suryodaya).
  - Unreceived MaterialRequests linked to the work order (17 late Suryodaya work orders; 21 of
    those requests are past `required_by_date`).
  - Open SubcontractOrders linked to the work order (33 late Suryodaya work orders; 43 of those
    orders are past `expected_delivery_date`).
  - Material shortage: `check_stock_availability` per work order (per-item `shortage`,
    `overall_status`), plus BOM material `lead_time_days`.
  - Workstation status on the BOM or routing operations (no late work order hits an inactive
    workstation today).
  - Draft QualityInspection referencing the work order.
  - Not readable: job-card progress, downtime reasons, ECO holds (refused).
- **Blocks downstream (inferred):**
  - `WorkOrder.sales_order_id` → SalesOrder `delivery_date` / `expected_shipment_date` (65 of 123
    Suryodaya work orders; 12 of 28 on Keystone, where SalesOrder is not readable, so escalate).
  - Sub-assemblies: there is no direct work-order-to-work-order link, and the indirect batch path
    is not usable (see [Links](#links)). A work order potentially supplies another when it makes
    an item that appears in the other's BOM `materials`; that shows possible, not proven, dependency
    (stock or another order could supply it). On Suryodaya, 62 open work orders make an item that is
    a material in some BOM. `BOM.parent_bom_id` is never set.
  - `ProductionPlan.items[].sales_order_id` (plans are not linked from work orders today).
  - Batches and serial numbers link back to `work_order_id`. `genealogy` finds a batch's
    producing work order but cannot follow consumption downstream (see below).
- **Reschedulable (mostly blocked; partly reported):** `WorkOrder.update` on a `not_started` work
  order is refused: "Cannot modify WorkOrder in 'not_started' status… Cancel first to make changes"
  (reported, 2026-09-16). Cancel needs `admin`, so our seat cannot follow that advice. Unknown:
  whether `draft`, `in_progress` or `stopped` work orders accept date updates, and what capacity
  rule a new date must respect. `finite_schedule` does not propose new dates: it projects every
  open order to finish today (see below). If most orders
  cannot be re-dated, "reschedule what you can" may mostly mean: re-date drafts, `stop` / `resume`,
  and escalate the rest with a proposed date (inferred).

## App endpoints (observed, 2026-09-17)

Called read-only on both tenants (GET, `readOnlyHint: true`). Raw results in
`/dumps/<tenant>-finite_schedule*.json` and `/dumps/<tenant>-genealogy-*.json`. Both return
`structuredContent = {status: "ok", result: {...}}`. The input schemas give no type for
`horizon_days` or `code`.

### `finite_schedule`

- **Output:** `counts` (`late`, `on_time`, `unknown`, `no_due_date`), `orders[]`,
  `workstation_load[]`, `capacity_basis`, `schedule_state` (`ready`), `complete` (true).
- **Each order:** `work_order_id`, `status_code`, `due_date`, `projected_finish`, `days_late`,
  `verdict` (`late` / `on_time` / `unknown`), `verdict_code` (the main cause), `causes[]` and
  `operations[]` (one per job card: `job_card_id`, `operation_name`, `workstation_id`,
  `scheduled_start`, `scheduled_finish`, `minutes`, `setup_minutes`, `state`). `handoff` names the
  WorkOrder and `can_edit` (true for our seat).
- **`horizon_days` changes only the echoed `horizon_days` / `horizon_end`** (default 21). The
  orders, verdicts and counts were identical for no argument, 14 and 90.
- **Scope:** open work orders that are submitted (`not_started`, `in_progress`, `stopped`).
  `draft` and `completed` are left out.
- **`due_date` is `planned_end_date`** for every order (61 / 61 Suryodaya, 22 / 22 Keystone).
- **Every order is projected to finish today**, and every operation is scheduled today. So
  `late` here means `planned_end_date` before today, the same as our own rule for submitted orders:
  Suryodaya 51 late, 10 on time (our rule also counts 6 late drafts, giving 57); Keystone 7 late,
  14 on time, 1 `unknown` (`workstation_unassigned`). A teammate saw 49 late on Suryodaya the day
  before, so the set moves.
- **Causes are thin:** `verdict_code` is `work_content_exceeds_due_date` for 50 of 51 late
  Suryodaya orders and `changeover_setup` for 1; all 7 on Keystone are
  `work_content_exceeds_due_date`. Most of those have negative `days_available` (the due date has
  passed), so the code restates "due date passed" rather than naming why. `causes[].downtime` and
  `causes[].blocking` are always empty (reported).
- **Workstation load shows downtime we cannot read directly:** every workstation has
  `downtime_minutes` > 0 (up to about 2,400 on Suryodaya against 480 declared minutes a day), yet the
  workstations stay `ready` and the orders still finish today. Downtime does not seem to reduce
  capacity in the projection (inferred). 5 Suryodaya workstations are `unavailable`
  (decommissioned, under maintenance, or `is_active: false`).
- **`capacity_basis`:** work calendar, labour capacity and initial setup are not modelled.
  Changeovers are modelled on Suryodaya (83 setup-matrix rules; 1 of 85 item changes costed), not on
  Keystone (0 rules).
- **Job-card data leaks through:** all 149 Suryodaya and 93 Keystone operations carry a
  `job_card_id` and label, although `JobCard.list/.get` refuse (reported, together with the
  downtime minutes, on 2026-09-17).
- **Use for the agent (inferred):** a quick list of late submitted orders with their operations
  and workstations. It is not evidence of cause and gives no new dates. Verify against
  `WorkOrder.get` before acting.

### `genealogy`

- **Input:** `code` is a lot code. A `Batch.batch_number` resolves (`resolution: resolved`); a
  WorkOrder number returns `resolution: code_not_found`. No argument: `-32602 Invalid request.`
- **Output:** `subject` (the Batch, with `work_order_id`), `upstream[]`, `downstream[]`,
  `recipients[]`, `unproven[]`, `counts`, `max_depth` (4), `depth_truncated`, and `lane_states`.
- **`lane_states`:** `WorkOrder`, `JobCard`, `Party` are `ready`; `StockEntry` and `StockLedger` are
  `permission_denied`.
- **Result on one batch per tenant:** `upstream` = the producing WorkOrder. `downstream` and
  `recipients` are empty, with 2 `unproven` hops (`documents_denied`, `ledger_denied`). Without stock
  access, genealogy cannot show which later orders or customers consumed a lot.
- **Use for the agent (inferred):** batch → producing work order only. Not a downstream tracer for
  our seat.

### `check_stock_availability`

- **Why it looked unsafe:** POST, `readOnlyHint: false`, no description in the tool or OpenAPI.
- **Why it looked safe:** `readOnlyHint` simply follows the HTTP method for all 35 endpoint tools
  (18 GET true, 17 POST false, including POST previews and verifies). Its permission gate is
  `WorkOrder.read`; the endpoints that create records gate on `.create`.
- **Guarded test (2026-09-17, Suryodaya, one call):** `{work_order_id}` of a draft work order team04
  created. Snapshots of WorkOrder, MaterialRequest, ProductionPlan, SubcontractOrder, Batch,
  SerialNumber, QualityInspection, Notification, AgentMemory and AgentMessage taken before and 3 s
  after showed **no created, removed or changed rows**, and the target work order was unchanged.
- **Output:** `structuredContent = {status: "ok", result: {overall_status, items[]}}`; each item has
  `item_id`, `required`, `available`, `shortage`, `status` (`ok` / `partial` / `critical`) and
  `is_critical`. The test order came back `critical` (2 of 3 materials with zero available).
- **Verdict:** read-only as far as our seat can observe. Not proven: we cannot read the stock
  ledger or the audit log (`/api/audit-log` needs an administrator or auditor role), so a stock
  reservation would be invisible. Safe for the agent to call; do not rely on it being idempotent.
- **Use for the agent (inferred):** the one direct material-shortage signal for "why is it late".

## UI walk (observed, 2026-09-17)

Walked in Chrome with the team04 login, look-only: no action button, form or Save was pressed. Plan:
[domain-learning-plan.md](domain-learning-plan.md) step 1. Suryodaya in full, Keystone briefly. No
screenshots kept.

### Where "late" shows

- **Company overview (both tenants):** a Manufacturing card with orders in progress (Suryodaya 19,
  Keystone 9), "Overdue production orders" (51 / 7) and "Orders not started" (38 / 11). A "Late
  production orders" list shows each order's "Due" date (= `planned_end_date`) and status.
- **All Work Orders:** status tiles. Suryodaya 123 total: draft 16, not started 38, in progress 19,
  completed 46, stopped 4. Keystone 28 total: draft 0, not started 11, in progress 9, completed 6,
  stopped 2. The per-status lists show different default columns (e.g. Pending Start shows planned
  start date, In Progress shows actual start date, Stopped shows notes); none shows planned end date
  or a late badge.
- **Finite schedule view** (All Work Orders → "Finite schedule"): the same data as the
  `finite_schedule` endpoint, with a per-order explanation (e.g. "The work itself does not fit… the
  order's own date leaves -91 day(s)") and a stated basis: 21-day horizon from today, declared
  workstation hours, recorded downtime and changeover setup charged, no work calendar, labour not a
  constraint, first operation on each machine never charged setup.
- **Manufacturing dashboard** page failed to load ("Unable to load the manufacturing dashboard"), also
  after Retry.

### Work order record

- **Header:** number and item, status, a coloured dot (appears to be priority), production
  strategy, BOM with revision, planned start → end dates, qty, produced, expected and actual cost.
- **Buttons per state; no Edit control in any state, including the team's own draft:**

  | State | Buttons |
  | --- | --- |
  | `draft` | Submit, Cancel |
  | `not_started` | Start Production, Cancel |
  | `in_progress` | Complete, Stop, Cancel |
  | `stopped` | Resume, Cancel |

  Cancel is offered in every state although it needs `admin`. Dates cannot be changed in the UI; this
  does not show whether `WorkOrder.update` accepts dates on a draft (still untested).
- **Overview cards:** BOM cost, cost variance, production progress, WIP accounting, sales order (shown
  as a raw id), operation time (planned, actual, job cards done).
- **Tabs:** Job Cards, Materials (BOM materials with lead time and critical flag; items shown as raw
  ids; no stock or shortage), Operations (sequence, workstation, planned and actual minutes, cost),
  3D View, Quality (inspections, with a New Inspection button), Traceability (batches and serial
  numbers).
- **Not shown on a work order:** stop reason, comments or history, linked material requests or
  subcontract orders, `approval_status`, project.
- A banner on every record: "Some related sections are unavailable in your permission scope: JobCard."

### Exception Cockpit (Suryodaya)

"Review named material shortages, automation failures, subcontract follow-up, and quality concerns."

| Section | What our seat sees |
| --- | --- |
| Material shortages | "Evidence is not available for this role" |
| Quality review | 28 inspections: 6 completed with result rejected, 22 draft |
| Subcontract follow-up | 4 submitted orders with delivery overdue |
| Automation failures | "Evidence is not available for this role" |

### Settings (General = ManufacturingPreferences)

- **Suryodaya:** auto create job cards on, auto consume materials off, allow over-production off
  (tolerance 10), backflush materials off, require quality inspection on, auto close work order on
  completion on, costing method standard. Enforcement of these settings is untested.
- **Keystone:** no settings record (empty form).
- The page is an editable form with Save; our seat can write it. Do not change it.

### Where the UI misleads

- **Refusals shown as empty lists:** the Downtime Log says "No records found" and a work order's Job
  Cards tab says "No job cards for this work order", while the API refuses both entities.
- **Hidden actuals:** an in-progress Suryodaya order shows 800 actual operation minutes with "0 / 0"
  job cards done; the minutes come from job cards we cannot read.
- **Sales order link:** opening it gives "You do not have access to this list. App 'accounting' is not
  enabled for your account", although `SalesOrder.get` works over MCP on Suryodaya. Different door,
  different answer; not a bug under the seat-boundary rule.
- **Page-scoped KPI:** the Subcontracting list's "Overdue 25" tile says "of 25 shown", i.e. the current
  page only, not the 100 orders.
- **Currency:** Keystone (a US business) shows costs in ₹.

## Seat boundaries hit

- Refused although listed: `JobCard.*`, `DowntimeEntry.*`, `EngineeringChangeOrder.*` (both
  tenants), and the 11 admin-only cancel transitions. Reported; until fixed, escalate for the data.
- Absent on Keystone: `SalesOrder.*` (no `viewer` role). Tracing a work order to its sales order on
  Keystone needs an escalation.
- Absent on both: stock, warehouse, purchasing and employee entities. Material availability beyond
  `check_stock_availability` needs an escalation. `genealogy` reports `StockEntry` and
  `StockLedger` as `permission_denied`.

## Next checks

- Agree as a team before any write test, e.g. `WorkOrder.update` on dates for a `draft`,
  `in_progress` or `stopped` work order the team created. The draft date test in
  [domain-learning-plan.md](domain-learning-plan.md) step 3 was skipped on 2026-09-17.
- Check `GET /api/bug-report/mine` and the [live bug tracker](https://claude.ai/artifact/6LvLawFFUXGoRHUQKbPg9h)
  for resolution notes before building around a refusal.

## Bug reports filed

Reports are stored per instance: `GET /api/bug-report/mine` on Suryodaya does not show reports
filed on Keystone. Check both before filing. Listed 2026-09-17; all status `new`, stored locally
(no GitHub issue, as expected).

Live tracker for bug reports and fixes: <https://claude.ai/artifact/6LvLawFFUXGoRHUQKbPg9h>.

| Instance | Filed | Title |
| --- | --- | --- |
| Suryodaya | 2026-09-16 | JobCard and DowntimeEntry are unreadable for manufacturing_user, though the schema grants read (both instances) |
| Suryodaya | 2026-09-16 | finite_schedule returns JobCard ids that JobCard.get reports as not found |
| Suryodaya | 2026-09-16 | Admin-only transitions are listed in tools/list for manufacturing_user |
| Suryodaya | 2026-09-16 | finite_schedule never attributes downtime or blocking to late orders |
| Suryodaya | 2026-09-17 | Follow-up to the JobCard/DowntimeEntry report: EngineeringChangeOrder is unreadable too |
| Suryodaya | 2026-09-17 | finite_schedule exposes downtime and job-card data that the entity tools refuse (follow-up to the row-scope and job-card-id reports) |
| Keystone | 2026-09-16 | Keystone Workstation numbers contain the literal format token |
| Keystone | 2026-09-17 | Keystone: Workstation numbers are malformed (duplicate of the report above, filed before we saw it; adds that all 12 Keystone SerialNumbers have an empty `number`) |
