# Root Management Economic Policy Plane

Status: target architecture and roadmap.

Updated: 2026-08-26.

Payment processing is explicitly deferred. This document defines how the
existing `root_mgmnt` skill and Root backend should evolve into a
root-governed economic policy plane for AdaOS subscriptions, resource
entitlements, usage accounting, quota exhaustion, and observable degradation.

## Current Baseline

The current `root_mgmnt` skill is a private root operator skill for LLM policy,
fleet activity, and subnet lifecycle hygiene.

Current skill contract:

- skill name: `root_mgmnt`
- current version: `0.5.1`
- activation: lazy, startup allowed, no background refresh
- primary scenario: `release_validation_ops`
- projection slot: `root_mgmnt.snapshot`
- Yjs target path: `data/root_mgmnt`
- update sources: Root SSE `snapshot.changed`, material subnet member events,
  and explicit refresh calls

Current tool surface:

- `get_snapshot`
- `refresh_snapshot`
- `get_metric_tile`
- `get_policy_summary`
- `get_fleet`
- `get_lifecycle_candidates`
- `get_audit_events`
- `get_subnet_details`
- `freeze_subnet_llm`
- `unfreeze_subnet_llm`
- `mark_dormant`
- `reactivate_subnet`
- `archive_dev_space`
- `retire_subnet`
- `set_policy_mode`
- `set_llm_enabled`
- `allow_subnet`
- `remove_allowed_subnet`

Current Root backend surface:

- `GET /v1/root_mgmnt/snapshot`
- `GET /v1/root_mgmnt/events`
- `POST /v1/root_mgmnt/policy`
- `POST /v1/root_mgmnt/subnets/:subnetId/action`

Current policy model covers:

- global LLM enablement
- LLM access mode: `open`, `allowlist`, `denyall`
- default model and allowed models
- development model profiles
- allowed subnets
- per-subnet lifecycle overrides
- per-subnet LLM freeze/block overrides

Current observability covers:

- root management snapshot
- fleet rows
- lifecycle candidates
- validation summary
- audit events
- LLM request and denial counters by subnet

Current gaps:

- no durable subscription plan schema
- no subscription assignment object
- no entitlement snapshot consumed by subnets
- no reserve/commit/release usage ledger
- no token-accurate root management accounting
- no local subnet enforcement for paid/noncritical resources
- no explicit disabled-resource projection in capacity/quota objects
- no payment integration, by design

## Problem

AdaOS subnets can spend scarce or paid resources through LLM calls, Codex/API
work, Root MCP sessions, managed-target tools, skill execution, background
subscriptions, projection rebuilds, Yjs writes, external integrations, storage,
media indexing, and development tasks.

The existing `root_mgmnt` slice is the right control point, but it should not
grow as one-off LLM switches. It should become a small, typed economic policy
plane that is Root-authored, subnet-enforced, and observable.

## Goals

- Make Root the authoritative policy and audit point for economic access.
- Represent subscription state separately from payment state.
- Project compact entitlements to subnets so they can enforce local resource
  policy without knowing billing details.
- Keep management, recovery, health, and observability reachable when paid
  resources are exhausted.
- Make every denial or downgrade visible with stable reason codes.
- Preserve the existing root management snapshot and tool surface while adding
  subscription fields incrementally.
- Support manual/admin-managed plans first.

## Non-Goals

- No payment provider integration in this phase.
- No checkout, customer portal, invoices, taxes, receipts, refunds,
  chargebacks, or dunning.
- No silent disabling of whole subnets.
- No payment-provider terms in skill manifests or runtime enforcement.
- No entitlement path that bypasses security, owner access, or runtime safety
  guards.

## Placement

The economic policy plane belongs at Root because Root is already the trust,
policy, routing, audit, and managed-target aggregation point.

```text
manual admin / future billing adapter
  -> root_mgmnt subscription state
  -> entitlement compiler
  -> entitlement snapshot per subnet
  -> Root gates: LLM, Root MCP, managed-target tools
  -> subnet gates: skills, subscriptions, IO, Yjs, background work
  -> observability: audit, usage, denials, incidents, quota projections
```

Root owns the durable source of truth. A subnet owns local enforcement for work
that happens inside the subnet after it receives an entitlement snapshot.

## Design Rule

Subscription state decides economic entitlement. Entitlement snapshots decide
runtime behavior. Payment systems, when added later, may update subscription
state, but they must not become direct dependencies of subnet enforcement.

## Core Objects

### SubscriptionPlan

A reusable plan template. It defines resource classes, limits, reset period,
grace behavior, and degradation policy.

Candidate fields:

- `plan_id`
- `title`
- `status`
- `resource_limits`
- `capability_grants`
- `reset_period`
- `grace_policy`
- `degradation_policy`
- `created_at`
- `updated_at`
- `updated_by`

### AdaosSubscription

The effective subscription assigned to an owner, organization, or subnet.
Payment details are intentionally absent.

Candidate fields:

- `subscription_id`
- `scope`: `owner`, `organization`, or `subnet`
- `scope_id`
- `plan_id`
- `state`: `active`, `trial`, `exhausted`, `past_due`, `suspended`,
  `cancelled`
- `current_period_start`
- `current_period_end`
- `grace_until`
- `manual_overrides`
- `created_at`
- `updated_at`
- `updated_by`

`past_due` is allowed as a manual/admin state before payments exist. A future
billing adapter may set the same state later.

### EntitlementSnapshot

The compact, Root-authored policy artifact consumed by subnets.

It answers:

- which capabilities are enabled
- which resource classes are limited
- which resources are exhausted
- which degradation mode applies
- when the snapshot expires
- which Root revision produced the decision
- why a capability is disabled

Subnets should enforce the entitlement snapshot, not raw plan or payment state.

Candidate fields:

- `schema`
- `subnet_id`
- `subscription_id`
- `plan_id`
- `revision`
- `issued_at`
- `expires_at`
- `offline_grace_until`
- `resources`
- `capabilities`
- `disabled_resources`
- `reason_codes`
- `root_policy_digest`

### UsageLedgerEvent

The append-only economic event stream.

Event kinds:

- `reserve`
- `commit`
- `release`
- `deny`
- `adjust`
- `reconcile`
- `override`

Candidate fields:

- `event_id`
- `idempotency_key`
- `request_id`
- `job_id`
- `subnet_id`
- `owner_id`
- `resource_class`
- `amount`
- `unit`
- `model`
- `status`
- `reason`
- `entitlement_revision`
- `created_at`
- `meta`

LLM and job resources should use `reserve -> commit actual -> release
remainder` when actual usage is only known after execution.

### EnforcementDecision

A shared decision envelope used by Root and subnets.

Recommended fields:

- `allowed`
- `policy_state`: `allow`, `observe`, `throttle`, `read_only`, `deny`,
  `disabled`
- `reason`
- `resource_class`
- `subscription_id`
- `entitlement_revision`
- `limit`
- `used`
- `remaining`
- `reset_at`
- `ttl_s`
- `observable_event_id`

## Identity And Scope

Economic policy should be resolved by explicit scope:

- `subnet` for direct subnet assignments and overrides
- `owner` for user-owned subnets
- `organization` for future team or business plans
- `platform_default` for bootstrap, local dev, and free/default behavior

Resolution precedence:

1. security and owner access policy
2. manual Root block, freeze, retire, or suspension
3. explicit subnet subscription
4. owner or organization subscription
5. platform default or trial policy
6. local runtime safety guards

Entitlements cannot make a forbidden security action allowed. Runtime safety
guards can still throttle or quarantine work that is economically entitled.

## Resource Classes

Initial resource classes should be broad enough to cover real costs without
overfitting to one provider.

- `llm.tokens.input`
- `llm.tokens.output`
- `llm.tokens.reasoning`
- `llm.requests`
- `llm.concurrent_jobs`
- `root_mcp.sessions`
- `root_mcp.tool_calls`
- `skill.activations`
- `skill.subscription_invocations`
- `skill.background_refresh`
- `yjs.write_bytes`
- `yjs.projection_rebuilds`
- `stream.fanout_bytes`
- `external_api.calls`
- `integration.outbox_messages`
- `storage.bytes`
- `media.indexing_jobs`
- `dev_factory.tasks`
- `build.test_minutes`
- `member_nodes`
- `browser_sessions`

First implementation priority:

- `llm.requests`
- `llm.tokens.input`
- `llm.tokens.output`
- `llm.tokens.reasoning`

The current Root LLM proxy already has model, caller, request, job, and
provider usage context, so this is the lowest-risk first accounting slice.

## Enforcement Model

### Root Enforcement

Root must enforce resources that cross Root:

- `/v1/llm/*` calls
- Root MCP session issuance
- Root MCP tool execution
- managed-target operational tools
- development factory task assignment
- future paid cloud-side services

Root enforcement is authoritative and fail-closed when the request would spend
Root-held paid resources.

### Subnet Enforcement

Subnets must enforce resources that can be spent locally:

- skill activation
- skill subscription handlers
- background refresh
- projection rebuild and materialization
- Yjs write pressure
- external integrations
- media indexing
- local development, build, and test jobs

Subnet enforcement should use the same broad vocabulary as runtime guarding:

```text
allow -> observe -> throttle -> read_only -> deny/disabled
```

Economic policy adds subscription and entitlement reasons to the existing
observable guard surface.

### Critical Management Budget

These surfaces must remain available even when a subscription is exhausted or
suspended:

- health, readiness, and liveness
- root management snapshot and events
- read-only control-plane inventory
- owner recovery and access management
- critical control subscriptions
- audit and denial reporting
- local UI status explaining disabled resources
- entitlement refresh attempts

Paid or noncritical work should be disabled observably, not by taking the whole
subnet offline.

## Exhaustion Behavior

When a subscription has no remaining entitlement for a resource:

- new spending requests are denied before work starts
- in-flight reservations may finish within reserved budget
- background work is paused or disabled
- paid skills move to `disabled_by_subscription`
- read-only projections remain available where possible
- denial events are recorded at Root and, when applicable, inside the subnet
- capacity/quota projections show the disabled resource and reason

Preferred reason codes:

- `subscription_missing`
- `subscription_inactive`
- `subscription_suspended`
- `subscription_exhausted`
- `quota_exhausted`
- `resource_not_in_plan`
- `entitlement_expired`
- `root_unavailable_grace_expired`
- `model_not_entitled`
- `capability_not_entitled`
- `manual_policy_block`

## Root Unavailable Behavior

Subnets may use cached entitlement snapshots until `expires_at`.

After expiry:

- critical management remains available
- spending paid resources fails closed
- low-risk local-only free resources may continue only if the last entitlement
  explicitly allowed offline grace
- every fail-closed decision uses `root_unavailable_grace_expired`

This keeps recovery possible while preventing indefinite paid-resource spend
under stale policy.

## Observability

Economic policy is not implemented if it only blocks calls. It must be visible.

Root should expose:

- current subscription state
- effective entitlement snapshot per subnet
- usage windows by resource class
- denied windows by reason code
- latest resource decision per subnet
- audit trail for policy changes, overrides, and denials
- lifecycle candidates such as dormant, exhausted, or retire candidate subnets

Subnets should expose:

- active entitlement revision
- entitlement freshness
- disabled capabilities and resources
- last denial reason
- local economic guard decisions
- affected skills, subscriptions, and jobs

Quota and capacity projections should represent disabled resources explicitly.
Capabilities should not simply disappear from the observable model.

## Snapshot Compatibility

The existing `/v1/root_mgmnt/snapshot` response must remain backward
compatible.

Existing fields should remain:

- `overview`
- `policy`
- `fleet`
- `lifecycle_candidates`
- `validations`
- `audit`

Economic fields should be additive. Recommended additions:

- `economy`
- `subscriptions`
- `plans`
- `entitlements`
- `usage`
- `denials`

Fleet rows may add:

- `subscription_state`
- `plan_id`
- `entitlement_revision`
- `resource_status`
- `exhausted_resources`
- `disabled_resources`
- `current_period_end`
- `grace_until`

The skill projection under `data/root_mgmnt` should expose the same information
through compact sections that UI surfaces can consume independently.

## API Shape

The existing `/v1/root_mgmnt/*` family should remain the human/admin-oriented
management surface. New endpoints should be additive.

Candidate Root endpoints:

- `GET /v1/root_mgmnt/snapshot`
- `GET /v1/root_mgmnt/events`
- `GET /v1/root_mgmnt/plans`
- `POST /v1/root_mgmnt/plans`
- `GET /v1/root_mgmnt/subscriptions`
- `POST /v1/root_mgmnt/subscriptions`
- `GET /v1/root_mgmnt/subnets/:subnetId/entitlements`
- `POST /v1/root_mgmnt/subnets/:subnetId/entitlements/refresh`
- `GET /v1/root_mgmnt/subnets/:subnetId/usage`
- `POST /v1/root_mgmnt/usage/adjust`

Candidate subnet endpoints or projections:

- `GET /api/economy/entitlement`
- `GET /api/economy/usage`
- `GET /api/economy/decisions`
- `data.runtime.economy`
- `data.quota.*`
- `data.capacity.*`

## Data Flow

```text
1. Admin or future billing adapter updates subscription state.
2. Root validates and stores the state in root_mgmnt.
3. Entitlement compiler derives an EntitlementSnapshot.
4. Root gates Root-side spending requests against the snapshot.
5. Subnet fetches or receives the snapshot.
6. Subnet applies local gates for noncritical resources.
7. UsageLedger records reservations, commits, releases, denials, and overrides.
8. Snapshot, quota, capacity, and incident projections expose the result.
```

## Use Cases

### Active Subscription

A subnet with an active subscription can call Root LLM endpoints, use entitled
Root MCP tools, and run entitled local paid resources. Usage is recorded by
resource class and shown in Root management views.

### Exhausted LLM Budget

Root denies new LLM work with `quota_exhausted`. The subnet remains manageable,
and the UI can show which LLM resource is exhausted and when it resets.

### Suspended Subscription

Root denies paid resources with `subscription_suspended`. Critical management,
read-only inventory, audit, and entitlement refresh remain reachable.

### Expired Entitlement While Root Is Unavailable

The subnet fails closed for paid resources after grace expiry with
`root_unavailable_grace_expired`. Local recovery and observability surfaces
continue.

### No Resource In Plan

A skill or Root MCP tool requests a capability outside the plan. The decision is
`resource_not_in_plan` or `capability_not_entitled`, and the capability remains
visible as disabled in projections.

## Roadmap

### Must

- Define durable schemas for `SubscriptionPlan`, `AdaosSubscription`,
  `EntitlementSnapshot`, `UsageLedgerEvent`, and `EnforcementDecision`.
- Extend `root_mgmnt` state with manual subscription and plan records.
- Keep payment fields out of the core subscription schema.
- Add entitlement compilation for a subnet from plan, subscription, overrides,
  and current usage.
- Enforce `llm.requests` and basic token/resource-class policy at Root.
- Convert current LLM request counters into resource-class usage summaries
  while preserving backward-compatible snapshot fields.
- Record denials with stable reason codes and audit event IDs.
- Expose effective entitlement state in the `root_mgmnt` snapshot.
- Add subnet-facing entitlement projection with expiry and revision.
- Keep critical management and read-only surfaces outside paid-resource denial.
- Document and test default fail-closed behavior for stale entitlements.
- Add tests for active, exhausted, suspended, missing, and expired-entitlement
  cases.

### Should

- Add `reserve -> commit -> release` accounting for LLM jobs.
- Track token usage by model/profile and subnet.
- Add Root MCP session and tool-call entitlement gates.
- Add local subnet enforcement hooks for skill activation and background
  subscriptions.
- Add capacity/quota projections that show disabled resources with reasons.
- Add observable guard events for economic `throttle`, `read_only`, and
  `disabled` decisions.
- Add root management UI affordances for plan assignment, manual suspension,
  quota adjustment, and denial drill-down.
- Add entitlement cache and grace behavior in subnet runtime.
- Add reconciliation for jobs that reserve budget but fail before commit.
- Add usage rollups for 24h, 7d, 30d, and current reset period.

### Could

- Add per-skill resource declarations in manifests.
- Add price-like internal weights without exposing payment details.
- Add organization/team-level subscription ownership.
- Add soft limits and warning thresholds before hard exhaustion.
- Add user-visible upgrade prompts once product UX is ready.
- Add free-tier offline allowances for local-only capabilities.
- Add anomaly detection for unexpected resource spikes.
- Add budget simulation and dry-run policy tools.
- Add per-scenario or per-workspace resource budgets.
- Add exportable economic audit reports.

### Deferred

- Payment provider integration.
- Checkout, customer portal, cards, invoices, taxes, receipts, refunds,
  chargebacks, and dunning.
- Automatic plan changes from external billing webhooks.
- Multi-currency pricing.
- Revenue recognition or accounting-system integration.
- Marketplace revenue share for third-party skills.

## Acceptance Criteria

The first complete slice is accepted when:

- a subnet with an active manual subscription can spend Root LLM resources;
- an exhausted subnet receives deterministic `quota_exhausted` denials;
- a suspended subnet receives deterministic `subscription_suspended` denials;
- the same subnet remains manageable through root management and read-only
  control-plane surfaces;
- denials are visible in Root audit and root management snapshot;
- the subnet can expose which resources are disabled and why;
- tests prove stale entitlement fail-closed behavior after grace expiry;
- existing `root_mgmnt` snapshot consumers continue to work unchanged;
- no payment-provider concept is required by implemented schemas.
