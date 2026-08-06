"""Regression tests for the pure Attractor implementation-to-PR pipeline."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from amplifier_module_loop_pipeline.context import PipelineContext
from amplifier_module_loop_pipeline.dot_parser import parse_dot
from amplifier_module_loop_pipeline.handlers.tool import ToolHandler
from amplifier_module_loop_pipeline.outcome import StageStatus
from amplifier_module_loop_pipeline.validation import validate_or_raise

ROOT = Path(__file__).parents[1]
PIPELINE = ROOT / "implement" / "implement.dot"
EXPECTED_NODES = {
    "Start",
    "Implement",
    "Test",
    "Review",
    "Decide",
    "Commit",
    "Publish",
    "VerifyPR",
    "Exit",
    "FailLoud",
}
SUCCESS_SPINE = (
    ("Start", "Implement"),
    ("Implement", "Test"),
    ("Test", "Review"),
    ("Review", "Decide"),
    ("Decide", "Commit"),
    ("Commit", "Publish"),
    ("Publish", "VerifyPR"),
    ("VerifyPR", "Exit"),
)


def load_graph():
    """Parse and validate the published pipeline contract."""
    graph = parse_dot(PIPELINE.read_text(encoding="utf-8"))
    graph.source_dir = str(PIPELINE.parent)
    validate_or_raise(graph)
    return graph


def _edge(graph, source: str, destination: str):
    return next(
        edge
        for edge in graph.edges
        if edge.from_node == source and edge.to_node == destination
    )


def _run_decide(target: Path) -> tuple[StageStatus, str]:
    graph = load_graph()
    context = PipelineContext()
    context.set("context.target_dir", str(target))
    outcome = asyncio.run(
        ToolHandler().execute(
            graph.nodes["Decide"], context, graph, str(target / ".test-logs")
        )
    )
    return outcome.status, outcome.context_updates["tool.last_line"]


def test_implement_pipeline_is_a_closed_valid_package_with_required_topology() -> None:
    graph = load_graph()

    assert set(graph.nodes) == EXPECTED_NODES
    assert graph.graph_attrs["param.task"].endswith(":required")
    assert graph.graph_attrs["param.test_command"].endswith(":required")
    assert "branch_name" in graph.graph_attrs["params"].split(",")
    assert all("dot_file" not in node.attrs for node in graph.nodes.values())

    for source, destination in SUCCESS_SPINE:
        edge = _edge(graph, source, destination)
        if source == "Start":
            assert edge.condition == ""
        elif source == "Decide":
            assert edge.condition.endswith("outcome=success")
        else:
            assert edge.condition == "outcome=success"

    for source in (
        "Implement",
        "Test",
        "Review",
        "Decide",
        "Commit",
        "Publish",
        "VerifyPR",
    ):
        assert any(
            edge.from_node == source
            and edge.to_node == "FailLoud"
            and "outcome=fail" in edge.condition
            for edge in graph.edges
        ), f"{source} must have an explicit failure route"


def test_decide_routes_review_results_once_and_fails_closed(tmp_path: Path) -> None:
    review_path = tmp_path / ".ai" / "implement" / "review.json"
    review_path.parent.mkdir(parents=True)

    review_path.write_text(
        json.dumps({"verdict": "pass", "feedback": ""}), encoding="utf-8"
    )
    assert _run_decide(tmp_path) == (StageStatus.SUCCESS, "review_pass")

    review_path.write_text(
        json.dumps({"verdict": "repair", "feedback": "Add the missing assertion."}),
        encoding="utf-8",
    )
    assert _run_decide(tmp_path) == (StageStatus.SUCCESS, "repair")
    assert _run_decide(tmp_path) == (StageStatus.SUCCESS, "repair_exhausted")

    review_path.write_text("not JSON", encoding="utf-8")
    assert _run_decide(tmp_path) == (StageStatus.SUCCESS, "review_invalid")

    review_path.unlink()
    assert _run_decide(tmp_path) == (StageStatus.SUCCESS, "review_invalid")

    graph = load_graph()
    decide_edges = [edge for edge in graph.edges if edge.from_node == "Decide"]
    marker_edges = [
        edge for edge in decide_edges if "context.tool.last_line=" in edge.condition
    ]
    repair_edge = _edge(graph, "Decide", "Implement")
    assert repair_edge.loop_restart is True
    assert marker_edges
    assert all("outcome=success" in edge.condition for edge in marker_edges)
    assert (
        _edge(graph, "Decide", "FailLoud").condition
        == "context.tool.last_line=repair_exhausted && outcome=success"
    )
    assert any(edge.condition == "outcome=fail" for edge in decide_edges)


def test_delivery_is_direct_github_and_verifies_derived_git_state() -> None:
    graph = load_graph()
    source = PIPELINE.read_text(encoding="utf-8")
    commit_script = graph.nodes["Commit"].attrs["tool_command"]
    publish_script = graph.nodes["Publish"].attrs["tool_command"]
    verify_script = graph.nodes["VerifyPR"].attrs["tool_command"]

    assert "/promote/pr" not in source
    assert _edge(graph, "Publish", "VerifyPR").condition == "outcome=success"
    assert not any(
        edge.from_node == "Publish" and edge.to_node == "Exit" for edge in graph.edges
    )
    assert [edge for edge in graph.edges if edge.to_node == "Exit"] == [
        _edge(graph, "VerifyPR", "Exit")
    ]

    assert "https://github.com/%s.git" in publish_script
    assert "'push', '--no-verify', github_url" in publish_script
    for script in (commit_script, publish_script, verify_script):
        assert "branch in {'main', base}" in script
        assert "resolve/implement-" in script

    for script in (publish_script, verify_script):
        assert "symbolic-ref', '--short', 'HEAD'" in script
        assert "rev-parse', 'HEAD'" in script
        assert "delivery.get('branch') != branch" in script
        assert "delivery.get('commit') != commit" in script
        assert "ls-remote" in script
        assert "headRefName" in script
        assert "baseRefName" in script
        assert "headRefOid" in script
        assert "state" in script
        assert "OPEN" in script
