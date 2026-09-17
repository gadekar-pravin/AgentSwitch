# Domain notes — manufacturing

Findings from the web UI, `/api/schemas` and `tools/list`. Record what was
observed, on which tenant (Suryodaya or Keystone) and when. Keep raw dumps in
`/dumps/` (gitignored); summarise them here. This repo is public: record counts and field
names, not record ids, customer names or other live data.

Labels: **observed** = seen in a response; **inferred** = our reading of schemas or data, not
confirmed; **untested** = not called yet.

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
  through `endpoint.manufacturing.check_stock_availability` (untested).

Tools relevant to "late work order":

| Purpose | Tools |
| --- | --- |
| Read | `WorkOrder.list/.get`, `BOM.*`, `Routing.*`, `Operation.*`, `Workstation.*`, `MaterialRequest.*`, `SubcontractOrder.*`, `ProductionPlan.*`, `QualityInspection.*`, `Batch.*`, `SerialNumber.*`, `Item.*`, `SalesOrder.list/.get` (Suryodaya only) |
| Listed but refused (see below) | `JobCard.*`, `DowntimeEntry.*`, `EngineeringChangeOrder.*` |
| Reschedule / state change | `WorkOrder.update` (planned dates, priority), `WorkOrder.submit`, `.start_production`, `.stop`, `.resume`, `.complete`; five `WorkOrder.cancel.<from>.cancelled` |
| App endpoints (untested) | `endpoint.manufacturing.finite_schedule` (GET, arg `horizon_days`), `.genealogy` (GET, arg `code`), `.check_stock_availability` (POST, args `work_order_id`, `bom_id`, `qty`; needs only `WorkOrder.read` but is not marked read-only), `.generate_production_plan`, `.create_work_orders_from_plan`, `.create_material_requests`, `.work_instructions.acknowledge` |
| Escalation | `endpoint.agent_governance.escalations.raise` / `.assignees` / `.update`, `AgentEscalation.*` |

### Listed tools that refuse (observed, both tenants)

`JobCard.list`, `DowntimeEntry.list` and `EngineeringChangeOrder.list` appear in `tools/list` but
return `-32001 Permission denied`, both with no arguments and with `limit` / `offset`. REST `GET /api/JobCard` returns `403`
`row_scope_denied`. The schemas grant `manufacturing_user` read on all three, and they are
manufacturing entities, not another app's data. §6 says a tool the seat may not use is absent,
not refused, so this looks like a platform bug (a candidate report; not filed yet). It also removes
the two most direct "why is it late" sources: job-card progress and downtime reasons.

### Schema traps for an agent (observed in `inputSchema`)

- `WorkOrder.list` and `JobCard.list` advertise create-time defaults on filter fields
  (`status` = `draft` / `open`, `priority` = `medium`, `qty` = 1, ...). The server does not apply
  them: `WorkOrder.list` without `status` returned every status. An agent or client that fills
  schema defaults would silently filter to drafts. Send only the filters you mean.
- `SalesOrder.list` has `date` defaulting to `"today"` (same risk).
- `WorkOrder.update` exposes `status`, `produced_qty`, `actual_*` and costs, also with defaults.
  Filling defaults on update could reset real values; changing `status` by update might bypass
  the workflow (untested; do not try on live data without agreement).
- Every WorkOrder cancel transition has flow role `admin`, yet the tools are listed for our
  `manufacturing_user` seat. Expect a refusal (untested).

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

## What "late" and "downstream" mean in the data

- **Late (inferred):** a work order not `completed` or `cancelled` whose `planned_end_date` is
  before today. A second signal: `draft` / `not_started` with `planned_start_date` before today.
  On 2026-09-17: Suryodaya 57 of 123 are past planned end (draft 6, not_started 34,
  in_progress 13, stopped 4); Keystone 7 of 28. No completed work order finished after its
  planned end on either tenant, so history gives no examples of "late but done".
- **Candidate causes we can read (inferred):**
  - `status = stopped` (4 on Suryodaya).
  - Unreceived MaterialRequests linked to the work order (17 late Suryodaya work orders; 21 of
    those requests are past `required_by_date`).
  - Open SubcontractOrders linked to the work order (33 late Suryodaya work orders; 43 of those
    orders are past `expected_delivery_date`).
  - BOM material `lead_time_days` and stock (via `check_stock_availability`, untested).
  - Workstation status on the BOM or routing operations (no late work order hits an inactive
    workstation today).
  - Draft QualityInspection referencing the work order.
  - Not readable: job-card progress, downtime reasons, ECO holds (refused).
- **Blocks downstream (inferred):**
  - `WorkOrder.sales_order_id` → SalesOrder `delivery_date` / `expected_shipment_date` (65 of 123
    Suryodaya work orders; 12 of 28 on Keystone, where SalesOrder is not readable, so escalate).
  - Sub-assemblies: there is no work-order-to-work-order link. A work order blocks another when
    it makes an item that appears in the other's BOM `materials`. On Suryodaya, 62 open work orders
    make an item that is a material in some BOM. `BOM.parent_bom_id` is never set.
  - `ProductionPlan.items[].sales_order_id` (plans are not linked from work orders today).
  - Batches and serial numbers link back to `work_order_id`; `genealogy` may trace this
    (untested).
- **Reschedulable (inferred, untested):** `WorkOrder.update` with `planned_start_date` /
  `planned_end_date` (and maybe `priority`), re-reading the record first. `stop` / `resume` change
  state, not dates. Unknown: whether updates are allowed after submit (`docstatus` 1), whether
  `finite_schedule` proposes dates, and what capacity rule a new date must respect.

## Seat boundaries hit

- Refused although listed: `JobCard.*`, `DowntimeEntry.*`, `EngineeringChangeOrder.*` (both
  tenants). Candidate bug report; otherwise escalate for the data.
- Absent on Keystone: `SalesOrder.*` (no `viewer` role). Tracing a work order to its sales order on
  Keystone needs an escalation.
- Absent on both: stock, warehouse, purchasing and employee entities. Material availability beyond
  `check_stock_availability` needs an escalation.

## Next checks

- Look at a few late work orders in the UI to confirm which date the business treats as "late".
- Call the untested read endpoints (`finite_schedule`, `genealogy`) and decide whether
  `check_stock_availability` is safe to call (it is POST and not marked read-only).
- Agree as a team before any write test (`WorkOrder.update` on dates, a cancel as `manufacturing_user`).
- Decide whether to file the listed-but-refused report (§10).
