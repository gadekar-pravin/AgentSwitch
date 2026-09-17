# Gap report — AgentSwitch vs [Carbon](https://carbon.ms/)

**AgentSwitch can investigate late work, but Carbon has the stronger scheduling engine.**

Updated for AgentSwitch Release 1 (2026-09-17), which fixed the access bugs this report first
described. Evidence: [domain notes](domain-notes.md#release-1-changes-observed-2026-09-17).

## Executive summary

- **Carbon is stronger at scheduling.** It models finite machine and operator capacity, explains why
  operations are delayed, supports non-persistent what-if analysis, and replans after inputs change.
  AgentSwitch's `finite_schedule` still projects every open order to finish on the same day. Before
  Release 1, 50 of 51 late Suryodaya orders got the generic `work_content_exceeds_due_date` code; after
  it, 46 cite `recorded_downtime` and 5 `due_date_passed`, but none names a blocking job.
- **The AgentSwitch agent can still improve the current workflow.** It can join records that a user
  would otherwise inspect separately, identify candidate causes, trace supported customer exposure,
  make an allowed change, and verify the result.
- **Several gaps cannot be solved through prompting.** Capacity-aware dates, reliable replanning,
  cross-job dependency data, and allocation of shared material supply require platform work or a
  cross-seat escalation.
- **The proposed advantage is orchestration, not a better scheduler.** No capability exclusive to our
  agent was established. Its value is completing the request as one evidence-bounded workflow and
  refusing or escalating when the data cannot support an answer.

## Evidence basis

| Source | What was reviewed | Evidence status |
| --- | --- | --- |
| Carbon | Official [scheduling](https://docs.carbon.ms/docs/reference/scheduling) and [MCP](https://docs.carbon.ms/api/mcp) documentation, plus [public source](https://github.com/crbnos/carbon/tree/0bbb3b53) | **Documented**, not run in a trial |
| AgentSwitch | UI, schemas, MCP catalogue, and read-only calls summarized in [domain notes](domain-notes.md) | **Observed**, except where marked inferred or untested |
| Fulcrum | Screened as secondary context | No conclusion in this report depends on its claims |

**Evidence labels:** *documented* means supported by Carbon's documentation or source; *observed*
means seen in AgentSwitch; *inferred* means the evidence suggests the conclusion but does not prove
it; *untested* means we have not safely exercised that behavior.

## 1. What does Carbon do that AgentSwitch does not?

| Capability | Carbon | AgentSwitch today | Gap assessment |
| --- | --- | --- | --- |
| **Capacity-aware scheduling** | Places operations forward within work-centre hours; subtracts maintenance downtime; reserves qualified operators when required | `finite_schedule` projected every submitted open order to finish on the current day. Its capacity basis says work calendars and labour are not modelled. Downtime appeared not to reduce the projection (*inferred*). Unchanged after Release 1: every order still has the same projected finish. | **Platform defect and model gap** |
| **Specific delay explanations** | Stores operation-level notes and conflict reasons, such as waiting behind named jobs, waiting for a work centre, or lacking a qualified operator | Before Release 1, 50 of 51 late Suryodaya orders received `work_content_exceeds_due_date`, largely restating that the due date had passed, and the cause arrays were empty. After it, 46 cite `recorded_downtime` with the downtime attached and 5 `due_date_passed`; `blocking` is still always empty. | **Agent partly; platform output gap** |
| **Expedite what-if** | Public source exposes a non-persistent forecast of projected completion | No equivalent simulation tool was found in the inspected MCP surface. | **Platform capability missing** |
| **Replanning** | Input changes can trigger a whole-location replan; Carbon reports jobs made newly late | No working capacity-based replan was found. | **Platform capability missing** |
| **Shared-material allocation** | Calculates shortfall across active jobs in priority order | `check_stock_availability` evaluates one work order at a time. `StockEntry` and `StockLedger` are outside the Production seat, so repeating the available call cannot establish which job should receive shared stock. | **Agent partly; seat limit plus scoped-service gap** |
| **Downstream schedule effects** | Explains predecessors inside a job and shows newly late jobs after a replan | No direct work-order-to-work-order dependency link exists. | **Agent partly; platform link missing** |

### Important qualification: access bugs are not feature gaps

`JobCard`, `DowntimeEntry`, and `EngineeringChangeOrder` provide progress, downtime, and
engineering-hold evidence. Before Release 1 their tools were listed for the Production seat but
refused access, and bug reports were filed. Release 1 fixed this: all three now read on both
tenants (*observed*). That was an **access bug**, not a missing AgentSwitch feature.

Carbon also has a limitation: our source review found no proven cross-job supply pegging. It can show
predecessor delays within a job and knock-on lateness after a replan, but that does not prove that one
production job supplies a particular consuming job.

## 2. Which gaps can our agent close with existing tools?

### The agent can close these gaps partly

| Task | What the agent can do now | Evidence boundary |
| --- | --- | --- |
| **Investigate why an order is late** | Join `WorkOrder`, linked `MaterialRequest` and `SubcontractOrder` records, `QualityInspection`, and `check_stock_availability` | Since Release 1 the agent can add `JobCard` progress, `DowntimeEntry` reasons, and `EngineeringChangeOrder` affected orders. These still produce **candidate explanations**, not proven causes: many downtime entries have no `work_order_id`, and whether an open ECO holds an order is untested. |
| **Trace customer exposure** | Follow an allowed `WorkOrder.sales_order_id` with `SalesOrder.get` | Available on both tenants since Release 1 (Keystone lacked `SalesOrder.*` before). A linked order shows exposure, not necessarily that the late work order is the sole cause. |
| **Find potentially affected production orders** | Match the late order's output item against materials in other BOMs | This identifies **potential consumers only**. Stock or another work order may satisfy the demand. |
| **Make a permitted date change** | Re-read the target, attempt only a state-valid update, then re-read and report the observed result | A reported `WorkOrder.update` call was refused for `not_started` (not re-checked after Release 1); the seat has no cancel tools; date updates for `draft`, `in_progress`, and `stopped` remain untested. |
| **Escalate blocked work** | Call `endpoint.agent_governance.escalations.raise` with the records checked, the missing evidence, and the requested action | Without a reliable capacity forecast, the agent must not invent a feasible replacement date. |

### These gaps require platform work or escalation

| Required capability | Why the agent cannot safely reconstruct it | Classification |
| --- | --- | --- |
| Capacity-aware projected dates | The available schedule does not model all required calendar and labour constraints. | **Platform defect and model gap** |
| Non-persistent what-if analysis | There is no inspected simulation tool that can test a change without saving it. | **Platform capability missing** |
| Dependable replanning | Re-reading records cannot reproduce a scheduling engine or attribute every concurrent change to our action. | **Platform capability missing** |
| Cross-job dependency or pegging | BOM matching shows possible demand, not a confirmed supply relationship between two work orders. | **Platform link missing** |
| Shared-material allocation | The Production seat cannot read `StockEntry` or `StockLedger`. It needs a human/EA escalation or a new Production-scoped aggregation service. | **Seat limit plus scoped-service gap** |

## 3. What can our agent do that Carbon cannot?

**No exclusive capability over Carbon is established by this study.** Carbon already exposes
permission-scoped reads and writes through MCP. The narrower opportunity is that the reviewed Carbon
sources do not demonstrate one workflow that completes the entire assigned request:

> “This work order is late. Find out why, tell me what it blocks downstream, and reschedule what you
> can.”

### Two concrete orchestration gaps

1. **Carbon keeps capacity and material evidence separate.** Its finite scheduler gates on work-centre
   and operator capacity; material shortage is not a scheduling constraint. The separate shortfall
   service can find missing material, but a shortage will not appear as the scheduler's late cause. An
   agent must join schedule causes and material evidence to produce one investigation.
2. **A Carbon MCP date change does not replan by itself.** After updating a due date or operation
   target, an agent must separately call `production_scheduleJob` or
   `production_notifyScheduleInputsChanged`. The replan covers every open job at the location, so the
   agent must then re-read the target and the wider schedule rather than treating the write response as
   proof of the final result.

These are not capabilities exclusive to our agent; another correctly designed Carbon-connected agent
could perform them. They are specific multi-step behaviors that the reviewed Carbon product sources do
not demonstrate as one end-to-end assistant workflow.

Our agent's design target is the following controlled workflow:

| Step | Agent action | Guardrail |
| --- | --- | --- |
| **1. Confirm lateness** | Re-read the work order and compare its current state and dates | Do not rely on an earlier list result in the shared, changing book. |
| **2. Investigate causes** | Gather material, subcontracting, quality, stock, and available schedule evidence | Separate recorded facts from candidate explanations and unknowns. |
| **3. Trace impact** | Report a linked sales-order risk and possible consuming work orders | Never turn a possible BOM relationship into a confirmed block. |
| **4. Act within authority** | Change only a supported field in a state that permits it | Refuse or escalate when the workflow, permissions, or evidence do not support the action. |
| **5. Verify** | Re-read the target and relevant downstream records | Report observed before-and-after differences without claiming that every concurrent change was caused by the agent. |

The defensible claim is therefore **better orchestration and evidence discipline**, not better
scheduling. The agent should complete as much of the goal as the current seat allows, state exactly
what remains unknown, and hand blocked work to a human with useful evidence. The harness must verify
the resulting platform state rather than trusting the agent's prose or a successful write response.
