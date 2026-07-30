"""Small, strict wrapper around ``amplifier-digital-twin``."""

from __future__ import annotations

import json
import subprocess
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from .json_data import JsonObject, as_object_list, parse_object


class HarnessError(RuntimeError):
    """Raised when a required harness transport contract is unavailable or malformed."""


@dataclass(frozen=True)
class ProcessResult:
    argv: tuple[str, ...]
    cwd: str
    returncode: int
    stdout: str
    stderr: str
    duration_seconds: float


@dataclass(frozen=True)
class DTUExecResult:
    outer: ProcessResult
    payload: JsonObject
    inner_exit_code: int
    inner_stdout: str
    inner_stderr: str


def run_process(
    argv: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str] | None = None,
    timeout_seconds: float,
) -> ProcessResult:
    """Run one command and preserve its complete outer-process result."""
    started = time.monotonic()
    try:
        completed = subprocess.run(
            list(argv),
            cwd=cwd,
            env=dict(env) if env is not None else None,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except FileNotFoundError as error:
        raise HarnessError(f"required executable is unavailable: {argv[0]}") from error
    except subprocess.TimeoutExpired as error:
        raise HarnessError(
            f"command timed out after {timeout_seconds}s: {' '.join(argv)}"
        ) from error
    return ProcessResult(
        argv=tuple(argv),
        cwd=str(cwd),
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
        duration_seconds=time.monotonic() - started,
    )


def _parse_object(result: ProcessResult, *, operation: str) -> JsonObject:
    if result.returncode != 0:
        raise HarnessError(
            f"{operation} outer process failed with {result.returncode}: {result.stderr[-800:]}"
        )
    try:
        payload = parse_object(result.stdout, source=operation)
    except (json.JSONDecodeError, ValueError) as error:
        raise HarnessError(f"{operation} did not emit one JSON object") from error
    return payload


def launch(
    profile: Path, *, name: str, timeout_seconds: float = 600
) -> tuple[ProcessResult, JsonObject]:
    """Launch one named controller or SUT DTU and parse its launch envelope."""
    result = run_process(
        ["amplifier-digital-twin", "launch", str(profile), "--name", name],
        cwd=profile.parent,
        timeout_seconds=timeout_seconds,
    )
    return result, _parse_object(result, operation="DTU launch")


def exec_json(
    instance_id: str, command: Sequence[str], *, timeout_seconds: float = 600
) -> DTUExecResult:
    """Execute a DTU command, preserving both outer and nested exit codes."""
    result = run_process(
        ["amplifier-digital-twin", "exec", instance_id, "--", *command],
        cwd=Path.cwd(),
        timeout_seconds=timeout_seconds,
    )
    payload = _parse_object(result, operation="DTU exec")
    inner_exit_code = payload.get("exit_code")
    if type(inner_exit_code) is not int:
        raise HarnessError("DTU exec envelope is missing an integer exit_code")
    inner_stdout = payload.get("stdout", "")
    inner_stderr = payload.get("stderr", "")
    if not isinstance(inner_stdout, str) or not isinstance(inner_stderr, str):
        raise HarnessError("DTU exec envelope stdout/stderr must be strings")
    return DTUExecResult(
        outer=result,
        payload=payload,
        inner_exit_code=inner_exit_code,
        inner_stdout=inner_stdout,
        inner_stderr=inner_stderr,
    )


def file_push(
    instance_id: str, source: Path, destination: str, *, recursive: bool
) -> ProcessResult:
    """Push a file or tree to one DTU without parsing its optional response body."""
    argv = ["amplifier-digital-twin", "file-push"]
    if recursive:
        argv.append("-r")
    argv.extend((instance_id, str(source), destination))
    return run_process(argv, cwd=source.parent, timeout_seconds=120)


def list_instances() -> tuple[ProcessResult, list[JsonObject]]:
    """List managed DTUs and require the documented list envelope."""
    result = run_process(
        ["amplifier-digital-twin", "list"], cwd=Path.cwd(), timeout_seconds=60
    )
    if result.returncode != 0:
        raise HarnessError(f"DTU list failed: {result.stderr[-800:]}")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise HarnessError("DTU list did not emit JSON") from error
    objects = as_object_list(payload)
    if objects is None:
        raise HarnessError("DTU list did not emit a list of objects")
    return result, [dict(item) for item in objects]


def destroy(instance_id: str) -> ProcessResult:
    """Destroy exactly the instance named by the caller."""
    return run_process(
        ["amplifier-digital-twin", "destroy", instance_id],
        cwd=Path.cwd(),
        timeout_seconds=120,
    )
