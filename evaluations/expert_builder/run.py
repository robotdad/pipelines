"""Serial entrypoint for deterministic expert-builder graph evaluation."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from .fixtures import (
    ensure_evaluation_path,
    run_controlled_parent_case,
    run_controlled_reality_case,
    run_controller_attractor_case,
    run_parent_rc_probe_case,
    run_real_reality_canary_case,
)
from .grade import FULL_SUITE_CASE_IDS, Verdict, grade_case, grade_run
from .json_data import JsonMapping, JsonObject, parse_object

REPOSITORY = Path(__file__).parents[2]
WORKSPACE = REPOSITORY.parent
EVALUATION_ROOT = WORKSPACE / ".work" / "evaluations" / "expert-builder"
PACKAGE = REPOSITORY / "expert_builder"
PARENT_DOT = PACKAGE / "expert_builder.dot"
REALITY_CHECK_DOT = PACKAGE / "reality_check.dot"
HARNESS_DOT = Path(__file__).parent / "harness" / "reference_folder_composition.dot"
SCENARIOS_PATH = Path(__file__).with_name("scenarios.json")
PROFILE_PATH = Path(__file__).with_name("profile.yaml")


def _source_tree_files(root: Path) -> tuple[Path, ...]:
    """Return a source subtree only when every traversed entry is non-symlinked."""
    if root.is_symlink() or not root.is_dir():
        raise ValueError(f"participating source root must be a real directory: {root}")
    files: list[Path] = []
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ValueError(f"participating source tree contains a symlink: {path}")
        if path.is_file() and "__pycache__" not in path.parts:
            files.append(path)
    return tuple(files)


def _source_files() -> tuple[Path, ...]:
    """Return every production/evaluation input participating in the suite."""
    values: set[Path] = {
        REPOSITORY / "pyproject.toml",
        REPOSITORY / "uv.lock",
    }
    for path in values:
        if path.is_symlink() or not path.is_file():
            raise ValueError(
                f"participating source must be a regular non-symlink file: {path}"
            )
    for root in (PACKAGE, Path(__file__).parent, REPOSITORY / "tests"):
        for path in _source_tree_files(root):
            if root != REPOSITORY / "tests" or path.name.startswith(
                (
                    "test_expert_builder",
                    "test_reference_repository",
                )
            ):
                values.add(path)
    return tuple(sorted(values))


def load_scenarios() -> JsonObject:
    """Load the static JSON scenario registry without another parser dependency."""
    payload = parse_object(
        SCENARIOS_PATH.read_text(encoding="utf-8"), source=str(SCENARIOS_PATH)
    )
    if type(payload.get("cases")) is not list:
        raise ValueError("scenarios.json must contain an object with a cases array")
    return payload


def _hash_sources() -> dict[str, str]:
    result: dict[str, str] = {}
    for path in _source_files():
        relative = str(path.relative_to(REPOSITORY))
        result[relative] = (
            hashlib.sha256(path.read_bytes()).hexdigest()
            if path.is_file()
            else "MISSING"
        )
    return result


def _git_state() -> JsonObject:
    completed = subprocess.run(
        ["git", "status", "--porcelain=v2"],
        cwd=REPOSITORY,
        capture_output=True,
        text=True,
        check=False,
    )
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPOSITORY,
        capture_output=True,
        text=True,
        check=False,
    )
    return {"head": head.stdout.strip(), "status": completed.stdout}


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _selection(
    scenarios: JsonMapping, suite: str, selected_case: str | None
) -> list[JsonObject]:
    values = scenarios.get("cases")
    if not isinstance(values, list):
        raise ValueError("cases must be a list")
    by_id = {
        str(case["id"]): case
        for case in values
        if isinstance(case, dict) and isinstance(case.get("id"), str)
    }
    expected_ids = FULL_SUITE_CASE_IDS[suite]
    if set(by_id) != set(FULL_SUITE_CASE_IDS["acceptance"]):
        raise ValueError(
            "scenario registry does not match the immutable evaluation policy"
        )
    selected = (
        [by_id[selected_case]]
        if selected_case is not None and selected_case in by_id
        else [by_id[case_id] for case_id in sorted(expected_ids)]
        if selected_case is None
        else []
    )
    if selected_case is not None and not selected:
        raise ValueError(f"unknown case: {selected_case}")
    return selected


def _unimplemented_observation(reason: str) -> JsonObject:
    return {
        "harness_error": {"phase": "collector", "reason": reason},
        "transport": {"outer_exit_code": None, "exec_envelope_exit_code": None},
        "subject": {
            "process_exit_code": None,
            "engine_status": "fail",
            "completed_nodes": [],
            "context": {},
            "failure_notes": reason,
        },
        "semantic": {"verdict": "fail", "class": "evaluation_infrastructure_failed"},
        "cleanup": {"created_ids": [], "destroy_attempted": [], "remaining_ids": []},
        "evidence": {"collector_error": reason},
    }


def _capture_case(case_dir: Path, scenario: JsonMapping) -> None:
    """Run one registered serial scenario and preserve its raw evidence."""
    ensure_evaluation_path(case_dir)
    _write_json(case_dir / "scenario.json", scenario)
    _write_json(case_dir / "command.json", {"runner": scenario["runner"]})
    runner = str(scenario["runner"])
    try:
        if runner == "controlled_reality":
            observation = run_controlled_reality_case(
                case_dir=case_dir,
                scenario=scenario,
                reality_check_dot=REALITY_CHECK_DOT,
            )
        elif runner == "controller_attractor":
            observation = run_controller_attractor_case(
                case_dir=case_dir,
                scenario=scenario,
                package_dir=PACKAGE,
                harness_dot=HARNESS_DOT,
            )
        elif runner == "parent_rc_probe":
            observation = run_parent_rc_probe_case(
                case_dir=case_dir,
                parent_dot=PARENT_DOT,
            )
        elif runner == "real_reality_canary":
            observation = run_real_reality_canary_case(
                case_dir=case_dir,
                scenario=scenario,
                reality_check_dot=REALITY_CHECK_DOT,
            )
        elif runner == "controlled_parent":
            observation = run_controlled_parent_case(
                case_dir=case_dir,
                scenario=scenario,
                parent_dot=PARENT_DOT,
            )
        else:
            observation = _unimplemented_observation(
                f"runner {runner!r} is not executed until Phase 3 graph wiring is present"
            )
    # Engine and transport adapters use RuntimeError for bounded operational failures;
    # convert only those documented collector failures into harness evidence.
    except (OSError, RuntimeError, subprocess.SubprocessError, ValueError) as error:
        observation = _unimplemented_observation(
            f"evaluation collector failed before a complete subject observation: {error}"
        )
    _write_json(case_dir / "observation.json", observation)
    for name in ("stdout.log", "stderr.log"):
        path = case_dir / name
        if not path.exists():
            path.write_text("", encoding="utf-8")
    (case_dir / "run-log").mkdir(exist_ok=True)
    (case_dir / "artifacts").mkdir(exist_ok=True)


def run(argv: list[str] | None = None) -> int:
    """Capture one fresh serial run and return the conjunctive grader exit code."""
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--suite", choices=("controlled", "acceptance"), default="acceptance"
    )
    parser.add_argument("--case")
    args = parser.parse_args(argv)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    run_dir = EVALUATION_ROOT / timestamp
    run_dir.mkdir(parents=True)
    ensure_evaluation_path(run_dir)
    scenarios = load_scenarios()
    try:
        selected = _selection(scenarios, args.suite, args.case)
        source_hashes = _hash_sources()
    except ValueError as error:
        _write_json(
            run_dir / "summary.json",
            {"verdict": Verdict.ERROR.value, "reason": str(error), "cases": []},
        )
        print(
            json.dumps(
                {
                    "run_dir": str(run_dir),
                    "verdict": Verdict.ERROR.value,
                    "reason": str(error),
                }
            )
        )
        return 2
    _write_json(
        run_dir / "manifest.json",
        {
            "suite": args.suite,
            "case": args.case,
            "git": _git_state(),
            "selected_cases": [case["id"] for case in selected],
            "source_files": sorted(source_hashes),
        },
    )
    _write_json(run_dir / "source-hashes.before.json", source_hashes)
    (run_dir / "preflight").mkdir()
    (run_dir / "controller").mkdir()
    for scenario in selected:
        case_dir = run_dir / "cases" / str(scenario["id"])
        case_dir.mkdir(parents=True)
        _capture_case(case_dir, scenario)
        grade = grade_case(case_dir, scenario)
        _write_json(case_dir / "grade.json", json.loads(json.dumps(grade, default=str)))
    _write_json(run_dir / "source-hashes.after.json", _hash_sources())
    summary = grade_run(run_dir)
    _write_json(run_dir / "summary.json", summary)
    (run_dir / "summary.txt").write_text(
        f"{summary['verdict']}: {', '.join(case['case_id'] for case in summary['cases'])}\n",
        encoding="utf-8",
    )
    print(json.dumps({"run_dir": str(run_dir), **summary}, sort_keys=True))
    return {Verdict.PASS.value: 0, Verdict.FAIL.value: 1, Verdict.ERROR.value: 2}[
        summary["verdict"]
    ]


if __name__ == "__main__":
    raise SystemExit(run())
