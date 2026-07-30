"""Package-boundary regression tests for the expert_builder Resolve entrypoint."""

from __future__ import annotations

import asyncio
import os
import re
import shutil
from pathlib import Path

import pytest
from amplifier_module_loop_pipeline.dot_parser import parse_dot
from amplifier_resolver_dot_graph.remote_dot import materialize_dot_tree

EXPECTED_PACKAGE_FILES = frozenset(
    {
        "expert_builder.dot",
        "admit/admit.dot",
        "plan.dot",
        "implement_loop.dot",
        "reality_check.dot",
        "deliver.dot",
        "references/prepare.dot",
        "references/verify.dot",
    }
)
PIPELINE_URI_TEMPLATE = (
    "git+https://github.com/robotdad/pipelines@{sha}"
    "#subdirectory=expert_builder/expert_builder.dot"
)
_SHA = re.compile(r"^[0-9a-f]{40}$")


def _assert_static_package_closure(entry_path: Path) -> None:
    """Validate every static child DOT stays in, and closes over, this package."""
    package_root = entry_path.parent.resolve()
    seen: set[Path] = set()

    def visit(dot_path: Path) -> None:
        dot_path = dot_path.resolve()
        if dot_path in seen:
            return
        seen.add(dot_path)

        graph = parse_dot(dot_path.read_text(encoding="utf-8"))
        for node in graph.nodes.values():
            dot_file = node.attrs.get("dot_file")
            if not dot_file:
                continue
            assert "$" not in dot_file, (
                f"{dot_path}: runtime-variable dependency {dot_file!r}"
            )
            assert not dot_file.startswith("/"), (
                f"{dot_path}: absolute dependency {dot_file!r}"
            )
            assert "://" not in dot_file, (
                f"{dot_path}: external dependency {dot_file!r}"
            )

            child = (dot_path.parent / dot_file).resolve()
            assert child.is_relative_to(package_root), (
                f"{dot_path}: dependency escapes expert_builder package: {dot_file!r}"
            )
            assert child.is_file(), f"{dot_path}: missing dependency {dot_file!r}"
            visit(child)

    visit(entry_path)
    actual = {path.relative_to(package_root).as_posix() for path in seen}
    assert actual == EXPECTED_PACKAGE_FILES


def test_expert_builder_working_tree_is_a_closed_package() -> None:
    _assert_static_package_closure(
        Path(__file__).parents[1] / "expert_builder" / "expert_builder.dot"
    )


@pytest.mark.remote
def test_expert_builder_remote_package_hydrates_from_one_sha_pinned_uri() -> None:
    sha = os.environ.get("PIPELINES_REMOTE_SHA", "")
    assert _SHA.fullmatch(sha), (
        "PIPELINES_REMOTE_SHA must be the 40-character commit SHA to hydrate."
    )
    entry_path, temp_dir = asyncio.run(
        materialize_dot_tree(PIPELINE_URI_TEMPLATE.format(sha=sha))
    )
    try:
        _assert_static_package_closure(Path(entry_path))
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
