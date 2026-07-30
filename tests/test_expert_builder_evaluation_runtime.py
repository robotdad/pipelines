"""Runtime contracts for deterministic expert-builder evaluation plumbing."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest
from amplifier_module_loop_pipeline.dot_parser import parse_dot

from evaluations.expert_builder import dtu_cli
from evaluations.expert_builder import fixtures as evaluation_fixtures
from evaluations.expert_builder.fixtures import (
    ControlledRealityBackend,
    _nested_checkpoints,
    _nested_failure_reason,
    _node_failure_reason,
    _parent_checkpoint,
    build_parent_rc_probe,
    ensure_evaluation_path,
    run_controlled_parent_case,
    run_controller_attractor_case,
    run_parent_rc_probe_case,
    run_real_reality_canary_case,
    write_fake_dtu,
)
from evaluations.expert_builder.grade import Verdict, grade_case
from evaluations.expert_builder.run import (
    EVALUATION_ROOT,
    HARNESS_DOT,
    PARENT_DOT,
    _capture_case,
    _selection,
    _source_tree_files,
    load_scenarios,
)


def test_exec_json_preserves_inner_127(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = tmp_path / "bin"
    call_log = tmp_path / "calls.jsonl"
    write_fake_dtu(
        bin_dir,
        envelope={"outer_exit_code": 0, "inner_exit_code": 127},
        call_log=call_log,
    )
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    result = dtu_cli.exec_json("test", ["echo", "hello"])

    assert result.outer.returncode == 0
    assert result.inner_exit_code == 127


def test_malformed_exec_envelope_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = tmp_path / "bin"
    write_fake_dtu(
        bin_dir,
        envelope={"outer_exit_code": 0, "exec_payload": {"exit_code": "127"}},
        call_log=tmp_path / "calls.jsonl",
    )
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    with pytest.raises(dtu_cli.HarnessError, match="integer exit_code"):
        dtu_cli.exec_json("test", ["false"])


def test_fake_dtu_records_exact_invocation_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = tmp_path / "bin"
    call_log = tmp_path / "calls.jsonl"
    profile = tmp_path / "profile.yaml"
    profile.write_text("name: test\n", encoding="utf-8")
    source = tmp_path / "source.txt"
    source.write_text("source\n", encoding="utf-8")
    write_fake_dtu(bin_dir, envelope={}, call_log=call_log)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    _, launch_payload = dtu_cli.launch(profile, name="controller")
    dtu_cli.exec_json(str(launch_payload["id"]), ["true"])
    dtu_cli.file_push(
        str(launch_payload["id"]), source, "/tmp/source.txt", recursive=False
    )
    dtu_cli.destroy(str(launch_payload["id"]))
    dtu_cli.list_instances()

    calls = [
        json.loads(line) for line in call_log.read_text(encoding="utf-8").splitlines()
    ]
    assert [call["operation"] for call in calls] == [
        "launch",
        "exec",
        "file-push",
        "destroy",
        "list",
    ]
    assert [call["sequence"] for call in calls] == [1, 2, 3, 4, 5]


def test_scenarios_are_unique_and_resolve_runner_and_fixture_names() -> None:
    scenarios = load_scenarios()
    ids = [str(case["id"]) for case in scenarios["cases"]]

    assert len(ids) == len(set(ids))
    assert {str(case["runner"]) for case in scenarios["cases"]} == {
        "controller_attractor",
        "controlled_reality",
        "parent_rc_probe",
        "real_reality_canary",
        "controlled_parent",
    }
    assert {str(case["fixture"]) for case in scenarios["cases"]} <= {
        "empty_references",
        "invalid_json",
        "two_references",
        "tracked_mutation",
        "inner_exit_127",
        "rc_exhausted",
        "launch_fail",
        "invalid_handle",
        "no_port_lifecycle",
        "real_no_port_canary",
        "target_validation_inner_failure",
        "target_validation_outer_failure",
        "target_toolchain_inner_failure",
        "target_toolchain_outer_failure",
        "deploy_inner_failure",
        "deploy_outer_failure",
        "reference_use_inner_failure",
        "reference_use_outer_failure",
        "real_validation_reference_canary",
        "parent_rc_pass",
        "parent_rc_exhausted",
        "parent_reference_prerequisite",
        "parent_infrastructure",
        "parent_manifest_substitution",
    }
    assert set(ids) == {
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
        "c5_rc_real_no_port_canary",
        "c14_rc_real_validation_reference_canary",
        "e1_parent_rc_pass_only_delivers",
        "e2_parent_rc_exhausted_no_deliver",
        "e3_parent_reference_prerequisite_terminal",
        "e4_parent_infrastructure_terminal",
        "e5_parent_manifest_substitution_detected",
    }


def test_controlled_suite_excludes_canary_and_acceptance_includes_it() -> None:
    scenarios = load_scenarios()

    controlled = {case["id"] for case in _selection(scenarios, "controlled", None)}
    acceptance = {case["id"] for case in _selection(scenarios, "acceptance", None)}

    assert "c5_rc_real_no_port_canary" not in controlled
    assert "c5_rc_real_no_port_canary" in acceptance


def test_harness_dot_is_parsed_when_supplied() -> None:
    if not HARNESS_DOT.exists():
        pytest.skip("DOT harness is independently supplied by dot-author")
    graph = parse_dot(HARNESS_DOT.read_text(encoding="utf-8"))

    assert graph.nodes
    assert HARNESS_DOT.parent.name == "harness"


def test_harness_dot_points_to_package_relative_children_when_supplied() -> None:
    if not HARNESS_DOT.exists():
        pytest.skip("DOT harness is independently supplied by dot-author")
    source = HARNESS_DOT.read_text(encoding="utf-8")

    assert 'dot_file="references/prepare.dot"' in source
    assert 'dot_file="references/verify.dot"' in source
    assert "evaluation_fixture_root" in source


def test_controlled_backend_rejects_unexpected_box_node(tmp_path: Path) -> None:
    from amplifier_module_loop_pipeline.context import PipelineContext
    from amplifier_module_loop_pipeline.graph import Node

    backend = ControlledRealityBackend(tmp_path, {"qa_verdict": "pass"})

    with pytest.raises(dtu_cli.HarnessError, match="unexpected controlled box node"):
        asyncio.run(
            backend.run(Node(id="Unexpected", shape="box"), "", PipelineContext())
        )


@pytest.mark.parametrize(
    ("variant", "expected"),
    (
        (
            "valid",
            {
                "schema_version": 1,
                "setup_commands": [],
                "deploy_command": "./controlled-target --deploy",
                "validation_command": "./controlled-target --self-test",
                "port": None,
            },
        ),
        (
            "obsolete_fields",
            {
                "schema_version": 1,
                "setup_cmds": [],
                "deploy_cmd": "./controlled-target --deploy",
                "validation_cmd": "./controlled-target --self-test",
                "sut_port": None,
            },
        ),
        (
            "extra_fields",
            {
                "schema_version": 1,
                "setup_commands": [],
                "deploy_command": "./controlled-target --deploy",
                "validation_command": "./controlled-target --self-test",
                "port": None,
                "profile": {"image": "ubuntu:24.04"},
            },
        ),
        (
            "absolute_deploy_path",
            {
                "schema_version": 1,
                "setup_commands": [],
                "deploy_command": "/opt/controlled/start",
                "validation_command": "./controlled-target --self-test",
                "port": None,
            },
        ),
    ),
)
def test_controlled_backend_writes_exact_dut_plan_variants(
    evaluation_case_dir: Path,
    variant: str,
    expected: dict[str, object],
) -> None:
    from amplifier_module_loop_pipeline.context import PipelineContext

    target = evaluation_case_dir / "target"
    target.mkdir()
    context = PipelineContext()
    context.set("context.target_dir", str(target))
    graph = parse_dot(
        (PARENT_DOT.parent / "reality_check.dot").read_text(encoding="utf-8")
    )
    backend = ControlledRealityBackend(
        evaluation_case_dir,
        {"dut_plan_variant": variant, "sut_port": "none"},
    )

    asyncio.run(backend.run(graph.nodes["DetectDUTPlan"], "", context))

    assert (
        json.loads((target / ".rc" / "dut_plan.json").read_text(encoding="utf-8"))
        == expected
    )


def test_parent_rc_probe_uses_production_nodes_and_edges() -> None:
    graph = parse_dot(PARENT_DOT.read_text(encoding="utf-8"))
    probe = build_parent_rc_probe(graph)
    assert set(probe.nodes) == {"SyntheticStart", "CheckRC", "RCExhausted", "done"}
    for node_id in ("CheckRC", "RCExhausted", "done"):
        source = graph.nodes[node_id]
        copied = probe.nodes[node_id]
        assert dict(copied.attrs) == dict(source.attrs)
        assert copied.max_retries == source.max_retries
        assert copied.allow_partial == source.allow_partial
        assert copied.goal_gate == source.goal_gate
    assert probe.nodes["RCExhausted"].allow_partial in (True, "true")
    production_edges = {
        (edge.from_node, edge.to_node, edge.condition)
        for edge in graph.edges
        if edge.from_node in {"CheckRC", "RCExhausted"}
        and edge.to_node in {"RCExhausted", "done"}
    }
    probe_edges = {
        (edge.from_node, edge.to_node, edge.condition) for edge in probe.edges
    }
    assert production_edges <= probe_edges


def test_parent_rc_probe_runs_real_partial_retry_semantics(
    evaluation_case_dir: Path,
) -> None:
    observation = run_parent_rc_probe_case(
        case_dir=evaluation_case_dir,
        parent_dot=PARENT_DOT,
    )

    assert observation["subject"]["process_exit_code"] == 1
    assert observation["subject"]["engine_status"] == "partial_success"
    assert observation["subject"]["context"]["rc_state"] == "rc_exhausted"
    assert "RCExhausted" in observation["subject"]["completed_nodes"]
    assert "Deliver" not in observation["subject"]["completed_nodes"]
    assert observation["semantic"] == {
        "domain": "parent",
        "verdict": "partial",
        "class": "rc_exhausted",
    }


def test_evaluation_code_does_not_import_amplifier_evaluation() -> None:
    root = Path(__file__).parents[1] / "evaluations" / "expert_builder"

    assert not any(
        "amplifier_evaluation" in path.read_text(encoding="utf-8")
        for path in root.glob("*.py")
    )


def test_output_root_stays_beneath_workspace_work_directory() -> None:
    workspace = Path(__file__).parents[2]

    assert EVALUATION_ROOT.resolve().is_relative_to(
        workspace / ".work" / "evaluations" / "expert-builder"
    )


def test_fixture_paths_outside_evaluation_root_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(dtu_cli.HarnessError, match="evaluation root"):
        ensure_evaluation_path(tmp_path)


@pytest.fixture
def evaluation_case_dir() -> Iterator[Path]:
    path = EVALUATION_ROOT / "pytest" / uuid4().hex / "cases" / "case"
    path.mkdir(parents=True)
    try:
        yield path
    finally:
        shutil.rmtree(path.parents[1], ignore_errors=True)


def test_controller_attractor_prepare_empty_captures_real_evidence(
    evaluation_case_dir: Path,
) -> None:
    scenario = {
        "id": "a1_prepare_empty",
        "runner": "controller_attractor",
        "fixture": "empty_references",
    }

    observation = run_controller_attractor_case(
        case_dir=evaluation_case_dir,
        scenario=scenario,
        package_dir=PARENT_DOT.parent,
        harness_dot=HARNESS_DOT,
    )

    assert observation["subject"]["process_exit_code"] == 0
    assert observation["subject"]["engine_status"] == "success"
    assert observation["semantic"] == {
        "domain": "references",
        "verdict": "pass",
        "class": "prepared",
    }
    assert (evaluation_case_dir / "run-log" / "prepare" / "checkpoint.json").is_file()
    assert (evaluation_case_dir / "artifacts" / "references.json").is_file()


def test_folder_mutation_captures_fingerprints_mutation_and_nested_failure(
    evaluation_case_dir: Path,
) -> None:
    scenario = {
        "id": "b2_folder_tracked_mutation",
        "runner": "controller_attractor",
        "fixture": "tracked_mutation",
        "graph": "folder",
    }

    observation = run_controller_attractor_case(
        case_dir=evaluation_case_dir,
        scenario=scenario,
        package_dir=PARENT_DOT.parent,
        harness_dot=HARNESS_DOT,
    )

    evidence = observation["evidence"]
    before = evidence["reference_fingerprints_before"]["reference-a"]
    after = evidence["reference_fingerprints_after"]["reference-a"]
    assert (
        before["tracked_worktree_diff_sha256"] != after["tracked_worktree_diff_sha256"]
    )
    mutation = json.loads(evidence["mutation_output"])
    assert mutation["mutation_class"] == "tracked"
    assert mutation["mutation_path"].endswith("/reference-a/tracked.txt")
    assert "tracked_worktree" in evidence["nested_failure_reason"]
    assert observation["semantic"]["class"] == "reference_mutation_detected"


def test_missing_parent_probe_structure_is_graph_fail_not_harness_error(
    evaluation_case_dir: Path,
) -> None:
    observation = run_parent_rc_probe_case(
        case_dir=evaluation_case_dir,
        parent_dot=PARENT_DOT,
    )

    if "RCExhausted" not in parse_dot(PARENT_DOT.read_text(encoding="utf-8")).nodes:
        assert observation["subject"]["engine_status"] == "fail"
        assert observation["semantic"]["class"] == "rc_exhausted_structure_missing"
        assert observation["semantic"]["domain"] == "parent"


def test_parent_checkpoint_selection_never_uses_nested_lexicographic_last(
    evaluation_case_dir: Path,
) -> None:
    log_root = evaluation_case_dir / "run-log" / "folder"
    nested = log_root / "zzzz-child"
    nested.mkdir(parents=True)
    (log_root / "checkpoint.json").write_text(
        json.dumps({"current_node": "ParentExit", "context": {"outcome": "fail"}}),
        encoding="utf-8",
    )
    (nested / "checkpoint.json").write_text(
        json.dumps({"current_node": "NestedExit", "context": {"outcome": "success"}}),
        encoding="utf-8",
    )

    assert _parent_checkpoint(log_root)["current_node"] == "ParentExit"
    checkpoints = _nested_checkpoints(log_root)
    assert set(checkpoints) == {"zzzz-child/checkpoint.json"}
    assert checkpoints["zzzz-child/checkpoint.json"]["current_node"] == "NestedExit"


def test_failure_reason_collection_deduplicates_mirrors_per_invocation(
    evaluation_case_dir: Path,
) -> None:
    log_root = evaluation_case_dir / "run-log"
    for invocation in ("prepare", "verify"):
        checkpoint = log_root / invocation / "checkpoint.json"
        checkpoint.parent.mkdir(parents=True, exist_ok=True)
        checkpoint.write_text("{}", encoding="utf-8")

    def write_status(
        relative: str, *, node_id: str | None, iteration: int | None, reason: str
    ) -> None:
        status: dict[str, object] = {"failure_reason": reason}
        if node_id is not None:
            status["node_id"] = node_id
        if iteration is not None:
            status["iteration"] = iteration
        path = log_root / relative / "status.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(status), encoding="utf-8")

    write_status(
        "prepare/PrepareReferences",
        node_id="PrepareReferences",
        iteration=0,
        reason="canonical mirror",
    )
    write_status(
        "prepare/iteration_0/PrepareReferences",
        node_id="PrepareReferences",
        iteration=0,
        reason="canonical mirror",
    )
    write_status(
        "prepare/iteration_1/PrepareReferences",
        node_id="PrepareReferences",
        iteration=1,
        reason="different iteration",
    )
    write_status(
        "prepare/alternate/PrepareReferences",
        node_id="PrepareReferences",
        iteration=0,
        reason="same identity different reason",
    )
    write_status(
        "prepare/OtherNode",
        node_id="OtherNode",
        iteration=0,
        reason="different node",
    )
    write_status(
        "verify/PrepareReferences",
        node_id="PrepareReferences",
        iteration=0,
        reason="canonical mirror",
    )
    write_status(
        "prepare/malformed",
        node_id=None,
        iteration=None,
        reason="malformed record",
    )

    assert set(_nested_failure_reason(log_root).splitlines()) == {
        "canonical mirror",
        "different iteration",
        "different node",
        "malformed record",
        "same identity different reason",
    }
    assert _nested_failure_reason(log_root).splitlines().count("canonical mirror") == 2
    assert _node_failure_reason(
        log_root / "prepare", "PrepareReferences"
    ).splitlines() == [
        "canonical mirror",
        "same identity different reason",
        "different iteration",
    ]


def test_registered_non_folder_tracked_mutation_collects_and_grades_complete_evidence(
    evaluation_case_dir: Path,
) -> None:
    scenarios = load_scenarios()
    scenario = next(
        case
        for case in scenarios["cases"]
        if case["id"] == "a4_verify_tracked_mutation"
    )

    _capture_case(evaluation_case_dir, scenario)
    observation = json.loads(
        (evaluation_case_dir / "observation.json").read_text(encoding="utf-8")
    )
    mutation = json.loads(observation["evidence"]["mutation_output"])
    grade = grade_case(evaluation_case_dir, scenario)

    assert mutation["mutation_class"] == "tracked"
    assert Path(mutation["mutation_path"]).is_absolute()
    assert mutation["mutation_path"].endswith("/reference-a/tracked.txt")
    assert observation["evidence"]["fixture_root"]
    assert observation["evidence"]["reference_order"] == [
        "reference-a",
        "reference-b",
    ]
    assert "tracked_worktree" in observation["evidence"]["nested_verify_failure_reason"]
    assert grade.verdict is Verdict.PASS


def test_registered_c4_exercises_strict_setup_and_records_nested_exit_127(
    evaluation_case_dir: Path,
) -> None:
    scenario = next(
        case
        for case in load_scenarios()["cases"]
        if case["id"] == "c4_rc_inner_exit_127"
    )

    _capture_case(evaluation_case_dir, scenario)
    observation = json.loads(
        (evaluation_case_dir / "observation.json").read_text(encoding="utf-8")
    )
    grade = grade_case(evaluation_case_dir, scenario)
    evidence = observation["evidence"]
    setup_result = evidence["setup_result"]

    assert evidence["initial_dependency_plan"]["state"] == "ready"
    assert evidence["profile_generated"] is True
    assert evidence["profile_launch_succeeded"] is True
    assert "NormalizeDUTPlan" in observation["subject"]["completed_nodes"]
    assert "InstallTargetToolchainStrict" in observation["subject"]["completed_nodes"]
    assert (
        "InstallReferenceDependenciesStrict"
        in observation["subject"]["completed_nodes"]
    )
    assert setup_result["outer_exit_code"] == 0
    assert setup_result["exit_code"] == 127
    assert observation["transport"] == {
        "outer_exit_code": 0,
        "exec_envelope_exit_code": 127,
        "recorded_exec_exit_code": 127,
    }
    assert grade.verdict is Verdict.PASS


@pytest.mark.parametrize(
    ("case_id", "required_nodes", "forbidden_nodes"),
    (
        (
            "c1_rc_launch_fail",
            {"DetectDUTPlan", "NormalizeDUTPlan", "LaunchDTUStrict", "TerminalLatch"},
            {"ParseHandleStrict"},
        ),
        (
            "c2_rc_invalid_handle",
            {
                "DetectDUTPlan",
                "NormalizeDUTPlan",
                "LaunchDTUStrict",
                "ParseHandleStrict",
                "RouteHandleStrict",
                "TerminalLatch",
            },
            {"PushSUTStrict"},
        ),
        (
            "c3_rc_no_port_valid_lifecycle",
            {
                "DetectDUTPlan",
                "NormalizeDUTPlan",
                "LaunchDTUStrict",
                "ParseHandleStrict",
                "PushSUTStrict",
                "InstallTargetToolchainStrict",
                "InstallReferenceDependenciesStrict",
                "DeployStrict",
                "Validate",
                "TerminalLatch",
            },
            set(),
        ),
        (
            "c4_rc_inner_exit_127",
            {
                "DetectDUTPlan",
                "NormalizeDUTPlan",
                "LaunchDTUStrict",
                "ParseHandleStrict",
                "PushSUTStrict",
                "InstallTargetToolchainStrict",
                "InstallReferenceDependenciesStrict",
                "TerminalLatch",
            },
            {"DeployStrict"},
        ),
    ),
)
def test_controlled_reality_cases_follow_normalized_strict_flow(
    evaluation_case_dir: Path,
    case_id: str,
    required_nodes: set[str],
    forbidden_nodes: set[str],
) -> None:
    scenario = next(case for case in load_scenarios()["cases"] if case["id"] == case_id)

    _capture_case(evaluation_case_dir, scenario)
    observation = json.loads(
        (evaluation_case_dir / "observation.json").read_text(encoding="utf-8")
    )
    completed = set(observation["subject"]["completed_nodes"])

    assert required_nodes <= completed
    assert not (forbidden_nodes & completed)
    assert observation["evidence"]["profile_generated"] is True
    if case_id != "c1_rc_launch_fail":
        assert observation["evidence"]["profile_launch_succeeded"] is True
    if observation["semantic"]["verdict"] == "fail":
        assert "No matching edge" not in observation["subject"]["failure_notes"]


def test_capture_fallback_always_includes_collector_evidence(
    evaluation_case_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario = {
        "id": "collector_failure",
        "runner": "controller_attractor",
        "fixture": "empty_references",
    }
    monkeypatch.setattr(
        "evaluations.expert_builder.run.run_controller_attractor_case",
        lambda **_: (_ for _ in ()).throw(RuntimeError("forced collector failure")),
    )

    _capture_case(evaluation_case_dir, scenario)
    observation = json.loads(
        (evaluation_case_dir / "observation.json").read_text(encoding="utf-8")
    )

    assert observation["evidence"]["collector_error"] == (
        "evaluation collector failed before a complete subject observation: "
        "forced collector failure"
    )


def test_c4_grader_rejects_a_prefailed_plan_that_bypasses_strict_setup(
    evaluation_case_dir: Path,
) -> None:
    scenario = next(
        case
        for case in load_scenarios()["cases"]
        if case["id"] == "c4_rc_inner_exit_127"
    )

    _capture_case(evaluation_case_dir, scenario)
    observation_path = evaluation_case_dir / "observation.json"
    observation = json.loads(observation_path.read_text(encoding="utf-8"))
    observation["subject"]["completed_nodes"].remove(
        "InstallReferenceDependenciesStrict"
    )
    observation["evidence"]["initial_dependency_plan"]["state"] = (
        "reference_prerequisite_failed"
    )
    observation["evidence"]["reference_dependency_plan"]["setup_results"] = []
    observation["evidence"]["setup_result"] = {}
    observation["transport"] = {
        "outer_exit_code": None,
        "exec_envelope_exit_code": None,
        "recorded_exec_exit_code": None,
    }
    observation_path.write_text(json.dumps(observation), encoding="utf-8")

    grade = grade_case(evaluation_case_dir, scenario)

    assert grade.verdict is Verdict.FAIL
    assert any(
        check.name == "c4 strict setup node visited" and not check.passed
        for check in grade.checks
    )


@pytest.mark.parametrize(
    ("mutation", "expected"),
    (
        ("delete_target_validation", Verdict.ERROR),
        ("forge_target_stdout", Verdict.FAIL),
        ("delete_cleanup", Verdict.ERROR),
        ("nonzero_deploy", Verdict.FAIL),
        ("leak_extra_dtu", Verdict.FAIL),
    ),
)
def test_c3_grader_rejects_deleted_and_spoofed_raw_evidence(
    evaluation_case_dir: Path,
    mutation: str,
    expected: Verdict,
) -> None:
    scenario = next(
        case
        for case in load_scenarios()["cases"]
        if case["id"] == "c3_rc_no_port_valid_lifecycle"
    )
    _capture_case(evaluation_case_dir, scenario)
    path = evaluation_case_dir / "observation.json"
    observation = json.loads(path.read_text(encoding="utf-8"))
    if mutation == "delete_target_validation":
        observation["evidence"]["target_validation"] = {}
    elif mutation == "forge_target_stdout":
        observation["evidence"]["target_validation"]["stdout"] = "forged\n"
    elif mutation == "delete_cleanup":
        observation["evidence"]["cleanup"] = {}
    elif mutation == "nonzero_deploy":
        observation["evidence"]["deploy_result"]["inner_exit_code"] = 1
    else:
        observation["evidence"]["dtu_list_after"] = [
            {"id": "leaked-extra", "name": "leaked-extra"}
        ]
        observation["cleanup"]["remaining_ids"] = ["leaked-extra"]
    path.write_text(json.dumps(observation), encoding="utf-8")
    assert grade_case(evaluation_case_dir, scenario).verdict is expected


def test_real_canary_collector_with_subprocess_fakes_captures_and_grades(
    evaluation_case_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = next(
        case
        for case in load_scenarios()["cases"]
        if case["id"] == "c5_rc_real_no_port_canary"
    )
    snapshots = iter(
        [
            [],
            [],
        ]
    )
    monkeypatch.setattr(
        evaluation_fixtures,
        "_preflight_real_canary",
        lambda: None,
    )
    monkeypatch.setattr(
        evaluation_fixtures,
        "_list_real_dtu_instances",
        lambda: next(snapshots),
    )

    def fake_attractor(*, target: Path, log_root: Path, **_: object):
        rc = target / ".rc"
        rc.mkdir()
        (rc / "profile.yaml").write_text(
            "base:\n  image: ubuntu:24.04\n", encoding="utf-8"
        )
        (rc / "base_url.txt").write_text("\n", encoding="utf-8")
        (rc / "feedback.txt").write_text("demo-cli:self-test:ok\n", encoding="utf-8")
        (rc / "verdict.json").write_text(
            json.dumps(
                {
                    "verdict": "pass",
                    "outcome_class": "target_behavior_pass",
                    "findings": "demo-cli:self-test:ok",
                }
            ),
            encoding="utf-8",
        )
        (rc / "requested_name.txt").write_text("canary-requested\n", encoding="utf-8")
        (rc / "launch.json").write_text(
            json.dumps({"id": "canary-sut", "name": "canary-sut"}),
            encoding="utf-8",
        )
        (rc / "dut_plan.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "setup_commands": [],
                    "deploy_command": "./demo-cli --self-test",
                    "validation_command": "./demo-cli --self-test",
                    "port": None,
                }
            ),
            encoding="utf-8",
        )
        (rc / "target_validation.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "run_token": "token",
                    "container": "canary-sut",
                    "command": "./demo-cli --self-test",
                    "outer_exit_code": 0,
                    "exit_code": 0,
                    "stdout": "demo-cli:self-test:ok\n",
                    "stderr": "",
                }
            ),
            encoding="utf-8",
        )
        target_validation_digest = hashlib.sha256(
            (rc / "target_validation.json").read_bytes()
        ).hexdigest()
        (rc / "cleanup.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "run_token": "token",
                    "requested_name": "canary-requested",
                    "returned_identifier": "canary-sut",
                    "destroy_identifier": "canary-sut",
                    "identity_source": "returned_identifier",
                    "attempted": True,
                    "outer_exit_code": 0,
                    "stdout": json.dumps({"id": "canary-sut", "destroyed": True}),
                    "stderr": "",
                }
            ),
            encoding="utf-8",
        )
        (rc / "deploy.out").write_text(
            json.dumps({"exit_code": 0, "stdout": "deployed", "stderr": ""}),
            encoding="utf-8",
        )
        (rc / "deploy_result.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "outer_exit_code": 0,
                    "inner_exit_code": 0,
                    "stdout": "deployed",
                    "stderr": "",
                }
            ),
            encoding="utf-8",
        )
        log_root.mkdir(parents=True)
        (log_root / "checkpoint.json").write_text(
            json.dumps(
                {
                    "completed_nodes": [
                        "Start",
                        "NormalizeDUTPlan",
                        "NormalizeDUTPlanStrict",
                        "LaunchDTUStrict",
                        "ParseHandleStrict",
                        "PushSUTStrict",
                        "DeployStrict",
                        "PersistDeployOK",
                        "ExecuteTargetValidationStrict",
                        "Validate",
                        "VerifyTargetValidationEvidence",
                        "CleanupIdentityStrict",
                        "TerminalLatch",
                    ],
                    "context": {
                        "outcome": "success",
                        "sut_base_url": "",
                        "run_token": "token",
                        "target_validation_digest": target_validation_digest,
                    },
                }
            ),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(
            ["attractor", "run"], 0, stdout="canary passed\n", stderr=""
        )

    monkeypatch.setattr(
        evaluation_fixtures,
        "_run_real_attractor_canary",
        fake_attractor,
    )

    observation = run_real_reality_canary_case(
        case_dir=evaluation_case_dir,
        scenario=scenario,
        reality_check_dot=PARENT_DOT.parent / "reality_check.dot",
    )
    (evaluation_case_dir / "observation.json").write_text(
        json.dumps(observation), encoding="utf-8"
    )
    grade = grade_case(evaluation_case_dir, scenario)

    assert grade.verdict is Verdict.PASS
    assert observation["evidence"]["profile_generated"] is True
    assert observation["evidence"]["profile_launch_succeeded"] is True
    assert observation["evidence"]["target_validation"]["stdout"] == (
        "demo-cli:self-test:ok\n"
    )
    assert observation["cleanup"]["remaining_ids"] == []
    assert observation["evidence"]["cleanup"]["destroy_identifier"] == "canary-sut"


def test_real_canary_pre_subject_failure_grades_error(
    evaluation_case_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = next(
        case
        for case in load_scenarios()["cases"]
        if case["id"] == "c5_rc_real_no_port_canary"
    )
    monkeypatch.setattr(
        evaluation_fixtures,
        "_preflight_real_canary",
        lambda: "provider credentials unavailable",
    )

    observation = run_real_reality_canary_case(
        case_dir=evaluation_case_dir,
        scenario=scenario,
        reality_check_dot=PARENT_DOT.parent / "reality_check.dot",
    )
    (evaluation_case_dir / "observation.json").write_text(
        json.dumps(observation), encoding="utf-8"
    )

    grade = grade_case(evaluation_case_dir, scenario)

    assert grade.verdict is Verdict.ERROR
    assert grade.defect_class == "harness"
    assert observation["harness_error"]["phase"] == "pre_subject"


def test_controlled_validate_rewrite_breaks_production_integrity_seal(
    evaluation_case_dir: Path,
) -> None:
    scenario = dict(
        next(
            case
            for case in load_scenarios()["cases"]
            if case["id"] == "c3_rc_no_port_valid_lifecycle"
        )
    )
    scenario["rewrite_target_validation"] = True

    _capture_case(evaluation_case_dir, scenario)
    observation = json.loads(
        (evaluation_case_dir / "observation.json").read_text(encoding="utf-8")
    )
    grade = grade_case(evaluation_case_dir, scenario)

    assert observation["semantic"]["class"] == "evaluation_infrastructure_failed"
    assert grade.verdict is Verdict.FAIL
    assert any(
        check.name == "target validation integrity seal" and not check.passed
        for check in grade.checks
    )


@pytest.mark.parametrize(
    "field", ("head", "index_sha256", "untracked", "submodule_state_sha256")
)
def test_mutation_grader_rejects_incomplete_typed_fingerprints(
    evaluation_case_dir: Path, field: str
) -> None:
    scenario = next(
        case
        for case in load_scenarios()["cases"]
        if case["id"] == "a4_verify_tracked_mutation"
    )
    _capture_case(evaluation_case_dir, scenario)
    path = evaluation_case_dir / "observation.json"
    observation = json.loads(path.read_text(encoding="utf-8"))
    del observation["evidence"]["reference_fingerprints_after"]["reference-a"][field]
    path.write_text(json.dumps(observation), encoding="utf-8")

    assert grade_case(evaluation_case_dir, scenario).verdict is Verdict.FAIL


def test_unchanged_reference_grader_rejects_changed_fingerprint(
    evaluation_case_dir: Path,
) -> None:
    scenario = next(
        case
        for case in load_scenarios()["cases"]
        if case["id"] == "a3_prepare_verify_unchanged"
    )
    _capture_case(evaluation_case_dir, scenario)
    path = evaluation_case_dir / "observation.json"
    observation = json.loads(path.read_text(encoding="utf-8"))
    observation["evidence"]["reference_fingerprints_after"]["reference-a"][
        "tracked_worktree_diff_sha256"
    ] = "0" * 64
    path.write_text(json.dumps(observation), encoding="utf-8")

    assert grade_case(evaluation_case_dir, scenario).verdict is Verdict.FAIL


def test_selection_uses_immutable_case_policy_not_registry_suite_tags() -> None:
    scenarios = load_scenarios()
    for case in scenarios["cases"]:
        if case["id"] == "c5_rc_real_no_port_canary":
            case["suite"] = ["controlled"]

    selected = {case["id"] for case in _selection(scenarios, "acceptance", None)}

    assert "c5_rc_real_no_port_canary" in selected


def test_source_tree_rejects_symlinked_production_and_evaluator_sources(
    tmp_path: Path,
) -> None:
    production = tmp_path / "production"
    production.mkdir()
    (production / "reality_check.dot").write_text("digraph {}", encoding="utf-8")
    production_link = tmp_path / "production-link"
    production_link.symlink_to(production, target_is_directory=True)
    with pytest.raises(ValueError, match="real directory"):
        _source_tree_files(production_link)

    evaluator = tmp_path / "evaluator"
    evaluator.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_text("pass\n", encoding="utf-8")
    (evaluator / "grade.py").symlink_to(outside)
    with pytest.raises(ValueError, match="contains a symlink"):
        _source_tree_files(evaluator)


def test_real_canary_timeout_runs_ownership_safe_postflight_cleanup(
    evaluation_case_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario = next(
        case
        for case in load_scenarios()["cases"]
        if case["id"] == "c5_rc_real_no_port_canary"
    )
    snapshots = iter(
        [
            [],
            [{"id": "run-owned", "name": "run-owned"}],
            [],
        ]
    )
    destroyed: list[str] = []
    monkeypatch.setattr(evaluation_fixtures, "_preflight_real_canary", lambda: None)
    monkeypatch.setattr(
        evaluation_fixtures, "_list_real_dtu_instances", lambda: next(snapshots)
    )
    monkeypatch.setattr(
        evaluation_fixtures,
        "_destroy_real_dtu",
        lambda identifier: (
            destroyed.append(identifier)
            or {
                "identifier": identifier,
                "outer_exit_code": 0,
                "stdout": json.dumps({"id": identifier, "destroyed": True}),
                "stderr": "",
                "response": {"id": identifier, "destroyed": True},
            }
        ),
    )

    def timeout_after_launch(*, target: Path, log_root: Path, **_: object):
        rc = target / ".rc"
        rc.mkdir()
        (rc / "launch.json").write_text(
            json.dumps({"id": "run-owned", "name": "run-owned"}), encoding="utf-8"
        )
        (rc / "cleanup.json").write_text(
            json.dumps({"returned_identifier": "run-owned"}), encoding="utf-8"
        )
        log_root.mkdir(parents=True)
        (log_root / "checkpoint.json").write_text(
            json.dumps({"context": {"dtu_container": "run-owned"}}),
            encoding="utf-8",
        )
        raise subprocess.TimeoutExpired(["attractor", "run"], 900)

    monkeypatch.setattr(
        evaluation_fixtures, "_run_real_attractor_canary", timeout_after_launch
    )

    observation = run_real_reality_canary_case(
        case_dir=evaluation_case_dir,
        scenario=scenario,
        reality_check_dot=PARENT_DOT.parent / "reality_check.dot",
    )
    (evaluation_case_dir / "observation.json").write_text(
        json.dumps(observation), encoding="utf-8"
    )

    assert destroyed == ["run-owned"]
    assert observation["harness_error"]["phase"] == "post_subject"
    assert observation["evidence"]["postflight"]["run_owned_identifier"] == "run-owned"
    assert observation["cleanup"]["remaining_ids"] == []
    assert grade_case(evaluation_case_dir, scenario).verdict is Verdict.ERROR


@pytest.mark.parametrize(
    ("case_id", "required", "forbidden"),
    (
        (
            "e1_parent_rc_pass_only_delivers",
            {"RC", "VerifyAfterRC", "Deliver", "VerifyAfterDeliver"},
            set(),
        ),
        (
            "e2_parent_rc_exhausted_no_deliver",
            {"RC", "RCExhausted"},
            {"Deliver", "BuildRCFix"},
        ),
        (
            "e3_parent_reference_prerequisite_terminal",
            {"RC", "ReferencePrerequisiteFailed"},
            {"Deliver", "BuildRCFix"},
        ),
        (
            "e4_parent_infrastructure_terminal",
            {"RC", "RCInfrastructureFailed"},
            {"Deliver", "BuildRCFix"},
        ),
        (
            "e5_parent_manifest_substitution_detected",
            {"PrepareReferences", "Plan", "VerifyAfterPlan"},
            {"Implement", "Deliver", "RC"},
        ),
    ),
)
def test_actual_parent_scenarios_run_parsed_production_graph(
    evaluation_case_dir: Path,
    case_id: str,
    required: set[str],
    forbidden: set[str],
) -> None:
    scenario = next(case for case in load_scenarios()["cases"] if case["id"] == case_id)

    observation = run_controlled_parent_case(
        case_dir=evaluation_case_dir, scenario=scenario, parent_dot=PARENT_DOT
    )
    (evaluation_case_dir / "observation.json").write_text(
        json.dumps(observation), encoding="utf-8"
    )
    grade = grade_case(evaluation_case_dir, scenario)

    completed = set(observation["subject"]["completed_nodes"])
    assert required <= completed
    assert not (forbidden & completed)
    assert observation["evidence"]["parent_checkpoint"]
    assert grade.verdict is Verdict.PASS


def test_parent_manifest_substitution_cannot_pass_from_semantic_label_alone(
    evaluation_case_dir: Path,
) -> None:
    scenario = next(
        case
        for case in load_scenarios()["cases"]
        if case["id"] == "e5_parent_manifest_substitution_detected"
    )
    observation = run_controlled_parent_case(
        case_dir=evaluation_case_dir, scenario=scenario, parent_dot=PARENT_DOT
    )
    observation["evidence"]["verify_failure_reason"] = ""
    (evaluation_case_dir / "observation.json").write_text(
        json.dumps(observation), encoding="utf-8"
    )

    assert grade_case(evaluation_case_dir, scenario).verdict is Verdict.FAIL
