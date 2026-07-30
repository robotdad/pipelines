"""Structural and behavioral contracts for expert_builder reference wiring.

These tests deliberately inspect parsed DOT structure and embedded stage contracts. They
must stay offline: DTU launches, public dependency installation, and LLM execution are
covered by later acceptance tests rather than this wiring suite.
"""

from __future__ import annotations

import ast
import asyncio
import errno
import hashlib
import json
import os
import re
import shutil
import threading
import time
from collections.abc import Iterable, Iterator
from pathlib import Path
from uuid import uuid4

import pytest
from amplifier_module_loop_pipeline.context import PipelineContext
from amplifier_module_loop_pipeline.dot_parser import parse_dot
from amplifier_module_loop_pipeline.handlers.codergen import _expand_variables
from amplifier_module_loop_pipeline.handlers.tool import ToolHandler
from amplifier_module_loop_pipeline.outcome import Outcome, StageStatus
from amplifier_module_loop_pipeline.validation import validate, validate_or_raise

ROOT = Path(__file__).parents[1]
WORK_ROOT = ROOT.parent / ".work"
PACKAGE = ROOT / "expert_builder"
PARENT_DOT = PACKAGE / "expert_builder.dot"

EXPECTED_VERIFY_NODES = {
    "VerifyAfterPlan",
    "VerifyAfterImplement",
    "VerifyAfterUserRun",
    "VerifyAfterRC",
    "VerifyAfterDeliver",
}


@pytest.fixture
def workspace_tmp_path(request: pytest.FixtureRequest) -> Iterator[Path]:
    """Create an isolated executable fixture beneath the workspace-owned .work tree."""
    root = WORK_ROOT / "pytest"
    root.mkdir(parents=True, exist_ok=True)
    stem = re.sub(r"[^A-Za-z0-9]+", "-", request.node.name)[:70]
    path = root / f"{stem}-{uuid4().hex}"
    path.mkdir()
    assert path.resolve().is_relative_to(WORK_ROOT.resolve())
    try:
        yield path
    finally:
        shutil.rmtree(path)


def graph(name: str):
    """Parse and validate one package-local DOT graph before asserting its contract."""
    path = PACKAGE / name
    parsed = parse_dot(path.read_text(encoding="utf-8"))
    parsed.source_dir = str(path.parent)
    validate_or_raise(parsed)
    return parsed


def source_node_block(graph_name: str, node_id: str) -> str:
    """Return one source node body so duplicate attributes cannot hide in parsing."""
    source = (PACKAGE / graph_name).read_text(encoding="utf-8")
    match = re.search(
        rf"(?ms)^\s*{re.escape(node_id)}\s*\[(?P<body>.*?)^\s*\];",
        source,
    )
    assert match, f"missing source block for {graph_name}:{node_id}"
    return match.group("body")


def execute_tool_node(
    graph_name: str,
    node_id: str,
    target: Path,
    context: PipelineContext | None = None,
    **values: object,
) -> tuple[Outcome, PipelineContext]:
    """Execute one deterministic node with logs contained by a workspace fixture."""
    assert target.resolve().is_relative_to(WORK_ROOT.resolve())
    parsed = graph(graph_name)
    selected = node(parsed, node_id)
    active = context or PipelineContext()
    active.set("context.target_dir", str(target))
    if (
        graph_name == "reality_check.dot"
        and node_id == "InitializeReferenceDependencyPlan"
        and "reference_manifest_digest" not in values
        and isinstance(values.get("references_manifest_path"), str)
    ):
        manifest = Path(str(values["references_manifest_path"]))
        values["reference_manifest_digest"] = hashlib.sha256(
            manifest.read_bytes()
        ).hexdigest()
    for key, value in values.items():
        active.set(key, value)
    outcome = asyncio.run(
        ToolHandler().execute(selected, active, parsed, str(target / ".test-logs"))
    )
    return outcome, active


def write_empty_references_manifest(target: Path) -> tuple[Path, str]:
    """Write a minimal current target-owned manifest and return path plus byte
    digest.
    """
    manifest = target / ".ai" / "references.json"
    manifest.parent.mkdir(parents=True)
    payload = {
        "schema_version": 1,
        "target_root": str(target.resolve()),
        "references": [],
    }
    raw = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()
    manifest.write_bytes(raw)
    return manifest.resolve(), hashlib.sha256(raw).hexdigest()


def node(parsed, node_id: str):
    assert node_id in parsed.nodes, f"missing node {node_id!r}"
    return parsed.nodes[node_id]


def node_attr(parsed, node_id: str, name: str) -> str:
    value = node(parsed, node_id).attrs.get(name)
    assert value is not None, f"{node_id} is missing {name!r}"
    return str(value)


def node_text(parsed, node_id: str) -> str:
    selected = node(parsed, node_id)
    return "\n".join(
        part
        for part in (
            selected.label,
            selected.prompt,
            *(str(value) for value in selected.attrs.values()),
        )
        if part
    )


def assert_edge(parsed, source: str, target: str, condition: str = "") -> None:
    assert any(
        edge.from_node == source
        and edge.to_node == target
        and (edge.condition or "") == condition
        for edge in parsed.edges
    ), f"missing edge {source} -> {target} [{condition}]"


def direct_targets(parsed, source: str) -> set[str]:
    return {edge.to_node for edge in parsed.edges if edge.from_node == source}


def reachable_from(parsed, start: str) -> set[str]:
    seen: set[str] = set()
    pending = [start]
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        pending.extend(direct_targets(parsed, current) - seen)
    return seen


def assert_terms(text: str, terms: Iterable[str], *, subject: str) -> None:
    lowered = text.casefold()
    missing = [term for term in terms if term.casefold() not in lowered]
    assert not missing, f"{subject} is missing contract terms: {missing!r}"


def embedded_python(parsed, node_id: str) -> ast.Module:
    """Parse the Python heredoc in a deterministic node without executing it."""
    command = node_attr(parsed, node_id, "tool_command")
    match = re.search(r"<<'PYEOF'\n(?P<body>.*?)\nPYEOF", command, re.DOTALL)
    assert match, (
        f"{node_id} must contain a Python heredoc for its deterministic contract"
    )
    return ast.parse(match.group("body"))


def ast_strings(tree: ast.AST) -> set[str]:
    return {
        value.value
        for value in ast.walk(tree)
        if isinstance(value, ast.Constant) and isinstance(value.value, str)
    }


def if_test_reads_key_directly(test: ast.AST, key: str) -> bool:
    """Detect plan['key'] or plan.get('key') without depending on source whitespace."""
    for value in ast.walk(test):
        if isinstance(value, ast.Subscript):
            slice_value = value.slice
            if isinstance(slice_value, ast.Constant) and slice_value.value == key:
                return True
        if (
            isinstance(value, ast.Call)
            and isinstance(value.func, ast.Attribute)
            and value.func.attr == "get"
            and value.args
        ):
            first = value.args[0]
            if isinstance(first, ast.Constant) and first.value == key:
                return True
    return False


def reader_rejects_nonempty_key(tree: ast.AST, key: str) -> bool:
    """Require a direct evidence-key guard whose rejection body fails or routes
    closed.
    """
    for branch in (value for value in ast.walk(tree) if isinstance(value, ast.If)):
        if not if_test_reads_key_directly(branch.test, key):
            continue
        body_text = " ".join(ast_strings(ast.Module(body=branch.body, type_ignores=[])))
        body_calls = {
            call.func.id
            for call in ast.walk(ast.Module(body=branch.body, type_ignores=[]))
            if isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
        }
        if "fail" in body_calls or "reference_prerequisite_failed" in body_text:
            return True
    return False


def test_all_affected_graphs_parse_and_have_one_exit() -> None:
    for name in (
        "expert_builder.dot",
        "plan.dot",
        "implement_loop.dot",
        "reality_check.dot",
        "deliver.dot",
    ):
        parsed = graph(name)
        exits = [item.id for item in parsed.nodes.values() if item.shape == "Msquare"]
        assert exits == ["Exit"] or exits == ["done"], (
            f"{name} must retain exactly one Msquare exit; found {exits!r}"
        )


def test_repaired_production_graphs_have_zero_diagnostics_no_obsolete_nodes_and_strict_topology() -> (
    None
):
    parent = graph("expert_builder.dot")
    reality = graph("reality_check.dot")

    assert validate(parent) == []
    assert validate(reality) == []
    assert "RCClassify" not in parent.nodes
    assert not (
        {
            "Deploy",
            "HardFailureTeardown",
            "InstallReferenceDependencies",
            "LaunchDTU",
            "ParseHandle",
            "PushSUT",
            "RouteHandle",
            "TeardownFail",
            "TeardownOK",
        }
        & set(reality.nodes)
    )
    assert {item.id for item in parent.nodes.values() if item.shape == "Msquare"} == {
        "done"
    }
    assert {item.id for item in reality.nodes.values() if item.shape == "Msquare"} == {
        "Exit"
    }
    assert_edge(parent, "RC", "VerifyAfterRC")
    assert_edge(parent, "VerifyAfterRC", "RCClassifyStrict")
    assert_edge(parent, "CheckRC", "RCExhausted", "context.rc_state=rc_exhausted")
    assert_edge(parent, "RCExhausted", "done")
    assert_edge(
        reality,
        "LaunchDTUStrict",
        "ParseHandleStrict",
        "context.tool.last_line=launch_ok",
    )
    assert_edge(reality, "CleanupIdentityStrict", "TerminalLatch")
    assert_edge(reality, "TerminalLatch", "Exit", "context.outcome=success")
    assert_edge(reality, "TerminalLatch", "Exit", "context.outcome=fail")


def test_parent_documents_and_prepares_optional_references_after_admission() -> None:
    parent = graph("expert_builder.dot")
    source = PARENT_DOT.read_text(encoding="utf-8")

    assert_terms(
        source,
        ("references", "optional", "json"),
        subject="expert_builder input contract",
    )
    prepare = node(parent, "PrepareReferences")
    assert prepare.shape == "folder"
    assert prepare.attrs.get("dot_file") == "references/prepare.dot"
    assert prepare.attrs.get("outputs") == "reference_state,reference_manifest_digest"
    assert_edge(
        parent, "CheckAdmit", "PrepareReferences", "context.admit_state=admitted"
    )
    assert_edge(parent, "PrepareReferences", "Plan")
    assert "Plan" not in direct_targets(parent, "CheckAdmit")


@pytest.mark.parametrize("node_id", sorted(EXPECTED_VERIFY_NODES))
def test_parent_declares_reference_integrity_folder_nodes(node_id: str) -> None:
    parent = graph("expert_builder.dot")
    verifier = node(parent, node_id)
    assert verifier.shape == "folder"
    assert verifier.attrs.get("dot_file") == "references/verify.dot"
    assert verifier.attrs.get("outputs") == "reference_integrity_state"


@pytest.mark.parametrize(
    ("stage", "verifier", "successor"),
    (
        ("Plan", "VerifyAfterPlan", "CheckPlan"),
        ("Implement", "VerifyAfterImplement", "CheckImpl"),
        ("UserRun", "VerifyAfterUserRun", "ReadVerdict"),
        ("RC", "VerifyAfterRC", "RCClassifyStrict"),
        ("Deliver", "VerifyAfterDeliver", "done"),
    ),
)
def test_successful_stage_paths_cannot_bypass_reference_verification(
    stage: str, verifier: str, successor: str
) -> None:
    parent = graph("expert_builder.dot")
    assert direct_targets(parent, stage) == {verifier}, (
        f"{stage} must flow only through {verifier} on its successful path"
    )
    assert_edge(parent, verifier, successor)


def test_verifier_edges_only_continue_on_success_and_never_route_failure_to_repair() -> None:  # fmt: skip
    parent = graph("expert_builder.dot")
    intended_successors = {
        "VerifyAfterPlan": "CheckPlan",
        "VerifyAfterImplement": "CheckImpl",
        "VerifyAfterUserRun": "ReadVerdict",
        "VerifyAfterRC": "RCClassifyStrict",
        "VerifyAfterDeliver": "done",
    }
    repair_nodes = {"Implement", "Reopen", "BuildRCFix"}

    for verifier, successor in intended_successors.items():
        node(parent, verifier)
        outgoing = [edge for edge in parent.edges if edge.from_node == verifier]
        assert len(outgoing) == 1, (
            f"{verifier} must have exactly one unconditional success successor"
        )
        edge = outgoing[0]
        assert edge.to_node == successor
        assert not edge.condition
        runs_on = str(edge.attrs.get("runs_on", "success")).casefold()
        assert runs_on == "success", (
            f"{verifier} -> {successor} must run only on success, not {runs_on!r}"
        )
        assert edge.to_node not in repair_nodes, (
            f"{verifier} must not directly route failure or a condition "
            "to target repair"
        )


def test_parent_hands_an_absolute_reference_manifest_to_reality_check() -> None:
    parent = graph("expert_builder.dot")
    command = node_attr(parent, "PrepareRC", "tool_command")
    assert_terms(
        command,
        ("references_manifest_path", ".ai/references.json"),
        subject="PrepareRC reference-manifest handoff",
    )
    assert any(marker in command for marker in ("abspath", ".resolve()", "Path(")), (
        "PrepareRC must publish an absolute references_manifest_path, "
        "not a cwd-relative path"
    )
    assert (
        "references_manifest_path" in node_text(parent, "RC")
        or "references_manifest_path" in command
    )


def test_parent_classifies_reference_prerequisites_before_repair_rounds() -> None:
    parent = graph("expert_builder.dot")
    command = node_attr(parent, "RCClassifyStrict", "tool_command")
    tree = embedded_python(parent, "RCClassifyStrict")
    constants = ast_strings(tree)

    assert "reference_prerequisite_failed" in constants
    assert "outcome_class" in command
    assert command.index("outcome_class") < command.index("rc_rounds"), (
        "RCClassify must inspect outcome_class before incrementing RC repair rounds"
    )
    assert_edge(
        parent,
        "CheckRC",
        "ReferencePrerequisiteFailed",
        "context.rc_state=reference_prerequisite_failed",
    )
    terminal = node(parent, "ReferencePrerequisiteFailed")
    assert terminal.shape == "parallelogram"
    terminal_command = node_attr(parent, "ReferencePrerequisiteFailed", "tool_command")
    assert_terms(
        terminal_command,
        ("reference_prerequisite_failed", "sys.exit"),
        subject="ReferencePrerequisiteFailed terminal tool",
    )
    assert direct_targets(parent, "ReferencePrerequisiteFailed") == {"done"}
    assert_edge(
        parent,
        "ReferencePrerequisiteFailed",
        "done",
        "context.outcome=fail",
    )
    assert not direct_targets(parent, "ReferencePrerequisiteFailed") & {
        "Implement",
        "Reopen",
        "BuildRCFix",
    }


def test_parent_preserves_normal_reality_check_routes() -> None:
    parent = graph("expert_builder.dot")
    rc = node(parent, "RC")
    assert rc.attrs.get("continue_on_fail") == "true"
    assert_edge(parent, "CheckRC", "Deliver", "context.rc_state=rc_pass")
    assert_edge(parent, "CheckRC", "BuildRCFix", "context.rc_state=rc_fix")
    assert_edge(parent, "CheckRC", "RCExhausted", "context.rc_state=rc_exhausted")
    assert_edge(parent, "RCExhausted", "done")
    assert not any(
        edge.from_node == "CheckRC"
        and edge.to_node == "done"
        and edge.condition == "context.rc_state=rc_exhausted"
        for edge in parent.edges
    )
    assert_edge(
        parent,
        "CheckRC",
        "RCInfrastructureFailed",
        "context.rc_state=rc_infrastructure_failed",
    )


@pytest.mark.parametrize(
    ("graph_name", "node_id", "extra_terms"),
    (
        ("plan.dot", "Plan", ("task", "reference")),
        ("implement_loop.dot", "Implement", ("implement", "reference")),
        ("expert_builder.dot", "UserRun", ("validation", "reference")),
        ("deliver.dot", "Deliver", ("delivery", "reference")),
    ),
)
def test_tool_capable_stages_receive_reference_context_and_single_target_boundary(
    graph_name: str, node_id: str, extra_terms: tuple[str, ...]
) -> None:
    stage_graph = graph(graph_name)
    prompt = node_text(stage_graph, node_id)
    assert_terms(
        prompt,
        (
            ".ai/reference_context.md",
            "read-only",
            "only",
            "target",
            *extra_terms,
        ),
        subject=f"{graph_name}:{node_id} prompt",
    )


def test_plan_explicitly_forbids_reference_modification_tasks() -> None:
    plan = graph("plan.dot")
    prompt = node_text(plan, "Plan").casefold()
    assert "reference" in prompt
    assert any(
        phrase in prompt
        for phrase in ("do not modify", "must not modify", "cannot modify")
    ), "Plan must forbid implementation tasks that modify reference repositories"


def test_deliver_excludes_transient_reference_artifacts_from_staging() -> None:
    deliver = graph("deliver.dot")
    command = node_attr(deliver, "DeliverFinalize", "tool_command")
    assert command.index("git', 'add', '-A'") < command.index("git', 'reset'")
    assert_terms(
        command,
        (
            ".ai/references.json",
            ".ai/reference_context.md",
            ".rc/reference_dependencies.json",
            "git', 'reset'",
            "staged",
        ),
        subject="DeliverFinalize staging exclusion",
    )


def test_reality_check_accepts_and_plans_reference_dependencies_before_dut_detection() -> (
    None
):
    reality = graph("reality_check.dot")
    source = (PACKAGE / "reality_check.dot").read_text(encoding="utf-8")
    assert_terms(
        source,
        ("references_manifest_path", ".rc/reference_dependencies.json"),
        subject="reality_check input and artifact contract",
    )
    assert_edge(reality, "Start", "InitializeReferenceDependencyPlan")
    assert_edge(
        reality, "InitializeReferenceDependencyPlan", "PlanReferenceDependencies"
    )
    assert_edge(reality, "PlanReferenceDependencies", "ReadReferenceDependencyPlan")
    assert_edge(reality, "ReadReferenceDependencyPlan", "CheckReferenceDependencies")
    assert_edge(
        reality,
        "CheckReferenceDependencies",
        "DetectDUTPlan",
        "context.tool.last_line=ready",
    )
    assert_edge(reality, "DetectDUTPlan", "NormalizeDUTPlan")
    assert_edge(
        reality,
        "NormalizeDUTPlan",
        "NormalizeDUTPlanStrict",
        "context.tool.last_line=plan_valid",
    )
    assert_edge(
        reality,
        "NormalizeDUTPlanStrict",
        "LaunchDTUStrict",
        "context.tool.last_line=plan_strict_valid",
    )


@pytest.mark.parametrize(
    ("payload", "expected_error"),
    (
        (
            {
                "schema_version": 1,
                "setup_cmds": [],
                "deploy_cmd": "true",
                "validation_cmd": "./app --self-test",
                "sut_port": None,
            },
            "exact five-field schema",
        ),
        (
            {
                "schema_version": 1,
                "setup_commands": [],
                "deploy_command": "true",
                "validation_command": "./app --self-test",
                "port": None,
                "profile": {"image": "ubuntu:24.04"},
            },
            "exact five-field schema",
        ),
        (
            {
                "schema_version": 1,
                "setup_commands": [],
                "deploy_command": "/opt/application/start",
                "validation_command": "./app --self-test",
                "port": None,
            },
            "absolute filesystem path",
        ),
    ),
)
def test_normalize_dut_plan_rejects_obsolete_extra_and_absolute_deploy_fields(
    workspace_tmp_path: Path,
    payload: dict[str, object],
    expected_error: str,
) -> None:
    target = workspace_tmp_path / "target"
    rc = target / ".rc"
    rc.mkdir(parents=True)
    (rc / "dut_plan.json").write_text(
        json.dumps(payload, indent=2) + "\n", encoding="utf-8"
    )

    outcome, context = execute_tool_node(
        "reality_check.dot",
        "NormalizeDUTPlan",
        target,
        software_path=str(target),
    )

    assert outcome.status == StageStatus.SUCCESS
    assert context.get("tool.last_line") == "infrastructure_failed"
    assert expected_error in (rc / "transport.err").read_text(encoding="utf-8")
    assert not (rc / "profile.yaml").exists()


@pytest.mark.parametrize(
    "validation_command",
    (
        "//bin/true",
        "../escape --test",
        "bash -c 'cat /etc/passwd'",
        "{host}/bin/tool --test",
    ),
)
def test_normalize_dut_plan_rejects_recursive_validation_path_escapes(
    workspace_tmp_path: Path, validation_command: str
) -> None:
    target = workspace_tmp_path / "target"
    rc = target / ".rc"
    rc.mkdir(parents=True)
    command = validation_command.format(host=target)
    (rc / "dut_plan.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "setup_commands": [],
                "deploy_command": "./app --deploy",
                "validation_command": command,
                "port": None,
            }
        ),
        encoding="utf-8",
    )
    _, context = execute_tool_node(
        "reality_check.dot", "NormalizeDUTPlan", target, software_path=str(target)
    )
    assert context.get("tool.last_line") == "infrastructure_failed"


@pytest.mark.parametrize("port", (True, 0, 65536, "8080"))
def test_normalize_dut_plan_rejects_invalid_port_types_and_ranges(
    workspace_tmp_path: Path, port: object
) -> None:
    target = workspace_tmp_path / "target"
    rc = target / ".rc"
    rc.mkdir(parents=True)
    (rc / "dut_plan.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "setup_commands": [],
                "deploy_command": "./app --deploy",
                "validation_command": "./app --self-test",
                "port": port,
            }
        ),
        encoding="utf-8",
    )
    _, context = execute_tool_node(
        "reality_check.dot", "NormalizeDUTPlan", target, software_path=str(target)
    )
    assert context.get("tool.last_line") == "infrastructure_failed"


def test_normalize_dut_plan_rejects_symlink_fifo_and_nonregular_outputs(
    workspace_tmp_path: Path,
) -> None:
    target = workspace_tmp_path / "target"
    rc = target / ".rc"
    rc.mkdir(parents=True)
    plan = rc / "dut_plan.json"
    fifo = rc / "fifo-plan"
    os.mkfifo(fifo)
    plan.symlink_to(fifo)
    _, context = execute_tool_node(
        "reality_check.dot", "NormalizeDUTPlan", target, software_path=str(target)
    )
    assert context.get("tool.last_line") == "infrastructure_failed"
    plan.unlink()
    plan = rc / "dut_plan.json"
    os.mkfifo(plan)
    writer_errors: list[BaseException] = []

    def write_fifo() -> None:
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            try:
                descriptor = os.open(plan, os.O_WRONLY | os.O_NONBLOCK)
            except OSError as error:
                if error.errno == errno.ENXIO:
                    time.sleep(0.01)
                    continue
                writer_errors.append(error)
                return
            try:
                os.write(descriptor, b"{}")
                return
            except OSError as error:
                writer_errors.append(error)
                return
            finally:
                os.close(descriptor)
        writer_errors.append(TimeoutError("FIFO reader did not become ready"))

    writer = threading.Thread(target=write_fifo)
    writer.start()
    _, context = execute_tool_node(
        "reality_check.dot", "NormalizeDUTPlan", target, software_path=str(target)
    )
    writer.join(timeout=2)
    assert not writer.is_alive()
    assert writer_errors == []
    assert context.get("tool.last_line") == "infrastructure_failed"
    plan.unlink()
    profile = rc / "profile.yaml"
    profile.mkdir()
    plan.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "setup_commands": [],
                "deploy_command": "./app --deploy",
                "validation_command": "./app --self-test",
                "port": None,
            }
        ),
        encoding="utf-8",
    )
    _, context = execute_tool_node(
        "reality_check.dot", "NormalizeDUTPlan", target, software_path=str(target)
    )
    assert context.get("tool.last_line") == "infrastructure_failed"
    profile.rmdir()
    profile.symlink_to(rc / "dut_plan.json")
    _, context = execute_tool_node(
        "reality_check.dot", "NormalizeDUTPlan", target, software_path=str(target)
    )
    assert context.get("tool.last_line") == "infrastructure_failed"


def test_normalize_dut_plan_rejects_symlinked_software_path(
    workspace_tmp_path: Path,
) -> None:
    target = workspace_tmp_path / "target"
    target.mkdir()
    link = workspace_tmp_path / "target-link"
    link.symlink_to(target, target_is_directory=True)
    rc = target / ".rc"
    rc.mkdir()
    (rc / "dut_plan.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "setup_commands": [],
                "deploy_command": "./app --deploy",
                "validation_command": "./app --self-test",
                "port": None,
            }
        ),
        encoding="utf-8",
    )
    _, context = execute_tool_node(
        "reality_check.dot", "NormalizeDUTPlan", target, software_path=str(link)
    )
    assert context.get("tool.last_line") == "infrastructure_failed"


def test_normalize_dut_plan_valid_no_port_generates_fixed_bound_artifacts(
    workspace_tmp_path: Path,
) -> None:
    target = workspace_tmp_path / "target"
    rc = target / ".rc"
    rc.mkdir(parents=True)
    plan = {
        "schema_version": 1,
        "setup_commands": [],
        "deploy_command": "./app --deploy",
        "validation_command": "./app --self-test",
        "port": None,
    }
    (rc / "dut_plan.json").write_text(json.dumps(plan), encoding="utf-8")
    _, context = execute_tool_node(
        "reality_check.dot", "NormalizeDUTPlan", target, software_path=str(target)
    )
    assert context.get("tool.last_line") == "plan_valid"
    assert (rc / "sut_port.txt").read_text().strip() == "none"
    assert (rc / "remote_root.txt").read_text().strip() == f"/sut/{target.name}"
    assert "ubuntu:24.04" in (rc / "profile.yaml").read_text()
    assert f"/sut/{target.name}" in (rc / "deploy_cmd.sh").read_text()
    assert "./app --self-test" in (rc / "validation_cmd.sh").read_text()


def test_reality_check_reference_planning_uses_documented_public_paths_only() -> None:
    reality = graph("reality_check.dot")
    planning = node_text(reality, "PlanReferenceDependencies")
    assert_terms(
        planning,
        (
            "use_in_validation",
            "caller order",
            "documentation",
            "section",
            "recommended",
            "default",
            "public",
            "missing",
            "ambiguous",
            "reference_prerequisite_failed",
        ),
        subject="PlanReferenceDependencies contract",
    )
    assert "local checkout" in planning.casefold()
    assert any(
        phrase in planning.casefold()
        for phrase in (
            "never install from",
            "do not install from",
            "must not install from",
        )
    ), "reference planning must forbid installation from the inspected local checkout"


def test_reality_check_dependency_reader_fails_closed_and_routes_only_two_states() -> (
    None
):
    reality = graph("reality_check.dot")
    reader = node_text(reality, "ReadReferenceDependencyPlan")
    assert_terms(
        reader,
        (
            ".rc/reference_dependencies.json",
            "schema",
            "use_in_validation",
            "caller order",
            "reference_prerequisite_failed",
        ),
        subject="ReadReferenceDependencyPlan contract",
    )
    assert_edge(
        reality,
        "CheckReferenceDependencies",
        "RenderReferencePrerequisiteFailure",
        "context.tool.last_line=reference_prerequisite_failed",
    )
    assert direct_targets(reality, "CheckReferenceDependencies") == {
        "DetectDUTPlan",
        "RenderReferencePrerequisiteFailure",
    }


def test_reality_check_installs_dependencies_after_push_before_deploy_and_records_setup() -> (
    None
):
    reality = graph("reality_check.dot")
    assert_edge(
        reality,
        "PushSUTStrict",
        "InstallTargetToolchainStrict",
        "context.tool.last_line=push_ok",
    )
    assert_edge(
        reality,
        "InstallTargetToolchainStrict",
        "InstallReferenceDependenciesStrict",
        "context.tool.last_line=toolchain_ok",
    )
    assert_edge(
        reality,
        "InstallReferenceDependenciesStrict",
        "DeployStrict",
        "context.tool.last_line=setup_ok",
    )
    assert_edge(
        reality,
        "InstallReferenceDependenciesStrict",
        "RenderInfrastructureFailure",
        "context.tool.last_line=infrastructure_failed",
    )
    install = node_text(reality, "InstallReferenceDependenciesStrict")
    assert_terms(
        install,
        (
            "reference_dependencies.json",
            "setup_steps",
            "setup_results",
            "outer_exit_code",
            "exit_code",
            "exit",
            "stdout",
            "stderr",
        ),
        subject="InstallReferenceDependencies contract",
    )


def test_reality_check_validation_records_ordered_dependency_use_evidence() -> None:
    reality = graph("reality_check.dot")
    prompt = node_text(reality, "Validate")
    assert_terms(
        prompt,
        (
            ".rc/reference_dependencies.json",
            "target_validation.json",
            "already run exactly once",
            "do not execute",
            "deployed target",
        ),
        subject="Validate dependency evidence contract",
    )


@pytest.mark.parametrize(
    "node_id",
    ("RenderPass", "RenderPartial", "SetFail", "RenderReferencePrerequisiteFailure"),
)
def test_every_reality_check_verdict_embeds_outcome_and_dependency_evidence(
    node_id: str,
) -> None:
    reality = graph("reality_check.dot")
    command = node_attr(reality, node_id, "tool_command")
    assert_terms(
        command,
        ("outcome_class", "reference_dependencies", "reference_dependencies.json"),
        subject=f"{node_id} verdict renderer",
    )


def test_ordinary_target_behavior_verdicts_retain_existing_routes() -> None:
    reality = graph("reality_check.dot")
    assert_edge(
        reality,
        "ReadVerdict",
        "RenderPass",
        "context.tool.last_line=pass",
    )
    assert_edge(
        reality,
        "ReadVerdict",
        "RenderPartial",
        "context.tool.last_line=partial",
    )
    assert_edge(
        reality,
        "ReadVerdict",
        "SetFail",
        "context.tool.last_line=fail",
    )


def test_prerequisite_renderer_fails_then_tears_down_without_target_repair() -> None:
    reality = graph("reality_check.dot")
    command = node_attr(reality, "RenderReferencePrerequisiteFailure", "tool_command")
    assert_terms(
        command,
        (
            '"verdict": "fail"',
            '"outcome_class": "reference_prerequisite_failed"',
            "reference_prerequisite",
        ),
        subject="RenderReferencePrerequisiteFailure verdict",
    )
    assert_edge(
        reality,
        "RenderReferencePrerequisiteFailure",
        "EmbedTargetValidationEvidence",
    )
    assert "Deploy" not in reachable_from(reality, "RenderReferencePrerequisiteFailure")
    assert "Validate" not in reachable_from(
        reality, "RenderReferencePrerequisiteFailure"
    )


# Integrated-review regressions: freshness, evidence, delivery boundary, prompt
# preservation.


def test_reference_dependency_run_is_reset_and_bound_before_planning() -> None:
    reality = graph("reality_check.dot")
    first = direct_targets(reality, "Start")
    assert len(first) == 1
    initializer_id = next(iter(first))
    assert initializer_id != "PlanReferenceDependencies", (
        "RealityCheck must initialize a fresh dependency-planning run before the agent"
    )
    initializer = node(reality, initializer_id)
    assert initializer.shape == "parallelogram"
    assert direct_targets(reality, initializer_id) == {
        "PlanReferenceDependencies",
        "RenderInfrastructureFailure",
    }
    assert_edge(reality, initializer_id, "PlanReferenceDependencies")
    assert_edge(
        reality,
        initializer_id,
        "RenderInfrastructureFailure",
        "context.outcome=fail",
    )
    command = node_attr(reality, initializer_id, "tool_command")
    assert_terms(
        command,
        (
            ".rc/reference_dependencies.json",
            "run_token",
            "references_manifest_path",
            "digest",
            "sha256",
            "resolve",
        ),
        subject="fresh reference-dependency run initializer",
    )
    assert any(term in command for term in ("unlink", "remove", "truncate")), (
        "initializer must prevent reuse of a prior dependency plan"
    )


def test_dependency_planner_and_reader_share_exact_freshness_binding() -> None:
    reality = graph("reality_check.dot")
    planning = node_text(reality, "PlanReferenceDependencies")
    reader = node_attr(reality, "ReadReferenceDependencyPlan", "tool_command")
    binding_terms = (
        "references_manifest_path",
        "references_manifest_digest",
        "run_token",
    )
    assert_terms(
        planning, binding_terms, subject="dependency planner freshness binding"
    )
    assert_terms(reader, binding_terms, subject="dependency reader freshness binding")
    assert_terms(
        reader,
        ("resolve", "sha256", "setup_results", "use_results"),
        subject="dependency reader current-input validation",
    )
    assert any(
        phrase in reader
        for phrase in (
            "if plan['setup_results']",
            "if plan.get('setup_results')",
            "setup_results != []",
        )
    ), "pre-setup reader must reject a plan that already contains setup evidence"
    assert any(
        phrase in reader
        for phrase in (
            "if plan['use_results']",
            "if plan.get('use_results')",
            "use_results != []",
        )
    ), "pre-setup reader must reject a plan that already contains use evidence"


def test_fresh_run_initializer_removes_a_prior_dependency_plan(
    workspace_tmp_path: Path,
) -> None:
    target = workspace_tmp_path / "target"
    target.mkdir()
    manifest_path, _ = write_empty_references_manifest(target)
    stale_plan = target / ".rc" / "reference_dependencies.json"
    stale_plan.parent.mkdir()
    stale_plan.write_text('{"state":"ready","stale":true}\n', encoding="utf-8")

    reality = graph("reality_check.dot")
    initializer_ids = direct_targets(reality, "Start")
    assert len(initializer_ids) == 1
    initializer_id = next(iter(initializer_ids))
    assert initializer_id != "PlanReferenceDependencies"
    assert node(reality, initializer_id).shape == "parallelogram"
    outcome, _ = execute_tool_node(
        "reality_check.dot",
        initializer_id,
        target,
        references_manifest_path=str(manifest_path),
    )
    assert outcome.status == StageStatus.SUCCESS
    assert not stale_plan.exists(), (
        "a prior run's dependency plan must not survive reset"
    )


def test_dependency_reader_rejects_a_stale_prior_run_plan(
    workspace_tmp_path: Path,
) -> None:
    target = workspace_tmp_path / "target"
    target.mkdir()
    manifest_path, _ = write_empty_references_manifest(target)
    plan_path = target / ".rc" / "reference_dependencies.json"
    plan_path.parent.mkdir()
    # This is the formerly-valid unbound schema. A current-run reader must
    # reject it rather than accept a prior run merely because paths and
    # dependency IDs still happen to match.
    stale_plan = {
        "schema_version": 1,
        "state": "ready",
        "references_manifest_path": str(manifest_path),
        "dependencies": [],
        "prerequisites": [],
        "setup_results": [],
        "use_results": [],
    }
    plan_path.write_text(
        json.dumps(stale_plan, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    outcome, context = execute_tool_node(
        "reality_check.dot",
        "ReadReferenceDependencyPlan",
        target,
        references_manifest_path=str(manifest_path),
    )
    assert outcome.status == StageStatus.SUCCESS
    assert context.get("tool.last_line") == "reference_prerequisite_failed"


def test_mechanical_use_evidence_gate_is_between_validate_and_verdict() -> None:
    reality = graph("reality_check.dot")
    assert direct_targets(reality, "Validate") == {
        "VerifyReferenceUseEvidence",
        "RenderInfrastructureFailure",
    }
    assert_edge(reality, "Validate", "VerifyReferenceUseEvidence")
    assert_edge(
        reality,
        "Validate",
        "RenderInfrastructureFailure",
        "context.outcome=fail",
    )
    gate = node(reality, "VerifyReferenceUseEvidence")
    assert gate.shape == "parallelogram"
    assert direct_targets(reality, "VerifyReferenceUseEvidence") == {
        "VerifyTargetValidationEvidence",
        "RenderInfrastructureFailure",
    }
    assert_edge(reality, "VerifyReferenceUseEvidence", "VerifyTargetValidationEvidence")
    assert_edge(reality, "VerifyTargetValidationEvidence", "ReadVerdict")
    assert_edge(
        reality,
        "VerifyReferenceUseEvidence",
        "RenderInfrastructureFailure",
        "context.outcome=fail",
    )


def test_mechanical_use_evidence_gate_requires_current_ordered_success() -> None:
    reality = graph("reality_check.dot")
    assert direct_targets(reality, "Validate") == {
        "VerifyReferenceUseEvidence",
        "RenderInfrastructureFailure",
    }
    assert direct_targets(reality, "VerifyReferenceUseEvidence") == {
        "VerifyTargetValidationEvidence",
        "RenderInfrastructureFailure",
    }
    command = node_attr(reality, "VerifyReferenceUseEvidence", "tool_command")
    assert_terms(
        command,
        (
            ".rc/reference_dependencies.json",
            "run_token",
            "use_steps",
            "use_results",
            "dependency",
            "command",
            "exit_code",
            "order",
            "missing",
            "stale",
            "partial",
            "fail",
        ),
        subject="mechanical reference-use evidence gate",
    )
    assert any(marker in command for marker in ("!= 0", "== 0", "exit_code"))
    assert "reference_prerequisite_failed" not in command, (
        "failed target use evidence is ordinary target behavior, not a "
        "prerequisite failure"
    )


def test_delivery_excludes_all_reality_check_artifacts_from_staging() -> None:
    deliver = graph("deliver.dot")
    tree = embedded_python(deliver, "DeliverFinalize")
    constants = ast_strings(tree)
    assert ".ai/references.json" in constants
    assert ".ai/reference_context.md" in constants
    assert any(value.rstrip("/") == ".rc" for value in constants), (
        "DeliverFinalize must exclude the entire .rc tree, not selected files within it"
    )
    command = node_attr(deliver, "DeliverFinalize", "tool_command")
    assert_terms(
        command,
        ("git", "reset", "--cached", ".rc"),
        subject="delivery-wide .rc staging exclusion and verification",
    )


@pytest.mark.parametrize(
    ("graph_name", "node_id", "original_terms"),
    (
        (
            "plan.dot",
            "Plan",
            (".ai/plan/index.md", "task_nn", "acceptance criteria"),
        ),
        (
            "implement_loop.dot",
            "Implement",
            (".ai/plan/progress.md", ".done", "one task"),
        ),
        (
            "deliver.dot",
            "Deliver",
            ("readme.md", "install", "usage", ".ai/delivery_summary.md"),
        ),
        (
            "expert_builder.dot",
            "UserRun",
            (".ai/clarified_spec.md", "edge", "install", "tests", "verdict.json"),
        ),
    ),
)
def test_modified_agent_has_one_prompt_and_preserves_original_and_reference_contracts(
    graph_name: str, node_id: str, original_terms: tuple[str, ...]
) -> None:
    source_block = source_node_block(graph_name, node_id)
    prompt_attributes = re.findall(r"(?m)^\s*prompt\s*=", source_block)
    assert len(prompt_attributes) == 1, (
        f"{graph_name}:{node_id} must have exactly one prompt attribute; "
        "later attributes "
        "must not silently replace the original contract"
    )
    prompt = node_text(graph(graph_name), node_id)
    assert_terms(
        prompt, original_terms, subject=f"{graph_name}:{node_id} original contract"
    )
    assert_terms(
        prompt,
        (".ai/reference_context.md", "read-only", "target"),
        subject=f"{graph_name}:{node_id} reference contract",
    )


# Attractor runtime regressions: non-vacuous dependencies, handle preservation, hard teardown.


@pytest.mark.parametrize(
    ("selected", "setup_steps", "use_steps", "expected_state"),
    (
        (True, [], ["tool validate /sut"], "reference_prerequisite_failed"),
        (True, ["tool install public-package"], [], "reference_prerequisite_failed"),
        (False, [], [], "ready"),
    ),
    ids=("empty-setup", "empty-use", "no-validation-dependencies"),
)
def test_dependency_reader_requires_nonempty_steps_for_each_selected_dependency(
    workspace_tmp_path: Path,
    selected: bool,
    setup_steps: list[str],
    use_steps: list[str],
    expected_state: str,
) -> None:
    target = workspace_tmp_path / "target"
    target.mkdir()
    manifest_path = target / ".ai" / "references.json"
    manifest_path.parent.mkdir()
    references = (
        [{"id": "selected-tool", "use_in_validation": True}] if selected else []
    )
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "target_root": str(target.resolve()),
                "references": references,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    init_outcome, context = execute_tool_node(
        "reality_check.dot",
        "InitializeReferenceDependencyPlan",
        target,
        references_manifest_path=str(manifest_path.resolve()),
    )
    assert init_outcome.status == StageStatus.SUCCESS
    canonical_path = context.get("references_manifest_path")
    manifest_digest = context.get("references_manifest_digest")
    run_token = context.get("run_token")
    dependencies = []
    if selected:
        dependencies.append(
            {
                "id": "selected-tool",
                "citation": {"file": "README.md", "section": "Install"},
                "public_install": {"selected_path": "public package index"},
                "identity": "public-package",
                "version": None,
                "setup_steps": setup_steps,
                "use_steps": use_steps,
            }
        )
    plan = {
        "schema_version": 1,
        "state": "ready",
        "references_manifest_path": canonical_path,
        "references_manifest_digest": manifest_digest,
        "run_token": run_token,
        "dependencies": dependencies,
        "prerequisites": [],
        "setup_results": [],
        "use_results": [],
    }
    plan_path = target / ".rc" / "reference_dependencies.json"
    plan_path.write_text(
        json.dumps(plan, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    outcome, context = execute_tool_node(
        "reality_check.dot",
        "ReadReferenceDependencyPlan",
        target,
        context,
    )
    assert outcome.status == StageStatus.SUCCESS
    assert context.get("tool.last_line") == expected_state


@pytest.mark.parametrize(
    "handle_text",
    (json.dumps({"status": "created"}), "{malformed-json"),
    ids=("missing-name-and-id", "malformed-handle"),
)
def test_parse_handle_preserves_generated_name_for_cleanup(
    workspace_tmp_path: Path, handle_text: str
) -> None:
    target = workspace_tmp_path / "target"
    rc = target / ".rc"
    rc.mkdir(parents=True)
    known_name = "rc-known-generated-name"
    (rc / "requested_name.txt").write_text(known_name, encoding="utf-8")
    (rc / "launch.json").write_text(handle_text, encoding="utf-8")
    (rc / "sut_port.txt").write_text("8080", encoding="utf-8")

    outcome, _ = execute_tool_node("reality_check.dot", "ParseHandleStrict", target)
    assert outcome.status == StageStatus.SUCCESS
    assert (rc / "requested_name.txt").read_text(encoding="utf-8") == known_name
    assert (rc / "parse_status.txt").read_text(encoding="utf-8") == "handle_fail\n"
    assert not (rc / "dtu_container.txt").exists()


def test_launch_records_generated_name_before_invoking_dtu_launch() -> None:
    reality = graph("reality_check.dot")
    command = node_attr(reality, "LaunchDTUStrict", "tool_command")
    name_write = command.find(".rc/requested_name.txt")
    launch_call = command.find("amplifier-digital-twin launch")
    assert name_write >= 0 and launch_call >= 0
    assert name_write < launch_call, (
        "LaunchDTUStrict must preserve its requested name before the fallible launch command"
    )
    assert ".rc/dtu_container.txt" not in command


def test_strict_transport_failures_route_to_infrastructure_renderer() -> None:
    reality = graph("reality_check.dot")
    required = {
        "NormalizeDUTPlan": "context.tool.last_line=infrastructure_failed",
        "LaunchDTUStrict": "context.tool.last_line=launch_fail",
        "RouteHandleStrict": "context.tool.last_line=handle_fail",
        "PushSUTStrict": "context.tool.last_line=push_fail",
        "InstallReferenceDependenciesStrict": (
            "context.tool.last_line=infrastructure_failed"
        ),
        "Validate": "context.outcome=fail",
        "VerifyReferenceUseEvidence": "context.outcome=fail",
        "VerifyTargetValidationEvidence": "context.outcome=fail",
    }
    for source, condition in required.items():
        assert_edge(reality, source, "RenderInfrastructureFailure", condition)


def test_cleanup_and_terminal_latch_are_the_only_post_verdict_path() -> None:
    reality = graph("reality_check.dot")
    for renderer in (
        "RenderPass",
        "RenderPartial",
        "SetFail",
        "RenderReferencePrerequisiteFailure",
        "RenderInfrastructureFailure",
    ):
        assert direct_targets(reality, renderer) == {"EmbedTargetValidationEvidence"}
        assert_edge(reality, renderer, "EmbedTargetValidationEvidence")
    assert direct_targets(reality, "EmbedTargetValidationEvidence") == {
        "CleanupIdentityStrict"
    }
    assert direct_targets(reality, "CleanupIdentityStrict") == {"TerminalLatch"}
    assert direct_targets(reality, "TerminalLatch") == {"Exit"}
    assert_edge(reality, "TerminalLatch", "Exit", "context.outcome=success")
    assert_edge(reality, "TerminalLatch", "Exit", "context.outcome=fail")
    cleanup = node(reality, "CleanupIdentityStrict")
    assert str(cleanup.attrs.get("runs_on", "")).casefold() == "always"
    latch = node_attr(reality, "TerminalLatch", "tool_command")
    assert_terms(
        latch,
        ("target_behavior_pass", "outcome_class", "exit"),
        subject="strict post-cleanup terminal latch",
    )


def test_cleanup_uses_returned_dtu_identifier_not_requested_name() -> None:
    reality = graph("reality_check.dot")
    command = node_attr(reality, "CleanupIdentityStrict", "tool_command")
    assert_terms(
        command,
        (
            "requested_name",
            "dtu_container",
            "amplifier-digital-twin",
            "destroy",
            "cleanup.json",
        ),
        subject="strict cleanup evidence",
    )
    assert "if returned" in command
    assert "requested_name_fallback" in command


# Combined E2E regressions: exact substitution, parent failure latch, clean-room criteria.


def test_dependency_planner_prompt_substitutes_exact_binding_variables(
    workspace_tmp_path: Path,
) -> None:
    target = workspace_tmp_path / "target"
    target.mkdir()
    manifest_path, _ = write_empty_references_manifest(target)
    init_outcome, context = execute_tool_node(
        "reality_check.dot",
        "InitializeReferenceDependencyPlan",
        target,
        references_manifest_path=str(manifest_path),
    )
    assert init_outcome.status == StageStatus.SUCCESS
    assert context.get("refplan_manifest_path") == context.get(
        "references_manifest_path"
    )
    assert context.get("refplan_manifest_digest") == context.get(
        "references_manifest_digest"
    )
    assert context.get("refplan_run_token") == context.get("run_token")

    context.set("references", "<references JSON>")
    reality = graph("reality_check.dot")
    prompt = node(reality, "PlanReferenceDependencies").prompt
    rendered = _expand_variables(prompt, reality, context)
    alias_values = {
        context.get("refplan_manifest_path"),
        context.get("refplan_manifest_digest"),
        context.get("refplan_run_token"),
    }
    assert all(str(value) in rendered for value in alias_values)
    assert "<references JSON>_manifest_" not in rendered
    assert "$refplan_manifest_path" not in rendered
    assert "$refplan_manifest_digest" not in rendered
    assert "$refplan_run_token" not in rendered


def test_parent_latches_reference_prerequisite_failure_diagnostic_to_exit() -> None:
    parent = graph("expert_builder.dot")
    exits = [item.id for item in parent.nodes.values() if item.shape == "Msquare"]
    assert exits == ["done"]
    assert_edge(
        parent,
        "ReferencePrerequisiteFailed",
        "done",
        "context.outcome=fail",
    )


@pytest.mark.parametrize(
    ("graph_name", "node_id"),
    (
        ("plan.dot", "Plan"),
        ("implement_loop.dot", "Implement"),
        ("expert_builder.dot", "UserRun"),
    ),
)
def test_effective_prompt_preserves_clean_room_acceptance_criteria(
    graph_name: str, node_id: str
) -> None:
    source_block = source_node_block(graph_name, node_id)
    assert len(re.findall(r"(?m)^\s*prompt\s*=", source_block)) == 1
    prompt = " ".join(node_text(graph(graph_name), node_id).casefold().split())
    assert "acceptance criteria" in prompt
    assert any(
        phrase in prompt
        for phrase in (
            "preserve explicit acceptance criteria verbatim",
            "do not weaken or substitute",
            "must not weaken or substitute",
        )
    )
    assert "reality check" in prompt
    assert any(term in prompt for term in ("dtu", "container", "clean-room"))
    assert any(term in prompt for term in ("defer", "deferred"))
    assert any(term in prompt for term in ("host", "clean shell"))
    assert any(
        phrase in prompt
        for phrase in (
            "does not satisfy clean-room",
            "cannot satisfy clean-room",
            "must not be represented as satisfying clean-room",
        )
    )
