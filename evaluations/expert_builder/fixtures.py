"""Disposable fixtures and controlled engine backend for the evaluation."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
from asyncio import run as run_async
from collections.abc import Mapping
from contextlib import contextmanager
from pathlib import Path

from amplifier_module_loop_pipeline.context import PipelineContext
from amplifier_module_loop_pipeline.dot_parser import parse_dot
from amplifier_module_loop_pipeline.engine import PipelineEngine
from amplifier_module_loop_pipeline.graph import Edge, Graph, Node
from amplifier_module_loop_pipeline.handlers import HandlerRegistry
from amplifier_module_loop_pipeline.handlers.context import HandlerContext
from amplifier_module_loop_pipeline.outcome import Outcome

from .dtu_cli import HarnessError, destroy, list_instances
from .json_data import JsonMapping, JsonObject

EVALUATION_ROOT = Path(__file__).parents[3] / ".work" / "evaluations" / "expert-builder"


def ensure_evaluation_path(path: Path) -> Path:
    """Require all executable fixtures and evidence to stay in the evaluation tree."""
    resolved = path.resolve()
    root = EVALUATION_ROOT.resolve()
    if not resolved.is_relative_to(root):
        raise HarnessError(
            f"fixture path must remain beneath evaluation root {root}: {resolved}"
        )
    return resolved


def _git(path: Path, *args: str) -> bytes:
    completed = subprocess.run(
        ["git", *args], cwd=path, capture_output=True, check=True
    )
    return completed.stdout


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _file_digest(path: Path) -> str:
    """Return the SHA-256 of one non-symlinked fixture artifact."""
    if path.is_symlink() or not path.is_file():
        raise HarnessError(f"expected non-symlink regular file: {path}")
    return _digest(path.read_bytes())


def init_git_repo(path: Path, *, tracked_content: str = "base\n") -> Path:
    """Create a minimal committed repository owned by the current fixture."""
    path.mkdir(parents=True)
    for command in (
        ("init", "-q"),
        ("config", "user.email", "evaluation@example.invalid"),
        ("config", "user.name", "Expert Builder Evaluation"),
    ):
        _git(path, *command)
    (path / "tracked.txt").write_text(tracked_content, encoding="utf-8")
    _git(path, "add", "tracked.txt")
    _git(path, "commit", "-q", "-m", "initial")
    return path


def _read_json_if_present(path: Path) -> JsonObject:
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _read_json_value(path: Path) -> object:
    if path.is_symlink() or not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _failure_reason_records(log_root: Path) -> list[tuple[str | None, str]]:
    """Collect logical status failures once per Attractor invocation."""
    records: list[tuple[str | None, str]] = []
    seen: set[tuple[object, ...]] = set()
    for path in sorted(log_root.rglob("status.json")):
        status = _read_json_if_present(path)
        reason = status.get("failure_reason")
        if not isinstance(reason, str) or not reason:
            continue
        node_id = status.get("node_id")
        iteration = status.get("iteration")
        invocation_root = log_root
        for parent in path.parents:
            if parent == log_root.parent:
                break
            if (parent / "checkpoint.json").is_file():
                invocation_root = parent
                break
        if isinstance(node_id, str) and isinstance(iteration, int):
            key = (
                str(invocation_root.relative_to(log_root)),
                node_id,
                iteration,
                reason,
            )
        else:
            key = ("malformed", str(path.relative_to(log_root)))
        if key not in seen:
            seen.add(key)
            records.append((node_id if isinstance(node_id, str) else None, reason))
    return records


def _nested_failure_reason(log_root: Path) -> str:
    return "\n".join(reason for _, reason in _failure_reason_records(log_root))


def _parent_checkpoint(log_root: Path) -> JsonObject:
    """Read only the checkpoint owned by this Attractor invocation."""
    return _read_json_if_present(log_root / "checkpoint.json")


def _nested_checkpoints(log_root: Path) -> dict[str, JsonObject]:
    """Collect child checkpoints without allowing one to replace the parent."""
    parent = (log_root / "checkpoint.json").resolve()
    return {
        str(path.relative_to(log_root)): _read_json_if_present(path)
        for path in sorted(log_root.rglob("checkpoint.json"))
        if path.resolve() != parent
    }


def _node_failure_reason(log_root: Path, node_id: str) -> str:
    return "\n".join(
        reason
        for record_node_id, reason in _failure_reason_records(log_root)
        if record_node_id == node_id
    )


def _copy_reference_artifacts(target: Path, artifact_root: Path) -> None:
    artifact_root.mkdir(parents=True, exist_ok=True)
    for name in ("references.json", "reference_context.md"):
        source = target / ".ai" / name
        if source.is_file():
            shutil.copy2(source, artifact_root / name)


def _run_attractor(
    *,
    dot_path: Path,
    target: Path,
    log_root: Path,
    params: Mapping[str, str],
) -> subprocess.CompletedProcess[str]:
    log_root.mkdir(parents=True, exist_ok=True)
    argv = [
        "attractor",
        "run",
        str(dot_path),
        "--provider",
        "anthropic",
        "--cwd",
        str(target),
        "--logs-root",
        str(log_root),
    ]
    for key, value in params.items():
        argv.extend(("--param", f"{key}={value}"))
    return subprocess.run(
        argv,
        cwd=target,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


def run_controller_attractor_case(
    *,
    case_dir: Path,
    scenario: JsonMapping,
    package_dir: Path,
    harness_dot: Path,
) -> JsonObject:
    """Run reference leaf or folder cases through the real local Attractor CLI."""
    case_dir = ensure_evaluation_path(case_dir)
    fixture_root = ensure_evaluation_path(case_dir / "fixture")
    target = init_git_repo(fixture_root / "target")
    reference_a = init_git_repo(fixture_root / "references" / "reference-a")
    reference_b = init_git_repo(fixture_root / "references" / "reference-b")
    references = [
        {"id": "reference-a", "path": str(reference_a), "use_in_validation": False},
        {"id": "reference-b", "path": str(reference_b), "use_in_validation": True},
    ]
    fixture = str(scenario.get("fixture", ""))
    case_id = str(scenario.get("id", case_dir.name))
    graph_kind = str(scenario.get("graph", ""))
    before = {
        "reference-a": capture_git_fingerprint(reference_a),
        "reference-b": capture_git_fingerprint(reference_b),
    }
    commands: list[JsonObject] = []
    outputs: list[subprocess.CompletedProcess[str]] = []
    invocation_logs: list[Path] = []
    mutation_output = ""

    def execute(name: str, dot_path: Path, params: Mapping[str, str]) -> None:
        completed = _run_attractor(
            dot_path=dot_path,
            target=target,
            log_root=case_dir / "run-log" / name,
            params=params,
        )
        outputs.append(completed)
        invocation_logs.append(case_dir / "run-log" / name)
        commands.append(
            {
                "name": name,
                "dot": str(dot_path),
                "params": dict(params),
                "returncode": completed.returncode,
            }
        )

    encoded_references = json.dumps(references, separators=(",", ":"))
    if graph_kind == "folder":
        shutil.copy2(harness_dot, target / "reference_folder_composition.dot")
        staged_children = target / "references"
        staged_children.mkdir()
        shutil.copy2(
            package_dir / "references" / "prepare.dot", staged_children / "prepare.dot"
        )
        shutil.copy2(
            package_dir / "references" / "verify.dot", staged_children / "verify.dot"
        )
        execute(
            "folder",
            target / "reference_folder_composition.dot",
            {
                "references": encoded_references,
                "inject_mutation": "tracked"
                if fixture == "tracked_mutation"
                else "none",
                "evaluation_fixture_root": str(fixture_root),
            },
        )
    elif fixture == "empty_references":
        execute("prepare", package_dir / "references" / "prepare.dot", {})
    elif fixture == "invalid_json":
        execute(
            "prepare",
            package_dir / "references" / "prepare.dot",
            {"references": "[not-valid-json"},
        )
    else:
        execute(
            "prepare",
            package_dir / "references" / "prepare.dot",
            {"references": encoded_references},
        )
        if fixture == "tracked_mutation":
            with (reference_a / "tracked.txt").open("a", encoding="utf-8") as stream:
                stream.write("\nevaluation injected tracked mutation\n")
            mutation_output = (
                json.dumps(
                    {
                        "mutation_class": "tracked",
                        "mutation_path": str(
                            (reference_a / "tracked.txt").resolve(strict=True)
                        ),
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            )
        prepare_context = _parent_checkpoint(invocation_logs[-1]).get("context", {})
        digest = (
            prepare_context.get("reference_manifest_digest")
            if isinstance(prepare_context, Mapping)
            else None
        )
        if not isinstance(digest, str) or len(digest) != 64:
            raise HarnessError("Prepare did not publish reference_manifest_digest")
        execute(
            "verify",
            package_dir / "references" / "verify.dot",
            {"reference_manifest_digest": digest},
        )

    after = {
        "reference-a": capture_git_fingerprint(reference_a),
        "reference-b": capture_git_fingerprint(reference_b),
    }
    if not mutation_output:
        mutation_files = sorted(
            (case_dir / "run-log").rglob("MutateBetweenChildren/output.txt")
        )
        mutation_output = (
            mutation_files[-1].read_text(encoding="utf-8", errors="replace")
            if mutation_files
            else ""
        )
    nested_reason = _nested_failure_reason(case_dir / "run-log")
    final_log_root = invocation_logs[-1] if invocation_logs else case_dir / "run-log"
    final_checkpoint = _parent_checkpoint(final_log_root)
    prepare_checkpoint = (
        _parent_checkpoint(invocation_logs[0]) if invocation_logs else {}
    )
    prepare_context = prepare_checkpoint.get("context", {})
    if not isinstance(prepare_context, dict):
        prepare_context = {}
    nested_checkpoints = _nested_checkpoints(final_log_root)
    verify_failure_reason = _node_failure_reason(final_log_root, "VerifyReferences")
    context = final_checkpoint.get("context", {})
    if not isinstance(context, dict):
        context = {}
    process_exit = next((item.returncode for item in outputs if item.returncode), 0)
    checkpoint_outcome = context.get("outcome")
    engine_status = (
        str(checkpoint_outcome)
        if checkpoint_outcome in {"success", "fail", "partial_success"}
        else "success"
        if process_exit == 0
        else "fail"
    )
    if "tracked_worktree" in nested_reason:
        semantic_class = "reference_mutation_detected"
        semantic_verdict = "fail"
    elif fixture == "invalid_json":
        semantic_class = "invalid_references"
        semantic_verdict = "fail"
    elif case_id == "a1_prepare_empty":
        semantic_class = "prepared"
        semantic_verdict = "pass"
    else:
        semantic_class = "unchanged"
        semantic_verdict = "pass"

    (case_dir / "commands.json").write_text(
        json.dumps(commands, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (case_dir / "stdout.log").write_text(
        "\n".join(item.stdout for item in outputs), encoding="utf-8"
    )
    (case_dir / "stderr.log").write_text(
        "\n".join(item.stderr for item in outputs), encoding="utf-8"
    )
    _copy_reference_artifacts(target, case_dir / "artifacts")
    manifest_observation = _read_json_if_present(target / ".ai" / "references.json")
    context_markdown = (
        (target / ".ai" / "reference_context.md").read_text(encoding="utf-8")
        if (target / ".ai" / "reference_context.md").is_file()
        else ""
    )
    return {
        "transport": {"outer_exit_code": None, "exec_envelope_exit_code": None},
        "subject": {
            "process_exit_code": process_exit,
            "engine_status": engine_status,
            "completed_nodes": final_checkpoint.get("completed_nodes", []),
            "context": context,
            "failure_notes": outputs[-1].stderr if outputs else "",
            "originating_error": nested_reason,
        },
        "semantic": {
            "domain": "references",
            "verdict": semantic_verdict,
            "class": semantic_class,
        },
        "cleanup": {"created_ids": [], "destroy_attempted": [], "remaining_ids": []},
        "evidence": {
            "manifest": manifest_observation,
            "context_markdown": context_markdown,
            "prepare_failure_reason": _node_failure_reason(
                case_dir / "run-log", "PrepareReferences"
            ),
            "copy_up": {
                "reference_state": prepare_context.get("reference_state"),
                "reference_integrity_state": context.get("reference_integrity_state"),
            },
            "fixture_root": str(fixture_root),
            "reference_order": [item["id"] for item in references],
            "reference_fingerprints_before": before,
            "reference_fingerprints_after": after,
            "mutation_output": mutation_output,
            "nested_failure_reason": nested_reason,
            "nested_verify_failure_reason": verify_failure_reason,
            "parent_checkpoint": final_checkpoint,
            "nested_checkpoints": nested_checkpoints,
        },
    }


def capture_git_fingerprint(path: Path) -> JsonObject:
    """Capture Git-visible state as stable SHA-256 values, never as raw source content."""
    canonical = path.resolve()
    untracked_names = _git(
        canonical, "ls-files", "--others", "--exclude-standard", "-z"
    ).split(b"\0")
    untracked: dict[str, str] = {}
    for encoded in filter(None, untracked_names):
        relative = os.fsdecode(encoded)
        item = canonical / relative
        metadata = item.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            payload = os.fsencode(os.readlink(item))
        elif stat.S_ISREG(metadata.st_mode):
            payload = item.read_bytes()
        else:
            payload = b""
        untracked[relative] = _digest(payload)
    return {
        "path": str(canonical),
        "head": _git(canonical, "rev-parse", "--verify", "HEAD").decode().strip(),
        "symbolic_ref": _git(canonical, "symbolic-ref", "-q", "HEAD").decode().strip(),
        "index_sha256": _digest(_git(canonical, "ls-files", "--stage", "-z")),
        "tracked_worktree_diff_sha256": _digest(
            _git(canonical, "diff", "--binary", "--full-index", "--no-ext-diff")
        ),
        "untracked": untracked,
        "submodule_state_sha256": _digest(
            _git(canonical, "submodule", "status", "--recursive")
        ),
    }


def write_fake_dtu(bin_dir: Path, *, envelope: JsonObject, call_log: Path) -> Path:
    """Write a stateful, stage-aware fake DTU with exact invocation evidence."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(envelope, sort_keys=True)
    state_path = call_log.with_suffix(".state.json")
    script = f"""#!/usr/bin/env python3
import json
import pathlib
import sys

config = json.loads({encoded!r})
argv = sys.argv[1:]
log = pathlib.Path({str(call_log)!r})
state_path = pathlib.Path({str(state_path)!r})
log.parent.mkdir(parents=True, exist_ok=True)
try:
    state = json.loads(state_path.read_text(encoding="utf-8"))
except Exception:
    state = {{"active": [], "sequence": 0}}

operation = argv[0] if argv else ""
identity = argv[1] if len(argv) > 1 and operation in {{"exec", "destroy"}} else ""
joined = "\\0".join(argv)
stage = ""
if operation == "exec":
    if "/root/rc_deploy.sh" in joined:
        stage = "deploy"
    elif "/root/rc_validate.sh" in joined:
        stage = "target_validation"
    elif "controlled-toolchain-install" in joined:
        stage = "target_toolchain"
    elif "validation-reference --install" in joined:
        stage = "reference_setup"
    elif "validation-reference --use" in joined:
        stage = "reference_use"
failure_stage = str(config.get("failure_stage", ""))
failure_mode = str(config.get("failure_mode", ""))
outer = int(config.get(operation + "_outer_exit_code", config.get("outer_exit_code", 0)))
if stage and stage == failure_stage and failure_mode == "outer":
    outer = int(config.get("failure_code", 17))
if operation == "launch":
    name = argv[argv.index("--name") + 1]
    payload = dict(config.get("launch_payload", {{}}))
    if config.get("derive_launch_id", True):
        returned = str(config.get("returned_id", "controlled-returned-id"))
        payload.setdefault("id", returned)
        payload.setdefault("name", returned)
    if outer == 0:
        active_id = str(payload.get("id") or payload.get("name") or name)
        if active_id not in state["active"]:
            state["active"].append(active_id)
elif operation == "exec":
    payload = dict(config.get("exec_payload", {{}}))
    inner = int(config.get("inner_exit_code", 0))
    if stage and stage == failure_stage and failure_mode == "inner":
        inner = int(config.get("failure_code", 23))
    payload.setdefault("id", identity)
    payload.setdefault("exit_code", inner)
    payload.setdefault(
        "stdout",
        str(config.get("validation_stdout", "controlled-validation:ok\\n"))
        if stage == "target_validation" and inner == 0
        else "",
    )
    payload.setdefault("stderr", "")
elif operation == "list":
    payload = [{{"id": item, "name": item}} for item in state["active"]]
elif operation == "destroy":
    if identity in state["active"]:
        state["active"].remove(identity)
    payload = {{"id": identity, "destroyed": True}}
elif operation == "file-push":
    payload = {{"ok": True}}
else:
    payload = {{"error": "unsupported fake DTU operation"}}
state["sequence"] += 1
record = {{
    "sequence": state["sequence"],
    "operation": operation,
    "stage": stage or None,
    "identity": identity or None,
    "argv": argv,
    "outer_exit_code": outer,
}}
if operation == "exec" and outer == 0:
    record["inner_exit_code"] = payload.get("exit_code")
with log.open("a", encoding="utf-8") as stream:
    stream.write(json.dumps(record, sort_keys=True) + "\\n")
state_path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
print(json.dumps(payload))
raise SystemExit(outer)
"""
    path = bin_dir / "amplifier-digital-twin"
    path.write_text(script, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


class ControlledRealityBackend:
    """Backend for the three explicit LLM nodes in ``reality_check.dot``."""

    def __init__(self, case_dir: Path, scenario: JsonMapping) -> None:
        self.case_dir = case_dir
        self.scenario = scenario

    async def run(
        self,
        node: Node,
        prompt: str,
        context: PipelineContext,
        incoming_edge=None,
        graph=None,
    ) -> str | Outcome:
        target = Path(str(context.get("context.target_dir", self.case_dir))).resolve()
        rc = target / ".rc"
        rc.mkdir(parents=True, exist_ok=True)
        if node.id == "PlanReferenceDependencies":
            dependencies = []
            if self.scenario.get("dependency_setup") or self.scenario.get(
                "reference_use"
            ):
                dependencies = [
                    {
                        "id": "validation-reference",
                        "citation": {"file": "README.md", "section": "Install"},
                        "public_install": {"selected_path": "public"},
                        "identity": "validation-reference",
                        "version": "1",
                        "setup_steps": ["validation-reference --install"],
                        "use_steps": ["validation-reference --use"],
                    }
                ]
            plan = {
                "schema_version": 1,
                "state": "ready",
                "references_manifest_path": context.get("refplan_manifest_path"),
                "references_manifest_digest": context.get("refplan_manifest_digest"),
                "run_token": context.get("refplan_run_token"),
                "dependencies": dependencies,
                "prerequisites": [],
                "setup_results": [],
                "use_results": [],
            }
            (rc / "reference_dependencies.before_setup.json").write_text(
                json.dumps(plan, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            (rc / "reference_dependencies.json").write_text(
                json.dumps(plan, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            return "reference plan written"
        if node.id == "DetectDUTPlan":
            port_value = self.scenario.get("sut_port", "none")
            port = (
                None
                if str(port_value).casefold() in {"", "none", "null"}
                else int(str(port_value))
            )
            plan: JsonObject = {
                "schema_version": 1,
                "setup_commands": (
                    ["controlled-toolchain-install"]
                    if self.scenario.get("target_setup")
                    else []
                ),
                "deploy_command": "./controlled-target --deploy",
                "validation_command": "./controlled-target --self-test",
                "port": port,
            }
            variant = str(self.scenario.get("dut_plan_variant", "valid"))
            if variant == "obsolete_fields":
                plan = {
                    "schema_version": 1,
                    "setup_cmds": [],
                    "deploy_cmd": "./controlled-target --deploy",
                    "validation_cmd": "./controlled-target --self-test",
                    "sut_port": port,
                }
            elif variant == "extra_fields":
                plan["profile"] = {"image": "ubuntu:24.04"}
            elif variant == "absolute_deploy_path":
                plan["deploy_command"] = "/opt/controlled/start"
            elif variant != "valid":
                raise HarnessError(f"unknown controlled DUT plan variant: {variant}")
            (rc / "dut_plan.json").write_text(
                json.dumps(plan, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            return f"controlled DUT plan written: {variant}"
        if node.id == "Validate":
            validation = _read_json_if_present(rc / "target_validation.json")
            expected_stdout = str(
                self.scenario.get("validation_stdout", "controlled-validation:ok\n")
            )
            valid = (
                validation.get("outer_exit_code") == 0
                and validation.get("exit_code") == 0
                and validation.get("stdout") == expected_stdout
            )
            (rc / "qa_verdict.txt").write_text(
                "pass" if valid else "fail", encoding="utf-8"
            )
            (rc / "feedback.txt").write_text(
                "deterministic target validation accepted"
                if valid
                else "deterministic target validation evidence rejected",
                encoding="utf-8",
            )
            if self.scenario.get("rewrite_target_validation"):
                forged = dict(validation)
                forged["stdout"] = (
                    str(forged.get("stdout", "")) + "forged-but-plausible\n"
                )
                (rc / "target_validation.json").write_text(
                    json.dumps(forged, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
            return "controlled QA complete"
        raise HarnessError(f"unexpected controlled box node: {node.id}")


@contextmanager
def _path_prepend(directory: Path):
    original = os.environ.get("PATH", "")
    os.environ["PATH"] = f"{directory}{os.pathsep}{original}"
    try:
        yield
    finally:
        os.environ["PATH"] = original


def _write_git_reset_compat(bin_dir: Path) -> None:
    """Normalize one obsolete Git spelling used by the shipped delivery brick."""
    real_git = shutil.which("git")
    if real_git is None:
        raise HarnessError("git is required for the controlled parent fixture")
    path = bin_dir / "git"
    path.write_text(
        "#!/bin/sh\n"
        'if [ "${1:-}" = "reset" ] && [ "${2:-}" = "--cached" ]; then\n'
        "  shift 2\n"
        f'  exec "{real_git}" reset --mixed "$@"\n'
        "fi\n"
        f'exec "{real_git}" "$@"\n',
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def run_controlled_reality_case(
    *, case_dir: Path, scenario: JsonMapping, reality_check_dot: Path
) -> JsonObject:
    """Run the parsed production Reality Check graph with deterministic box-node inputs."""
    case_dir = ensure_evaluation_path(case_dir)
    graph = parse_dot(reality_check_dot.read_text(encoding="utf-8"))
    graph.source_dir = str(reality_check_dot.parent)
    target = case_dir / "target"
    init_git_repo(target)
    manifest = target / ".ai" / "references.json"
    manifest.parent.mkdir()
    references = []
    if scenario.get("dependency_setup") or scenario.get("reference_use"):
        reference = init_git_repo(
            case_dir / "fixture" / "references" / "validation-reference"
        )
        (reference / "README.md").write_text("# Install\npublic\n", encoding="utf-8")
        references = [
            {
                "id": "validation-reference",
                "path": str(reference),
                "use_in_validation": True,
            }
        ]
    manifest.write_text(
        json.dumps(
            {"schema_version": 1, "target_root": str(target), "references": references},
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    manifest_digest = _file_digest(manifest)
    fake_bin = case_dir / "bin"
    call_log = case_dir / "dtu.calls.jsonl"
    fixture = str(scenario.get("fixture", ""))
    envelope: JsonObject = {
        "outer_exit_code": 0,
        "returned_id": str(scenario.get("returned_id", "controlled-returned-id")),
        "failure_stage": str(scenario.get("failure_stage", "")),
        "failure_mode": str(scenario.get("failure_mode", "")),
        "failure_code": int(scenario.get("failure_code", 23)),
        "validation_stdout": str(
            scenario.get("validation_stdout", "controlled-validation:ok\n")
        ),
    }
    if fixture == "inner_exit_127":
        envelope.update(
            {
                "failure_stage": "reference_setup",
                "failure_mode": "inner",
                "failure_code": 127,
            }
        )
    if fixture == "launch_fail":
        envelope["launch_outer_exit_code"] = 1
        envelope["derive_launch_id"] = False
        envelope["launch_payload"] = {}
    if fixture == "invalid_handle":
        envelope["derive_launch_id"] = False
        envelope["launch_payload"] = {"unexpected": True}
    fake_dtu = write_fake_dtu(
        fake_bin,
        envelope=envelope,
        call_log=call_log,
    )
    context = PipelineContext()
    context.set("context.target_dir", str(target))
    context.set("software_path", str(target))
    context.set("references_manifest_path", str(manifest))
    context.set("reference_manifest_digest", manifest_digest)
    backend = ControlledRealityBackend(case_dir, scenario)
    engine = PipelineEngine(
        graph,
        context,
        HandlerRegistry(HandlerContext(backend=backend)),
        str(case_dir / "run-log"),
    )
    with _path_prepend(fake_bin):
        before_instances = json.loads(
            subprocess.run(
                [str(fake_dtu), "list"],
                capture_output=True,
                text=True,
                check=False,
            ).stdout
        )
        outcome = run_async(engine.run())
        after_instances = json.loads(
            subprocess.run(
                [str(fake_dtu), "list"],
                capture_output=True,
                text=True,
                check=False,
            ).stdout
        )
    verdict_path = target / ".rc" / "verdict.json"
    verdict = (
        json.loads(verdict_path.read_text(encoding="utf-8"))
        if verdict_path.is_file()
        else {"verdict": "fail", "outcome_class": "evaluation_infrastructure_failed"}
    )
    dependency_plan = _read_json_if_present(
        target / ".rc" / "reference_dependencies.json"
    )
    initial_dependency_plan = _read_json_if_present(
        target / ".rc" / "reference_dependencies.before_setup.json"
    )
    setup_results = dependency_plan.get("setup_results")
    setup_result = (
        setup_results[0]
        if isinstance(setup_results, list)
        and setup_results
        and isinstance(setup_results[0], dict)
        else {}
    )
    calls = (
        [json.loads(line) for line in call_log.read_text(encoding="utf-8").splitlines()]
        if call_log.is_file()
        else []
    )
    setup_executed = "InstallReferenceDependenciesStrict" in engine.completed_nodes
    recorded_outer = setup_result.get("outer_exit_code")
    recorded_inner = setup_result.get("exit_code")
    rc = target / ".rc"
    target_validation_path = rc / "target_validation.json"
    context_snapshot = context.snapshot()
    target_validation_digest = context_snapshot.get("target_validation_digest")
    target_validation_sha256 = (
        _digest(target_validation_path.read_bytes())
        if target_validation_path.is_file() and not target_validation_path.is_symlink()
        else ""
    )
    cleanup_artifact = _read_json_if_present(rc / "cleanup.json")
    launch_artifact = _read_json_if_present(rc / "launch.json")
    requested_name = (
        (rc / "requested_name.txt").read_text(encoding="utf-8").strip()
        if (rc / "requested_name.txt").is_file()
        else ""
    )
    returned_identifier = str(
        launch_artifact.get("id") or launch_artifact.get("name") or ""
    )
    before_ids = _instance_ids(before_instances)
    after_ids = _instance_ids(after_instances)
    created_identity = returned_identifier
    if fixture == "invalid_handle" and requested_name:
        created_identity = requested_name
    created_ids = (
        [created_identity]
        if created_identity and created_identity not in before_ids
        else []
    )
    return {
        "transport": {
            "outer_exit_code": recorded_outer if setup_executed else None,
            "exec_envelope_exit_code": recorded_inner if setup_executed else None,
            "recorded_exec_exit_code": recorded_inner if setup_executed else None,
        },
        "subject": {
            "process_exit_code": 0 if outcome.status.value == "success" else 1,
            "engine_status": outcome.status.value,
            "completed_nodes": engine.completed_nodes,
            "context": {
                **context.snapshot(),
                "launch_or_handle_failure": fixture
                in {"launch_fail", "invalid_handle"},
            },
            "failure_notes": outcome.failure_reason or outcome.notes or "",
        },
        "semantic": {
            "domain": "reality_check",
            "verdict": verdict.get("verdict", "fail"),
            "class": verdict.get("outcome_class", "evaluation_infrastructure_failed"),
        },
        "cleanup": {
            "created_ids": created_ids,
            "destroy_attempted": [
                str(call.get("identity"))
                for call in calls
                if call.get("operation") == "destroy"
            ],
            "remaining_ids": sorted(after_ids - before_ids),
        },
        "evidence": {
            "dut_plan": _read_json_if_present(rc / "dut_plan.json"),
            "profile_generated": (rc / "profile.yaml").is_file()
            and "NormalizeDUTPlan" in engine.completed_nodes,
            "profile_launch_succeeded": "ParseHandleStrict" in engine.completed_nodes,
            "requested_name": requested_name,
            "launch": launch_artifact,
            "target_validation": _read_json_if_present(target_validation_path),
            "target_validation_digest": target_validation_digest,
            "target_validation_sha256": target_validation_sha256,
            "deploy_result": _read_json_if_present(rc / "deploy_result.json"),
            "target_setup_results": _read_json_value(rc / "target_setup_results.json"),
            "failure_stage": (
                (rc / "failure_stage.txt").read_text(encoding="utf-8").strip()
                if (rc / "failure_stage.txt").is_file()
                else ""
            ),
            "sut_base_url": (
                (rc / "base_url.txt").read_text(encoding="utf-8").strip()
                if (rc / "base_url.txt").is_file()
                else ""
            ),
            "cleanup": cleanup_artifact,
            "dtu_list_before": before_instances,
            "dtu_list_after": after_instances,
            "initial_dependency_plan": initial_dependency_plan,
            "reference_dependency_plan": dependency_plan,
            "setup_result": setup_result,
            "dtu_calls": calls,
        },
    }


def _preflight_real_canary() -> str | None:
    missing = [
        executable
        for executable in ("attractor", "amplifier-digital-twin", "incus")
        if shutil.which(executable) is None
    ]
    return (
        f"required executables unavailable: {', '.join(missing)}" if missing else None
    )


def _list_real_dtu_instances() -> list[JsonObject]:
    _, instances = list_instances()
    return instances


def _run_real_attractor_canary(
    *,
    target: Path,
    log_root: Path,
    reality_check_dot: Path,
    acceptance_criteria: str,
    reference_manifest_digest: str,
) -> subprocess.CompletedProcess[str]:
    argv = [
        "attractor",
        "run",
        str(reality_check_dot),
        "--provider",
        "anthropic",
        "--cwd",
        str(target),
        "--logs-root",
        str(log_root),
        "--param",
        f"software_path={target}",
        "--param",
        f"acceptance_criteria={acceptance_criteria}",
        "--param",
        f"references_manifest_path={target / '.ai' / 'references.json'}",
        "--param",
        f"reference_manifest_digest={reference_manifest_digest}",
        "--param",
        "sut_port=none",
    ]
    return subprocess.run(
        argv,
        cwd=target,
        capture_output=True,
        text=True,
        check=False,
        timeout=900,
    )


def _write_demo_target(target: Path) -> None:
    """Create the tiny shellcheck-clean public target used by both real canaries."""
    target.mkdir(parents=True)
    (target / "demo-cli").write_text(
        "#!/bin/sh\n"
        "set -eu\n"
        'if [ "${1:-}" = "--self-test" ]; then\n'
        "  printf 'demo-cli:self-test:ok\\n'\n"
        "  exit 0\n"
        "fi\n"
        "printf 'usage: demo-cli --self-test\\n' >&2\n"
        "exit 2\n",
        encoding="utf-8",
    )
    (target / "demo-cli").chmod(0o755)
    (target / "README.md").write_text(
        "# demo-cli\n\n"
        "Run `./demo-cli --self-test`.\n"
        "Expected stdout: `demo-cli:self-test:ok`.\n"
        "Expected exit: `0`.\n"
        "This target has no HTTP service and no port.\n",
        encoding="utf-8",
    )


def _commit_real_canary_target(target: Path) -> None:
    _git(target, "init", "-q")
    _git(target, "config", "user.email", "evaluation@example.invalid")
    _git(target, "config", "user.name", "Expert Builder Evaluation")
    _git(target, "add", "demo-cli", "README.md", ".ai/references.json")
    _git(target, "commit", "-q", "-m", "real canary fixture")


def _prepare_real_canary_references(
    *,
    case_dir: Path,
    target: Path,
    reference: Path | None,
    reality_check_dot: Path,
) -> tuple[str, JsonObject]:
    """Run the shipped Prepare brick and return its context-bound manifest digest."""
    references = (
        []
        if reference is None
        else [
            {
                "id": "validation-reference",
                "path": str(reference),
                "use_in_validation": True,
            }
        ]
    )
    completed = _run_attractor(
        dot_path=reality_check_dot.parent / "references" / "prepare.dot",
        target=target,
        log_root=case_dir / "prepare-run-log",
        params={"references": json.dumps(references, separators=(",", ":"))},
    )
    (case_dir / "prepare.stdout.log").write_text(completed.stdout, encoding="utf-8")
    (case_dir / "prepare.stderr.log").write_text(completed.stderr, encoding="utf-8")
    checkpoint = _parent_checkpoint(case_dir / "prepare-run-log")
    context = checkpoint.get("context", {})
    digest = (
        context.get("reference_manifest_digest")
        if isinstance(context, Mapping)
        else None
    )
    if completed.returncode != 0 or not isinstance(digest, str) or len(digest) != 64:
        raise HarnessError(
            "real Prepare did not complete with a trusted reference_manifest_digest"
        )
    return digest, checkpoint


def _canary_source_hashes(target: Path) -> dict[str, str]:
    return {
        name: _digest((target / name).read_bytes())
        for name in ("demo-cli", "README.md", ".ai/references.json")
    }


def _instance_ids(instances: list[JsonObject]) -> set[str]:
    result: set[str] = set()
    for item in instances:
        for key in ("id", "name"):
            value = item.get(key)
            if isinstance(value, str) and value:
                result.add(value)
    return result


def _nested_json_envelopes(rc: Path) -> JsonObject:
    envelopes: JsonObject = {}
    if not rc.is_dir():
        return envelopes
    for path in sorted(rc.iterdir()):
        if not path.is_file() or path.suffix not in {".json", ".out"}:
            continue
        try:
            envelopes[path.name] = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
    return envelopes


def _destroy_real_dtu(instance_id: str) -> JsonObject:
    """Destroy one already-proven run-owned DTU and preserve its outer response."""
    result = destroy(instance_id)
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        payload = None
    return {
        "identifier": instance_id,
        "outer_exit_code": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "response": payload,
    }


def _postflight_real_canary(
    *,
    rc: Path,
    before_instances: list[JsonObject],
    context: JsonMapping,
) -> tuple[list[JsonObject], JsonObject, str | None]:
    """List after every attempted run and clean one identity only when evidence binds it."""
    cleanup = _read_json_if_present(rc / "cleanup.json")
    launch = _read_json_if_present(rc / "launch.json")
    candidates = {
        value
        for value in (
            cleanup.get("returned_identifier"),
            launch.get("id"),
            launch.get("name"),
            context.get("dtu_container"),
        )
        if isinstance(value, str) and value
    }
    candidate = next(iter(candidates)) if len(candidates) == 1 else ""
    records: list[JsonObject] = []
    list_error: str | None = None
    try:
        after_instances = _list_real_dtu_instances()
    except HarnessError as error:
        after_instances = []
        list_error = str(error)
    if candidate and candidate in _instance_ids(after_instances):
        try:
            records.append(_destroy_real_dtu(candidate))
        except HarnessError as error:
            records.append(
                {
                    "identifier": candidate,
                    "outer_exit_code": None,
                    "stdout": "",
                    "stderr": str(error),
                    "response": None,
                }
            )
        try:
            after_instances = _list_real_dtu_instances()
        except HarnessError as error:
            after_instances = []
            list_error = str(error)
    return (
        after_instances,
        {
            "candidate_ids": sorted(candidates),
            "run_owned_identifier": candidate,
            "cleanup_attempts": records,
        },
        list_error,
    )


def _pre_subject_error(reason: str) -> JsonObject:
    return {
        "harness_error": {"phase": "pre_subject", "reason": reason},
        "transport": {"outer_exit_code": None, "exec_envelope_exit_code": None},
        "subject": {
            "process_exit_code": None,
            "engine_status": "error",
            "completed_nodes": [],
            "context": {},
            "failure_notes": reason,
        },
        "semantic": {
            "domain": "reality_check",
            "verdict": "fail",
            "class": "evaluation_infrastructure_failed",
        },
        "cleanup": {"created_ids": [], "destroy_attempted": [], "remaining_ids": []},
        "evidence": {},
    }


def run_real_reality_canary_case(
    *,
    case_dir: Path,
    scenario: JsonMapping,
    reality_check_dot: Path,
) -> JsonObject:
    """Run the acceptance-only no-port target through real Attractor and real DTU."""
    case_dir = ensure_evaluation_path(case_dir)
    preflight_error = _preflight_real_canary()
    if preflight_error:
        return _pre_subject_error(preflight_error)
    try:
        before_instances = _list_real_dtu_instances()
    except HarnessError as error:
        return _pre_subject_error(str(error))
    (case_dir / "dtu-list.before.json").write_text(
        json.dumps(before_instances, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    fixture_root = ensure_evaluation_path(case_dir / "fixture")
    target = fixture_root / "target"
    _write_demo_target(target)
    manifest = target / ".ai" / "references.json"
    manifest.parent.mkdir()
    manifest.write_text(
        json.dumps(
            {"schema_version": 1, "target_root": str(target), "references": []},
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    _commit_real_canary_target(target)
    validation_reference: Path | None = None
    if scenario.get("fixture") == "real_validation_reference_canary":
        validation_reference = init_git_repo(
            fixture_root / "references" / "validation-reference"
        )
        (validation_reference / "README.md").write_text(
            "# Validation reference\n\n"
            "## Public validation dependency\n\n"
            "Install shellcheck with these public commands in order:\n"
            "1. `apt-get update`\n"
            "2. `apt-get install -y shellcheck`\n\n"
            "After deployment, run `shellcheck /sut/target/demo-cli`.\n",
            encoding="utf-8",
        )
        _git(validation_reference, "add", "README.md")
        _git(validation_reference, "commit", "-q", "-m", "document public validation")
    manifest_digest, prepare_checkpoint = _prepare_real_canary_references(
        case_dir=case_dir,
        target=target,
        reference=validation_reference,
        reality_check_dot=reality_check_dot,
    )
    reference_before = (
        {"validation-reference": capture_git_fingerprint(validation_reference)}
        if validation_reference is not None
        else {}
    )
    source_hashes_before = _canary_source_hashes(target)
    (case_dir / "source-hashes.before.json").write_text(
        json.dumps(source_hashes_before, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    acceptance = (
        "Run ./demo-cli --self-test inside the deployed SUT DTU. "
        "Require stdout exactly demo-cli:self-test:ok and exit 0. "
        "No HTTP service or port exists; use no base URL."
    )
    log_root = case_dir / "run-log"
    command = {
        "runner": "real_reality_canary",
        "dot": str(reality_check_dot),
        "provider": "anthropic",
        "target": str(target),
    }
    (case_dir / "command.json").write_text(
        json.dumps(command, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    try:
        completed = _run_real_attractor_canary(
            target=target,
            log_root=log_root,
            reality_check_dot=reality_check_dot,
            acceptance_criteria=acceptance,
            reference_manifest_digest=manifest_digest,
        )
    except (OSError, subprocess.SubprocessError) as error:
        rc = target / ".rc"
        checkpoint = _parent_checkpoint(log_root)
        context = checkpoint.get("context", {})
        if not isinstance(context, Mapping):
            context = {}
        after_instances, postflight, post_list_error = _postflight_real_canary(
            rc=rc,
            before_instances=before_instances,
            context=context,
        )
        artifact_root = case_dir / "artifacts" / "rc"
        if rc.is_dir():
            shutil.copytree(rc, artifact_root)
        (case_dir / "dtu-list.after.json").write_text(
            json.dumps(after_instances, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        observation = _pre_subject_error(str(error))
        observation["harness_error"] = {
            "phase": "post_subject",
            "reason": str(error),
        }
        observation["cleanup"] = {
            "created_ids": [postflight["run_owned_identifier"]]
            if postflight["run_owned_identifier"]
            else [],
            "destroy_attempted": [
                str(record["identifier"])
                for record in postflight["cleanup_attempts"]
                if isinstance(record, Mapping)
            ],
            "remaining_ids": sorted(
                _instance_ids(after_instances) - _instance_ids(before_instances)
            ),
        }
        observation["evidence"] = {
            "dtu_list_before": before_instances,
            "dtu_list_after": after_instances,
            "postflight": postflight,
            "postflight_list_error": post_list_error,
            "cleanup": _read_json_if_present(rc / "cleanup.json"),
            "launch": _read_json_if_present(rc / "launch.json"),
            "parent_checkpoint": checkpoint,
            "source_hashes_before": source_hashes_before,
            "source_hashes_after": _canary_source_hashes(target),
        }
        return observation
    (case_dir / "stdout.log").write_text(completed.stdout, encoding="utf-8")
    (case_dir / "stderr.log").write_text(completed.stderr, encoding="utf-8")

    checkpoint = _parent_checkpoint(log_root)
    completed_nodes = checkpoint.get("completed_nodes", [])
    context = checkpoint.get("context", {})
    if not isinstance(completed_nodes, list):
        completed_nodes = []
    if not isinstance(context, dict):
        context = {}
    if not completed_nodes:
        rc = target / ".rc"
        after_instances, postflight, post_list_error = _postflight_real_canary(
            rc=rc,
            before_instances=before_instances,
            context=context,
        )
        artifact_root = case_dir / "artifacts" / "rc"
        if rc.is_dir():
            shutil.copytree(rc, artifact_root)
        (case_dir / "dtu-list.after.json").write_text(
            json.dumps(after_instances, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        error = _pre_subject_error(
            completed.stderr.strip() or "Attractor failed before the first subject node"
        )
        error["harness_error"] = {
            "phase": "post_subject",
            "reason": completed.stderr.strip()
            or "Attractor failed before the first subject node",
        }
        error["cleanup"] = {
            "created_ids": [postflight["run_owned_identifier"]]
            if postflight["run_owned_identifier"]
            else [],
            "destroy_attempted": [
                str(record["identifier"])
                for record in postflight["cleanup_attempts"]
                if isinstance(record, Mapping)
            ],
            "remaining_ids": sorted(
                _instance_ids(after_instances) - _instance_ids(before_instances)
            ),
        }
        error["evidence"] = {
            "dtu_list_before": before_instances,
            "dtu_list_after": after_instances,
            "postflight": postflight,
            "postflight_list_error": post_list_error,
            "cleanup": _read_json_if_present(rc / "cleanup.json"),
            "launch": _read_json_if_present(rc / "launch.json"),
            "parent_checkpoint": checkpoint,
            "source_hashes_before": source_hashes_before,
            "source_hashes_after": _canary_source_hashes(target),
        }
        return error

    rc = target / ".rc"
    verdict = _read_json_if_present(rc / "verdict.json")
    cleanup = _read_json_if_present(rc / "cleanup.json")
    after_instances, postflight, post_list_error = _postflight_real_canary(
        rc=rc,
        before_instances=before_instances,
        context=context,
    )
    (case_dir / "dtu-list.after.json").write_text(
        json.dumps(after_instances, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    container = cleanup.get("returned_identifier") or postflight["run_owned_identifier"]
    created_ids = [container] if isinstance(container, str) and container else []
    after_ids = _instance_ids(after_instances)
    remaining = [item for item in created_ids if item in after_ids]
    artifact_root = case_dir / "artifacts" / "rc"
    if rc.is_dir():
        shutil.copytree(rc, artifact_root)
    base_url = (
        (rc / "base_url.txt").read_text(encoding="utf-8").strip()
        if (rc / "base_url.txt").is_file()
        else context.get("sut_base_url", "")
    )
    launch = _read_json_if_present(rc / "launch.json")
    requested_name = (
        (rc / "requested_name.txt").read_text(encoding="utf-8").strip()
        if (rc / "requested_name.txt").is_file()
        else ""
    )
    observation = {
        "transport": {
            "outer_exit_code": completed.returncode,
            "exec_envelope_exit_code": None,
        },
        "subject": {
            "process_exit_code": completed.returncode,
            "engine_status": context.get(
                "outcome", "success" if completed.returncode == 0 else "fail"
            ),
            "completed_nodes": completed_nodes,
            "context": context,
            "failure_notes": completed.stderr,
        },
        "semantic": {
            "domain": "reality_check",
            "verdict": verdict.get("verdict", "fail"),
            "class": verdict.get("outcome_class", "evaluation_infrastructure_failed"),
        },
        "cleanup": {
            "created_ids": created_ids,
            "destroy_attempted": created_ids if cleanup.get("attempted") else [],
            "remaining_ids": remaining,
        },
        "evidence": {
            "fixture_root": str(fixture_root),
            "prepare_checkpoint": prepare_checkpoint,
            "reference_manifest_digest": manifest_digest,
            "reference_fingerprints_before": reference_before,
            "reference_fingerprints_after": (
                {"validation-reference": capture_git_fingerprint(validation_reference)}
                if validation_reference is not None
                else {}
            ),
            "profile_generated": (rc / "profile.yaml").is_file()
            and "NormalizeDUTPlan" in completed_nodes,
            "profile_launch_succeeded": "ParseHandleStrict" in completed_nodes,
            "target_pushed": "PushSUTStrict" in completed_nodes,
            "target_deployed": "DeployStrict" in completed_nodes,
            "requested_name": requested_name,
            "launch": launch,
            "dut_plan": _read_json_if_present(rc / "dut_plan.json"),
            "target_validation": _read_json_if_present(rc / "target_validation.json"),
            "target_validation_digest": context.get("target_validation_digest"),
            "target_validation_sha256": (
                _digest((rc / "target_validation.json").read_bytes())
                if (rc / "target_validation.json").is_file()
                and not (rc / "target_validation.json").is_symlink()
                else ""
            ),
            "sut_base_url": base_url,
            "dtu_list_before": before_instances,
            "dtu_list_after": after_instances,
            "nested_envelopes": _nested_json_envelopes(rc),
            "reference_dependency_plan": _read_json_if_present(
                rc / "reference_dependencies.json"
            ),
            "deploy_result": _read_json_if_present(rc / "deploy_result.json"),
            "source_hashes_before": source_hashes_before,
            "source_hashes_after": _canary_source_hashes(target),
            "cleanup": cleanup,
            "postflight": postflight,
            "postflight_list_error": post_list_error,
            "parent_checkpoint": checkpoint,
        },
    }
    (case_dir / "source-hashes.after.json").write_text(
        json.dumps(
            observation["evidence"]["source_hashes_after"],
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    if post_list_error:
        observation["harness_error"] = {
            "phase": "post_subject",
            "reason": post_list_error,
        }
    return observation


def build_parent_rc_probe(parent_graph: Graph) -> Graph:
    """Copy the parent exhaustion nodes and edges into a narrow executable probe."""
    required = {"CheckRC", "RCExhausted", "done"}
    missing = required - set(parent_graph.nodes)
    if missing:
        raise HarnessError(f"parent graph is missing probe nodes: {sorted(missing)}")

    def copy_node(source: Node) -> Node:
        return Node(
            id=source.id,
            label=source.label,
            shape=source.shape,
            type=source.type,
            prompt=source.prompt,
            attrs=dict(source.attrs),
            handler_type=source.handler_type,
            max_retries=source.max_retries,
            goal_gate=source.goal_gate,
            retry_target=source.retry_target,
            fallback_retry_target=source.fallback_retry_target,
            fidelity=source.fidelity,
            thread_id=source.thread_id,
            timeout=source.timeout,
            llm_model=source.llm_model,
            llm_provider=source.llm_provider,
            reasoning_effort=source.reasoning_effort,
            auto_status=source.auto_status,
            allow_partial=source.allow_partial,
            response_schema=source.response_schema,
        )

    def copy_edge(source: Edge) -> Edge:
        return Edge(
            from_node=source.from_node,
            to_node=source.to_node,
            label=source.label,
            condition=source.condition,
            weight=source.weight,
            attrs=dict(source.attrs),
            fidelity=source.fidelity,
            thread_id=source.thread_id,
            loop_restart=source.loop_restart,
        )

    copied_nodes = {
        node_id: copy_node(parent_graph.nodes[node_id]) for node_id in required
    }
    copied_nodes["SyntheticStart"] = Node(id="SyntheticStart", shape="Mdiamond")
    copied_edges = [
        copy_edge(edge)
        for edge in parent_graph.edges
        if edge.from_node in {"CheckRC", "RCExhausted"}
        and edge.to_node in {"RCExhausted", "done"}
    ]
    copied_edges.append(Edge("SyntheticStart", "CheckRC"))
    return Graph(
        name="parent_rc_probe",
        nodes=copied_nodes,
        edges=copied_edges,
        source_dir=parent_graph.source_dir,
    )


def run_parent_rc_probe_case(*, case_dir: Path, parent_dot: Path) -> JsonObject:
    """Run the derived exhaustion probe, or report missing production structure as graph failure."""
    case_dir = ensure_evaluation_path(case_dir)
    parent = parse_dot(parent_dot.read_text(encoding="utf-8"))
    parent.source_dir = str(parent_dot.parent)
    try:
        probe = build_parent_rc_probe(parent)
    except HarnessError as error:
        return {
            "transport": {"outer_exit_code": None, "exec_envelope_exit_code": None},
            "subject": {
                "process_exit_code": 1,
                "engine_status": "fail",
                "completed_nodes": [],
                "context": {"rc_state": "rc_exhausted"},
                "failure_notes": str(error),
                "originating_error": str(error),
            },
            "semantic": {
                "domain": "parent",
                "verdict": "fail",
                "class": "rc_exhausted_structure_missing",
            },
            "cleanup": {
                "created_ids": [],
                "destroy_attempted": [],
                "remaining_ids": [],
            },
        }
    context = PipelineContext()
    context.set("context.target_dir", str(case_dir))
    context.set("rc_state", "rc_exhausted")
    engine = PipelineEngine(
        probe,
        context,
        HandlerRegistry(HandlerContext()),
        str(case_dir / "run-log"),
    )
    outcome = run_async(engine.run())
    return {
        "transport": {"outer_exit_code": None, "exec_envelope_exit_code": None},
        "subject": {
            "process_exit_code": 1 if outcome.status.value != "success" else 0,
            "engine_status": outcome.status.value,
            "completed_nodes": engine.completed_nodes,
            "context": context.snapshot(),
            "failure_notes": outcome.failure_reason or outcome.notes or "",
        },
        "semantic": {"domain": "parent", "verdict": "partial", "class": "rc_exhausted"},
        "cleanup": {"created_ids": [], "destroy_attempted": [], "remaining_ids": []},
    }


class ControlledParentBackend(ControlledRealityBackend):
    """Deterministic LLM boundary for the parsed, unmodified parent graph."""

    async def run(
        self,
        node: Node,
        prompt: str,
        context: PipelineContext,
        incoming_edge=None,
        graph=None,
    ) -> str | Outcome:
        target = Path(str(context.get("context.target_dir", self.case_dir))).resolve()
        ai = target / ".ai"
        fixture = str(self.scenario.get("fixture", ""))
        if node.id == "AdmitSpec":
            (ai / "admit").mkdir(parents=True, exist_ok=True)
            (ai / "admit" / "assessment.md").write_text(
                "Deterministic evaluation brief is admissible.\n", encoding="utf-8"
            )
            (ai / "admit" / "verdict.txt").write_text("admit", encoding="utf-8")
            return "admitted"
        if node.id == "Plan":
            plan = ai / "plan"
            plan.mkdir(parents=True, exist_ok=True)
            (plan / "INDEX.md").write_text(
                "# Plan\n\n## Tasks (in implementation order)\n"
                "01. task_01_demo.md - build demo target\n",
                encoding="utf-8",
            )
            (plan / "task_01_demo.md").write_text(
                "# Build demo target\n\n"
                "Create an executable demo-cli whose --self-test prints "
                "demo-cli:self-test:ok.\n",
                encoding="utf-8",
            )
            if fixture == "parent_manifest_substitution":
                manifest = ai / "references.json"
                manifest.write_text(
                    json.dumps(
                        {
                            "schema_version": 1,
                            "target_root": str(target),
                            "references": [],
                        },
                        sort_keys=True,
                    )
                    + "\n",
                    encoding="utf-8",
                )
            return "plan written"
        if node.id == "Implement":
            (target / "demo-cli").write_text(
                "#!/bin/sh\n"
                'if [ "${1:-}" = "--self-test" ]; then\n'
                "  printf 'demo-cli:self-test:ok\\n'\n"
                "  exit 0\n"
                "fi\n"
                "exit 2\n",
                encoding="utf-8",
            )
            (target / "demo-cli").chmod(0o755)
            done = ai / "plan" / ".done"
            done.mkdir(parents=True, exist_ok=True)
            (done / "task_01_demo.md").touch()
            with (ai / "plan" / "PROGRESS.md").open("a", encoding="utf-8") as stream:
                stream.write("\n## task_01_demo.md (done)\n- Did: created demo-cli.\n")
            return "implemented"
        if node.id == "UserRun":
            validation = ai / "validation"
            validation.mkdir(parents=True, exist_ok=True)
            (validation / "verdict.json").write_text(
                json.dumps(
                    {
                        "result": "pass",
                        "fault": "",
                        "fix_request": "",
                        "evidence": "demo-cli:self-test:ok",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            return "validated"
        if node.id == "Deliver":
            (target / "README.md").write_text(
                "# demo-cli\n\nRun `./demo-cli --self-test`.\n", encoding="utf-8"
            )
            (ai / "delivery_summary.md").write_text(
                "Delivered demo-cli.\n", encoding="utf-8"
            )
            return "delivered"
        if (
            node.id == "PlanReferenceDependencies"
            and fixture == "parent_reference_prerequisite"
        ):
            rc = target / ".rc"
            rc.mkdir(parents=True, exist_ok=True)
            plan = {
                "schema_version": 1,
                "state": "reference_prerequisite_failed",
                "references_manifest_path": context.get("refplan_manifest_path"),
                "references_manifest_digest": context.get("refplan_manifest_digest"),
                "run_token": context.get("refplan_run_token"),
                "dependencies": [],
                "prerequisites": [{"id": "validation-reference", "cause": "fixture"}],
                "setup_results": [],
                "use_results": [],
            }
            (rc / "reference_dependencies.json").write_text(
                json.dumps(plan, sort_keys=True) + "\n", encoding="utf-8"
            )
            return "reference prerequisite failure"
        if node.id == "Validate" and fixture == "parent_rc_exhausted":
            rc = target / ".rc"
            (rc / "qa_verdict.txt").write_text("fail", encoding="utf-8")
            (rc / "feedback.txt").write_text("controlled RC failure", encoding="utf-8")
            return "controlled QA fail"
        return await super().run(node, prompt, context, incoming_edge, graph)


def run_controlled_parent_case(
    *, case_dir: Path, scenario: JsonMapping, parent_dot: Path
) -> JsonObject:
    """Exercise the parsed production parent and its real folder children."""
    case_dir = ensure_evaluation_path(case_dir)
    parent = parse_dot(parent_dot.read_text(encoding="utf-8"))
    parent.source_dir = str(parent_dot.parent)
    target = init_git_repo(case_dir / "target")
    reference = init_git_repo(case_dir / "fixture" / "references" / "reference-a")
    (reference / "README.md").write_text(
        "# Context\n\nRead-only evaluation reference.\n", encoding="utf-8"
    )
    _git(reference, "add", "README.md")
    _git(reference, "commit", "-q", "-m", "reference docs")
    before = {"reference-a": capture_git_fingerprint(reference)}
    fixture = str(scenario.get("fixture", ""))
    envelope: JsonObject = {"returned_id": "parent-controlled-dtu"}
    if fixture == "parent_infrastructure":
        envelope.update(
            {
                "launch_outer_exit_code": 1,
                "derive_launch_id": False,
                "launch_payload": {},
            }
        )
    fake_bin = case_dir / "bin"
    calls = case_dir / "dtu.calls.jsonl"
    write_fake_dtu(fake_bin, envelope=envelope, call_log=calls)
    _write_git_reset_compat(fake_bin)
    context = PipelineContext()
    context.set("context.target_dir", str(target))
    context.set(
        "spec",
        "Build demo-cli for a user. Done means ./demo-cli --self-test prints "
        "demo-cli:self-test:ok and exits 0.",
    )
    context.set(
        "references",
        json.dumps(
            [{"id": "reference-a", "path": str(reference), "use_in_validation": False}],
            separators=(",", ":"),
        ),
    )
    if fixture == "parent_rc_exhausted":
        context.set("rc_max_rounds", "0")
    backend = ControlledParentBackend(case_dir, scenario)
    engine = PipelineEngine(
        parent,
        context,
        HandlerRegistry(HandlerContext(backend=backend)),
        str(case_dir / "run-log"),
    )
    with _path_prepend(fake_bin):
        outcome = run_async(engine.run())
    checkpoint = _parent_checkpoint(case_dir / "run-log")
    final_context = context.snapshot()
    verdict = _read_json_if_present(target / ".rc" / "verdict.json")
    completed = list(engine.completed_nodes)
    nested = _nested_checkpoints(case_dir / "run-log")
    after = {"reference-a": capture_git_fingerprint(reference)}
    if fixture == "parent_rc_pass":
        semantic = {"domain": "parent", "verdict": "pass", "class": "delivered"}
    elif fixture == "parent_rc_exhausted":
        semantic = {"domain": "parent", "verdict": "partial", "class": "rc_exhausted"}
    elif fixture == "parent_reference_prerequisite":
        semantic = {
            "domain": "parent",
            "verdict": "fail",
            "class": "reference_prerequisite_failed",
        }
    elif fixture == "parent_infrastructure":
        semantic = {
            "domain": "parent",
            "verdict": "fail",
            "class": "evaluation_infrastructure_failed",
        }
    else:
        semantic = {
            "domain": "parent",
            "verdict": "fail",
            "class": "reference_manifest_substitution_detected",
        }
    return {
        "transport": {"outer_exit_code": None, "exec_envelope_exit_code": None},
        "subject": {
            "process_exit_code": 0 if outcome.status.value == "success" else 1,
            "engine_status": outcome.status.value,
            "completed_nodes": completed,
            "context": final_context,
            "failure_notes": outcome.failure_reason or outcome.notes or "",
        },
        "semantic": semantic,
        "cleanup": {"created_ids": [], "destroy_attempted": [], "remaining_ids": []},
        "evidence": {
            "parent_checkpoint": checkpoint,
            "nested_checkpoints": nested,
            "rc_verdict": verdict,
            "reference_order": ["reference-a"],
            "reference_fingerprints_before": before,
            "reference_fingerprints_after": after,
            "reference_manifest_digest": final_context.get("reference_manifest_digest"),
            "manifest_digest_after": _file_digest(target / ".ai" / "references.json"),
            "verify_failure_reason": _node_failure_reason(
                case_dir / "run-log", "VerifyReferences"
            ),
            "dtu_calls": [
                json.loads(line)
                for line in calls.read_text(encoding="utf-8").splitlines()
            ]
            if calls.is_file()
            else [],
        },
    }
