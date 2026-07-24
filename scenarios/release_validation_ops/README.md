# Release Validation Operations

Private operator scenario for the internal Root node. This document is the source of truth for the non-public validation topology and must not be copied into public AdaOS documentation.

## Purpose

The scenario combines two skills:

- `root_mgmnt` supplies Root fleet, policy, lifecycle, and audit context.
- `release_validation_skill` owns the test registry view, manual campaign controls, evidence projection, and terminal notifications.

The AdaOS core service `adaos.services.release_validation` owns the durable schemas, state transitions, classification rules, and allowlisted SSH runner. Keeping those rules in core prevents a UI or LLM tool call from turning test observation into arbitrary remote execution.

## First topology

The initial registry contains one local assignment target:

| Field | Value |
| --- | --- |
| Node ID | `linux-exp-01` |
| Address | `root@192.168.0.30:22` |
| Identity | `d:/git/inimatic/adaos/.ssh/adaos_linux_exp` |
| Profile | `observe` |
| Runtime API | `127.0.0.1:8778` on the target |
| Supervisor API | `127.0.0.1:8776` on the target |

The identity path is stored in the private state but is redacted from API, skill, and Yjs snapshots.

## Durable contracts

`TestNode` records transport coordinates, capabilities, enabled state, and allowed profiles. The first implementation accepts only SSH nodes with exactly the `observe` profile.

`TestSuite` is versioned and contains only named checks from the core allowlist. The first suite is `adaos-observe-smoke` version `1.0.1`.

`ValidationCampaign` binds a suite, node set, quorum, and target policy. The default `latest_installed` policy attests the build already selected and installed by the standard updater. Supplying a build or commit selects the backward-compatible `exact` policy. A new campaign starts as `pending`.

`ValidationAssignment` is the immutable attempt identity for one campaign/node pair. It stores state timestamps, bounded check evidence, and a terminal classification.

State is stored atomically in `.adaos/state/release_validation/state.json`. Set `ADAOS_RELEASE_VALIDATION_STATE_PATH` only for isolated tests or migration.

## State machines

Campaign:

`pending -> running -> passed | failed | inconclusive`

Assignment:

`assigned -> running -> uploading -> passed | failed | inconclusive | timed_out`

A product invariant failure or product-check timeout makes the campaign `failed`. An unavailable runner, missing key, disabled node, missing capability, or exhausted SSH transport timeout is `inconclusive`; it does not mark the tested build defective. Each fixed check gets one bounded retry after a transport timeout or SSH exit `255`. All assignments must avoid product failures and quorum must pass.

## Observe-only boundary

The runner does not accept shell text from a campaign, suite, API request, skill call, or UI field. It can execute only these fixed checks:

1. SSH connectivity with batch mode and strict host-key checking.
2. `systemctl is-active adaos.service`.
3. Local runtime `/api/ping`.
4. Local supervisor `/api/supervisor/public/update-status`.
5. Active-slot manifest identity. An exact campaign also matches it to the requested build.

There is no update, rollback, restart, package install, test upload, or arbitrary command capability in this profile. A future execution profile must use a separate capability, runner, and review path.

## Manual operation

1. Activate `release_validation_ops` on the development node.
2. Select **Register contracts** once. Registration is idempotent.
3. Leave **Build or commit** empty to validate the latest installed build. Enter an exact identity only for a diagnostic comparison; a Git commit may be abbreviated to at least seven hexadecimal characters.
4. Select **Run latest pending**.
5. Read terminal status in the metric widget, assignment table, evidence viewer, and validation journal.

**Run latest pending** is idempotent when no pending campaign exists: it refreshes the latest terminal result without raising an action error. **Retry latest** creates a new campaign for the same target and preserves the previous attempt as evidence.

The equivalent one-call skill tool is `release_validation_skill.run_default_observe()`. Its optional `target_build` argument enables exact comparison; do not use branch names such as `rev2026` as release identities.

The current manual observe-only runner neither updates the target node nor waits for an update. In default mode it validates the active slot selected by the standard GitHub/Root update flow. An optional exact target remains a diagnostic assertion and terminates with `target_build_mismatch` when it differs.

The local authenticated HTTP surface is under `/api/release-validation`: register nodes/suites, create a campaign, then call `POST /campaigns/{campaign_id}/run`. CI integration is intentionally not wired in this phase.

## Notifications

At terminal campaign state, the API and skill publish `ui.notify`. The current node router forwards that event through its configured Telegram route, so the existing administration channel receives the result. No Telegram token or channel ID is stored in this scenario or skill.

`passed` is informational. `failed`, `timed_out`, and `inconclusive` are sent with critical severity for operator attention, but only `failed` or `timed_out` are build-defect candidates.

## Future CI and Root orchestration

The planned CI adapter should authenticate to Root and submit an exact build plus suite ID. Root creates assignments only for registered nodes with compatible capabilities and an active lease. Nodes wait for or attest the exact installed build, run a reviewed suite, upload bounded evidence, and report terminal state. Root aggregates quorum and exposes one campaign result to CI.

Automatic rollback must remain a separate policy decision. Recommended first integration:

1. Root marks the build `suspect` after the first critical product failure and blocks promotion.
2. Root requires either a second independent failing assignment or an operator decision before rollback.
3. Root emits `ui.notify` to Telegram and records an audit event containing campaign, build, failing check, node, and evidence reference.
4. CI polls the campaign endpoint or receives a signed callback and fails the deployment job.

Later phases can add a dedicated test-agent protocol, headless browser suites, artifact retention, signed results, leases/heartbeats, and anti-affinity scheduling. Browser suites should run through a reviewed browser skill or agent capability, not through the SSH observe runner.

## Migration to the internal node

Move the scenario and skill together, install a core revision containing `adaos.services.release_validation`, copy or re-register the private node registry, verify the SSH known-host entry and identity permissions, activate the scenario, and run one exact-build campaign. Telegram routing belongs to the destination node and requires no artifact changes.
