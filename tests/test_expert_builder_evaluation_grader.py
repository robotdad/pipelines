"""Contract tests for the expert-builder evaluation oracle."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from evaluations.expert_builder import grade as evaluation_grade
from evaluations.expert_builder.grade import Verdict, grade_case, grade_run


def _scenario(
    case_id: str = "case",
    *,
    process_exit_code: int = 0,
    engine_status: str = "success",
    semantic_class: str = "target_behavior_pass",
    semantic_verdict: str = "pass",
) -> dict[str, object]:
    return {
        "id": case_id,
        "expected": {
            "process_exit_code": process_exit_code,
            "engine_status": engine_status,
            "semantic_class": semantic_class,
            "semantic_verdict": semantic_verdict,
            "cleanup": {"remaining_ids": []},
        },
    }


def _observation(
    *,
    process_exit_code: int = 0,
    engine_status: str = "success",
    semantic_class: str = "target_behavior_pass",
    semantic_verdict: str = "pass",
    remaining_ids: list[str] | None = None,
) -> dict[str, object]:
    return {
        "transport": {"outer_exit_code": 0, "exec_envelope_exit_code": 0},
        "subject": {
            "process_exit_code": process_exit_code,
            "engine_status": engine_status,
            "completed_nodes": [],
            "context": {},
        },
        "semantic": {"verdict": semantic_verdict, "class": semantic_class},
        "cleanup": {
            "created_ids": ["created"],
            "destroy_attempted": ["created"],
            "remaining_ids": remaining_ids or [],
        },
    }


def _write_case(
    root: Path, scenario: dict[str, object], observation: dict[str, object]
) -> Path:
    case_dir = root / "cases" / str(scenario["id"])
    case_dir.mkdir(parents=True)
    (case_dir / "scenario.json").write_text(
        json.dumps(scenario, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (case_dir / "observation.json").write_text(
        json.dumps(observation, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return case_dir


def test_exact_success_tuple_grades_pass(tmp_path: Path) -> None:
    scenario = _scenario()
    grade = grade_case(_write_case(tmp_path, scenario, _observation()), scenario)

    assert grade.verdict is Verdict.PASS
    assert grade.defect_class == "none"
    assert not grade.contradictions


def test_expected_graph_failure_with_correct_tuple_grades_pass(tmp_path: Path) -> None:
    scenario = _scenario(
        process_exit_code=1,
        engine_status="fail",
        semantic_class="reference_prerequisite_failed",
        semantic_verdict="fail",
    )
    observation = _observation(
        process_exit_code=1,
        engine_status="fail",
        semantic_class="reference_prerequisite_failed",
        semantic_verdict="fail",
    )

    grade = grade_case(_write_case(tmp_path, scenario, observation), scenario)

    assert grade.verdict is Verdict.PASS


def test_process_success_with_fail_verdict_is_a_graph_contradiction(
    tmp_path: Path,
) -> None:
    scenario = _scenario()
    observation = _observation(
        semantic_class="target_behavior_failed", semantic_verdict="fail"
    )

    grade = grade_case(_write_case(tmp_path, scenario, observation), scenario)

    assert grade.verdict is Verdict.FAIL
    assert grade.defect_class == "graph"
    assert "process exit 0 with non-success semantic class" in grade.contradictions


def test_engine_success_with_rc_exhausted_is_a_contradiction(tmp_path: Path) -> None:
    scenario = _scenario()
    observation = _observation(
        semantic_class="rc_exhausted", semantic_verdict="partial"
    )

    grade = grade_case(_write_case(tmp_path, scenario, observation), scenario)

    assert grade.verdict is Verdict.FAIL
    assert "engine success with rc_exhausted" in grade.contradictions


def test_missing_required_evidence_is_a_harness_error(tmp_path: Path) -> None:
    scenario = _scenario()
    case_dir = tmp_path / "cases" / "case"
    case_dir.mkdir(parents=True)

    grade = grade_case(case_dir, scenario)

    assert grade.verdict is Verdict.ERROR
    assert grade.defect_class == "harness"


def test_source_hash_drift_is_a_harness_error(tmp_path: Path) -> None:
    scenario = _scenario()
    _write_case(tmp_path, scenario, _observation())
    (tmp_path / "source-hashes.before.json").write_text(
        '{"a": "before"}\n', encoding="utf-8"
    )
    (tmp_path / "source-hashes.after.json").write_text(
        '{"a": "after"}\n', encoding="utf-8"
    )

    result = grade_run(tmp_path)

    assert result["verdict"] == "ERROR"
    assert result["cases"][0]["defect_class"] == "harness"


def test_dtu_leak_is_a_graph_failure(tmp_path: Path) -> None:
    scenario = _scenario()
    observation = _observation(remaining_ids=["created"])

    grade = grade_case(_write_case(tmp_path, scenario, observation), scenario)

    assert grade.verdict is Verdict.FAIL
    assert grade.defect_class == "graph"


def test_overall_error_dominates_fail_and_retains_all_cases(tmp_path: Path) -> None:
    failure = _scenario("failure")
    error = _scenario("error")
    _write_case(
        tmp_path,
        failure,
        _observation(semantic_class="target_behavior_failed", semantic_verdict="fail"),
    )
    error_dir = tmp_path / "cases" / "error"
    error_dir.mkdir(parents=True)
    (error_dir / "scenario.json").write_text(json.dumps(error), encoding="utf-8")

    result = grade_run(tmp_path)

    assert result["verdict"] == "ERROR"
    assert {case["case_id"] for case in result["cases"]} == {"__run__"}


def test_overall_pass_requires_every_required_case_to_pass(tmp_path: Path) -> None:
    first = _scenario("first")
    second = _scenario(
        "second",
        process_exit_code=1,
        engine_status="fail",
        semantic_class="target_behavior_failed",
        semantic_verdict="fail",
    )
    _write_case(tmp_path, first, _observation())
    _write_case(
        tmp_path,
        second,
        _observation(
            process_exit_code=1,
            engine_status="fail",
            semantic_class="target_behavior_failed",
            semantic_verdict="fail",
        ),
    )

    assert grade_run(tmp_path)["verdict"] == "ERROR"


def test_grading_same_evidence_twice_is_byte_equivalent(tmp_path: Path) -> None:
    scenario = _scenario()
    _write_case(tmp_path, scenario, _observation())

    assert grade_run(tmp_path) == grade_run(tmp_path)


def test_prepare_leaf_success_class_can_pass(tmp_path: Path) -> None:
    scenario = _scenario(semantic_class="prepared", semantic_verdict="pass")
    observation = _observation(semantic_class="prepared", semantic_verdict="pass")

    grade = grade_case(_write_case(tmp_path, scenario, observation), scenario)

    assert grade.verdict is Verdict.PASS
    assert not grade.contradictions


def test_verify_leaf_success_class_can_pass(tmp_path: Path) -> None:
    scenario = _scenario(semantic_class="unchanged", semantic_verdict="pass")
    observation = _observation(semantic_class="unchanged", semantic_verdict="pass")

    assert (
        grade_case(_write_case(tmp_path, scenario, observation), scenario).verdict
        is Verdict.PASS
    )


def _fingerprint(path: Path, *, tracked_diff: str) -> dict[str, object]:
    def digest(value: str) -> str:
        return value * 64

    return {
        "path": str(path),
        "head": "head",
        "symbolic_ref": "refs/heads/main",
        "index_sha256": digest("i"),
        "tracked_worktree_diff_sha256": digest(tracked_diff[0]),
        "untracked": {},
        "submodule_state_sha256": digest("s"),
    }


def _complete_mutation_evidence(tmp_path: Path) -> dict[str, object]:
    fixture_root = tmp_path / "fixture"
    first_root = fixture_root / "references" / "reference-a"
    second_root = fixture_root / "references" / "reference-b"
    return {
        "fixture_root": str(fixture_root),
        "reference_order": ["reference-a", "reference-b"],
        "reference_fingerprints_before": {
            "reference-a": _fingerprint(first_root, tracked_diff="clean"),
            "reference-b": _fingerprint(second_root, tracked_diff="clean"),
        },
        "reference_fingerprints_after": {
            "reference-a": _fingerprint(first_root, tracked_diff="modified"),
            "reference-b": _fingerprint(second_root, tracked_diff="clean"),
        },
        "mutation_output": json.dumps(
            {
                "mutation_class": "tracked",
                "mutation_path": str(first_root / "tracked.txt"),
            }
        ),
        "nested_verify_failure_reason": (
            "Command exited with code 1: reference integrity changed: tracked_worktree"
        ),
    }


def test_reference_mutation_semantic_label_alone_cannot_pass(tmp_path: Path) -> None:
    scenario = _scenario(
        process_exit_code=1,
        engine_status="fail",
        semantic_class="reference_mutation_detected",
        semantic_verdict="fail",
    )
    observation = _observation(
        process_exit_code=1,
        engine_status="fail",
        semantic_class="reference_mutation_detected",
        semantic_verdict="fail",
    )

    grade = grade_case(_write_case(tmp_path, scenario, observation), scenario)

    assert grade.verdict is Verdict.ERROR
    assert grade.defect_class == "harness"
    assert any(
        check.name == "mutation evidence" and not check.passed for check in grade.checks
    )


def test_reference_mutation_full_raw_evidence_tuple_passes(tmp_path: Path) -> None:
    scenario = _scenario(
        process_exit_code=1,
        engine_status="fail",
        semantic_class="reference_mutation_detected",
        semantic_verdict="fail",
    )
    observation = _observation(
        process_exit_code=1,
        engine_status="fail",
        semantic_class="reference_mutation_detected",
        semantic_verdict="fail",
    )
    observation["evidence"] = _complete_mutation_evidence(tmp_path)

    grade = grade_case(_write_case(tmp_path, scenario, observation), scenario)

    assert grade.verdict is Verdict.PASS
    assert all(check.passed for check in grade.checks)


def test_reality_check_pass_still_requires_target_behavior_pass(tmp_path: Path) -> None:
    scenario = _scenario(semantic_class="prepared", semantic_verdict="pass")
    observation = _observation(semantic_class="prepared", semantic_verdict="pass")
    semantic = observation["semantic"]
    assert isinstance(semantic, dict)
    semantic["domain"] = "reality_check"

    grade = grade_case(_write_case(tmp_path, scenario, observation), scenario)

    assert grade.verdict is Verdict.FAIL
    assert (
        "reality-check pass verdict without target_behavior_pass"
        in grade.contradictions
    )


def test_grade_run_rejects_empty_manifest_and_missing_case_set(tmp_path: Path) -> None:
    (tmp_path / "cases").mkdir()
    (tmp_path / "manifest.json").write_text(
        json.dumps(
            {
                "suite": "controlled",
                "case": None,
                "selected_cases": [],
                "source_files": [],
            }
        ),
        encoding="utf-8",
    )
    result = grade_run(tmp_path)
    run_grade = next(case for case in result["cases"] if case["case_id"] == "__run__")
    assert result["verdict"] == "ERROR"
    assert {check["name"] for check in run_grade["checks"] if not check["passed"]} >= {
        "manifest selected cases",
        "complete equal source hashes",
    }


def test_grade_run_rejects_symlinked_hashes_and_extra_case_directory(
    tmp_path: Path,
) -> None:
    cases = tmp_path / "cases"
    cases.mkdir()
    (cases / "extra").mkdir()
    (tmp_path / "manifest.json").write_text(
        json.dumps(
            {
                "suite": "controlled",
                "case": "a1_prepare_empty",
                "selected_cases": ["a1_prepare_empty"],
                "source_files": ["input"],
            }
        ),
        encoding="utf-8",
    )
    source = tmp_path / "source.json"
    source.write_text(json.dumps({"input": "a" * 64}), encoding="utf-8")
    (tmp_path / "source-hashes.before.json").symlink_to(source)
    (tmp_path / "source-hashes.after.json").write_text(
        json.dumps({"input": "b" * 64}), encoding="utf-8"
    )
    result = grade_run(tmp_path)
    run_grade = next(case for case in result["cases"] if case["case_id"] == "__run__")
    assert result["verdict"] == "ERROR"
    assert {check["name"] for check in run_grade["checks"] if not check["passed"]} >= {
        "exact case directory set",
        "complete equal source hashes",
    }


def test_grade_run_rejects_immutable_suite_policy_tag_removal(
    tmp_path: Path, monkeypatch
) -> None:
    registry = json.loads(
        (
            Path(__file__).parents[1]
            / "evaluations"
            / "expert_builder"
            / "scenarios.json"
        ).read_text(encoding="utf-8")
    )
    altered = deepcopy(registry)
    next(
        case for case in altered["cases"] if case["id"] == "c5_rc_real_no_port_canary"
    )["suite"] = ["controlled"]
    original_read_json = evaluation_grade._read_json

    def read_with_tag_typo(path: Path) -> object:
        return altered if path.name == "scenarios.json" else original_read_json(path)

    monkeypatch.setattr(evaluation_grade, "_read_json", read_with_tag_typo)
    (tmp_path / "cases").mkdir()
    (tmp_path / "manifest.json").write_text(
        json.dumps(
            {
                "suite": "acceptance",
                "case": None,
                "selected_cases": [],
                "source_files": [],
            }
        ),
        encoding="utf-8",
    )

    result = grade_run(tmp_path)
    run_grade = next(case for case in result["cases"] if case["case_id"] == "__run__")

    assert result["verdict"] == "ERROR"
    assert "immutable full-suite policy" in {
        check["name"] for check in run_grade["checks"] if not check["passed"]
    }


def test_grade_run_rejects_missing_case_directory_for_full_suite(
    tmp_path: Path,
) -> None:
    (tmp_path / "cases").mkdir()
    (tmp_path / "manifest.json").write_text(
        json.dumps(
            {
                "suite": "controlled",
                "case": None,
                "selected_cases": ["a1_prepare_empty"],
                "source_files": [],
            }
        ),
        encoding="utf-8",
    )

    result = grade_run(tmp_path)
    run_grade = next(case for case in result["cases"] if case["case_id"] == "__run__")

    assert result["verdict"] == "ERROR"
    assert "exact case directory set" in {
        check["name"] for check in run_grade["checks"] if not check["passed"]
    }


def _complete_c14_observation(tmp_path: Path) -> dict[str, object]:
    token = "a" * 64
    digest = "b" * 64
    reference = tmp_path / "fixture" / "references" / "validation-reference"
    fingerprint = _fingerprint(reference, tracked_diff="clean")
    return {
        "subject": {
            "completed_nodes": [
                "InstallReferenceDependenciesStrict",
                "ExecuteReferenceUseSteps",
                "ExecuteTargetValidationStrict",
                "VerifyReferenceUseEvidence",
            ]
        },
        "evidence": {
            "reference_manifest_digest": digest,
            "reference_fingerprints_before": {
                "validation-reference": fingerprint,
            },
            "reference_fingerprints_after": {
                "validation-reference": deepcopy(fingerprint),
            },
            "target_validation": {"run_token": token},
            "reference_dependency_plan": {
                "schema_version": 1,
                "state": "ready",
                "references_manifest_digest": digest,
                "run_token": token,
                "dependencies": [
                    {
                        "id": "validation-reference",
                        "citation": {
                            "file": str(reference / "README.md"),
                            "section": "## Public validation dependency",
                        },
                        "public_install": {"selected_path": "apt-get"},
                        "identity": "shellcheck",
                        "version": None,
                        "setup_steps": [
                            "apt-get update",
                            "apt-get install -y shellcheck",
                        ],
                        "use_steps": ["shellcheck /sut/target/demo-cli"],
                    }
                ],
                "setup_results": [
                    {
                        "id": "validation-reference",
                        "command": "apt-get update",
                        "outer_exit_code": 0,
                        "exit_code": 0,
                    },
                    {
                        "id": "validation-reference",
                        "command": "apt-get install -y shellcheck",
                        "outer_exit_code": 0,
                        "exit_code": 0,
                    },
                ],
                "use_results": [
                    {
                        "id": "validation-reference",
                        "command": "shellcheck /sut/target/demo-cli",
                        "run_token": token,
                        "outer_exit_code": 0,
                        "exit_code": 0,
                    }
                ],
            },
        },
    }


def _complete_a2_observation(tmp_path: Path) -> dict[str, object]:
    reference = tmp_path / "fixture" / "references" / "reference-a"
    fingerprint = _fingerprint(reference, tracked_diff="clean")
    return {
        "evidence": {
            "manifest": {},
            "context_markdown": "",
            "prepare_failure_reason": (
                "Command exited with code 1: ERROR: "
                "references must be valid JSON array text: decoder wording changed"
            ),
            "copy_up": {},
            "reference_order": ["reference-a"],
            "reference_fingerprints_before": {"reference-a": fingerprint},
            "reference_fingerprints_after": {"reference-a": deepcopy(fingerprint)},
        }
    }


def test_a2_oracle_accepts_stable_json_array_domain_marker(tmp_path: Path) -> None:
    checks, missing = evaluation_grade._reference_leaf_checks(
        _complete_a2_observation(tmp_path), "a2_prepare_invalid_json"
    )

    assert missing is False
    assert all(check.passed for check in checks)


def test_a2_oracle_rejects_failure_without_json_array_domain_marker(
    tmp_path: Path,
) -> None:
    observation = _complete_a2_observation(tmp_path)
    evidence = observation["evidence"]
    assert isinstance(evidence, dict)
    evidence["prepare_failure_reason"] = "Command exited with code 1: unrelated failure"

    checks, missing = evaluation_grade._reference_leaf_checks(
        observation, "a2_prepare_invalid_json"
    )

    assert missing is False
    assert any(
        check.name == "PrepareReferences invalid JSON diagnostic" and not check.passed
        for check in checks
    )


def test_c14_oracle_accepts_one_result_per_declared_command(tmp_path: Path) -> None:
    checks, missing = evaluation_grade._validation_reference_canary_checks(
        _complete_c14_observation(tmp_path)
    )

    assert missing is False
    assert all(check.passed for check in checks)


def test_c14_oracle_accepts_live_heading_and_apt_metadata_shapes(
    tmp_path: Path,
) -> None:
    observation = _complete_c14_observation(tmp_path)
    evidence = observation["evidence"]
    assert isinstance(evidence, dict)
    plan = evidence["reference_dependency_plan"]
    assert isinstance(plan, dict)
    dependencies = plan["dependencies"]
    assert isinstance(dependencies, list)
    dependency = dependencies[0]
    assert isinstance(dependency, dict)
    citation = dependency["citation"]
    assert isinstance(citation, dict)
    citation["section"] = "Public validation dependency"
    dependency["public_install"] = {"selected_path": "apt"}

    checks, missing = evaluation_grade._validation_reference_canary_checks(observation)

    assert missing is False
    assert all(check.passed for check in checks)


def test_c14_oracle_accepts_complete_live_relative_citation_shape(
    tmp_path: Path,
) -> None:
    observation = _complete_c14_observation(tmp_path)
    evidence = observation["evidence"]
    assert isinstance(evidence, dict)
    plan = evidence["reference_dependency_plan"]
    assert isinstance(plan, dict)
    dependencies = plan["dependencies"]
    assert isinstance(dependencies, list)
    dependency = dependencies[0]
    assert isinstance(dependency, dict)
    citation = dependency["citation"]
    assert isinstance(citation, dict)
    citation.update(
        {
            "file": "README.md",
            "section": "## Public validation dependency",
        }
    )
    dependency["public_install"] = {"selected_path": "apt"}
    dependency["version"] = "0.9.0-1"

    checks, missing = evaluation_grade._validation_reference_canary_checks(observation)

    assert missing is False
    assert [check.name for check in checks if not check.passed] == []


@pytest.mark.parametrize(
    "citation_file",
    (
        "../README.md",
        "other/README.md",
        "/tmp/unrelated/validation-reference/README.md",
    ),
)
def test_c14_oracle_rejects_unbound_citation_files(
    tmp_path: Path, citation_file: str
) -> None:
    observation = _complete_c14_observation(tmp_path)
    evidence = observation["evidence"]
    assert isinstance(evidence, dict)
    plan = evidence["reference_dependency_plan"]
    assert isinstance(plan, dict)
    dependencies = plan["dependencies"]
    assert isinstance(dependencies, list)
    dependency = dependencies[0]
    assert isinstance(dependency, dict)
    citation = dependency["citation"]
    assert isinstance(citation, dict)
    citation["file"] = citation_file

    checks, missing = evaluation_grade._validation_reference_canary_checks(observation)

    assert missing is False
    assert (
        next(
            check for check in checks if check.name == "C14 citation file binding"
        ).passed
        is False
    )


def test_c14_oracle_accepts_exact_fingerprint_bound_absolute_citation(
    tmp_path: Path,
) -> None:
    observation = _complete_c14_observation(tmp_path)
    evidence = observation["evidence"]
    assert isinstance(evidence, dict)
    before = evidence["reference_fingerprints_before"]
    assert isinstance(before, dict)
    fingerprint = before["validation-reference"]
    assert isinstance(fingerprint, dict)
    reference_root = fingerprint["path"]
    assert isinstance(reference_root, str)
    plan = evidence["reference_dependency_plan"]
    assert isinstance(plan, dict)
    dependencies = plan["dependencies"]
    assert isinstance(dependencies, list)
    dependency = dependencies[0]
    assert isinstance(dependency, dict)
    citation = dependency["citation"]
    assert isinstance(citation, dict)
    citation["file"] = str(Path(reference_root) / "README.md")

    checks, missing = evaluation_grade._validation_reference_canary_checks(observation)

    assert missing is False
    assert next(
        check for check in checks if check.name == "C14 citation file binding"
    ).passed
    assert all(check.passed for check in checks)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("section", "Wrong validation dependency"),
        ("selected_path", "aptitude"),
    ),
)
def test_c14_oracle_rejects_non_equivalent_metadata(
    tmp_path: Path, field: str, value: str
) -> None:
    observation = _complete_c14_observation(tmp_path)
    evidence = observation["evidence"]
    assert isinstance(evidence, dict)
    plan = evidence["reference_dependency_plan"]
    assert isinstance(plan, dict)
    dependencies = plan["dependencies"]
    assert isinstance(dependencies, list)
    dependency = dependencies[0]
    assert isinstance(dependency, dict)
    if field == "section":
        citation = dependency["citation"]
        assert isinstance(citation, dict)
        citation[field] = value
    else:
        public_install = dependency["public_install"]
        assert isinstance(public_install, dict)
        public_install[field] = value

    checks, missing = evaluation_grade._validation_reference_canary_checks(observation)

    assert missing is False
    assert (
        next(check for check in checks if check.name == "C14 exact dependency").passed
        is False
    )


def test_c14_oracle_accepts_nonempty_observed_version(tmp_path: Path) -> None:
    observation = _complete_c14_observation(tmp_path)
    evidence = observation["evidence"]
    assert isinstance(evidence, dict)
    plan = evidence["reference_dependency_plan"]
    assert isinstance(plan, dict)
    dependencies = plan["dependencies"]
    assert isinstance(dependencies, list)
    dependencies[0]["version"] = "0.9.0-1"

    checks, missing = evaluation_grade._validation_reference_canary_checks(observation)

    assert missing is False
    assert all(check.passed for check in checks)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("identity", "not-shellcheck"),
        ("version", ""),
        ("version", {"invented": "0.9.0"}),
    ),
)
def test_c14_oracle_rejects_wrong_identity_or_invalid_non_null_version(
    tmp_path: Path, field: str, value: object
) -> None:
    observation = _complete_c14_observation(tmp_path)
    evidence = observation["evidence"]
    assert isinstance(evidence, dict)
    plan = evidence["reference_dependency_plan"]
    assert isinstance(plan, dict)
    dependencies = plan["dependencies"]
    assert isinstance(dependencies, list)
    dependencies[0][field] = value

    checks, missing = evaluation_grade._validation_reference_canary_checks(observation)

    assert missing is False
    exact_dependency = next(
        check for check in checks if check.name == "C14 exact dependency"
    )
    assert exact_dependency.passed is False


@pytest.mark.parametrize(
    "mutation",
    (
        "missing_setup",
        "reordered_setup",
        "extra_setup",
        "wrong_setup_command",
        "wrong_setup_id",
        "missing_use",
        "extra_use",
        "wrong_use_command",
        "wrong_use_id",
        "wrong_use_token",
    ),
)
def test_c14_oracle_rejects_result_cardinality_order_and_binding_errors(
    tmp_path: Path, mutation: str
) -> None:
    observation = _complete_c14_observation(tmp_path)
    evidence = observation["evidence"]
    assert isinstance(evidence, dict)
    plan = evidence["reference_dependency_plan"]
    assert isinstance(plan, dict)
    setup = plan["setup_results"]
    use = plan["use_results"]
    assert isinstance(setup, list)
    assert isinstance(use, list)

    if mutation == "missing_setup":
        setup.pop()
    elif mutation == "reordered_setup":
        setup.reverse()
    elif mutation == "extra_setup":
        setup.append(deepcopy(setup[-1]))
    elif mutation == "wrong_setup_command":
        setup[0]["command"] = "apt-get update --wrong"
    elif mutation == "wrong_setup_id":
        setup[0]["id"] = "other-reference"
    elif mutation == "missing_use":
        use.clear()
    elif mutation == "extra_use":
        use.append(deepcopy(use[0]))
    elif mutation == "wrong_use_command":
        use[0]["command"] = "shellcheck wrong-target"
    elif mutation == "wrong_use_id":
        use[0]["id"] = "other-reference"
    else:
        use[0]["run_token"] = "c" * 64

    checks, missing = evaluation_grade._validation_reference_canary_checks(observation)

    assert missing is False
    assert any(not check.passed for check in checks)


@pytest.mark.parametrize(
    "mutation",
    (
        "changed_setup",
        "reordered_setup",
        "missing_setup",
        "extra_setup",
        "changed_use",
        "missing_use",
        "extra_use",
    ),
)
def test_c14_oracle_rejects_declared_command_mutations(
    tmp_path: Path, mutation: str
) -> None:
    observation = _complete_c14_observation(tmp_path)
    evidence = observation["evidence"]
    assert isinstance(evidence, dict)
    plan = evidence["reference_dependency_plan"]
    assert isinstance(plan, dict)
    dependencies = plan["dependencies"]
    assert isinstance(dependencies, list)
    dependency = dependencies[0]
    assert isinstance(dependency, dict)
    setup_steps = dependency["setup_steps"]
    use_steps = dependency["use_steps"]
    assert isinstance(setup_steps, list)
    assert isinstance(use_steps, list)

    if mutation == "changed_setup":
        setup_steps[0] = "apt-get update --wrong"
    elif mutation == "reordered_setup":
        setup_steps.reverse()
    elif mutation == "missing_setup":
        setup_steps.pop()
    elif mutation == "extra_setup":
        setup_steps.append("true")
    elif mutation == "changed_use":
        use_steps[0] = "shellcheck wrong-target"
    elif mutation == "missing_use":
        use_steps.clear()
    else:
        use_steps.append("true")

    checks, missing = evaluation_grade._validation_reference_canary_checks(observation)

    assert missing is False
    assert (
        next(check for check in checks if check.name == "C14 exact dependency").passed
        is False
    )


@pytest.mark.parametrize(
    ("result_group", "exit_field"),
    (
        ("setup_results", "outer_exit_code"),
        ("setup_results", "exit_code"),
        ("use_results", "outer_exit_code"),
        ("use_results", "exit_code"),
    ),
)
def test_c14_oracle_rejects_nonzero_setup_and_use_result_exits(
    tmp_path: Path, result_group: str, exit_field: str
) -> None:
    observation = _complete_c14_observation(tmp_path)
    evidence = observation["evidence"]
    assert isinstance(evidence, dict)
    plan = evidence["reference_dependency_plan"]
    assert isinstance(plan, dict)
    results = plan[result_group]
    assert isinstance(results, list)
    result = results[0]
    assert isinstance(result, dict)
    result[exit_field] = 1

    checks, missing = evaluation_grade._validation_reference_canary_checks(observation)

    assert missing is False
    binding_check = (
        "C14 setup result bindings"
        if result_group == "setup_results"
        else "C14 use result bindings"
    )
    assert (
        next(check for check in checks if check.name == binding_check).passed is False
    )


def test_c14_oracle_rejects_target_validation_run_token_mismatch(
    tmp_path: Path,
) -> None:
    observation = _complete_c14_observation(tmp_path)
    evidence = observation["evidence"]
    assert isinstance(evidence, dict)
    target = evidence["target_validation"]
    assert isinstance(target, dict)
    target["run_token"] = "c" * 64

    checks, missing = evaluation_grade._validation_reference_canary_checks(observation)

    assert missing is False
    assert (
        next(check for check in checks if check.name == "C14 plan binding").passed
        is False
    )


@pytest.mark.parametrize("mutation", ("changed", "malformed"))
def test_c14_oracle_rejects_changed_or_malformed_reference_fingerprint(
    tmp_path: Path, mutation: str
) -> None:
    observation = _complete_c14_observation(tmp_path)
    evidence = observation["evidence"]
    assert isinstance(evidence, dict)
    after = evidence["reference_fingerprints_after"]
    assert isinstance(after, dict)
    fingerprint = after["validation-reference"]
    assert isinstance(fingerprint, dict)
    if mutation == "changed":
        fingerprint["tracked_worktree_diff_sha256"] = "z" * 64
    else:
        del fingerprint["head"]

    checks, missing = evaluation_grade._validation_reference_canary_checks(observation)

    assert missing is False
    assert any(
        check.name.startswith("reference validation-reference") and not check.passed
        for check in checks
    )
