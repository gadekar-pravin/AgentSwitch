# Plan: move the agent and harness to the S17Code architecture

Status: agreed by the team (2026-09-19). Phase 1 is done (see section 10); phases 2 and later are not
started.

Reference architecture: S17Code (`EAG3-17/S17Code`, package `s17code/`, `config/`, `proofs/`). We port
its patterns into `agentswitch/`. We do not depend on or import S17Code.

## 1. Decisions

| Decision | Choice |
| --- | --- |
| Scope | Both the agent loop (`agent.py`, `llm_client.py`) and the eval harness (`harness/`) |
| Reuse | Port the patterns; no dependency on `s17code`, its `glc_v5` gateway or Ollama. OpenRouter stays the provider behind one seam, so the platform's own LLM can replace it later |
| Components | Capability manifest, frontier planner, live graph with a journal, evidence-readiness critic; config-owned policy; metered model calls with a budget ledger and spans; rubric LLM judge as a secondary score |
| Planner wire format | S17's patch (`add` with `depends_on`, `finish`, `reason`) sent as the arguments of one function, `plan_frontier`, using native function calling |
| When to replan | After each frontier settles. S17 replans after every node; that is a config switch here, off by default |
| Scoring authority | Database-reading verifiers decide every verdict. The judge never changes a verdict or an exit code |
| Critic | Deterministic. No LLM critic in this plan |
| Config format | One TOML file, `config/agentswitch.toml` (`tomllib`, standard library) |
| Graph library | networkx (`DiGraph`), as in S17. Our first runtime dependency, added with `uv add networkx` (section 5.4) |
| Tests | AI writes all test code. A test scores for the brief only if a teammate specified it (section 9, `CLAUDE.md`) |

The choices came from two rounds of review with independent advisors (section 13).

## 2. Why, and what must not change

The course brief grades "your own loop, a task set with verifiers that read the database rather than
your agent's prose, and every run written to disk before anything is scored". S17Code gives us a
cleaner loop: the model proposes only the next steps, Python decides what may enter the graph, and
every step is journalled and metered. It does not give us better scoring. Our harness already scores
the right way, so the harness change is mostly structure, not rules.

These behaviours stay exactly as they are:

1. **Code builds the scored answer.** Lateness, causes and downstream impact are computed by the
   `analyze_*` rules in `investigate.py` from the records the agent read. Model prose never determines
   the verdict; the judge scores it separately.
2. **The one write stays guarded.** `reschedule()` re-reads the work order and writes only planned dates
   on a draft created by our login, then re-reads to confirm. Otherwise it returns `not_needed`,
   `cannot_plan` or `escalated`, with proposed dates when it could compute them.
3. **The pre-write target is pinned.** The answer is built from the model's last read of the target
   before the write (`agent.py:304-314`), and the verifiers compare against the same pre-action
   snapshot (`harness/runner.py:240-243`).
4. **No retry of `tools/call`.** A transport failure is `outcome_unknown` and is never repeated.
5. **Run record before score.** The record is written (exclusive create, fsync, secrets redacted, hard
   failure if a secret survives), read back from disk, and scored from the persisted call log plus fresh
   MCP reads.
6. **Verdict rules.** fail beats inconclusive beats pass; inconclusive is never a pass; `not_applicable`
   only when every non-audit verifier is N/A. Existing drift handling and the documented scoring limits
   in the README stay as they are.
7. **Fixture safety.** Fixture file before the fixture write, restore in `finally`, concurrent-edit
   detection, `restore.json`, `fixture_residue` scored inconclusive, exit code 5 on a failed restore,
   overriding every other code.
8. **Live target selection.** Targets are picked from live data at run time; no record ids are
   committed.
9. **Seat boundary.** Only tools from `tools/list`, only schema arguments, destructive tools always
   refused, never `PUT /api/accounting/locale`, never the platform's built-in agents.
10. **The deterministic subject stays forever** as the baseline the LLM agent is compared against.
11. **The independent rule copies in `harness/rules.py` stay independent.** Sharing code between the
    agent and its verifiers would let one bug check itself.

## 3. What S17Code has, and what we take

| S17 part | Take | Change or leave out |
| --- | --- | --- |
| Capability registry: `Capability`, `Argument`, strict `validate()`, families, `side_effect` | Yes | Arguments built from the live MCP `inputSchema`; validation rejects bad values instead of trimming them or filling defaults |
| Frontier planner with repair loop and dedup | Yes | Replan per frontier by default; two repair budgets (5.3); duplicates reported back instead of dropped silently; a provider error in the planner fails the run visibly. (S17 lets a critic call's exception escape `plan()`, `planner.py:171-196`) |
| Evidence-readiness critic | Yes, deterministic | S17's critic is an LLM call. Ours is today's required-read and contradiction checks |
| Live graph: `GraphPatch` as the only mutation, journal, replay, networkx `DiGraph` store | Yes | The journal is kept in memory and embedded in the run record. No on-disk checkpoint and no resume (5.5) |
| Concurrent executor | Yes | Threads, not asyncio (our MCP client is synchronous). `max_workers = 1` until phase 5 |
| Terminal join | Yes | The terminal node runs only when nothing else is running or pending, enforced by the executor. S17 auto-joins only already-succeeded leaves when the planner gives no dependencies (`planner.py:384-390`) |
| Run ends when the terminal succeeds | Yes | Same as S17 (`planner.py:105-109`): no extra model call |
| Answer worker | No | S17's terminal worker asks an LLM to write the answer. Ours runs `_build_raw` |
| `ActionOutbox` | Idea only | One durable action receipt written before the write (5.6) |
| `request_approval` human gate | No | The harness cannot answer a human gate; write authority is decided up front (5.6) |
| Metered call seam, ledger, pure budget policy, price table | Yes | One model, so the policy is `proceed` or `refuse`; no tier ladder, downgrade or branch |
| Journal-to-span telemetry | Yes | Spans as JSON beside the run record. No OTLP exporter (needs a dependency) |
| Rubric judge (`evals/`) | Yes | One judge model; result and cost in a sidecar file; never part of the verdict |
| Proof harness (`proofs/harness.py`) | Shape only | Offline mode is explicit. S17 falls back to offline automatically when the gateway is unreachable (`proofs/harness.py:180-190`) |
| Declarative contract checks | Some | Only checks backed by the call log and journal. No `answer_contains` (grades prose), no `min_rounds` (rewards waste) |
| Channels, A2A, memory, UI, coding surface, events/autonomy, markdown skills, web research | No | Not relevant to a seat agent |

## 4. Target layout

| Path | Holds |
| --- | --- |
| `config/agentswitch.toml` | Tables `[models]`, `[pricing]`, `[budgets]`, `[limits]`, `[evals]` (section 8) |
| `agentswitch/config.py` | Loads and validates the config; env may override named keys; records the effective config and its hash |
| `agentswitch/capabilities.py` | Registry built from the live seat catalogue plus two local capabilities |
| `agentswitch/graph.py` | networkx `DiGraph` wrapper, node states, `GraphPatch`, journal, replay |
| `agentswitch/executor.py` | Runs ready nodes, frontier settling, exclusive and terminal rules, calls the planner |
| `agentswitch/planner.py` | `plan_frontier` prompt and schema, patch validation, dedup, repairs, deterministic critic |
| `agentswitch/answer.py` | `_build_raw`, read requirements, answer and refusal shapes, moved out of `agent.py` and `harness/subjects.py` |
| `agentswitch/economics.py` | Metered call seam, admission, ledger, pricing |
| `agentswitch/telemetry.py` | Span tree built from the journal |
| `agentswitch/judge.py` | Rubric judge |
| `agentswitch/offline.py` | Offline MCP and LLM transports (infrastructure) |
| `agentswitch/harness/tasks.jsonl` | Task data (section 7.1) |
| `proofs/` | Offline proof scripts; output to `proofs/out/` (gitignored) |

`agent.py` keeps the current loop as the `llm` subject until phase 8. `mcp_client.py`, `investigate.py`,
`reschedule.py` and `harness/rules.py` keep their rules.

## 5. The agent

### 5.1 Capabilities

- **Reads:** the 14 allowlisted read-only tools, intersected with the live `tools/list` as today
  (`agent.py:109`). A tool must be in the catalogue, `readOnlyHint` true and not destructive. List tools
  keep today's reduced filter sets; the worker adds paging arguments itself, pages to the end and
  removes duplicate ids.
- **`reschedule_work_order`:** always offered, as today. Whether it may actually write is decided by the
  run's write authority (5.6), not by hiding it: the two late-order tasks ask for a reschedule without
  write permission, and their verifier requires a `reschedule` claim (`write_verifiers.py:121-123`).
- **`answer`:** the terminal capability, with `outcome` (`answered` or `refused`), `refusal_reason`
  (nullable) and `prose`. Named `answer`, not `finish`, so it is not confused with the patch's `finish`
  flag.
- **Families** replace name checks: `read`, `list`, `side_effect`, `exclusive`, `terminal`.
- **One argument gate.** `validate()` checks unknown keys, required keys, types, enums and nullable
  values, and keeps today's extra list-filter rules: no empty values, no `:placeholder` values, `*_id`
  filters must be strings (`agent.py:924-963`). It rejects bad values; it does not trim strings or insert
  defaults. A tool whose schema uses constructs the gate does not support is left out of the manifest
  and logged, rather than half-validated. The validated arguments are what enter the graph, are
  deduplicated and are sent to MCP.

Coarse capabilities such as `find_causes` were rejected: they would move the reads back into code and
leave nothing for the model to decide.

### 5.2 Planner

One planner call per round. The model must answer with exactly one call to `plan_frontier`:

```json
{"add": [{"id": "wo_read", "capability": "WorkOrder.get", "arguments": {"id": "..."}, "depends_on": []}],
 "finish": false,
 "reason": "why these steps, based on outcomes so far"}
```

`add[].arguments` is an open object in the function schema: one function schema cannot express a
different argument schema per capability. Python validates it per capability with `validate()`.

`cancel` is left out as a simplification: the executor never cancels running work, and failed parents
are handled by rule (5.4).

The prompt is rebuilt each round rather than appended to a transcript: goal, target id, today, the
manifest, authority, limits, and each node with a bounded projection of its outcome plus truncation
flags. Today's loop resends the whole transcript every turn; one `late_with_cause` run on 2026-09-18 used
641,974 prompt tokens. Full records stay in the evidence store.

Python validates every patch:

- capability is offered this run; arguments pass `validate()`;
- at most `max_new_tasks` new nodes and at most `max_nodes` in total. `max_new_tasks` must be at least
  the size of the required-read list (about 12 calls), so one frontier can cover it;
- `depends_on` names existing nodes only; no dependency on a failed node; no cycles. A node that depends
  on another node in the same patch is discarded and reported, for the planner to propose again next
  round;
- identical work (same capability, same validated arguments) already pending, running or succeeded is
  not added again. The planner is told which existing node already covers it and that node's outcome. A
  patch whose additions are all duplicates is a soft repair (5.3). A failed node is never treated as a
  duplicate, so the model may propose that read again; the executor never repeats a call on its own;
- `answer` must be the only addition in its patch, and nothing may be added after it;
- an empty patch while nothing is pending or running is a hard repair.

### 5.3 Critic and repairs

There are two separate repair budgets, both in config.

- **Hard repairs** fix a patch that breaks the rules above: unknown capability, bad arguments, bad
  dependency, limits, authority, more than one function call, a contradiction (below). When they run
  out, the run fails visibly. There is no fallback to the old loop.
- **Soft repairs** cover missing evidence. When the patch adds `answer` with `outcome = answered`, the
  deterministic critic runs today's required-read check (`_read_requirements`, `agent.py:591`): exact
  filters, complete scans. Missing reads go back to the planner as `not ready; missing=[...]` with the
  exact calls. When soft repairs run out, the `answer` is accepted and the missing reads become unknowns
  in the answer, as today (`agent.py:1284-1302`).
- **Contradiction checks** (hard): `answered` needs a successful model read of the target; `not_found`
  is impossible after a successful target read.
- The required reads are computed from the full evidence store, not from the projections, so
  truncation cannot hide a requirement. A required tool missing from the catalogue becomes an unknown,
  not an endless repair.
- A refusal (`not_found`, `outside_seat`, `unsupported`, `source_unavailable`) does not need the
  lateness read list.

### 5.4 Graph and executor

- **Store:** one networkx `DiGraph` per run, as in S17 (`s17code/core/live_graph/store.py`). Node state
  and outcome live in node attributes, read and written only through `graph.py`, which exposes typed
  accessors so the rest of the code does not touch raw attribute dicts.
- **What networkx is used for:** the acyclic check when a patch is applied
  (`is_directed_acyclic_graph`), `ancestors` for the write rule in 5.6 (a succeeded `WorkOrder.get` of
  the target must be an ancestor), `descendants` to mark nodes `blocked` when a parent fails, and
  `node_link_data` to put the final graph in the run record. Cycles cannot actually form, because
  `depends_on` names only existing nodes, so the acyclic check is a guard.
- **Version:** pinned through `uv.lock`. As in S17 (`store.py:113`), `node_link_data` is called with
  `edges="edges"` so the key name is explicit. Its output is part of the run record, so a networkx
  upgrade that changes the format needs a schema version bump.
- **Node states:** `pending`, `running`, `succeeded`, `failed`. A node whose parent failed is marked
  `failed` with reason `blocked` straight away, so nothing waits on it.
- **A list read succeeds only when its scan is complete.** An empty or duplicate page before the
  reported total makes the node `failed` with reason `incomplete_scan`. The rows it did read stay in the
  evidence store, marked partial, and can support an answer but never count as a complete scan.
- **Frontier:** the nodes accepted in one patch. It settles when none of them is pending or running.
  With `replan = "frontier"`, the planner runs once per settled frontier. With `replan = "node"` it runs
  after each outcome (S17 behaviour).
- **Ready rule:** a node runs when every parent has succeeded.
- **Exclusive rule:** a node in the `exclusive` family starts only when nothing else is running, and
  nothing else starts until it finishes.
- **Terminal rule:** `answer` starts only when nothing else is pending or running. When it succeeds the
  run ends; no planner call follows.
- **Threads:** read nodes run in a thread pool of `max_workers`. Before `max_workers` goes above 1
  (phase 5), three things must be made safe: the MCP client's request-id counter (`mcp_client.py:471`),
  and the harness recorders' shared sequence, call list and observations
  (`harness/recorder.py:38-50, 124-150`). `ScopedWriteTools` also changes a shared `phase` during its
  guard read (`recorder.py:247`).
- **Call attribution:** each MCP call log entry records its node id and phase at call time and both a
  start and an end sequence, so ordering checks can use completion order.
- **Shutdown:** on any exception the pool is drained and joined before the harness's `finally` restore
  runs.
- **Evidence order:** outcomes are added to the evidence store in node-id order within a frontier, so
  the answer does not depend on thread timing.

### 5.5 Journal

- **Events:** `run_started`, `graph_patched` (accepted patch, rejected attempts, repair counts, meter
  records), `task_started`, `task_succeeded`, `task_failed`, `action_started`, `action_finished`,
  `run_finished`, `run_failed`. Each has a sequence number and wall-clock and monotonic timestamps.
- **Kept in memory** and embedded in the run record, which goes through the existing redaction and
  hard failure. There is no separate checkpoint file.
- **Replay** rebuilds the graph from the journal. The harness compares it with the final graph in the
  same record (`journal_consistent`, 7.4).
- **No resume.** A crashed run is not resumed: resuming could re-enter the write. Harness runs are short
  enough to run again.

### 5.6 The write

`reschedule_work_order` is in the `side_effect` and `exclusive` families.

1. **Write authority** is granted only when all of these hold: `--allow-draft-writes` is set, the task
   has `writes: true`, the target was selected, and the fixture was prepared. The effective authority is
   stored in the run record. Without it, the worker calls `reschedule(..., allow_write=False)`, which
   still returns the plan and proposed dates (`escalated`/`writes_disabled`, `reschedule.py:224-225`).
2. **Accepted** only for the target id, only once per run, and only with a succeeded model
   `WorkOrder.get` of the target as an ancestor. Code adds that edge if the model left it out and rejects
   the node if no such read exists.
3. **Pin** the model's last read of the target before anything else, as today.
4. **Receipt.** When write authority is granted, the worker first writes `<stem>.action.json` (key: run
   id, target id, action name; through the same redacting, exclusive, fsynced writer). If that write
   fails, the reschedule does not run. The journal records `action_started`.
5. **Run `reschedule()` unchanged:** guard re-read, owned-draft check, date-only update, confirmation
   read. The executor must never skip the guard read because the graph already read the target.
6. **`action_finished`** records the result: `applied`, `mismatch`, `not_needed`, `cannot_plan`,
   `escalated`, `write_failed` or `unknown`.
7. **Interruptions.** A receipt without `action_finished` means the outcome is uncertain. The run is
   failed, the update is never sent again in that run, and only a read may be used to report the
   current dates. The existing fixture and restore handling covers the record. A new run is a separate
   action.
8. **No transactional claim.** Nothing stops another team editing the record between the guard read
   and the update. The confirmation read and the verifiers catch the result.

### 5.7 Terminal worker and the answer

- `answered`: the worker runs `_build_raw` (moved to `answer.py`) over the evidence store, using the
  pinned target when a reschedule ran. It adds the unknowns for missing reads and the fixed stock-ledger
  unknown, and stores the prose separately.
- `refused`: the worker emits today's refusal shape (`harness/subjects.py:20-27`) without `_build_raw`.
- **Model reads** are reads from nodes the model proposed. Reads made inside `reschedule()`, by target
  selection or by verifiers are kept apart. This is slightly stricter than today, where
  `_read_requirements` also counts a target read made by `reschedule()` (`agent.py:604`); the change is
  intentional.
- The answer shape does not change, so the verifiers do not change.
- An execution failure (budget refused, hard repairs exhausted, limits hit, provider error) is a failed
  run, never a refusal answer.

## 6. Economics and telemetry

- **One seam.** Every provider attempt for the planner and the judge goes through `admit → call →
  charge`. The client's own retry loop (`llm_client.py:147-168`) moves into the seam, so every attempt
  is admitted, counted and charged.
- **Admission** estimates the cost before each attempt: input tokens estimated from characters (with a
  safety factor, including the tool schema) plus the configured `max_tokens`, at the config price. The
  client starts sending `max_tokens`; it does not today. The estimate is an estimate, not a proof of the
  upper bound. If the remaining budget cannot cover it, the call is refused and the run fails visibly.
- **Charge** uses OpenRouter's `usage.cost` when present (it is present, in USD, in the run records we
  checked), otherwise the config price. Both are recorded, with the difference. An attempt with no usage
  block (timeout, network error) keeps its reserved amount charged. If an actual charge exceeds the
  reservation, the ledger records the overrun and later admissions use the true remaining budget.
- **Money** is held in integer micro-dollars.
- **Ceilings** on attempts per run and per round apply regardless of price. Read nodes make no model
  calls, so there is no per-node ceiling.
- **Ledger** is written into the run record.
- **Spans** are built only from the journal: run → round → planner attempt | node → MCP call, with GenAI
  attributes (provider, model, tokens) and cost. They are written to `<stem>.spans.json` through the
  same redacting writer. Prompt and completion text are not included.
- **Which budget:** the ledger enforces our OpenRouter spend. The 1M-token daily limit in `CLAUDE.md`
  belongs to the platform's in-app agent.

## 7. The harness

### 7.1 Tasks as data

`agentswitch/harness/tasks.jsonl` holds one task per line with the fields `TASKS` has today (`id`,
`request`, `request_kind`, `selector`, `expected`, `brief_refusal`, `reschedule`, `writes`) plus
`expectation`, a plain-text success criterion used only by the judge.

`selector` names an entry in a Python registry; an unknown name is a configuration error (exit 2).
Verifier selection stays in code (`harness/runner.py:295-437`): task data does not choose verifiers.
Target selection stays live and in code.

### 7.2 Subjects

- `deterministic`: `investigate()` + `reschedule()`, permanent baseline.
- `llm`: today's loop, until phase 8.
- `graph`: the new agent.

All three produce the same answer shape and face the same verifiers.

### 7.3 Run record and files

Schema version 2.0 adds, for the `graph` subject: the manifest offered, the journal, the final graph,
accepted and rejected patches, the action receipt, the effective write authority, the ledger, and the
effective config with its hash.

Order per task: select → fixture file → fixture write → subject (with `<stem>.action.json` before a write)
→ run record → read it back → score → restore in `finally` → `restore.json` → `score.json` →
`spans.json`. Then, only with `--judge`, `<stem>.judge.json`. The judge runs after `score.json` exists
and writes its own file.

Every file goes through the existing redacting, exclusive, fsynced writer with its hard failure.

### 7.4 Audit checks

The current verifiers stay the authority. New checks run only when the run record has a journal. They
return `pass`, `fail` or `inconclusive`, and they are added to the audit set in `_score_verdict`
(`harness/runner.py:149`), so they can fail a task but cannot turn an N/A task into a pass.

| Check | Fails when |
| --- | --- |
| `journal_consistent` | Replaying the journal does not give the final graph in the record |
| `write_after_target_read` | `action_started` is not preceded by `task_succeeded` for a model `WorkOrder.get` of the same target |
| `single_subject_write` | More than one subject `WorkOrder.update` attempt, or a receipt without `action_finished`. Fixture and restore writes are excluded |
| `capabilities_registered` | A node used a capability not in the manifest offered for that run |
| `limits_respected` | Node, frontier, repair or attempt ceilings were exceeded |
| `terminal_last` | Any node started after `answer` started, or was still pending or running when it started |

`no_writes` and `writes_in_scope` stay as they are.

### 7.5 Judge

- Scores prose against the rubric in `[evals]`: `addresses_task`, `specific`, `consistent`,
  `complete`, and `meets_expectation` when the task has an `expectation`.
- Forced JSON output, strict parsing. Unparseable output is `judge_failed`, which is neither resolved
  nor unresolved. Empty prose is `unresolved` without a call.
- Requests keep `data_collection = "deny"`: prose and the task contain live tenant data.
- Discloses self-judging (judge model equals the agent model).
- Has its own budget; its cost and attempts go in the judge file. A judge budget refusal, provider error
  or write failure is recorded in that file or printed, and changes neither the verdict nor the exit
  code.

### 7.6 CLI and exit codes

`--subject graph` and `--judge` are added. Exit codes stay 0, 2, 3, 4, 5 (overrides) and 130. Offline
mode is only for proofs and cannot write to `/runs/`.

## 8. Config

- One file, `config/agentswitch.toml`, validated at start. A missing key or wrong type is exit 2.
  - `[models]`: agent model, reasoning effort, seed, `max_tokens`, timeouts, retry count.
  - `[pricing]`: price per million input and output tokens per model, and a default row so an unknown
    model is never free.
  - `[budgets]`: per-run budget (provisionally $0.25 per task run), judge budget (provisionally $0.05
    per task), attempt ceilings, admission safety factor.
  - `[limits]`: `max_nodes`, `max_new_tasks`, `max_workers`, hard and soft repair counts, page size,
    projection size, `replan`.
  - `[evals]`: rubric criteria, weights, scale, threshold, per-criterion floor, judge model.
- Env overrides only for named keys, with documented precedence. Secrets stay in `.env`.
  `OPENROUTER_MODEL` stays as an override during migration.
- Python keeps the safety rules that config must never relax: destructive tools refused, one scoped
  write, `data_collection = "deny"`, no retry of `tools/call`.
- Limits live in the config file, not in env vars. S17 keeps them in env vars, and its own `CLAUDE.md`
  lists missing env vars as a common cause of silent behaviour changes.

## 9. Offline proofs and tests

- `agentswitch/offline.py` provides an offline MCP transport (fixed records) and an offline LLM
  transport (fixed replies). The planner, validation, executor, journal, economics and spans stay real.
- Offline mode is chosen explicitly; there is no fallback from live to offline. Offline output goes to
  `proofs/out/`, never `/runs/`, and is never scored as a harness run.
- **Authorship.** AI writes all test and proof code. Following `CLAUDE.md` ("Tests: AI writes the
  code; what counts is who specified the test"), a test scores for the brief only when a teammate
  specified what it checks, its inputs and the expected result. Tests Claude or Codex add on their own
  score zero. Each test's docstring states `Spec: human (<name>)` or `Spec: AI (<tool>)`.

Candidate tests, described here in prose and drafted by AI. They are AI-originated until a teammate
adopts a spec, owns it and fills in the exact inputs and expected results. They use the offline
transports.

1. A patch naming an unknown capability, an unknown argument, a missing required argument or a
   `:placeholder` value is rejected with a hard repair prompt.
2. A patch with a dependency on a missing or failed node, or a cycle, is rejected; a node whose parent
   fails is marked `blocked`.
3. Without write authority, `reschedule_work_order` returns `escalated`/`writes_disabled` with proposed
   dates and sends no update.
4. `answer` for `answered` with a required read missing gets a soft repair naming the exact read; after
   the soft repairs run out, the answer is accepted with that read listed as unknown.
5. A duplicate read is not added again, and the planner is told which node covers it.
6. A list read that hits an empty page before the reported total is `failed` with `incomplete_scan`,
   and the critic does not count it as complete.
7. `answer` does not start while a read is running.
8. The write node does not start while another node is running, and nothing starts during it.
9. If writing `<stem>.action.json` fails, no update is sent.
10. An attempt the budget cannot cover is refused before the transport is called, and the run is
    failed, not refused.
11. Replaying the journal gives the final graph.
12. An unparseable judge reply gives `judge_failed` and changes neither the verdict nor the exit code.
13. A new audit check on a `draft_writes_disabled` task leaves the verdict `not_applicable`.
14. A task file naming an unknown selector is a configuration error.

## 10. Migration phases

"Verdicts" means pass/fail/inconclusive/not_applicable per task. Live comparisons record the tenant,
date and selected target, and treat `inconclusive` from drift or fixture residue as neither a pass nor
a failure of the phase.

| Phase | Work | Exit criterion |
| --- | --- | --- |
| 0 | This plan. Fix the stale "scaffold only" section of `CLAUDE.md` (done 2026-09-19: now lists what exists and points here). The `CLAUDE.md` test-authorship change is committed (`9d8b00e`) | Team agrees the plan and the `CLAUDE.md` changes |
| 1 | Untangle: create `answer.py`; remove `agent.main`'s import from `harness.subjects`; one shared page-size constant and one BOM-matching helper for `agent.py` and `investigate.py` (not for `rules.py`). Done 2026-09-19: `answer.py` holds `Store`, `read_requirements`, `coverage`, `build_raw`, `refusal` and `project_answer`; `investigate.py` holds `PAGE_LIMIT` and `matching_bom_ids`. The verifiers keep their own copies | ruff clean; `deterministic` and `llm` verdicts unchanged on the read-only tasks on Suryodaya. Met 2026-09-19: 6/6 pass for both subjects before and after the change, one run each; `deterministic` answers identical |
| 2 | Config and economics: TOML loader, metered seam with the retry loop inside it, `max_tokens`, ledger in the run record, all under the current `llm` subject | Same verdicts; a run with a tiny budget fails visibly and is persisted; effective config recorded |
| 2b | Task data to `tasks.jsonl` (harness only; can run beside 3–6) | `deterministic` verdicts unchanged |
| 3 | Capability registry from the live catalogue; the current loop uses `validate()` | The read part of the manifest matches today's `build_tool_menu` on the same catalogue |
| 4 | `uv add networkx`; graph, journal, executor (`max_workers = 1`), planner with `plan_frontier`, critic and repairs, terminal worker, audit checks from 7.4 and their `_score_verdict` wiring, offline transports; `graph` subject on read-only tasks | Offline tests 1–7 and 10–13 pass. `graph` verdicts equal `deterministic` verdicts on the six read-only tasks, on both tenants, in 3 runs per tenant. Planner validity, repair counts and cost per task recorded next to `llm` |
| 5 | Concurrency: locks on the MCP request id and the recorders, per-call start/end sequences and node ids, pool shutdown before restore; `max_workers` above 1 | Same verdicts as phase 4 with `max_workers = 4`; journal shows overlapping reads with correct node attribution |
| 6 | The write: write authority, pin, receipt, exclusive rule | Tests 3, 8 and 9 pass. `reschedule_own_draft` passes and restores on Suryodaya with `graph`; `writes_in_scope`, `write_after_target_read` and `single_subject_write` pass |
| 7 | Spans file and judge sidecar, `--judge` | `deterministic` verdicts unchanged; a forced `judge_failed` changes neither verdict nor exit code |
| 8 | Switch: `graph` becomes the LLM subject when the switch criteria in section 14 (answer 2) are met; update README and `CLAUDE.md`. The old loop stays as a frozen baseline (answer 3) | Team sign-off |

The phase 4 comparison is against `deterministic`, because the `llm` subject has not yet been run on
Keystone.

## 11. Risks

| Risk | Mitigation |
| --- | --- |
| GLM-5.3-flash handles the nested `plan_frontier` schema poorly | Phase 4 records validity and repair rates; the model is a config change. Unverified until measured |
| Bounded projections hide an id the model needs for its next read | Truncation flags; the critic computes required reads from full evidence and names the exact missing calls |
| More planner calls than today | Per-frontier replanning; a fresh bounded prompt per round instead of a growing transcript; `max_new_tasks` large enough for the required reads in one frontier; budget ceilings |
| Races in the MCP client or recorders | `max_workers = 1` until phase 5 adds the locks |
| The shared book changes during a run | Unchanged: guard re-read, confirmation read, existing drift handling |
| A repeated write | Once per run, receipt before the write, no resume, uncertain outcome never retried |
| Secrets in new fields or files | Every file goes through the existing redacting writer with its hard failure |
| Judge output mistaken for the score | Separate file written after `score.json`; never in the verifier list; exit codes ignore it |
| Live tenant data sent to the judge's provider | `data_collection = "deny"`; judge off unless `--judge` |
| Agent and verifiers disagree on covering filters | Unchanged: the agent requires exact filters, while the verifiers also accept an empty filter as covering (`agent.py:682`, `verifiers.py:789`) |

## 12. Not in this plan

LLM critic, judge panels of more than one model, OTLP export, tier ladders, human gates, resume,
per-node replanning as the default, and everything S17 has for channels, A2A, memory, UI, coding and
autonomy. Each can be added later if a task needs it.

## 13. How this plan was made

- Both codebases were mapped in full by read-only agents.
- Round 1: three independent advisors (Astra, Fable, Grok) answered the same brief on capability
  mapping, safety under concurrency, harness mapping, config and migration. They agreed on
  fine-grained capabilities, a code-built answer, a deterministic critic, TOML, an own DAG, a guarded
  single write with no replay, a judge that never scores, and keeping the deterministic subject. They
  split on the planner wire format and replan timing; the team chose S17's patch through one function
  and per-frontier replanning.
- Round 2: two advisors (Astra, Fable) reviewed the first draft against both codebases. Their findings
  changed this version: `reschedule_work_order` stays always offered, the pre-write pin is kept, repairs
  are split into hard and soft, incomplete list scans fail their node, new audits join the audit set,
  retries move inside the metered seam, the journal stays in memory, the LLM critic was dropped, and the
  five config files became one.
- After review, the team chose networkx over the advisors' own-DAG recommendation, to stay close to
  S17's graph store; and chose to have AI write all test code, with teammates specifying the tests
  that count for the brief.
- Checked directly in code or run records: the unlocked request-id counter (`mcp_client.py:471`), the
  hard-coded audit set (`harness/runner.py:149`), the call-start sequence (`harness/recorder.py:41`), the
  client's retry loop, the pre-write pin, the read-only tasks that require a reschedule claim,
  `usage.cost` and the 641,974-token run.

## 14. Open questions: provisional answers

The answers below are provisional (2026-09-19), set so the work can start. The team can change any of
them. Cost figures come from the OpenRouter `usage.cost` of the 44 `llm` run records in `runs/` from
2026-09-18 (Suryodaya; several models were compared that day): refusals cost $0.0003–$0.12, and late
orders cost $0.016–$0.25, with a median of about $0.04. The highest single run cost $0.28.

1. **Cost ceiling.** `[budgets]` sets a per-task-run budget of $0.25 and a judge budget of $0.05 per
   task. That is about six times the $0.042 GLM late-order cost in the README and below the highest
   run seen. A run that hits the budget fails visibly (section 6). Revisit after phase 4 has measured
   the planner's real cost.
2. **Switch criteria.** `graph` replaces `llm` as the LLM subject when all of these hold:
   - Its verdicts equal `deterministic`'s on the six read-only tasks on both tenants, in 3 runs per
     tenant spread over at least 2 days.
   - `reschedule_own_draft` passes and restores on Suryodaya in 2 runs. It runs on Keystone too only if
     Keystone has a draft created by our login.
   - No `fail`. An `inconclusive` caused by drift or fixture residue is rerun, not counted.
   - Its median cost per late-order task is at most 1.5 times the `llm` median on the same tenant, and
     no run exceeds the budget.
3. **The old loop.** Keep the `llm` subject after the switch as a frozen baseline: bug fixes only, no
   new features. It shows graph against loop on the same tasks, and it is the fallback if `graph`
   regresses. Delete it after the capstone is submitted.
4. **Owners and test specs.** Pravin Gadekar owns every phase until the team assigns owners. Nobody
   specifies the section 9 tests on the team's behalf: under `CLAUDE.md` each test stays AI-originated
   until a named teammate adopts its spec. The team picks which ones to adopt before phase 4.
