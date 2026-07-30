# Expert Builder Reference Repositories Design

## Goal

Allow the existing `expert_builder` pipeline to inspect zero or more already-present, advisory read-only reference repositories while preserving one authorized write and delivery target and verifying that each reference matches its recorded baseline at the integrity gates.

## Background

Some development tasks require source-level awareness beyond the repository being changed. For example, a target UX repository may need to follow APIs and conventions from `amplifier-bundle-attractor`, then prove compatibility with Attractor in a clean-room reality check.

The pipeline does not need multi-repository scheduling or delivery. It needs one development loop with:

- exactly one authorized write and delivery target: the current working directory;
- zero or more advisory read-only repositories available for inspection;
- explicit selection of references that also represent validation dependencies; and
- deterministic verification that references match their recorded baselines at defined gates.

## Chosen Approach

Add optional reference-repository support directly to `expert_builder`.

The root pipeline gains one optional scalar input named `references`. When supplied with non-whitespace content, it is a JSON string encoding an array of reference repositories and whether each should inform final validation. `PrepareReferences` normalizes an omitted input, pipeline-context null, or a zero-length or whitespace-only string to the JSON array `[]` before parsing, preserving the current single-repository behavior.

References are advisory read-only by contract, reinforced by deterministic integrity checks at defined gates. The first version does not use physical read-only mounts because that would require broader Resolve workspace or provisioning changes.

References marked for validation do not carry installation commands. RealityCheck cites the exact documentation file and section it relies on, then follows the applicable recommended or default public installation path. Exact-checkout installation is intentionally excluded because no demonstrated scenario requires its transport and installation complexity.

References are trusted caller- or operator-supplied inputs in the same trust class as the submitted specification and target repository. The pipeline does not make untrusted repository documentation safe to follow; supplying only trusted references is a precondition, not a new sandboxing responsibility.

This behavior remains inside `expert_builder`. A reusable brick should be extracted only after a second pipeline demonstrates the same need.

This document is an implementation brief for direct Attractor pipeline authors changing
`expert_builder`. It is not input to `expert_builder` and does not ask the pipeline to
modify itself.

## Architecture

`expert_builder` remains a single-target pipeline:

```text
Submitted spec + optional references JSON
  -> Admit
  -> PrepareReferences
  -> Plan -> VerifyReferenceIntegrity
  -> Implement -> VerifyReferenceIntegrity
  -> Local Validation -> VerifyReferenceIntegrity
  -> RealityCheck -> VerifyReferenceIntegrity
  -> RouteRealityCheckVerdict
       reference_prerequisite_failed -> Terminal Failure
       target behavior failure -> existing RC repair -> Implement
       pass -> Deliver -> VerifyReferenceIntegrity -> Exit success
```

The current working directory is:

- the only authorized write and delivery target;
- the only source of implementation tasks;
- the only repository copied into the DTU as the software under test; and
- the only repository committed or delivered.

Reference repositories are advisory read-only and already present in the worker environment. `expert_builder` validates and records them; it does not discover, clone, provision, copy, commit, or deliver them.

## Input Contract

`references` is an optional input whose non-whitespace value must be a JSON string encoding an array:

```json
[
  {
    "id": "attractor",
    "path": "/project/workspace/amplifier-bundle-attractor",
    "use_in_validation": true
  },
  {
    "id": "design-system",
    "path": "/project/workspace/design-system",
    "use_in_validation": false
  }
]
```

Each entry contains:

| Field | Requirement | Meaning |
|---|---|---|
| `id` | Required non-empty string | Unique stable identifier used in normalized context and diagnostics. |
| `path` | Required non-empty string | Existing path within a non-bare Git worktree available to the worker. |
| `use_in_validation` | Optional boolean; defaults to `false` | Whether RealityCheck should treat the reference as a documented runtime or tool dependency. |

Unknown entry keys are rejected so misspellings and unsupported options fail loudly.

Before JSON parsing, `PrepareReferences` normalizes an omitted input, null at the pipeline context level, or a zero-length or whitespace-only string to the literal JSON array `[]`. Any supplied non-whitespace content must parse as a JSON array under this schema. The JSON text `null` and every other non-array JSON type fail validation.

`PrepareReferences` enforces these invariants before Plan:

- the input is valid JSON with the expected array and entry shapes;
- required strings are non-empty, `use_in_validation` is boolean when present, and unknown keys are rejected;
- every ID is unique;
- every path exists within a non-bare Git worktree;
- each path is resolved to the canonical physical root of that worktree, and normalized context uses that canonical path;
- no canonical reference root overlaps the canonical target root by equality or containment in either direction; and
- no canonical reference roots overlap one another by equality or containment in either direction.

After normalization, `[]` adds no reference-specific work to the existing flow.

## Components and Data Flow

### PrepareReferences

After Admit succeeds, a deterministic `PrepareReferences` step applies the documented empty-input normalization, then parses and validates the resulting JSON array. For each canonical reference worktree it captures a deterministic baseline of Git-visible repository state covering:

- HEAD and ref state;
- the index;
- tracked content and untracked non-ignored paths and content;
- file modes and symlink targets as represented by Git; and
- submodule status where Git reports it.

A reference may be dirty when the run begins. Its initial state becomes the baseline; the requirement is that it match that baseline at each integrity gate.

Intentionally ignored files are outside this guarantee. The baseline is not a complete filesystem snapshot and does not detect transient writes reverted before a gate.

The step writes two artifacts under the target repository:

- `.ai/references.json`: canonical paths, validation flags, and baseline digests and metadata;
- `.ai/reference_context.md`: concise agent-facing repository context and the single-target rule.

Baseline artifacts store digests and metadata only, never reference file contents. Both files are transient, run-local artifacts containing machine-specific paths or fingerprints. Deliver excludes them from staging, commits, and package output.

Every tool-capable stage receives the normalized reference context and the same advisory read-only and single-target instructions. This applies to Plan, Implement, Local Validation, RealityCheck, and Deliver rather than relying on instructions only during implementation.

### Plan

Plan receives the clarified specification and `.ai/reference_context.md`. It may inspect references for APIs, conventions, examples, and expected behavior.

Every planned implementation task must target the primary working directory. A reference may inform a task but cannot become an authorized write or delivery target.

### Implement

Implement receives the same reference context. Agents may inspect reference files as needed, but instructions identify the primary working directory as the only authorized write and delivery target and treat references as advisory read-only.

The advisory restriction is backed by the deterministic integrity gates rather than trusted as prompt-only policy.

### Reference Integrity Gates

A deterministic gate compares every reference against its recorded baseline using the same Git-visible dimensions captured during preparation. Plan, Implement, Local Validation, RealityCheck, and Deliver are each followed by a gate; in particular, Local Validation is checked before RealityCheck consumes documentation, the RealityCheck verdict is checked before the parent routes it, and a final post-Deliver check must pass before success/Exit. Any difference is a terminal failure that identifies the affected reference and reports the observed differences.

The guarantee is point-in-time: references match their baselines at each gate, and no successful run can bypass the final check. It does not cover intentionally ignored files or claim to detect transient writes reverted before comparison.

### Local Validation

Local validation remains focused on the target repository. References remain available for inspection, but a context-only reference does not create dependency setup work. Its integrity gate must pass before RealityCheck reads reference documentation.

### RealityCheck

`use_in_validation` primarily affects RealityCheck. The parent passes the normalized `.ai/references.json` artifact explicitly into RealityCheck. Before profile or deployment generation, RealityCheck examines each marked reference and derives a run-local `.rc/reference_dependencies.json` containing:

- reference ID;
- required documentation citation: the exact file and section selected, or an explicit missing/ambiguous result with the inspected candidate locations;
- selected recommended or default public installation path, or an explicit unresolved result;
- resolved public dependency identity and version when observable; and
- ordered setup and use steps.

This artifact is internal derived planning, not a caller-supplied command DSL, and is not delivery output. Validation references are processed sequentially in caller array order. RealityCheck fails fast on a prerequisite setup failure; it does not perform dependency solving or define compatibility policy.

The DTU receives only the primary software under test. It installs each required dependency through the selected documented public surface, confirms the installed tool or runtime works, and exercises the completed target with it. The derived dependency results and evidence are part of the RealityCheck verdict.

Reality-check evidence records:

- the required documentation citation result;
- the resolved public dependency identity and version when observable;
- the installation commands actually run, or an explicit record that none ran;
- the validation commands actually run, or an explicit record that none ran; and
- the observed outcomes.

This evidence provides observability, not exact-checkout reproduction. It does not claim that the DTU used the exact reference checkout, and no reference source tree is transported into the DTU.

### Deliver

Deliver stages, commits, and reports only the primary repository. The transient `.ai/references.json` and `.ai/reference_context.md` files are excluded from staging, commits, and package output. Reference context and integrity evidence do not expand the delivery boundary.

## Failure Handling

Reference preparation fails before Plan when non-whitespace input is malformed JSON, parses as JSON `null` or any other non-array type, or contains invalid entry shapes, empty required strings, unknown keys, duplicate IDs, missing paths, bare or non-Git repositories, or canonical worktree overlaps involving the target or another reference.

A reference integrity violation is a terminal pipeline failure, not an implementation repair task. The implementation loop must not repair or reset a reference because references are advisory read-only and outside the authorized write and delivery boundary.

A validation reference supplies no setup commands. RealityCheck must cite and follow the applicable recommended or default public installation path from its documentation. Missing or ambiguous instructions, an unavailable public dependency, or failure to install or verify it produces a structured verdict with `outcome_class: "reference_prerequisite_failed"`.

The `reference_prerequisite_failed` verdict identifies the reference and records the citation and selected path when available, resolved public identity/version when observable, attempted commands and steps, outcomes, and failure cause. The parent surfaces that evidence as a terminal failure and never appends a target repair task.

After all dependency setup succeeds, failures in target behavior follow the existing RealityCheck repair loop: evidence becomes target-repository implementation work, followed by validation again. Invalid reference configuration, `reference_prerequisite_failed`, and reference integrity failures never enter that loop.

## Testing and Acceptance

### Deterministic Contract Tests

Reference preparation tests cover:

- omitted input, pipeline-context null, zero-length strings, whitespace-only strings, and `[]` all normalizing to `[]` and preserving current behavior;
- JSON `null` text and every other non-array JSON type failing validation;
- valid input with one reference;
- valid input with multiple references;
- defaulting an omitted `use_in_validation` to `false`;
- empty required strings, wrong field types, and unknown keys;
- duplicate IDs;
- missing paths;
- non-Git paths;
- bare Git repositories;
- canonical path normalization;
- equal, containing, and contained overlaps with the target or between references;
- Git-visible file modes, symlink targets, and reported submodule status;
- intentionally ignored changes remaining outside the integrity guarantee;
- baseline artifacts containing digests and metadata but no reference file contents;
- initially dirty references remaining unchanged; and
- detection of tracked working-tree, staged/index, untracked path/content, and HEAD/ref or branch changes.

### Pipeline Contract Tests

Pipeline tests prove that:

- Plan receives the submitted specification and normalized reference context;
- Implement receives the same reference context;
- generated tasks target only the primary repository;
- context-only references are excluded from DTU dependency planning;
- the normalized validation-reference artifact is passed explicitly into RealityCheck;
- `.rc/reference_dependencies.json` is derived before profile/deploy generation and is part of the verdict evidence;
- two validation references are processed sequentially in caller array order;
- RealityCheck cites the documentation file and section used and follows its applicable recommended or default public installation path;
- missing or ambiguous installation documentation produces terminal `reference_prerequisite_failed` evidence and no target repair task;
- normal target behavior failures retain the existing RealityCheck repair route;
- reference mutation during Local Validation, RealityCheck, or Deliver is caught by the subsequent integrity gate;
- integrity failures cannot route into implementation repair;
- Deliver stages only the primary repository; and
- `.ai/references.json` and `.ai/reference_context.md` are absent from staged changes, commits, and package output, and `.rc/reference_dependencies.json` remains internal RealityCheck output.

The existing immutable remote-package hydration test remains mandatory. The published `expert_builder` package must continue to hydrate its untouched DOT closure from one immutable entrypoint.

### End-to-End Acceptance

Acceptance uses three scenarios:

1. **No references:** an existing single-repository fixture behaves exactly as it does today.
2. **Context-only reference:** `robotdad/agent-notes` informs a small target change, remains unchanged at every integrity gate, and creates no DTU dependency setup.
3. **Validation dependency:** `robotdad/amplifier-bundle-plugin-compat` is marked `use_in_validation: true` for a tiny demo plugin target. RealityCheck cites and follows its documented public installation path in the DTU, records the public dependency identity/version when observable, confirms the compatibility tool works, and exercises the demo plugin with it.

The third scenario proves the user outcome rather than an internal source-transport mechanism. Exact-checkout installation is neither implemented nor tested.

## Explicit Non-Goals

The first version does not provide:

- exact-checkout dependency installation;
- copying reference repositories into the DTU;
- caller-supplied setup or shell-command schemas;
- repository discovery, cloning, or provisioning;
- physical read-only mounts or filesystem permissions;
- sandboxing untrusted reference documentation;
- more than one authorized write and delivery target;
- cross-repository commits, delivery, or rollback;
- dependency solving or compatibility policy;
- per-repository scheduling or parallelism; or
- a generalized repository-context package or reusable brick.

## Open Questions

None. The scope and first-version decisions are resolved.
