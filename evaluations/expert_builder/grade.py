"""Pure, deterministic reader for expert-builder evaluation evidence."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path

from .json_data import JsonMapping, JsonObject, as_object, parse_object


class Verdict(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    ERROR = "ERROR"


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    expected: object
    observed: object


@dataclass(frozen=True)
class CaseGrade:
    case_id: str
    verdict: Verdict
    defect_class: str
    checks: tuple[Check, ...]
    contradictions: tuple[str, ...]


_SUCCESS_CLASSES = {"target_behavior_pass", "prepared", "unchanged", "delivered"}
_PARTIAL_CLASSES = {"target_behavior_partial", "rc_exhausted", "validation_exhausted"}
_INFRASTRUCTURE_CLASSES = {
    "evaluation_infrastructure_failed",
    "reference_prerequisite_failed",
}

CONTROLLED_CASE_IDS = frozenset(
    {
        "a1_prepare_empty",
        "a2_prepare_invalid_json",
        "a3_prepare_verify_unchanged",
        "a4_verify_tracked_mutation",
        "b1_folder_unchanged",
        "b2_folder_tracked_mutation",
        "c1_rc_launch_fail",
        "c2_rc_invalid_handle",
        "c3_rc_no_port_valid_lifecycle",
        "c4_rc_inner_exit_127",
        "c6_rc_target_validation_inner_failure",
        "c7_rc_target_validation_outer_failure",
        "c8_rc_target_toolchain_inner_failure",
        "c9_rc_target_toolchain_outer_failure",
        "c10_rc_deploy_inner_failure",
        "c11_rc_deploy_outer_failure",
        "c12_rc_reference_use_inner_failure",
        "c13_rc_reference_use_outer_failure",
        "d1_parent_rc_exhausted",
        "e1_parent_rc_pass_only_delivers",
        "e2_parent_rc_exhausted_no_deliver",
        "e3_parent_reference_prerequisite_terminal",
        "e4_parent_infrastructure_terminal",
        "e5_parent_manifest_substitution_detected",
    }
)
ACCEPTANCE_CASE_IDS = CONTROLLED_CASE_IDS | {
    "c5_rc_real_no_port_canary",
    "c14_rc_real_validation_reference_canary",
}
FULL_SUITE_CASE_IDS = {
    "controlled": CONTROLLED_CASE_IDS,
    "acceptance": ACCEPTANCE_CASE_IDS,
}


def _read_json(path: Path) -> JsonObject:
    return parse_object(path.read_text(encoding="utf-8"), source=str(path))


def _expectation_checks(
    observation: JsonMapping, expected: JsonMapping
) -> tuple[Check, ...]:
    subject = as_object(observation.get("subject"))
    semantic = as_object(observation.get("semantic"))
    cleanup = as_object(observation.get("cleanup"))
    if subject is None or semantic is None or cleanup is None:
        return (Check("observation shape", False, "mapping sections", observation),)
    values = {
        "process_exit_code": subject.get("process_exit_code"),
        "engine_status": subject.get("engine_status"),
        "semantic_class": semantic.get("class"),
        "semantic_verdict": semantic.get("verdict"),
        "cleanup.remaining_ids": cleanup.get("remaining_ids"),
    }
    checks: list[Check] = []
    for key, expectation in expected.items():
        if key == "cleanup" and isinstance(expectation, Mapping):
            for cleanup_key, cleanup_expected in expectation.items():
                name = f"cleanup.{cleanup_key}"
                checks.append(
                    Check(
                        name,
                        values.get(name) == cleanup_expected,
                        cleanup_expected,
                        values.get(name),
                    )
                )
        elif key in values:
            checks.append(
                Check(key, values[key] == expectation, expectation, values[key])
            )
    return tuple(checks)


def detect_contradictions(observation: JsonMapping) -> tuple[str, ...]:
    """Return every semantic mismatch independently of scenario expectations."""
    subject = as_object(observation.get("subject"))
    semantic = as_object(observation.get("semantic"))
    cleanup = as_object(observation.get("cleanup"))
    transport = as_object(observation.get("transport"))
    if subject is None or semantic is None or cleanup is None or transport is None:
        return ("observation sections are malformed",)

    process = subject.get("process_exit_code")
    engine = subject.get("engine_status")
    completed = subject.get("completed_nodes")
    context = subject.get("context")
    outcome_class = semantic.get("class")
    verdict = semantic.get("verdict")
    domain = semantic.get("domain")
    contradictions: list[str] = []

    if process == 0 and outcome_class not in _SUCCESS_CLASSES:
        contradictions.append("process exit 0 with non-success semantic class")
    if process != 0 and outcome_class in _SUCCESS_CLASSES:
        contradictions.append("process nonzero with semantic success")
    if engine == "success" and (
        verdict in {"fail", "partial"}
        or outcome_class in _PARTIAL_CLASSES | _INFRASTRUCTURE_CLASSES
    ):
        contradictions.append(f"engine success with {outcome_class}")
    if engine == "partial_success" and outcome_class not in _PARTIAL_CLASSES:
        contradictions.append(
            "engine partial_success without declared partial or exhaustion class"
        )
    if (
        domain == "reality_check"
        and verdict == "pass"
        and outcome_class != "target_behavior_pass"
    ):
        contradictions.append("reality-check pass verdict without target_behavior_pass")
    if (
        domain == "reality_check"
        and verdict != "pass"
        and outcome_class == "target_behavior_pass"
    ):
        contradictions.append("non-pass verdict with target_behavior_pass")
    if (
        isinstance(context, Mapping)
        and context.get("rc_state") == "rc_exhausted"
        and isinstance(completed, list)
        and "Deliver" in completed
    ):
        contradictions.append("rc_exhausted reached Deliver")
    if outcome_class == "target_behavior_failed" and (
        isinstance(context, Mapping)
        and context.get("launch_or_handle_failure", False) is True
    ):
        contradictions.append(
            "launch or handle failure rendered as target_behavior_failed"
        )
    outer = transport.get("outer_exit_code")
    inner = transport.get("exec_envelope_exit_code")
    recorded = transport.get("recorded_exec_exit_code")
    if outer == 0 and type(inner) is int and inner != 0 and recorded == 0:
        contradictions.append("DTU exec inner nonzero recorded as 0")
    created = cleanup.get("created_ids")
    remaining = cleanup.get("remaining_ids")
    if isinstance(created, list) and isinstance(remaining, list):
        leaked = set(created) & set(remaining)
        if leaked:
            contradictions.append(
                f"graph-created DTUs remain after cleanup: {sorted(leaked)}"
            )
    notes = subject.get("failure_notes")
    if (
        isinstance(notes, str)
        and domain == "reality_check"
        and "No matching edge" in notes
    ):
        contradictions.append(
            "reality-check semantic failure ended in No matching edge"
        )
    if (
        domain == "reality_check"
        and isinstance(context, Mapping)
        and context.get("sut_base_url") == "http://localhost:none"
    ):
        contradictions.append(
            "no-port lifecycle produced bogus localhost:none base URL"
        )
    return tuple(contradictions)


def _reference_mutation_checks(
    observation: JsonMapping,
) -> tuple[tuple[Check, ...], bool]:
    """Validate a tracked reference mutation from collector-owned raw evidence."""
    evidence = as_object(observation.get("evidence"))
    subject = as_object(observation.get("subject"))
    if evidence is None or subject is None:
        return (
            (
                Check(
                    "mutation evidence", False, "complete raw evidence object", evidence
                ),
            ),
            True,
        )

    required = (
        "fixture_root",
        "reference_order",
        "reference_fingerprints_before",
        "reference_fingerprints_after",
        "mutation_output",
        "nested_verify_failure_reason",
    )
    missing = [name for name in required if name not in evidence]
    if missing:
        return (
            (
                Check(
                    "mutation evidence",
                    False,
                    "all required fields",
                    {"missing": missing},
                ),
            ),
            True,
        )

    checks: list[Check] = []
    fixture_root_raw = evidence.get("fixture_root")
    reference_order = evidence.get("reference_order")
    before = evidence.get("reference_fingerprints_before")
    after = evidence.get("reference_fingerprints_after")
    mutation_raw = evidence.get("mutation_output")
    verify_reason = evidence.get("nested_verify_failure_reason")
    if not (
        isinstance(fixture_root_raw, str)
        and fixture_root_raw
        and isinstance(reference_order, list)
        and reference_order
        and all(isinstance(item, str) and item for item in reference_order)
        and isinstance(before, Mapping)
        and isinstance(after, Mapping)
        and isinstance(mutation_raw, str)
        and isinstance(verify_reason, str)
    ):
        return (
            (
                Check(
                    "mutation evidence",
                    False,
                    "well-typed required fields",
                    {
                        "fixture_root": fixture_root_raw,
                        "reference_order": reference_order,
                    },
                ),
            ),
            True,
        )

    try:
        mutation = json.loads(mutation_raw)
    except json.JSONDecodeError:
        mutation = None
    checks.append(
        Check(
            "mutation output class",
            isinstance(mutation, Mapping)
            and mutation.get("mutation_class") == "tracked",
            "tracked",
            mutation.get("mutation_class")
            if isinstance(mutation, Mapping)
            else mutation,
        )
    )

    fingerprint_checks, fingerprints_missing = _reference_fingerprint_checks(
        before,
        after,
        reference_order,
        unchanged=False,
    )
    checks.extend(fingerprint_checks)
    if fingerprints_missing:
        return tuple(checks), True

    first_id = reference_order[0]
    first_before = before.get(first_id)
    first_after = after.get(first_id)
    if not isinstance(first_before, Mapping) or not isinstance(first_after, Mapping):
        return (
            (
                *checks,
                Check(
                    "first reference fingerprints",
                    False,
                    f"before/after mappings for {first_id}",
                    {"before": first_before, "after": first_after},
                ),
            ),
            True,
        )

    first_root_raw = first_before.get("path")
    mutation_path_raw = (
        mutation.get("mutation_path") if isinstance(mutation, Mapping) else None
    )
    path_valid = False
    expected_mutation_path: object = None
    if isinstance(first_root_raw, str) and isinstance(mutation_path_raw, str):
        fixture_root = Path(fixture_root_raw).resolve()
        first_root = Path(first_root_raw).resolve()
        mutation_path = Path(mutation_path_raw).resolve()
        expected_path = first_root / "tracked.txt"
        expected_mutation_path = str(expected_path)
        path_valid = (
            first_root.is_relative_to(fixture_root)
            and mutation_path.is_relative_to(fixture_root)
            and mutation_path == expected_path
        )
    checks.append(
        Check(
            "mutation path",
            path_valid,
            expected_mutation_path,
            mutation_path_raw,
        )
    )

    before_tracked = first_before.get("tracked_worktree_diff_sha256")
    after_tracked = first_after.get("tracked_worktree_diff_sha256")
    checks.append(
        Check(
            "tracked worktree fingerprint changed",
            isinstance(before_tracked, str)
            and isinstance(after_tracked, str)
            and before_tracked != after_tracked,
            "different SHA-256 values",
            {"before": before_tracked, "after": after_tracked},
        )
    )
    for dimension in _FINGERPRINT_FIELDS - {"tracked_worktree_diff_sha256"}:
        checks.append(
            Check(
                f"first reference {dimension} unchanged",
                first_before.get(dimension) == first_after.get(dimension),
                first_before.get(dimension),
                first_after.get(dimension),
            )
        )

    for reference_id in reference_order[1:]:
        checks.append(
            Check(
                f"reference {reference_id} unchanged",
                before.get(reference_id) == after.get(reference_id),
                before.get(reference_id),
                after.get(reference_id),
            )
        )

    checks.append(
        Check(
            "nested VerifyReferences failure",
            bool(verify_reason) and "tracked_worktree" in verify_reason,
            "exact VerifyReferences failure reason containing tracked_worktree",
            verify_reason,
        )
    )
    checks.append(
        Check(
            "mutation parent process nonzero",
            isinstance(subject.get("process_exit_code"), int)
            and subject.get("process_exit_code") != 0,
            "nonzero",
            subject.get("process_exit_code"),
        )
    )
    checks.append(
        Check(
            "mutation parent engine fail",
            subject.get("engine_status") == "fail",
            "fail",
            subject.get("engine_status"),
        )
    )
    return tuple(checks), False


def _real_canary_checks(
    observation: JsonMapping,
) -> tuple[tuple[Check, ...], bool]:
    evidence = as_object(observation.get("evidence"))
    subject = as_object(observation.get("subject"))
    cleanup = as_object(observation.get("cleanup"))
    if evidence is None or subject is None or cleanup is None:
        return (
            (Check("real canary evidence", False, "complete evidence", evidence),),
            True,
        )
    required = (
        "profile_generated",
        "profile_launch_succeeded",
        "target_pushed",
        "target_deployed",
        "sut_base_url",
        "dtu_list_before",
        "dtu_list_after",
        "nested_envelopes",
        "source_hashes_before",
        "source_hashes_after",
        "cleanup",
        "parent_checkpoint",
    )
    missing = [name for name in required if name not in evidence]
    if missing:
        return (
            (
                Check(
                    "real canary evidence",
                    False,
                    "all required fields",
                    {"missing": missing},
                ),
            ),
            True,
        )
    completed = subject.get("completed_nodes")
    required_nodes = {
        "NormalizeDUTPlan",
        "LaunchDTUStrict",
        "ParseHandleStrict",
        "PushSUTStrict",
        "DeployStrict",
        "Validate",
        "TerminalLatch",
    }
    checks = [
        Check(
            name.replace("_", " "), evidence.get(name) is True, True, evidence.get(name)
        )
        for name in (
            "profile_generated",
            "profile_launch_succeeded",
            "target_pushed",
            "target_deployed",
        )
    ]
    checks.extend(
        (
            Check(
                "no HTTP base URL",
                evidence.get("sut_base_url") in ("", None),
                "",
                evidence.get("sut_base_url"),
            ),
            Check(
                "real canary completed nodes",
                isinstance(completed, list) and required_nodes <= set(completed),
                sorted(required_nodes),
                completed,
            ),
            Check(
                "real canary source hashes",
                evidence.get("source_hashes_before")
                == evidence.get("source_hashes_after"),
                evidence.get("source_hashes_before"),
                evidence.get("source_hashes_after"),
            ),
            Check(
                "real canary DTU absent",
                cleanup.get("remaining_ids") == [],
                [],
                cleanup.get("remaining_ids"),
            ),
        )
    )
    return tuple(checks), False


def _controlled_inner_exit_127_checks(
    observation: JsonMapping,
) -> tuple[tuple[Check, ...], bool]:
    """Require c4 to exercise strict setup parsing rather than pre-failing its plan."""
    evidence = as_object(observation.get("evidence"))
    subject = as_object(observation.get("subject"))
    transport = as_object(observation.get("transport"))
    if evidence is None or subject is None or transport is None:
        return (
            (
                Check(
                    "c4 strict setup evidence",
                    False,
                    "evidence, subject, and transport mappings",
                    {"evidence": evidence, "subject": subject, "transport": transport},
                ),
            ),
            True,
        )

    required = (
        "initial_dependency_plan",
        "reference_dependency_plan",
        "setup_result",
        "dtu_calls",
    )
    missing = [name for name in required if name not in evidence]
    if missing:
        return (
            (
                Check(
                    "c4 strict setup evidence",
                    False,
                    "all required c4 evidence",
                    {"missing": missing},
                ),
            ),
            True,
        )

    initial_plan = evidence.get("initial_dependency_plan")
    plan = evidence.get("reference_dependency_plan")
    setup_result = evidence.get("setup_result")
    completed = subject.get("completed_nodes")
    dependencies = (
        initial_plan.get("dependencies") if isinstance(initial_plan, Mapping) else None
    )
    setup_results = plan.get("setup_results") if isinstance(plan, Mapping) else None
    checks = (
        Check(
            "c4 strict setup node visited",
            isinstance(completed, list)
            and "InstallReferenceDependenciesStrict" in completed,
            "InstallReferenceDependenciesStrict",
            completed,
        ),
        Check(
            "c4 starts from ready dependency plan",
            isinstance(initial_plan, Mapping)
            and initial_plan.get("state") == "ready"
            and isinstance(dependencies, list)
            and len(dependencies) == 1
            and isinstance(dependencies[0], Mapping)
            and dependencies[0].get("setup_steps") == ["validation-reference --install"]
            and dependencies[0].get("use_steps") == ["validation-reference --use"],
            "one ready dependency with setup and use steps",
            initial_plan,
        ),
        Check(
            "c4 recorded one setup result",
            isinstance(setup_results, list)
            and len(setup_results) == 1
            and isinstance(setup_result, Mapping),
            "one setup result",
            setup_results,
        ),
        Check(
            "c4 setup outer exit code",
            isinstance(setup_result, Mapping)
            and setup_result.get("outer_exit_code") == 0,
            0,
            setup_result.get("outer_exit_code")
            if isinstance(setup_result, Mapping)
            else setup_result,
        ),
        Check(
            "c4 setup inner exit code",
            isinstance(setup_result, Mapping) and setup_result.get("exit_code") == 127,
            127,
            setup_result.get("exit_code")
            if isinstance(setup_result, Mapping)
            else setup_result,
        ),
        Check(
            "c4 transport preserves outer success and inner failure",
            transport.get("outer_exit_code") == 0
            and transport.get("exec_envelope_exit_code") == 127
            and transport.get("recorded_exec_exit_code") == 127,
            {
                "outer_exit_code": 0,
                "exec_envelope_exit_code": 127,
                "recorded_exec_exit_code": 127,
            },
            dict(transport),
        ),
    )
    return checks, False


_FINGERPRINT_FIELDS = {
    "path",
    "head",
    "symbolic_ref",
    "index_sha256",
    "tracked_worktree_diff_sha256",
    "untracked",
    "submodule_state_sha256",
}


def _typed_fingerprint(value: object) -> bool:
    if not isinstance(value, Mapping) or set(value) != _FINGERPRINT_FIELDS:
        return False
    hashes = (
        "index_sha256",
        "tracked_worktree_diff_sha256",
        "submodule_state_sha256",
    )
    untracked = value.get("untracked")
    return (
        isinstance(value.get("path"), str)
        and Path(str(value["path"])).is_absolute()
        and isinstance(value.get("head"), str)
        and bool(value["head"])
        and isinstance(value.get("symbolic_ref"), str)
        and all(
            isinstance(value.get(name), str) and len(str(value[name])) == 64
            for name in hashes
        )
        and isinstance(untracked, Mapping)
        and all(
            isinstance(name, str) and isinstance(digest, str) and len(digest) == 64
            for name, digest in untracked.items()
        )
    )


def _reference_fingerprint_checks(
    before: object,
    after: object,
    reference_order: object,
    *,
    unchanged: bool,
) -> tuple[tuple[Check, ...], bool]:
    """Require complete, typed, order-bound reference snapshots."""
    if not (
        isinstance(before, Mapping)
        and isinstance(after, Mapping)
        and isinstance(reference_order, list)
        and all(isinstance(item, str) and item for item in reference_order)
        and len(reference_order) == len(set(reference_order))
    ):
        return (
            (
                Check(
                    "reference fingerprint evidence",
                    False,
                    "typed before/after mappings and unique reference order",
                    {
                        "before": before,
                        "after": after,
                        "reference_order": reference_order,
                    },
                ),
            ),
            True,
        )
    expected_ids = set(reference_order)
    checks: list[Check] = [
        Check(
            "reference fingerprint membership",
            set(before) == expected_ids == set(after),
            list(reference_order),
            {"before": sorted(before), "after": sorted(after)},
        )
    ]
    for reference_id in reference_order:
        before_value = before.get(reference_id)
        after_value = after.get(reference_id)
        checks.extend(
            (
                Check(
                    f"reference {reference_id} typed before fingerprint",
                    _typed_fingerprint(before_value),
                    "complete typed fingerprint",
                    before_value,
                ),
                Check(
                    f"reference {reference_id} typed after fingerprint",
                    _typed_fingerprint(after_value),
                    "complete typed fingerprint",
                    after_value,
                ),
            )
        )
        if unchanged:
            checks.append(
                Check(
                    f"reference {reference_id} unchanged fingerprint",
                    before_value == after_value,
                    before_value,
                    after_value,
                )
            )
    return tuple(checks), False


def _reference_leaf_checks(
    observation: JsonMapping, case_id: str
) -> tuple[tuple[Check, ...], bool]:
    evidence = observation.get("evidence")
    if not isinstance(evidence, Mapping):
        return ((Check("reference raw evidence", False, "mapping", evidence),), True)
    required = {
        "manifest",
        "context_markdown",
        "prepare_failure_reason",
        "copy_up",
        "reference_order",
        "reference_fingerprints_before",
        "reference_fingerprints_after",
    }
    missing = sorted(required - set(evidence))
    if missing:
        return (
            (
                Check(
                    "reference raw evidence", False, "all fields", {"missing": missing}
                ),
            ),
            True,
        )
    manifest = evidence.get("manifest")
    copy_up = evidence.get("copy_up")
    before = evidence.get("reference_fingerprints_before")
    after = evidence.get("reference_fingerprints_after")
    fingerprint_checks, fingerprints_missing = _reference_fingerprint_checks(
        before,
        after,
        evidence.get("reference_order"),
        unchanged=True,
    )
    checks: list[Check] = list(fingerprint_checks)
    if fingerprints_missing:
        return tuple(checks), True
    if case_id == "a2_prepare_invalid_json":
        reason = evidence.get("prepare_failure_reason")
        checks.append(
            Check(
                "PrepareReferences invalid JSON diagnostic",
                isinstance(reason, str)
                and "PrepareReferences" not in reason
                and "references must be valid JSON array text" in reason,
                "invalid JSON-array domain marker",
                reason,
            )
        )
        return tuple(checks), False
    if not isinstance(manifest, Mapping) or not isinstance(copy_up, Mapping):
        return (
            (*checks, Check("reference manifest/copy-up", False, "mappings", evidence)),
            True,
        )
    references = manifest.get("references")
    expected_references = (
        []
        if case_id == "a1_prepare_empty"
        else [
            ("reference-a", False),
            ("reference-b", True),
        ]
    )
    observed = (
        [
            (item.get("id"), item.get("use_in_validation"))
            for item in references
            if isinstance(item, Mapping)
        ]
        if isinstance(references, list)
        else None
    )
    checks.extend(
        (
            Check(
                "reference manifest order/flags",
                observed == expected_references,
                expected_references,
                observed,
            ),
            Check(
                "reference context markdown",
                isinstance(evidence.get("context_markdown"), str)
                and bool(str(evidence.get("context_markdown")).strip()),
                "nonempty",
                evidence.get("context_markdown"),
            ),
            Check(
                "reference state copy-up",
                copy_up.get("reference_state") == "prepared",
                "prepared",
                copy_up.get("reference_state"),
            ),
        )
    )
    if case_id in {"a3_prepare_verify_unchanged", "b1_folder_unchanged"}:
        checks.append(
            Check(
                "reference integrity copy-up",
                copy_up.get("reference_integrity_state") == "unchanged",
                "unchanged",
                copy_up.get("reference_integrity_state"),
            )
        )
    return tuple(checks), False


def _target_validation_checks(
    observation: JsonMapping, *, real_canary: bool
) -> tuple[tuple[Check, ...], bool]:
    evidence = as_object(observation.get("evidence"))
    subject = as_object(observation.get("subject"))
    if evidence is None or subject is None:
        return (
            (Check("target validation evidence", False, "mappings", evidence),),
            True,
        )
    target = as_object(evidence.get("target_validation"))
    plan = as_object(evidence.get("dut_plan"))
    launch = as_object(evidence.get("launch"))
    cleanup = as_object(evidence.get("cleanup"))
    calls = evidence.get("dtu_calls")
    seal = evidence.get("target_validation_digest")
    independent_hash = evidence.get("target_validation_sha256")
    if target is None or plan is None or launch is None or cleanup is None:
        return (
            (
                Check(
                    "target validation evidence", False, "complete mappings", evidence
                ),
            ),
            True,
        )
    exact = {
        "schema_version",
        "run_token",
        "container",
        "command",
        "outer_exit_code",
        "exit_code",
        "stdout",
        "stderr",
    }
    if set(target) != exact:
        return (
            (
                Check(
                    "target validation exact schema",
                    False,
                    sorted(exact),
                    sorted(target),
                ),
            ),
            True,
        )
    deploy_result = evidence.get("deploy_result")
    if not isinstance(deploy_result, Mapping):
        return (
            (Check("deploy result evidence", False, "mapping", deploy_result),),
            True,
        )
    expected_command = (
        "./demo-cli --self-test" if real_canary else "./controlled-target --self-test"
    )
    expected_stdout = (
        "demo-cli:self-test:ok\n" if real_canary else "controlled-validation:ok\n"
    )
    returned = launch.get("id") or launch.get("name")
    completed = subject.get("completed_nodes")
    validation_call = None
    deploy_call = None
    if isinstance(calls, list):
        validation_call = next(
            (
                item
                for item in calls
                if isinstance(item, Mapping)
                and item.get("stage") == "target_validation"
            ),
            None,
        )
        deploy_call = next(
            (
                item
                for item in calls
                if isinstance(item, Mapping) and item.get("stage") == "deploy"
            ),
            None,
        )
    order_ok = (
        isinstance(deploy_call, Mapping)
        and isinstance(validation_call, Mapping)
        and int(deploy_call.get("sequence", -1))
        < int(validation_call.get("sequence", -1))
    )
    if not isinstance(calls, list) and isinstance(completed, list):
        order_ok = (
            "PersistDeployOK" in completed
            and "ExecuteTargetValidationStrict" in completed
            and "CleanupIdentityStrict" in completed
            and completed.index("PersistDeployOK")
            < completed.index("ExecuteTargetValidationStrict")
            < completed.index("CleanupIdentityStrict")
        )
    checks = (
        Check(
            "target validation exact schema",
            set(target) == exact,
            sorted(exact),
            sorted(target),
        ),
        Check(
            "target validation command",
            target.get("command") == expected_command,
            expected_command,
            target.get("command"),
        ),
        Check(
            "target validation returned identity",
            bool(returned) and target.get("container") == returned,
            returned,
            target.get("container"),
        ),
        Check(
            "target validation exits",
            target.get("outer_exit_code") == 0 and target.get("exit_code") == 0,
            [0, 0],
            [target.get("outer_exit_code"), target.get("exit_code")],
        ),
        Check(
            "deploy success evidence",
            deploy_result.get("outer_exit_code") == 0
            and deploy_result.get("inner_exit_code") == 0,
            [0, 0],
            deploy_result,
        ),
        Check(
            "target validation exact stdout",
            target.get("stdout") == expected_stdout and target.get("stderr") == "",
            expected_stdout,
            target.get("stdout"),
        ),
        Check(
            "target validation integrity seal",
            isinstance(seal, str) and len(seal) == 64 and seal == independent_hash,
            "checkpoint/context digest equals independently hashed artifact",
            {"checkpoint_digest": seal, "artifact_sha256": independent_hash},
        ),
        Check(
            "target validation run token",
            isinstance(target.get("run_token"), str)
            and target.get("run_token") == cleanup.get("run_token"),
            cleanup.get("run_token"),
            target.get("run_token"),
        ),
        Check(
            "target validation path",
            isinstance(completed, list)
            and {
                "PersistDeployOK",
                "ExecuteTargetValidationStrict",
                "VerifyTargetValidationEvidence",
                "CleanupIdentityStrict",
            }
            <= set(completed),
            "deploy, execute, verify, cleanup",
            completed,
        ),
        Check(
            "target validation invocation order",
            order_ok,
            "deploy before validation",
            {"deploy": deploy_call, "validation": validation_call},
        ),
    )
    return checks, False


def _instance_ids_from_evidence(value: object) -> set[str] | None:
    if not isinstance(value, list):
        return None
    result: set[str] = set()
    for item in value:
        if not isinstance(item, Mapping):
            return None
        identifier = item.get("id") or item.get("name")
        if not isinstance(identifier, str) or not identifier:
            return None
        result.add(identifier)
    return result


def _cleanup_identity_checks(
    observation: JsonMapping, fixture: str
) -> tuple[tuple[Check, ...], bool]:
    evidence = observation.get("evidence")
    cleanup_top = observation.get("cleanup")
    if not isinstance(evidence, Mapping) or not isinstance(cleanup_top, Mapping):
        return ((Check("cleanup evidence", False, "mappings", evidence),), True)
    cleanup = evidence.get("cleanup")
    launch = evidence.get("launch")
    before = _instance_ids_from_evidence(evidence.get("dtu_list_before"))
    after = _instance_ids_from_evidence(evidence.get("dtu_list_after"))
    if (
        not isinstance(cleanup, Mapping)
        or not isinstance(launch, Mapping)
        or before is None
        or after is None
    ):
        return (
            (Check("cleanup evidence", False, "complete typed evidence", evidence),),
            True,
        )
    requested = evidence.get("requested_name")
    returned = launch.get("id") or launch.get("name") or ""
    if fixture == "launch_fail":
        expected_destroy, expected_source, expected_attempted = "", "none", False
    elif fixture == "invalid_handle":
        expected_destroy, expected_source, expected_attempted = (
            requested,
            "requested_name_fallback",
            True,
        )
    else:
        expected_destroy, expected_source, expected_attempted = (
            returned,
            "returned_identifier",
            True,
        )
    response_ok = not expected_attempted
    if expected_attempted and isinstance(cleanup.get("stdout"), str):
        try:
            response = json.loads(str(cleanup["stdout"]))
        except json.JSONDecodeError:
            response = None
        response_ok = isinstance(response, Mapping) and (
            response.get("destroyed") is True or response.get("ok") is True
        )
    exact = {
        "schema_version",
        "run_token",
        "requested_name",
        "returned_identifier",
        "destroy_identifier",
        "identity_source",
        "attempted",
        "outer_exit_code",
        "stdout",
        "stderr",
    }
    if set(cleanup) != exact:
        return (
            (Check("cleanup exact schema", False, sorted(exact), sorted(cleanup)),),
            True,
        )
    leaked = after - before
    checks = (
        Check(
            "cleanup exact schema",
            set(cleanup) == exact,
            sorted(exact),
            sorted(cleanup),
        ),
        Check(
            "cleanup requested name binding",
            isinstance(requested, str)
            and bool(requested)
            and cleanup.get("requested_name") == requested,
            requested,
            cleanup.get("requested_name"),
        ),
        Check(
            "cleanup returned identity binding",
            cleanup.get("returned_identifier") == returned,
            returned,
            cleanup.get("returned_identifier"),
        ),
        Check(
            "cleanup destroy identity",
            cleanup.get("destroy_identifier") == expected_destroy
            and cleanup.get("identity_source") == expected_source,
            [expected_destroy, expected_source],
            [cleanup.get("destroy_identifier"), cleanup.get("identity_source")],
        ),
        Check(
            "cleanup attempt",
            cleanup.get("attempted") is expected_attempted,
            expected_attempted,
            cleanup.get("attempted"),
        ),
        Check(
            "cleanup outer result",
            (cleanup.get("outer_exit_code") == 0)
            if expected_attempted
            else cleanup.get("outer_exit_code") is None,
            0 if expected_attempted else None,
            cleanup.get("outer_exit_code"),
        ),
        Check(
            "cleanup response",
            response_ok,
            "successful destroy response",
            cleanup.get("stdout"),
        ),
        Check("cleanup no new instances", leaked == set(), [], sorted(leaked)),
        Check(
            "cleanup top-level identity",
            cleanup_top.get("remaining_ids") == []
            and cleanup_top.get("destroy_attempted")
            == ([expected_destroy] if expected_attempted else []),
            [expected_destroy] if expected_attempted else [],
            cleanup_top,
        ),
    )
    return checks, False


_STAGE_PATHS = {
    "target_validation": ("ExecuteTargetValidationStrict", "Validate"),
    "target_toolchain": (
        "InstallTargetToolchainStrict",
        "InstallReferenceDependenciesStrict",
    ),
    "deploy": ("DeployStrict", "ExecuteReferenceUseSteps"),
    "reference_use": ("ExecuteReferenceUseSteps", "ExecuteTargetValidationStrict"),
}


def _controlled_path_checks(
    observation: JsonMapping, scenario: JsonMapping
) -> tuple[tuple[Check, ...], bool]:
    evidence = observation.get("evidence")
    subject = observation.get("subject")
    if not isinstance(evidence, Mapping) or not isinstance(subject, Mapping):
        return ((Check("controlled path evidence", False, "mappings", evidence),), True)
    completed = subject.get("completed_nodes")
    calls = evidence.get("dtu_calls")
    if not isinstance(completed, list) or not isinstance(calls, list):
        return ((Check("controlled path evidence", False, "lists", evidence),), True)
    case_id = str(scenario.get("id"))
    required = {
        "NormalizeDUTPlan",
        "NormalizeDUTPlanStrict",
        "CleanupIdentityStrict",
        "TerminalLatch",
    }
    forbidden: set[str] = set()
    if case_id == "c1_rc_launch_fail":
        required |= {"LaunchDTUStrict", "RenderInfrastructureFailure"}
        forbidden |= {"ParseHandleStrict"}
    elif case_id == "c2_rc_invalid_handle":
        required |= {
            "LaunchDTUStrict",
            "ParseHandleStrict",
            "RouteHandleStrict",
            "RenderInfrastructureFailure",
        }
        forbidden |= {"PushSUTStrict"}
    elif case_id == "c3_rc_no_port_valid_lifecycle":
        required |= {
            "PushSUTStrict",
            "InstallTargetToolchainStrict",
            "InstallReferenceDependenciesStrict",
            "DeployStrict",
            "PersistDeployOK",
            "ExecuteReferenceUseSteps",
            "ExecuteTargetValidationStrict",
            "Validate",
            "VerifyReferenceUseEvidence",
            "VerifyTargetValidationEvidence",
            "RenderPass",
        }
    stage = str(scenario.get("failure_stage", ""))
    mode = str(scenario.get("failure_mode", ""))
    if stage in _STAGE_PATHS:
        stop, boundary = _STAGE_PATHS[stage]
        required.add(stop)
        forbidden.add(boundary)
    stage_calls = (
        [
            item
            for item in calls
            if isinstance(item, Mapping) and item.get("stage") == stage
        ]
        if stage
        else []
    )
    stage_ok = True
    if stage:
        stage_ok = len(stage_calls) == 1
        if stage_ok:
            call = stage_calls[0]
            stage_ok = (
                mode == "outer" and call.get("outer_exit_code") not in (None, 0)
            ) or (
                mode == "inner"
                and call.get("outer_exit_code") == 0
                and isinstance(call.get("inner_exit_code"), int)
                and call.get("inner_exit_code") != 0
            )
    sequence_ok = all(
        isinstance(item, Mapping) and item.get("sequence") == index
        for index, item in enumerate(calls, 1)
    )
    checks = (
        Check(
            "controlled required nodes",
            required <= set(completed),
            sorted(required),
            completed,
        ),
        Check(
            "controlled stop boundary",
            not (forbidden & set(completed)),
            sorted(forbidden),
            completed,
        ),
        Check(
            "controlled stage invocation",
            stage_ok,
            {"stage": stage, "mode": mode},
            stage_calls,
        ),
        Check(
            "controlled invocation sequence",
            sequence_ok,
            list(range(1, len(calls) + 1)),
            [
                item.get("sequence") if isinstance(item, Mapping) else None
                for item in calls
            ],
        ),
        Check(
            "controlled failure stage artifact",
            (not stage or evidence.get("failure_stage") in {"", stage}),
            stage,
            evidence.get("failure_stage"),
        ),
    )
    return checks, False


def _parent_exhaustion_checks(
    observation: JsonMapping,
) -> tuple[tuple[Check, ...], bool]:
    subject = observation.get("subject")
    if not isinstance(subject, Mapping) or not isinstance(
        subject.get("completed_nodes"), list
    ):
        return (
            (Check("D1 path evidence", False, "completed node list", subject),),
            True,
        )
    completed = subject["completed_nodes"]
    return (
        (
            Check(
                "D1 exact path",
                completed == ["SyntheticStart", "CheckRC", "RCExhausted"],
                ["SyntheticStart", "CheckRC", "RCExhausted"],
                completed,
            ),
            Check("D1 no delivery", "Deliver" not in completed, "absent", completed),
            Check(
                "D1 rc state",
                isinstance(subject.get("context"), Mapping)
                and subject["context"].get("rc_state") == "rc_exhausted",
                "rc_exhausted",
                subject.get("context"),
            ),
        ),
        False,
    )


def _validation_reference_canary_checks(
    observation: JsonMapping,
) -> tuple[tuple[Check, ...], bool]:
    """Require C14 to prove documented setup and use, not just a final pass verdict."""
    evidence = as_object(observation.get("evidence"))
    subject = as_object(observation.get("subject"))
    if evidence is None or subject is None:
        return ((Check("C14 evidence", False, "mappings", evidence),), True)
    plan = as_object(evidence.get("reference_dependency_plan"))
    target = as_object(evidence.get("target_validation"))
    if plan is None or target is None:
        return ((Check("C14 evidence", False, "plan and validation", evidence),), True)
    dependencies = plan.get("dependencies")
    setup = plan.get("setup_results")
    use = plan.get("use_results")
    expected_digest = evidence.get("reference_manifest_digest")
    fingerprints = _reference_fingerprint_checks(
        evidence.get("reference_fingerprints_before"),
        evidence.get("reference_fingerprints_after"),
        ["validation-reference"],
        unchanged=True,
    )[0]
    dependency = (
        dependencies[0]
        if isinstance(dependencies, list) and len(dependencies) == 1
        else {}
    )
    expected_setup = [
        ("validation-reference", "apt-get update"),
        ("validation-reference", "apt-get install -y shellcheck"),
    ]
    expected_use = [
        ("validation-reference", "shellcheck /sut/target/demo-cli"),
    ]
    planned_version = (
        dependency.get("version") if isinstance(dependency, Mapping) else None
    )
    version_valid = planned_version is None or (
        isinstance(planned_version, str) and bool(planned_version.strip())
    )
    citation = dependency.get("citation") if isinstance(dependency, Mapping) else None
    public_install = (
        dependency.get("public_install") if isinstance(dependency, Mapping) else None
    )
    fingerprints_before = evidence.get("reference_fingerprints_before")
    reference_fingerprint = (
        fingerprints_before.get("validation-reference")
        if isinstance(fingerprints_before, Mapping)
        else None
    )
    reference_root = (
        reference_fingerprint.get("path")
        if isinstance(reference_fingerprint, Mapping)
        else None
    )
    citation_file = citation.get("file") if isinstance(citation, Mapping) else None
    absolute_readme = (
        str(Path(reference_root) / "README.md")
        if isinstance(reference_root, str) and Path(reference_root).is_absolute()
        else None
    )
    citation_file_valid = isinstance(citation_file, str) and (
        citation_file == "README.md" or citation_file == absolute_readme
    )
    citation_heading_valid = (
        isinstance(citation, Mapping)
        and isinstance(citation.get("section"), str)
        and citation["section"].lstrip("#").strip() == "Public validation dependency"
    )
    public_install_valid = public_install in (
        {"selected_path": "apt"},
        {"selected_path": "apt-get"},
    )

    def result_sequence(value: object) -> list[tuple[object, object]] | None:
        if not isinstance(value, list):
            return None
        return [
            (item.get("id"), item.get("command"))
            if isinstance(item, Mapping)
            else (None, None)
            for item in value
        ]

    def results_bound(
        value: object,
        expected: list[tuple[str, str]],
        *,
        require_run_token: bool,
    ) -> bool:
        if not isinstance(value, list) or len(value) != len(expected):
            return False
        token = plan.get("run_token")
        for result, (dependency_id, command) in zip(value, expected, strict=True):
            if not (
                isinstance(result, Mapping)
                and result.get("id") == dependency_id
                and result.get("command") == command
                and result.get("outer_exit_code") == 0
                and result.get("exit_code") == 0
            ):
                return False
            if require_run_token and result.get("run_token") != token:
                return False
        return True

    completed = subject.get("completed_nodes")
    checks = (
        Check(
            "C14 exact dependency",
            isinstance(dependency, Mapping)
            and dependency.get("id") == "validation-reference"
            and citation_file_valid
            and citation_heading_valid
            and public_install_valid
            and dependency.get("identity") == "shellcheck"
            and version_valid
            and dependency.get("setup_steps")
            == ["apt-get update", "apt-get install -y shellcheck"]
            and dependency.get("use_steps") == ["shellcheck /sut/target/demo-cli"],
            "one ordered, cited public shellcheck dependency",
            dependencies,
        ),
        Check(
            "C14 citation file binding",
            citation_file_valid,
            "README.md or exact fingerprint-bound absolute README.md",
            {
                "citation_file": citation_file,
                "reference_root": reference_root,
            },
        ),
        Check(
            "C14 citation heading",
            citation_heading_valid,
            "Public validation dependency heading",
            citation,
        ),
        Check(
            "C14 public install path",
            public_install_valid,
            "apt or apt-get",
            public_install,
        ),
        Check(
            "C14 setup result command order/cardinality",
            result_sequence(setup) == expected_setup,
            expected_setup,
            result_sequence(setup),
        ),
        Check(
            "C14 setup result bindings",
            results_bound(setup, expected_setup, require_run_token=False),
            "one outer=0/inner=0 result per declared setup command",
            setup,
        ),
        Check(
            "C14 use result command order/cardinality",
            result_sequence(use) == expected_use,
            expected_use,
            result_sequence(use),
        ),
        Check(
            "C14 use result bindings",
            results_bound(use, expected_use, require_run_token=True),
            "one run-token-bound outer=0/inner=0 result per declared use command",
            use,
        ),
        Check(
            "C14 plan binding",
            isinstance(expected_digest, str)
            and len(expected_digest) == 64
            and plan.get("references_manifest_digest") == expected_digest
            and isinstance(plan.get("run_token"), str)
            and len(str(plan.get("run_token"))) == 64
            and target.get("run_token") == plan.get("run_token"),
            "manifest digest and run token bound across plan and validation",
            {
                "manifest_digest": plan.get("references_manifest_digest"),
                "run_token": plan.get("run_token"),
                "validation_token": target.get("run_token"),
            },
        ),
        Check(
            "C14 strict nodes",
            isinstance(completed, list)
            and {
                "InstallReferenceDependenciesStrict",
                "ExecuteReferenceUseSteps",
                "ExecuteTargetValidationStrict",
                "VerifyReferenceUseEvidence",
            }
            <= set(completed),
            "setup, use, validation, and evidence verification",
            completed,
        ),
        *fingerprints,
    )
    return checks, False


def _parent_integration_checks(
    observation: JsonMapping, case_id: str
) -> tuple[tuple[Check, ...], bool]:
    """Grade actual-parent paths from parent and nested execution evidence."""
    subject = as_object(observation.get("subject"))
    evidence = as_object(observation.get("evidence"))
    if subject is None or evidence is None:
        return (
            (Check("parent integration evidence", False, "mappings", observation),),
            True,
        )
    completed = subject.get("completed_nodes")
    if not isinstance(completed, list):
        return ((Check("parent completed nodes", False, "list", completed),), True)
    required: set[str]
    forbidden: set[str]
    if case_id == "e1_parent_rc_pass_only_delivers":
        required = {
            "RC",
            "VerifyAfterRC",
            "RCClassifyStrict",
            "CheckRC",
            "Deliver",
            "VerifyAfterDeliver",
        }
        forbidden = set()
    elif case_id == "e2_parent_rc_exhausted_no_deliver":
        required = {"RC", "VerifyAfterRC", "RCClassifyStrict", "CheckRC", "RCExhausted"}
        forbidden = {"Deliver", "BuildRCFix"}
    elif case_id == "e3_parent_reference_prerequisite_terminal":
        required = {
            "RC",
            "VerifyAfterRC",
            "RCClassifyStrict",
            "CheckRC",
            "ReferencePrerequisiteFailed",
        }
        forbidden = {"Deliver", "BuildRCFix"}
    elif case_id == "e4_parent_infrastructure_terminal":
        required = {
            "RC",
            "VerifyAfterRC",
            "RCClassifyStrict",
            "CheckRC",
            "RCInfrastructureFailed",
        }
        forbidden = {"Deliver", "BuildRCFix"}
    else:
        required = {"PrepareReferences", "Plan", "VerifyAfterPlan"}
        forbidden = {"Implement", "Deliver", "RC"}
    checks: list[Check] = [
        Check(
            "actual parent required nodes",
            required <= set(completed),
            sorted(required),
            completed,
        ),
        Check(
            "actual parent forbidden nodes",
            not (forbidden & set(completed)),
            sorted(forbidden),
            completed,
        ),
        Check(
            "actual parent parsed graph checkpoint",
            isinstance(evidence.get("parent_checkpoint"), Mapping),
            "parent checkpoint",
            evidence.get("parent_checkpoint"),
        ),
    ]
    fingerprint_checks, missing = _reference_fingerprint_checks(
        evidence.get("reference_fingerprints_before"),
        evidence.get("reference_fingerprints_after"),
        evidence.get("reference_order"),
        unchanged=True,
    )
    if missing:
        return (*checks, *fingerprint_checks), True
    checks.extend(fingerprint_checks)
    if case_id == "e5_parent_manifest_substitution_detected":
        checks.extend(
            (
                Check(
                    "manifest substitution changes trusted bytes",
                    evidence.get("reference_manifest_digest")
                    != evidence.get("manifest_digest_after"),
                    "prepared digest differs from target-owned replacement",
                    {
                        "prepared": evidence.get("reference_manifest_digest"),
                        "after": evidence.get("manifest_digest_after"),
                    },
                ),
                Check(
                    "next Verify child rejects manifest substitution",
                    isinstance(evidence.get("verify_failure_reason"), str)
                    and "reference manifest digest mismatch"
                    in str(evidence.get("verify_failure_reason")),
                    "trusted digest mismatch",
                    evidence.get("verify_failure_reason"),
                ),
            )
        )
    return tuple(checks), False


def grade_case(case_dir: Path, scenario: JsonMapping) -> CaseGrade:
    """Grade one already-captured case without running commands or modifying evidence."""
    case_id = str(scenario.get("id", case_dir.name))
    observation_path = case_dir / "observation.json"
    if not observation_path.is_file():
        return CaseGrade(
            case_id,
            Verdict.ERROR,
            "harness",
            (Check("observation.json", False, "present", "missing"),),
            (),
        )
    try:
        observation = _read_json(observation_path)
    except (OSError, json.JSONDecodeError) as error:
        return CaseGrade(
            case_id,
            Verdict.ERROR,
            "harness",
            (Check("observation.json", False, "valid JSON", str(error)),),
            (),
        )
    if not isinstance(observation, Mapping):
        return CaseGrade(
            case_id,
            Verdict.ERROR,
            "harness",
            (Check("observation shape", False, "object", type(observation).__name__),),
            (),
        )
    harness_error = observation.get("harness_error")
    if isinstance(harness_error, Mapping):
        return CaseGrade(
            case_id,
            Verdict.ERROR,
            "harness",
            (
                Check(
                    "harness execution",
                    False,
                    "completed subject observation",
                    dict(harness_error),
                ),
            ),
            (),
        )
    expected = scenario.get("expected", {})
    if not isinstance(expected, Mapping):
        return CaseGrade(
            case_id,
            Verdict.ERROR,
            "harness",
            (Check("scenario expected", False, "object", expected),),
            (),
        )
    checks = _expectation_checks(observation, expected)
    semantic = observation.get("semantic")
    if case_id in {
        "a1_prepare_empty",
        "a2_prepare_invalid_json",
        "a3_prepare_verify_unchanged",
        "b1_folder_unchanged",
    }:
        raw_checks, evidence_missing = _reference_leaf_checks(observation, case_id)
        checks = (*checks, *raw_checks)
        if evidence_missing:
            return CaseGrade(case_id, Verdict.ERROR, "harness", checks, ())
    if (
        isinstance(semantic, Mapping)
        and semantic.get("class") == "reference_mutation_detected"
    ):
        mutation_checks, evidence_missing = _reference_mutation_checks(observation)
        checks = (*checks, *mutation_checks)
        if evidence_missing:
            return CaseGrade(case_id, Verdict.ERROR, "harness", checks, ())
    if scenario.get("runner") == "real_reality_canary":
        canary_checks, evidence_missing = _real_canary_checks(observation)
        checks = (*checks, *canary_checks)
        if evidence_missing:
            return CaseGrade(case_id, Verdict.ERROR, "harness", checks, ())
        if case_id == "c14_rc_real_validation_reference_canary":
            reference_checks, evidence_missing = _validation_reference_canary_checks(
                observation
            )
            checks = (*checks, *reference_checks)
            if evidence_missing:
                return CaseGrade(case_id, Verdict.ERROR, "harness", checks, ())
        validation_checks, evidence_missing = _target_validation_checks(
            observation, real_canary=True
        )
        checks = (*checks, *validation_checks)
        if evidence_missing:
            return CaseGrade(case_id, Verdict.ERROR, "harness", checks, ())
        cleanup_checks, evidence_missing = _cleanup_identity_checks(
            observation, str(scenario.get("fixture", ""))
        )
        checks = (*checks, *cleanup_checks)
        if evidence_missing:
            return CaseGrade(case_id, Verdict.ERROR, "harness", checks, ())
    if scenario.get("runner") == "controlled_reality":
        path_checks, evidence_missing = _controlled_path_checks(observation, scenario)
        checks = (*checks, *path_checks)
        if evidence_missing:
            return CaseGrade(case_id, Verdict.ERROR, "harness", checks, ())
        cleanup_checks, evidence_missing = _cleanup_identity_checks(
            observation, str(scenario.get("fixture", ""))
        )
        checks = (*checks, *cleanup_checks)
        if evidence_missing:
            return CaseGrade(case_id, Verdict.ERROR, "harness", checks, ())
        if case_id == "c3_rc_no_port_valid_lifecycle":
            validation_checks, evidence_missing = _target_validation_checks(
                observation, real_canary=False
            )
            checks = (*checks, *validation_checks)
            if evidence_missing:
                return CaseGrade(case_id, Verdict.ERROR, "harness", checks, ())
            evidence = observation.get("evidence")
            checks = (
                *checks,
                Check(
                    "C3 empty base URL",
                    isinstance(evidence, Mapping)
                    and evidence.get("sut_base_url") == "",
                    "",
                    evidence.get("sut_base_url")
                    if isinstance(evidence, Mapping)
                    else evidence,
                ),
            )
    if scenario.get("fixture") == "inner_exit_127":
        c4_checks, evidence_missing = _controlled_inner_exit_127_checks(observation)
        checks = (*checks, *c4_checks)
        if evidence_missing:
            return CaseGrade(case_id, Verdict.ERROR, "harness", checks, ())
    if case_id == "d1_parent_rc_exhausted":
        d1_checks, evidence_missing = _parent_exhaustion_checks(observation)
        checks = (*checks, *d1_checks)
        if evidence_missing:
            return CaseGrade(case_id, Verdict.ERROR, "harness", checks, ())
    if scenario.get("runner") == "controlled_parent":
        parent_checks, evidence_missing = _parent_integration_checks(
            observation, case_id
        )
        checks = (*checks, *parent_checks)
        if evidence_missing:
            return CaseGrade(case_id, Verdict.ERROR, "harness", checks, ())
    contradictions = detect_contradictions(observation)
    if contradictions:
        return CaseGrade(case_id, Verdict.FAIL, "graph", checks, contradictions)
    if all(check.passed for check in checks):
        return CaseGrade(case_id, Verdict.PASS, "none", checks, ())
    return CaseGrade(case_id, Verdict.FAIL, "graph", checks, ())


def _serialise_grade(grade: CaseGrade) -> JsonObject:
    return asdict(grade)


def grade_run(run_dir: Path) -> JsonObject:
    """Grade a complete, registry-bound suite; incomplete structure is an ERROR."""
    grades: list[CaseGrade] = []
    run_checks: list[Check] = []
    registry_path = Path(__file__).with_name("scenarios.json")
    manifest_path = run_dir / "manifest.json"
    case_root = run_dir / "cases"
    try:
        registry = _read_json(registry_path)
        manifest = _read_json(manifest_path)
    except (OSError, json.JSONDecodeError) as error:
        registry, manifest = {}, {}
        run_checks.append(
            Check("suite metadata", False, "valid registry/manifest", str(error))
        )
    cases = registry.get("cases") if isinstance(registry, Mapping) else None
    registry_by_id: dict[str, JsonMapping] = {}
    registry_valid = isinstance(cases, list) and bool(cases)
    if isinstance(cases, list) and cases:
        for item in cases:
            if not isinstance(item, Mapping):
                registry_valid = False
                continue
            case_id = item.get("id")
            expected = item.get("expected")
            suites = item.get("suite")
            if (
                not isinstance(case_id, str)
                or not case_id
                or case_id in registry_by_id
                or not isinstance(suites, list)
                or not suites
                or not all(value in {"controlled", "acceptance"} for value in suites)
                or item.get("runner")
                not in {
                    "controller_attractor",
                    "controlled_reality",
                    "parent_rc_probe",
                    "real_reality_canary",
                    "controlled_parent",
                }
                or not isinstance(item.get("fixture"), str)
                or not isinstance(expected, Mapping)
                or set(expected)
                != {
                    "process_exit_code",
                    "engine_status",
                    "semantic_class",
                    "semantic_verdict",
                    "cleanup",
                }
            ):
                registry_valid = False
            else:
                registry_by_id[case_id] = item
    registered_ids = set(registry_by_id)
    expected_registry_ids = ACCEPTANCE_CASE_IDS
    policy_membership_valid = registered_ids == expected_registry_ids and all(
        set(registry_by_id[case_id].get("suite", []))
        == (
            {"controlled", "acceptance"}
            if case_id in CONTROLLED_CASE_IDS
            else {"acceptance"}
        )
        for case_id in registered_ids
    )
    run_checks.append(Check("scenario registry schema", registry_valid, True, registry))
    run_checks.append(
        Check(
            "immutable full-suite policy",
            policy_membership_valid,
            {
                "controlled": sorted(CONTROLLED_CASE_IDS),
                "acceptance": sorted(ACCEPTANCE_CASE_IDS),
            },
            {
                case_id: registry_by_id[case_id].get("suite")
                for case_id in sorted(registry_by_id)
            },
        )
    )

    selected = manifest.get("selected_cases") if isinstance(manifest, Mapping) else None
    suite = manifest.get("suite") if isinstance(manifest, Mapping) else None
    selected_case = manifest.get("case") if isinstance(manifest, Mapping) else None
    selected_valid = (
        isinstance(selected, list)
        and bool(selected)
        and all(isinstance(item, str) and item for item in selected)
        and len(selected) == len(set(selected))
        and suite in {"controlled", "acceptance"}
    )
    expected_ids: set[str] = set()
    if selected_valid and isinstance(selected, list):
        suite_case_ids = (
            FULL_SUITE_CASE_IDS[suite]
            if suite in ("controlled", "acceptance")
            else frozenset()
        )
        expected_ids = (
            {str(selected_case)}
            if isinstance(selected_case, str) and selected_case
            else set(suite_case_ids)
        )
        selected_valid = set(selected) == expected_ids
    run_checks.append(
        Check("manifest selected cases", selected_valid, sorted(expected_ids), selected)
    )

    actual_ids: set[str] = set()
    case_dirs_valid = case_root.is_dir() and not case_root.is_symlink()
    if case_dirs_valid:
        children = list(case_root.iterdir())
        case_dirs_valid = all(
            path.is_dir() and not path.is_symlink() for path in children
        )
        actual_ids = {path.name for path in children}
        case_dirs_valid = case_dirs_valid and actual_ids == expected_ids
    run_checks.append(
        Check(
            "exact case directory set",
            case_dirs_valid,
            sorted(expected_ids),
            sorted(actual_ids),
        )
    )

    source_files = (
        manifest.get("source_files") if isinstance(manifest, Mapping) else None
    )
    before_path = run_dir / "source-hashes.before.json"
    after_path = run_dir / "source-hashes.after.json"
    source_valid = all(
        path.is_file() and not path.is_symlink() for path in (before_path, after_path)
    )
    before_value: object = None
    after_value: object = None
    if source_valid:
        try:
            before_value = _read_json(before_path)
            after_value = _read_json(after_path)
        except (OSError, json.JSONDecodeError):
            source_valid = False
    source_valid = (
        source_valid
        and isinstance(source_files, list)
        and bool(source_files)
        and len(source_files) == len(set(source_files))
        and isinstance(before_value, Mapping)
        and isinstance(after_value, Mapping)
        and set(before_value) == set(source_files) == set(after_value)
        and before_value == after_value
        and all(
            isinstance(value, str) and len(value) == 64 and value != "MISSING"
            for value in before_value.values()
        )
    )
    run_checks.append(
        Check(
            "complete equal source hashes",
            source_valid,
            source_files,
            {"before": before_value, "after": after_value},
        )
    )

    if case_dirs_valid and registry_valid:
        for case_id in sorted(expected_ids):
            case_dir = case_root / case_id
            scenario_path = case_dir / "scenario.json"
            scenario_valid = scenario_path.is_file() and not scenario_path.is_symlink()
            try:
                scenario = _read_json(scenario_path) if scenario_valid else None
            except (OSError, json.JSONDecodeError):
                scenario = None
            if scenario != registry_by_id.get(case_id):
                grades.append(
                    CaseGrade(
                        case_id,
                        Verdict.ERROR,
                        "harness",
                        (
                            Check(
                                "case scenario binding",
                                False,
                                registry_by_id.get(case_id),
                                scenario,
                            ),
                        ),
                        (),
                    )
                )
            elif isinstance(scenario, Mapping):
                grades.append(grade_case(case_dir, scenario))

    if not all(check.passed for check in run_checks):
        grades.insert(
            0,
            CaseGrade("__run__", Verdict.ERROR, "harness", tuple(run_checks), ()),
        )

    if any(grade.verdict is Verdict.ERROR for grade in grades):
        verdict = Verdict.ERROR
    elif any(grade.verdict is Verdict.FAIL for grade in grades):
        verdict = Verdict.FAIL
    else:
        verdict = Verdict.PASS
    return {
        "verdict": verdict.value,
        "cases": [_serialise_grade(grade) for grade in grades],
    }
