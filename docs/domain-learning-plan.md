# Domain learning plan — manufacturing

Plan for finishing Step 1 of the brief ("learn the domain": real records in the UI, `/api/schemas`,
workflow states and transitions). Findings go into [domain-notes.md](domain-notes.md). This repo is
public: record field names and aggregate counts only, no record ids, customer names, platform URLs or
logins.

Status (2026-09-17): plan drafted and advisor-reviewed. Step 1 done by Claude in Chrome, look-only;
findings in [domain-notes.md](domain-notes.md#ui-walk-observed-2026-09-17). Step 2 done; tables in
[domain-notes.md](domain-notes.md#workorder-links-and-actions). Step 3 skipped by the user's decision.
Step 4 not done.

## Starting point

Most of Step 1 is already done over the API (see domain-notes.md): identity and roles on both
tenants, `tools/list`, all 424 schemas, every row of 11 manufacturing entities, states and
transitions, refusals, and the three app endpoints. Still missing:

- Nobody has looked at the web UI.
- Some schemas and fields are not in the notes: `ManufacturingPreferences`, `QualityPreferences`,
  WorkOrder `approval_status`, `production_strategy`, `project_id`, `quality_inspection_required`,
  and links held in child tables.
- Which WorkOrder dates can be changed in which state. Only the `not_started` refusal is known.

## Plan

About 1–1.5 hours in total.

### 1. UI walk (human, 15–30 min, Suryodaya first)

- Work Order list: columns, filters, any late or overdue badge, sort by planned end.
- One work order in each of `draft`, `not_started`, `in_progress` and `stopped`: which fields are
  editable, which action buttons show, the linked-records panel, any comments or history timeline,
  and how `approval_status` is shown.
- One Sales Order: does it list its work orders?
- A quick look at the same views on Keystone.
- Look only: save nothing and click no action buttons. Screenshots go in `dumps/` (gitignored).

**Stop when:** we know what the UI calls late (or that it gives no definition) and how the four
states differ.

### 2. Targeted schema reads (Claude, 15 min)

- `ManufacturingPreferences` (`auto_create_job_cards`, `auto_consume_materials`,
  `backflush_materials`, `require_quality_inspection`, `auto_close_wo_on_completion`) and
  `QualityPreferences`. These say what Start Production or Complete may trigger.
- The WorkOrder fields listed above.
- Child-table links (under `children.<table>.shape` in the schema, not `fields`):
  `SubcontractOrder.supplied_materials[].batch_id`, `JobCard.materials_consumed[].batch_id`,
  `EngineeringChangeOrder.affected_work_orders[].work_order_id`.
- Read the preference records on both tenants, read-only.

**Output:** two small tables in domain-notes.md:

- WorkOrder links, with evidence strength and access limits.
- WorkOrder actions: source state, action, target state, role, what the schema says, what we observed.

### 3. Date-update test on the team's draft (Claude, 10 min, needs the user's approval)

- Target: the draft work order team04 already created on Suryodaya. No other record.
- Change `planned_start_date` and `planned_end_date` only, sending no other fields (schema defaults
  on update could reset real values).
- Re-read immediately before and after. Stop on a refusal, a concurrent change or any unexpected
  change.

Testing dates on submitted orders would mean taking the draft through Submit, Start Production and
Complete. That cannot be undone (cancel is admin-only) and may create job cards or consume stock
(see step 2). **That is a separate team decision.** Until then the agent treats a date change on a
submitted order as "escalate".

### 4. Fold into the notes (Claude, 20 min)

- Check `GET /api/bug-report/mine` on both instances for resolution notes.
- Add steps 1–3 to domain-notes.md, apply the corrections below, and rewrite "Next checks" as the
  known unknowns handed to the agent build.

## Exit test

A teammate can take one real late work order and say, resting only on observed evidence:

- the rule that makes it late;
- its candidate causes;
- what it confirmed or potentially supplies downstream;
- which actions are verified, which are untested, and what gets escalated.

Untested writes do not block finishing Step 1; they block claiming those actions work.

## Known corrections to apply

- **Withdrawn after step 2:** this plan first said a readable work-order-to-work-order link exists
  through `SubcontractOrder.supplied_materials[].batch_id` → `Batch.work_order_id` (93 cross-order
  lines on Suryodaya). The schema path exists, but the data does not hold together: the line item
  differs from the batch item in 93 of 93, and the batch item is not in the receiving order's BOM in
  93 of 93 (details in [domain-notes.md](domain-notes.md#links)). "No work-order-to-work-order link"
  in [gap-report.md](gap-report.md) stands.
- A shared BOM item shows that one work order **potentially supplies** another, not that it blocks it.
- Stop and Resume change production state, not dates. They are not rescheduling.
- WorkOrder `commentable` and `trackable` are in `reserved_behaviors`, not `behaviors`, and no comment
  tools exist for our seat over MCP. A timeline, if the UI shows one, is visible only to a person.
- Transition guards (`condition`, `auto`) exist elsewhere on the platform (e.g. Invoice `Mark Overdue`),
  but no manufacturing flow has one. The `not_started` refusal is a server rule, not a flow guard.
- Scope the genealogy conclusion to the batches actually tested.
- `approval_status` values (observed): `not_required` on all 123 Suryodaya work orders; on Keystone,
  12 of 28 are `approved`, and 12 have a `project_id`. Whether `pending_approval` or `rejected` blocks
  a transition is unknown.

## Decisions needed

Both settled on 2026-09-17:

1. Who does the UI walk: Claude in Chrome, with the user signed in.
2. The step 3 date update on the team's draft work order: skipped.
