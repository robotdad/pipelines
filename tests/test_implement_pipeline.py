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


def test_delivery_selects_direct_or_fork_publication_and_verifies_exact_pr_state() -> (
    None
):
    graph = load_graph()
    source = PIPELINE.read_text(encoding="utf-8")
    commit_script = graph.nodes["Commit"].attrs["tool_command"]
    publish_script = graph.nodes["Publish"].attrs["tool_command"]
    verify_script = graph.nodes["VerifyPR"].attrs["tool_command"]

    assert "/promote/pr" not in source
    assert "--force" not in publish_script
    assert _edge(graph, "Publish", "VerifyPR").condition == "outcome=success"
    assert not any(
        edge.from_node == "Publish" and edge.to_node == "Exit" for edge in graph.edges
    )
    assert [edge for edge in graph.edges if edge.to_node == "Exit"] == [
        _edge(graph, "VerifyPR", "Exit")
    ]

    for script in (commit_script, publish_script, verify_script):
        assert "resolve/implement-" in script

    # Publication identifies the authenticated actor before selecting an explicit
    # GitHub destination from the target repository's push permission.
    assert "['gh', 'api', '--method', method, endpoint]" in publish_script
    assert "actor = gh_api('user')" in publish_script
    assert "target = gh_api('repos/%s' % target_repo)" in publish_script
    assert "permissions.get('push') is True" in publish_script
    assert "push_repo = target_repo" in publish_script
    assert "publication_mode = 'direct'" in publish_script
    assert "head_owner = target_repo.split('/', 1)[0]" in publish_script

    # A non-writable target is published from the authenticated actor's verified
    # fork. The fork is reused when present and created/polled when absent.
    assert (
        "fork_repo = '%s/%s' % (actor_login, target_repo.split('/', 1)[1])"
        in publish_script
    )
    fork_api_read = "fork = gh_api('repos/%s' % fork_repo, check=False)"
    assert publish_script.count(fork_api_read) == 2
    assert "token = os.environ.get('GH_TOKEN', '')" in publish_script
    assert "gh_api('repos/%s/forks' % target_repo, method='POST')" in publish_script
    assert "validate_fork(fork)" in publish_script
    assert (
        "return repo_data.get('fork') is True and parent == target_repo and source == target_repo and (repo_data.get('permissions') or {}).get('push') is True"
        in publish_script
    )
    assert "push_repo = fork_repo" in publish_script
    assert "publication_mode = 'fork'" in publish_script
    assert "head_owner = actor_login" in publish_script

    # Direct and fork modes share a non-force explicit remote push and require
    # the publication repository's branch to contain the tested local commit.
    assert "push_url = 'https://github.com/%s.git' % push_repo" in publish_script
    assert "'push', '--no-verify', push_url" in publish_script
    assert "'ls-remote', push_url, 'refs/heads/' + local_branch" in publish_script
    assert "remote[0] != local_commit" in publish_script
    assert "origin" not in publish_script

    # The PR lookup and creation are made against the target repository with a
    # qualified cross-repository head, not the hosted worker's Git remote.
    assert "'repos/%s/pulls' % target_repo" in publish_script
    assert "'head': '%s:%s' % (head_owner, head_branch)" in publish_script
    assert "'head': '%s:%s' % (head_owner, local_branch)" in publish_script
    assert "method='POST'" in publish_script
    assert "'base': base_branch" in publish_script
    assert "matching_prs(head_owner, local_branch)" in publish_script

    delivery_fields = {
        "target_repo",
        "push_repo",
        "publication_mode",
        "head_owner",
        "head_branch",
        "base_branch",
        "commit",
        "pr_number",
        "pr_url",
    }
    for field in delivery_fields:
        assert "'%s'" % field in publish_script
        assert "'%s'" % field in verify_script
    assert "report = {" in publish_script
    assert "expected_keys = {" in verify_script

    # Verification derives local authority from Git, verifies the published
    # branch in its actual repository, then checks the target PR's exact state.
    for script in (publish_script, verify_script):
        assert "symbolic-ref', '--short', 'HEAD'" in script
        assert "rev-parse', 'HEAD'" in script
        assert "ls-remote" in script

    assert "delivery['publication_mode'] == 'direct'" in verify_script
    assert "delivery['publication_mode'] == 'fork'" in verify_script
    assert "validate_fork(gh_api('repos/%s' % delivery['push_repo']))" in verify_script
    assert (
        "(repo_data.get('permissions') or {}).get('push') is not True" in verify_script
    )
    assert "'repos/%s/pulls/%s' % (target_repo, delivery['pr_number'])" in verify_script
    assert "base.get('repo') or {}).get('full_name') != target_repo" in verify_script
    assert "base.get('ref') != delivery['base_branch']" in verify_script
    assert (
        "head.get('repo') or {}).get('full_name') != delivery['push_repo']"
        in verify_script
    )
    assert (
        "head.get('repo') or {}).get('owner', {}).get('login') != delivery['head_owner']"
        in verify_script
    )
    assert "head.get('ref') != delivery['head_branch']" in verify_script
    assert "head.get('sha') != local_commit" in verify_script
    assert "str(pr.get('state', '')).lower() != 'open'" in verify_script
    assert "pr.get('html_url') != delivery['pr_url']" in verify_script
    assert "delivery['pr_url'] != expected_url" in verify_script

    # Commit writes immutable pre-publication evidence. Publish consumes that
    # artifact rather than treating a prior delivery report as its input, then
    # atomically replaces the delivery report only after publication succeeds.
    assert graph.nodes["Publish"].attrs["requires"] == (
        ".ai/implement/commit.json,.ai/implement/test-command.txt"
    )
    assert "metadata = {" in commit_script
    assert (
        "with open('.ai/implement/commit.json', 'w', encoding='utf-8')" in commit_script
    )
    assert "with open('.ai/implement/commit.json', encoding='utf-8')" in publish_script
    assert "commit_report = json.load(handle)" in publish_script
    assert (
        "with open('.ai/implement/delivery.json', encoding='utf-8')"
        not in publish_script
    )
    assert "report_path = '.ai/implement/delivery.json'" in publish_script
    assert "temporary_path = report_path + '.tmp-%s' % os.getpid()" in publish_script
    assert "os.fsync(handle.fileno())" in publish_script
    assert "os.replace(temporary_path, report_path)" in publish_script

    # Verification requires both artifacts and corroborates each against the
    # local checkout and GitHub, rather than trusting delivery.json by itself.
    assert graph.nodes["VerifyPR"].attrs["requires"] == (
        ".ai/implement/commit.json,.ai/implement/delivery.json"
    )
    assert "with open('.ai/implement/commit.json', encoding='utf-8')" in verify_script
    assert "with open('.ai/implement/delivery.json', encoding='utf-8')" in verify_script
    assert "commit_report = json.load(handle)" in verify_script
    assert "delivery = json.load(handle)" in verify_script
    assert "commit report, and delivery report do not corroborate" in verify_script

    # A pre-existing validated fork proceeds immediately. A newly created fork
    # has a bounded metadata-plus-authenticated-Git readiness loop before push.
    assert "if fork is None:" in publish_script
    assert "for delay in (0, 1, 2, 4, 8, 12):" in publish_script
    assert "time.sleep(delay)" in publish_script
    assert "isinstance(fork, dict) and fork_is_valid(fork)" in publish_script
    assert "'ls-remote', fork_url" in publish_script
    assert "metadata-and-Git ready within bounded backoff" in publish_script
    assert "else:\n        validate_fork(fork)" in publish_script

    # The URL is surfaced for hosted consumers, while the final line remains
    # the deterministic routing marker. This graph deliberately writes no
    # Resolve coordination state.
    assert "print(delivery['pr_url'])\nprint('verified')" in verify_script
    assert ".resolve" not in "\n".join((commit_script, publish_script, verify_script))
