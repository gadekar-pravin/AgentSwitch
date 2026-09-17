# Domain notes — manufacturing

Findings from the web UI, `/api/schemas` and `tools/list`. Record what was
observed, on which tenant (Suryodaya or Keystone) and when. Keep raw dumps in
`/dumps/` (gitignored); summarise them here.

## Identity

- `GET /api/auth/me` → roles: _TBD_; allowed_apps: _TBD_

## Entities and workflow states

| Entity | Key fields | States and transitions | Notes |
| --- | --- | --- | --- |
| WorkOrder | | | |
| BOM | | | |
| Routing | | | |
| JobCard | | | |

## MCP tools available to the seat

- Count from `tools/list`: _TBD_
- Tools relevant to "late work order" (read / transition / update): _TBD_

## What "late" and "downstream" mean in the data

- Late: _which date fields, compared to what_
- Blocks downstream: _which links connect a work order to other work orders, sales orders or stock_
- Reschedulable: _which fields or transitions change a schedule, and what rules apply_

## Seat boundaries hit

- _Entities or tools that returned 403 or were absent, and who to escalate to_
