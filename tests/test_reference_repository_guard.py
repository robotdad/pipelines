"""Behavioral tests for expert_builder reference preparation and integrity guards."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import stat
import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path
from uuid import uuid4

import pytest
from amplifier_module_loop_pipeline.context import PipelineContext
from amplifier_module_loop_pipeline.dot_parser import parse_dot
from amplifier_module_loop_pipeline.handlers.tool import ToolHandler
from amplifier_module_loop_pipeline.outcome import Outcome, StageStatus
from amplifier_module_loop_pipeline.validation import validate_or_raise

ROOT = Path(__file__).parents[1]
WORK_ROOT = ROOT.parent / ".work"
PREPARE_DOT = ROOT / "expert_builder" / "references" / "prepare.dot"
VERIFY_DOT = ROOT / "expert_builder" / "references" / "verify.dot"
UNSET = object()


@pytest.fixture
def tmp_path(request: pytest.FixtureRequest) -> Iterator[Path]:
    """Provide an isolated per-test directory inside the workspace."""
    fixture_root = WORK_ROOT / "pytest"
    fixture_root.mkdir(parents=True, exist_ok=True)
    test_name = "".join(
        character if character.isalnum() else "-" for character in request.node.nodeid
    )[:80]
    while True:
        path = fixture_root / f"{test_name}-{uuid4().hex}"
        try:
            path.mkdir()
        except FileExistsError:
            continue
        break
    assert path.resolve().is_relative_to(WORK_ROOT.resolve())
    try:
        yield path
    finally:
        shutil.rmtree(path)


def git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=check, capture_output=True, text=True
    )


def git_bytes(cwd: Path, *args: str) -> bytes:
    """Return a Git command's stdout without text decoding."""
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True
    ).stdout


def capture_git_visible_state(
    repository: Path, *, seeded_paths: tuple[str, ...]
) -> dict[str, object]:
    """Capture an independent, content-aware snapshot for non-mutation assertions."""
    untracked_listing = git_bytes(
        repository, "ls-files", "--others", "--exclude-standard", "-z"
    )
    untracked_entries: list[tuple[bytes, int, int, bytes]] = []
    for encoded_path in filter(None, untracked_listing.split(b"\0")):
        path = repository / os.fsdecode(encoded_path)
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            payload = os.fsencode(os.readlink(path))
        elif stat.S_ISREG(metadata.st_mode):
            payload = path.read_bytes()
        else:
            payload = b""
        untracked_entries.append(
            (
                encoded_path,
                stat.S_IFMT(metadata.st_mode),
                stat.S_IMODE(metadata.st_mode),
                payload,
            )
        )

    return {
        "head": git_bytes(repository, "rev-parse", "--verify", "HEAD"),
        "ref": git_bytes(repository, "symbolic-ref", "-q", "HEAD"),
        "index": git_bytes(repository, "ls-files", "--stage", "-z"),
        "staged_diff": git_bytes(
            repository,
            "diff",
            "--cached",
            "--binary",
            "--full-index",
            "--no-ext-diff",
        ),
        "tracked_status": git_bytes(
            repository,
            "status",
            "--porcelain=v2",
            "-z",
            "--untracked-files=no",
            "--ignore-submodules=none",
        ),
        "tracked_diff": git_bytes(
            repository, "diff", "--binary", "--full-index", "--no-ext-diff"
        ),
        "untracked_listing": untracked_listing,
        "untracked_entries": tuple(untracked_entries),
        "seeded_file_bytes": tuple(
            (relative_path, (repository / relative_path).read_bytes())
            for relative_path in seeded_paths
        ),
    }


def init_repo(
    path: Path, *, filename: str = "tracked.txt", content: str = "base\n"
) -> Path:
    path.mkdir(parents=True)
    git(path, "init", "-q")
    git(path, "config", "user.email", "tests@example.invalid")
    git(path, "config", "user.name", "Pipeline Tests")
    (path / filename).write_text(content, encoding="utf-8")
    git(path, "add", filename)
    git(path, "commit", "-q", "-m", "initial")
    return path


def load_graph(path: Path):
    graph = parse_dot(path.read_text(encoding="utf-8"))
    graph.source_dir = str(path.parent)
    validate_or_raise(graph)
    return graph


def run_tool_node(
    dot_path: Path,
    node_id: str,
    target: Path,
    **context_values: object,
) -> tuple[Outcome, PipelineContext]:
    assert target.resolve().is_relative_to(WORK_ROOT.resolve())
    graph = load_graph(dot_path)
    context = PipelineContext()
    context.set("context.target_dir", str(target))
    for key, value in context_values.items():
        context.set(key, value)
    outcome = asyncio.run(
        ToolHandler().execute(
            graph.nodes[node_id], context, graph, str(target / ".test-logs")
        )
    )
    return outcome, context


def prepare(
    target: Path, references: object = UNSET
) -> tuple[Outcome, PipelineContext]:
    values = {} if references is UNSET else {"references": references}
    return run_tool_node(PREPARE_DOT, "PrepareReferences", target, **values)


def verify(target: Path) -> tuple[Outcome, PipelineContext]:
    manifest = target / ".ai" / "references.json"
    return run_tool_node(
        VERIFY_DOT,
        "VerifyReferences",
        target,
        reference_manifest_digest=hashlib.sha256(manifest.read_bytes()).hexdigest(),
    )


def test_reference_helper_dots_exist() -> None:
    assert PREPARE_DOT.is_file()
    assert VERIFY_DOT.is_file()


def assert_prepare_fails(target: Path, references: object, expected: str) -> None:
    outcome, _ = prepare(target, references)
    assert outcome.status == StageStatus.FAIL
    assert expected.lower() in (outcome.failure_reason or "").lower()


def normalize_markdown(markdown: str) -> str:
    """Normalize formatting differences while retaining semantic order."""
    return " ".join(markdown.casefold().split())


def assert_segment_contains(label: str, segment: str, *terms: str) -> None:
    missing = [term for term in terms if term.casefold() not in segment]
    assert not missing, (
        f"{label} is missing associated terms {missing!r}; segment={segment!r}"
    )


def assert_target_authorization_clause(context: str, target_path: str) -> None:
    """Find one compact clause granting all write/delivery actions only to target."""
    anchor = "only the target"
    required = (
        target_path.casefold(),
        "modified",
        "staged",
        "committed",
        "delivered",
    )
    search_from = 0
    examined: list[str] = []
    while (anchor_position := context.find(anchor, search_from)) >= 0:
        clause = context[
            max(0, anchor_position - 200) : anchor_position + len(anchor) + 600
        ]
        examined.append(clause)
        if all(term in clause for term in required):
            return
        search_from = anchor_position + len(anchor)
    pytest.fail(
        "No bounded target-only authorization clause associates the canonical "
        f"target with all actions; examined={examined!r}"
    )


@pytest.mark.parametrize("raw", [UNSET, None, "", "  \n\t", "[]"])
def test_empty_inputs_normalize_to_empty_reference_list(
    tmp_path: Path, raw: object
) -> None:
    target = init_repo(tmp_path / "target")

    outcome, context = prepare(target, raw)

    assert outcome.status == StageStatus.SUCCESS
    assert context.get("reference_state") == "prepared"
    references_file = target / ".ai" / "references.json"
    context_file = target / ".ai" / "reference_context.md"
    assert references_file.is_file()
    assert context_file.is_file()
    data = json.loads(references_file.read_text(encoding="utf-8"))
    assert data["schema_version"] == 1
    assert data["target_root"] == str(target.resolve())
    assert data["references"] == []
    assert (
        "only authorized write and delivery target"
        in context_file.read_text(encoding="utf-8").lower()
    )


def test_prepare_rejects_ai_symlink_that_escapes_target(tmp_path: Path) -> None:
    target = init_repo(tmp_path / "target")
    external = tmp_path / "external-ai"
    external.mkdir()
    ai_path = target / ".ai"
    references_file = external / "references.json"
    context_file = external / "reference_context.md"
    os.symlink(external, ai_path, target_is_directory=True)

    try:
        outcome, _ = prepare(target, "[]")
    finally:
        if ai_path.is_symlink():
            ai_path.unlink()

    assert not references_file.exists()
    assert not context_file.exists()
    failure_reason = outcome.failure_reason or ""
    assert outcome.status == StageStatus.FAIL
    assert "Traceback" not in failure_reason, failure_reason
    assert failure_reason.startswith("Command exited with code 1:")
    assert "ERROR:" in failure_reason
    assert len(failure_reason) <= 700
    assert ".ai" in failure_reason


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("null", "array"),
        ("{}", "array"),
        (json.dumps("reference"), "array"),
        ("1", "array"),
        ("true", "array"),
        ("[", "json"),
    ],
)
def test_invalid_top_level_references_fail_closed(
    tmp_path: Path, raw: str, expected: str
) -> None:
    target = init_repo(tmp_path / "target")

    assert_prepare_fails(target, raw, expected)


@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        (None, "entry"),
        ({}, "id"),
        ({"id": "", "path": "/reference"}, "id"),
        ({"id": 1, "path": "/reference"}, "id"),
        ({"id": "reference"}, "path"),
        ({"id": "reference", "path": ""}, "path"),
        ({"id": "reference", "path": 1}, "path"),
        (
            {
                "id": "reference",
                "path": "/reference",
                "use_in_validation": "yes",
            },
            "use_in_validation",
        ),
        ({"id": "reference", "path": "/reference", "extra": True}, "unknown"),
    ],
)
def test_invalid_reference_entries_identify_the_offending_field(
    tmp_path: Path, entry: object, expected: str
) -> None:
    target = init_repo(tmp_path / "target")

    assert_prepare_fails(target, json.dumps([entry]), expected)


def test_invalid_duplicate_reference_ids_fail_closed(tmp_path: Path) -> None:
    target = init_repo(tmp_path / "target")
    first = init_repo(tmp_path / "first")
    second = init_repo(tmp_path / "second")
    references = json.dumps(
        [
            {"id": "duplicate", "path": str(first)},
            {"id": "duplicate", "path": str(second)},
        ]
    )

    assert_prepare_fails(target, references, "duplicate")


def test_missing_reference_path_fails_closed(tmp_path: Path) -> None:
    target = init_repo(tmp_path / "target")
    references = json.dumps(
        [{"id": "missing", "path": str(tmp_path / "does-not-exist")}]
    )

    assert_prepare_fails(target, references, "path")


def test_non_git_reference_path_fails_closed(tmp_path: Path) -> None:
    target = init_repo(tmp_path / "target")
    non_git = tmp_path / "not-a-repository"
    non_git.mkdir()
    references = json.dumps([{"id": "not-git", "path": str(non_git)}])

    assert_prepare_fails(target, references, "git")


def test_invalid_bare_reference_repository_fails_closed(tmp_path: Path) -> None:
    target = init_repo(tmp_path / "target")
    git(tmp_path, "init", "--bare", "bare.git")
    references = json.dumps([{"id": "bare", "path": str(tmp_path / "bare.git")}])

    assert_prepare_fails(target, references, "bare")


def test_unreadable_untracked_file_fails_with_bounded_actionable_error(
    tmp_path: Path,
) -> None:
    target = init_repo(tmp_path / "target")
    reference = init_repo(tmp_path / "reference")
    unreadable = reference / "unreadable.txt"
    unreadable.write_text("untracked fixture\n", encoding="utf-8")
    references = json.dumps([{"id": "unreadable-reference", "path": str(reference)}])

    unreadable.chmod(0)
    try:
        outcome, _ = prepare(target, references)
    finally:
        unreadable.chmod(stat.S_IRUSR | stat.S_IWUSR)

    failure_reason = outcome.failure_reason or ""
    assert outcome.status == StageStatus.FAIL
    assert "Traceback" not in failure_reason, failure_reason
    assert failure_reason.startswith("Command exited with code 1:")
    assert "ERROR:" in failure_reason
    assert len(failure_reason) <= 700
    assert "unreadable-reference" in failure_reason
    assert unreadable.name in failure_reason
    assert "read" in failure_reason.lower()


def test_path_inside_worktree_normalizes_to_worktree_root(tmp_path: Path) -> None:
    target = init_repo(tmp_path / "target")
    reference = init_repo(tmp_path / "reference")
    nested_path = reference / "nested" / "directory"
    nested_path.mkdir(parents=True)
    references = json.dumps([{"id": "reference", "path": str(nested_path)}])

    outcome, _ = prepare(target, references)

    assert outcome.status == StageStatus.SUCCESS
    data = json.loads((target / ".ai" / "references.json").read_text(encoding="utf-8"))
    assert data["references"][0]["path"] == str(reference.resolve())


def test_path_target_cannot_also_be_a_reference(tmp_path: Path) -> None:
    target = init_repo(tmp_path / "target")
    references = json.dumps([{"id": "same", "path": str(target)}])

    assert_prepare_fails(target, references, "target")
    assert not (target / ".ai" / "references.json").exists()


def test_overlap_target_cannot_contain_a_reference(tmp_path: Path) -> None:
    target = init_repo(tmp_path / "target")
    reference = init_repo(target / "nested-reference")
    references = json.dumps([{"id": "nested", "path": str(reference)}])

    assert_prepare_fails(target, references, "overlap")
    assert not (target / ".ai" / "references.json").exists()


def test_overlap_reference_cannot_contain_the_target(tmp_path: Path) -> None:
    reference = init_repo(tmp_path / "reference")
    target = init_repo(reference / "nested-target")
    references = json.dumps([{"id": "parent", "path": str(reference)}])

    assert_prepare_fails(target, references, "overlap")
    assert not (target / ".ai" / "references.json").exists()


def test_references_cannot_overlap_each_other(tmp_path: Path) -> None:
    target = init_repo(tmp_path / "target")
    parent = init_repo(tmp_path / "parent-reference")
    child = init_repo(parent / "nested-reference")
    references = json.dumps(
        [
            {"id": "parent", "path": str(parent)},
            {"id": "child", "path": str(child)},
        ]
    )

    assert_prepare_fails(target, references, "overlap")
    assert not (target / ".ai" / "references.json").exists()


def test_valid_reference_defaults_to_context_only(tmp_path: Path) -> None:
    target = init_repo(tmp_path / "target")
    reference = init_repo(tmp_path / "reference")
    references = json.dumps([{"id": "context", "path": str(reference)}])

    outcome, _ = prepare(target, references)

    assert outcome.status == StageStatus.SUCCESS
    data = json.loads((target / ".ai" / "references.json").read_text(encoding="utf-8"))
    assert len(data["references"]) == 1
    stored_reference = data["references"][0]
    assert stored_reference["id"] == "context"
    assert stored_reference["path"] == str(reference.resolve())
    assert stored_reference["use_in_validation"] is False
    context = (
        (target / ".ai" / "reference_context.md").read_text(encoding="utf-8").lower()
    )
    assert "context-only" in context


def test_valid_references_preserve_order_and_label_validation_dependencies(
    tmp_path: Path,
) -> None:
    target = init_repo(tmp_path / "target")
    validation_reference = init_repo(tmp_path / "validation-reference")
    context_reference = init_repo(tmp_path / "context-reference")
    references = json.dumps(
        [
            {
                "id": "validation",
                "path": str(validation_reference),
                "use_in_validation": True,
            },
            {"id": "context", "path": str(context_reference)},
        ]
    )

    outcome, _ = prepare(target, references)

    assert outcome.status == StageStatus.SUCCESS
    data = json.loads((target / ".ai" / "references.json").read_text(encoding="utf-8"))
    assert [reference["id"] for reference in data["references"]] == [
        "validation",
        "context",
    ]
    assert [reference["path"] for reference in data["references"]] == [
        str(validation_reference.resolve()),
        str(context_reference.resolve()),
    ]
    assert [reference["use_in_validation"] for reference in data["references"]] == [
        True,
        False,
    ]
    context = normalize_markdown(
        (target / ".ai" / "reference_context.md").read_text(encoding="utf-8")
    )
    target_path = str(target.resolve()).casefold()
    validation_path = str(validation_reference.resolve()).casefold()
    context_path = str(context_reference.resolve()).casefold()

    validation_path_position = context.find(validation_path)
    context_path_position = context.find(context_path)
    assert validation_path_position >= 0, (
        f"Validation reference path is absent: {validation_path!r}"
    )
    assert context_path_position >= 0, (
        f"Context reference path is absent: {context_path!r}"
    )
    assert validation_path_position < context_path_position, (
        "Reference paths are not in caller order: "
        f"validation={validation_path_position}, context={context_path_position}"
    )

    validation_id_position = context.rfind("validation", 0, validation_path_position)
    context_id_position = context.rfind(
        "context",
        validation_path_position + len(validation_path),
        context_path_position,
    )
    assert validation_id_position >= 0, (
        "Validation reference ID is not associated before its canonical path"
    )
    assert context_id_position >= 0, (
        "Context reference ID is not associated before its canonical path"
    )

    validation_segment = context[validation_id_position:context_id_position]
    context_segment = context[context_id_position:]
    assert_segment_contains(
        "validation reference entry",
        validation_segment,
        "validation",
        validation_path,
        "validation dependency",
    )
    assert_segment_contains(
        "context reference entry",
        context_segment,
        "context",
        context_path,
        "context-only",
    )
    assert_target_authorization_clause(context, target_path)


def test_dirty_reference_baseline_is_private_and_immediately_verifiable(
    tmp_path: Path,
) -> None:
    target = init_repo(tmp_path / "target")
    reference = init_repo(tmp_path / "reference")
    tracked_secret = "tracked-secret-must-not-leak"
    untracked_secret = "untracked-secret-must-not-leak"
    (reference / "tracked.txt").write_text(f"{tracked_secret}\n", encoding="utf-8")
    (reference / "untracked-secret.txt").write_text(
        f"{untracked_secret}\n", encoding="utf-8"
    )
    references = json.dumps([{"id": "dirty-reference", "path": str(reference)}])
    before_prepare = capture_git_visible_state(
        reference, seeded_paths=("tracked.txt", "untracked-secret.txt")
    )

    prepare_outcome, _ = prepare(target, references)

    assert prepare_outcome.status == StageStatus.SUCCESS
    assert (
        capture_git_visible_state(
            reference, seeded_paths=("tracked.txt", "untracked-secret.txt")
        )
        == before_prepare
    )
    manifest_text = (target / ".ai" / "references.json").read_text(encoding="utf-8")
    context_text = (target / ".ai" / "reference_context.md").read_text(encoding="utf-8")
    manifest = json.loads(manifest_text)
    stored_reference = manifest["references"][0]
    baseline = stored_reference["baseline"]
    assert set(stored_reference) == {
        "id",
        "path",
        "use_in_validation",
        "baseline",
    }
    assert set(baseline) == {"head", "ref", "digest", "dimensions"}
    assert set(baseline["dimensions"]) == {
        "index",
        "tracked_worktree",
        "untracked",
        "submodules",
    }
    for secret in (tracked_secret, untracked_secret):
        assert secret not in manifest_text
        assert secret not in context_text

    verify_outcome, verify_context = verify(target)

    assert verify_outcome.status == StageStatus.SUCCESS
    assert verify_context.get("reference_integrity_state") == "unchanged"


@pytest.mark.parametrize(
    "schema_version",
    [True, 1.0],
    ids=["json-boolean-true", "json-float-one"],
)
def test_verify_rejects_non_integer_manifest_schema_version(
    tmp_path: Path, schema_version: object
) -> None:
    target = init_repo(tmp_path / "target")
    reference = init_repo(tmp_path / "reference")
    references = json.dumps(
        [{"id": "schema-version-reference", "path": str(reference)}]
    )

    prepare_outcome, _ = prepare(target, references)

    assert prepare_outcome.status == StageStatus.SUCCESS
    manifest_path = target / ".ai" / "references.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["schema_version"] = schema_version
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    verify_outcome, _ = verify(target)
    failure_reason = verify_outcome.failure_reason or ""

    assert verify_outcome.status == StageStatus.FAIL
    assert "schema" in failure_reason.lower()
    assert "traceback" not in failure_reason
    assert len(failure_reason) <= 700


def test_ignored_file_mutation_does_not_change_reference_integrity(
    tmp_path: Path,
) -> None:
    target = init_repo(tmp_path / "target")
    reference = init_repo(tmp_path / "reference")
    (reference / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
    git(reference, "add", ".gitignore")
    git(reference, "commit", "-q", "-m", "ignore fixture")
    references = json.dumps([{"id": "ignored-reference", "path": str(reference)}])

    prepare_outcome, _ = prepare(target, references)

    assert prepare_outcome.status == StageStatus.SUCCESS

    (reference / "ignored.txt").write_text("ignored mutation\n", encoding="utf-8")
    verify_outcome, verify_context = verify(target)

    assert verify_outcome.status == StageStatus.SUCCESS
    assert verify_context.get("reference_integrity_state") == "unchanged"


def _no_reference_setup(reference: Path) -> None:
    """Leave the initial reference fixture unchanged."""


def _seed_untracked_file(reference: Path) -> None:
    (reference / "existing-untracked.txt").write_text(
        "untracked baseline\n", encoding="utf-8"
    )


def _seed_tracked_symlink(reference: Path) -> None:
    (reference / "replacement.txt").write_text("replacement\n", encoding="utf-8")
    os.symlink("tracked.txt", reference / "tracked-link.txt")
    git(reference, "add", "replacement.txt", "tracked-link.txt")
    git(reference, "commit", "-q", "-m", "add tracked symlink")


def _modify_tracked_file(reference: Path) -> None:
    (reference / "tracked.txt").write_text("modified tracked file\n", encoding="utf-8")


def _stage_tracked_file_change(reference: Path) -> None:
    (reference / "tracked.txt").write_text("staged tracked file\n", encoding="utf-8")
    git(reference, "add", "tracked.txt")


def _create_untracked_file(reference: Path) -> None:
    (reference / "new-untracked.txt").write_text(
        "new untracked file\n", encoding="utf-8"
    )


def _change_untracked_file(reference: Path) -> None:
    (reference / "existing-untracked.txt").write_text(
        "changed untracked file\n", encoding="utf-8"
    )


def _change_tracked_file_mode(reference: Path) -> None:
    tracked_file = reference / "tracked.txt"
    tracked_file.chmod(tracked_file.stat().st_mode | stat.S_IXUSR)


def _replace_tracked_symlink_target(reference: Path) -> None:
    link = reference / "tracked-link.txt"
    link.unlink()
    os.symlink("replacement.txt", link)


def _change_head_and_ref(reference: Path) -> None:
    git(reference, "checkout", "-q", "-b", "integrity-head-change")
    (reference / "tracked.txt").write_text(
        "committed branch change\n", encoding="utf-8"
    )
    git(reference, "add", "tracked.txt")
    git(reference, "commit", "-q", "-m", "advance reference")


def assert_verify_detects_mutation(
    target: Path, reference_id: str, expected_dimensions: set[str]
) -> None:
    outcome, _ = verify(target)
    failure_reason = outcome.failure_reason or ""

    assert outcome.status == StageStatus.FAIL
    assert reference_id in failure_reason
    assert any(dimension in failure_reason for dimension in expected_dimensions), (
        "Integrity failure did not identify an expected changed dimension: "
        f"expected={sorted(expected_dimensions)!r}, reason={failure_reason!r}"
    )


@pytest.mark.parametrize(
    ("setup", "mutate", "expected_dimensions"),
    [
        (_no_reference_setup, _modify_tracked_file, {"tracked_worktree"}),
        (_no_reference_setup, _stage_tracked_file_change, {"index"}),
        (_no_reference_setup, _create_untracked_file, {"untracked"}),
        (_seed_untracked_file, _change_untracked_file, {"untracked"}),
        (_no_reference_setup, _change_tracked_file_mode, {"tracked_worktree"}),
        (_seed_tracked_symlink, _replace_tracked_symlink_target, {"tracked_worktree"}),
        (_no_reference_setup, _change_head_and_ref, {"head", "ref"}),
    ],
    ids=[
        "tracked-worktree",
        "staged-index",
        "new-untracked",
        "changed-untracked",
        "executable-mode",
        "symlink-target",
        "head-and-ref",
    ],
)
def test_reference_mutations_fail_integrity(
    tmp_path: Path,
    setup: Callable[[Path], None],
    mutate: Callable[[Path], None],
    expected_dimensions: set[str],
) -> None:
    target = init_repo(tmp_path / "target")
    reference = init_repo(tmp_path / "reference")
    setup(reference)
    references = json.dumps([{"id": "mutation-reference", "path": str(reference)}])

    prepare_outcome, _ = prepare(target, references)

    assert prepare_outcome.status == StageStatus.SUCCESS

    mutate(reference)

    assert_verify_detects_mutation(target, "mutation-reference", expected_dimensions)


def test_submodule_checkout_advance_fails_reference_integrity(tmp_path: Path) -> None:
    target = init_repo(tmp_path / "target")
    child = init_repo(tmp_path / "child")
    reference = init_repo(tmp_path / "reference")
    git(
        reference,
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "add",
        str(child),
        "deps/child",
    )
    git(reference, "add", ".gitmodules", "deps/child")
    git(reference, "commit", "-q", "-m", "add local child")
    references = json.dumps([{"id": "submodule-reference", "path": str(reference)}])

    prepare_outcome, _ = prepare(target, references)

    assert prepare_outcome.status == StageStatus.SUCCESS

    checkout = reference / "deps" / "child"
    git(checkout, "config", "user.email", "tests@example.invalid")
    git(checkout, "config", "user.name", "Pipeline Tests")
    (checkout / "tracked.txt").write_text("advanced child checkout\n", encoding="utf-8")
    git(checkout, "add", "tracked.txt")
    git(checkout, "commit", "-q", "-m", "advance child")

    assert_verify_detects_mutation(target, "submodule-reference", {"submodules"})
