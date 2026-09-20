# Architecture

The implementation separates discovery, compilation, and replay because they have different trust and determinism properties. `DiscoveryEngine` runs a bounded observe-decide-act loop against the async `SurfaceDriver`, asks an `LLMClient` for exactly one schema-constrained action, checks it with the shared `PolicyEngine`, and records a redacted `DiscoveryTrace`. OpenAI uses Responses structured output; Anthropic uses a forced typed tool. The scripted client is isolated as a test fixture and is explicitly excluded from genuine submission evidence.

`CapabilityCompiler` is a pure transformation from a successful trace plus explicit compiler configuration to a versioned `CapabilityArtifact`. It neither imports nor calls a provider. `ReplayEngine` accepts the artifact, inputs, policy, driver, condition evaluator, evidence recorder, and handoff manager. Its package has no import path to discovery or LLM code, enforced by a static test and the submission validator.

Application logic depends only on `SurfaceDriver`. Playwright types stay in `computer_use.surfaces` and the live handoff boundary. The current single-process layout keeps session ownership and evidence sequencing legible. This trades distributed durability and remote operations for a smaller system whose safety claims are directly inspectable.

# Artifact schema

Pydantic v2 models define stable string enums and strict, versioned JSON contracts. A capability contains identity/version, description and risk, target-app metadata, typed inputs, semantic steps, known business outcomes, typed outputs, one final success condition, and provenance/approval metadata. Schemas are exported to `artifacts/schemas` and examples are validated against the shipped capability schema.

Targets are semantic: role/accessibility name, visible text, label, optional frame hints, and an explicitly permitted structural fallback. Resolution is ordered and requires exactly one match; zero and multiple matches are different typed failures. Recorded input values are matched by typed normalization and replaced by `{{parameter}}`. Multiple matching inputs make compilation fail rather than guess. Output reads bind by named source and parsers produce values such as `Decimal`.

`Condition` is the single predicate for explicit waits, per-step checkpoints, known business-outcome detection, resume validation, and final success. This avoids subtly different predicate languages at each layer. Artifacts remain drafts until `approve` changes only their approval metadata.

# Determinism & error handling

Discovery is intentionally not claimed to be deterministic, even with temperature zero. Production replay is deterministic with respect to the approved artifact, input, policy, and observed UI. Recovery is artifact-bounded: each step declares attempt count, backoff, and retryable error codes. Recovery never asks an LLM. The transient fixture proves exactly one checkpoint retry before success, and retry exhaustion is represented as a structured hard failure rather than a fifth status.

`ReplayStatus` has exactly four terminal values: `success`, `business_outcome`, `hard_failure`, and `escalated`. Known business outcomes are tested immediately after a step/checkpoint error and before hard-failure classification, so a missing member is not treated as broken automation. Permission denial and unexpected UI states produce `FailureDetail` containing the failing step, expected condition, bounded redacted observation, evidence references, and retry count. Every run emits a typed result and append-only JSONL events.

Synchronization uses page navigation, locators, and `Condition` checks with bounded timeouts. The demo launcher uses a health poll. No fixed sleep is the primary browser synchronization mechanism; the only engine sleep is an optional artifact-declared retry backoff.

# Heterogeneity & multi-tenant

`SurfaceDriver` is the extension seam for heterogeneity. An accessibility-based desktop adapter, a remote-browser adapter, or a guarded screenshot/coordinate adapter can implement the same observe, navigate, click, type, read, wait, screenshot, and close protocol. The domain, compiler, replay, conditions, policy, and evidence layers would remain independent of the underlying automation library. Screenshot-coordinate targeting would need weaker-confidence metadata and stricter approval because it cannot provide the semantic uniqueness guarantees of accessible web targets.

The artifact model is suitable for a vendor-base capability plus tenant/version override layer, but that storage system is not implemented. A production design would keep a reviewed vendor artifact immutable, overlay narrowly scoped tenant target/route differences, and record a compatibility fingerprint for UI version, accessibility tree landmarks, and route set. Canary replays would compare fingerprints and checkpoint performance. Drift would disable or quarantine the affected override and fall back to human handling, rather than rerecording every tenant automatically.

# Escalation & handoff

Handoff is an explicit ownership state machine on the original live `SurfaceDriver` session: automation requests intervention and becomes released; a named human acquires control; automation is prohibited from acting; the human acts in the same browser; the human releases control; automation re-observes and validates a `Condition`; only then does automation regain ownership. The manager verifies the surface identity so a substitute session cannot be presented as continuity.

The irreversible sub-account commit demonstrates this boundary. Replay stops with `escalated` before the commit and persists goal, reason, current step, safe observation, and paused screenshot. Control transitions go to `control-events.jsonl`. After the human action, resume validation and a second screenshot establish the terminal state. The operator interface and local terminal prompt are intentionally thin; remote streaming and production operator authentication are not implemented.

# Safety

Discovery and replay instantiate the same `PolicyEngine` class and call its action evaluation before UI actions. Policy covers allowed origins, routes, action types, artifact approval, step/run limits, risk levels, and ownership. Off-origin navigation is denied. Draft artifacts cannot replay without an explicit development override. Irreversible actions always require human control.

Redaction happens before evidence serialization. Structured values, exception text, URLs, and representative member IDs are masked recursively. Prompt construction replaces goal input values with reference availability, and observations are bounded. API keys remain `SecretStr` configuration, authorization headers are never logged, and `.env`, runtime sessions, browser profiles, storage state, cookies, caches, and credentials are gitignored. The submission scan checks common credential forms and rejects raw synthetic member IDs in artifacts or evidence.

Prompt-injection resistance is structural rather than heuristic alone: the system prompt marks UI content as untrusted, the model can select only the fixed decision schema, policy independently validates every selected action, and model output cannot broaden origins or the action vocabulary. This reduces risk but does not make arbitrary hostile UIs safe; production would add stronger content isolation and provider-side monitoring.

# Cuts

Implemented deeply are the typed artifact and schemas, deterministic compiler/replay, strict semantic resolver, unified conditions, business outcomes, bounded recovery, shared policy/redaction, evidence, and same-session ownership transfer. The legacy app includes success, missing-member, invalid-input, transient first-load, permission-denied, iframe, empty-state, alert, ambiguous-target, and irreversible-commit fixtures.

Deliberate cuts are desktop automation, screenshot-coordinate execution, remote co-browsing, production authentication, provider retry orchestration, multi-tenant persistence, compatibility-fingerprint services, and scaling infrastructure. They are described only as extension designs, not as shipped features. The operator UI remains local and minimal.

A genuine OpenAI Responses run against the live browser produced the committed `evidence/discovery` trace. The compiler then generated and explicitly approved the reviewable artifact, and replay succeeded with a different fixture input without calling a model. The credential remained in the ignored local environment and did not enter artifacts, events, screenshots, the commit, or the deliverable archive. The strict submission verifier passes, including genuine-evidence completeness.
