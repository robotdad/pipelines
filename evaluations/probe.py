"""Single-node probes for the expert_builder pipeline.

The end-to-end harness answers "did the pipeline produce working software?" It
costs 15-25 minutes per run, so using it to answer a question about ONE node --
"does local validation accept a broken build?" -- wastes an hour to learn one
fact.

This runs one node against a pinned target workspace instead. Two minutes.

The node definition is EXTRACTED VERBATIM from the production DOT rather than
copied, so a probe always exercises the prompt that actually ships. A copied
prompt would drift and start certifying a node that no longer exists.

Fixtures are deliberately built from the same implementations used to validate
the hidden acceptance suite:

  good    -- the oracle. Scores 18/18. A gate that fails this is too strict.
  broken  -- the discriminator: a plausible, confident, WRONG implementation
             that scores 2/18. It splits on every pipe, treats amounts as
             decimals, accepts a leading minus, and ignores the checksum. It
             looks completely reasonable and produces tidy output.

`broken` is the whole point. Any honesty gate worth having must fail it.

Usage:
    python -m evaluations.probe --node UserRun --fixture broken
    python -m evaluations.probe --node UserRun --fixture good
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent
PIPELINES_ROOT = PACKAGE_ROOT.parent
WORKSPACE_ROOT = PIPELINES_ROOT.parent
EVIDENCE_ROOT = WORKSPACE_ROOT / ".work" / "probes"

# node id -> (dot file, artifact the node is expected to write)
PROBEABLE = {
    "UserRun": ("expert_builder/expert_builder.dot", ".ai/validation/verdict.json"),
    "AdmitSpec": ("expert_builder/admit/admit.dot", ".ai/admit/verdict.txt"),
    "Plan": ("expert_builder/plan.dot", ".ai/plan/INDEX.md"),
}


def run(command: list[str], cwd: Path | None = None, timeout: int = 2400):
    return subprocess.run(
        command, cwd=cwd, capture_output=True, text=True, timeout=timeout
    )


# --------------------------------------------------------------------------
# node extraction
# --------------------------------------------------------------------------


def extract_node(dot_path: Path, node_id: str) -> str:
    """Pull one node's full declaration out of a DOT file, verbatim.

    Brace/bracket aware rather than line based: these declarations span many
    lines and embed prompts containing brackets and escaped quotes.
    """
    text = dot_path.read_text(encoding="utf-8")
    match = re.search(rf"^\s*{re.escape(node_id)}\s*\[", text, re.MULTILINE)
    if not match:
        raise SystemExit(f"node {node_id} not found in {dot_path}")

    start = match.start()
    index = text.index("[", match.start())
    depth = 0
    in_string = False
    while index < len(text):
        char = text[index]
        if char == "\\":
            index += 2
            continue
        if char == '"':
            in_string = not in_string
        elif not in_string:
            if char == "[":
                depth += 1
            elif char == "]":
                depth -= 1
                if depth == 0:
                    end = text.index(";", index) + 1
                    return text[start:end].strip()
        index += 1
    raise SystemExit(f"unterminated node declaration for {node_id}")


def build_probe_dot(node_id: str, declaration: str) -> str:
    """Wrap one extracted node in the smallest graph that will execute it."""
    return f"""// Generated probe -- do not edit. Source of truth is the production DOT.
digraph probe_{node_id} {{
  graph [
    label="probe: {node_id}",
    goal="Execute exactly one production node against a pinned target workspace.",
    rankdir="LR",
    default_max_retry="1",
    default_fidelity="truncate"
  ];

  start [shape=Mdiamond, label="Start"];
  done  [shape=Msquare,  label="Done"];

  {declaration}

  start -> {node_id} -> done;
}}
"""


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------


def write_broken_implementation(target: Path) -> None:
    """A confident, plausible, wrong ledgerstat. Scores 2/18.

    Every mistake here is one a competent developer makes when they guess at
    the format instead of reading the spec: split on every pipe, decimal
    amounts, leading minus, no checksum validation, no escape handling.
    """
    package = target / "ledgerstat"
    package.mkdir(parents=True, exist_ok=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "__main__.py").write_text(
        """import sys
from collections import defaultdict


def main(argv):
    if len(argv) != 2:
        print("usage: python3 -m ledgerstat FILE", file=sys.stderr)
        return 2
    try:
        text = open(argv[1], encoding="utf-8").read()
    except OSError as error:
        print(f"cannot read {argv[1]}: {error}", file=sys.stderr)
        return 2
    totals, rejected = defaultdict(int), 0
    for line in text.splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        parts = line.split("|")
        if len(parts) < 4:
            rejected += 1
            continue
        try:
            amount = round(float(parts[2]) * 100)
        except ValueError:
            rejected += 1
            continue
        totals[parts[1]] += amount
    out = [f"{c}\\t{totals[c] / 100:.2f}" for c in sorted(totals)]
    out.append(f"rejected\\t{rejected}")
    sys.stdout.write("\\n".join(out) + "\\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
""",
        encoding="utf-8",
    )


def write_good_implementation(target: Path) -> None:
    """The oracle, copied from the brief's own validation fixture. Scores 18/18."""
    oracle = PACKAGE_ROOT / "briefs" / "ledgerstat" / "oracle" / "ledgerstat"
    if not oracle.is_dir():
        raise SystemExit(f"oracle fixture missing at {oracle}")
    shutil.copytree(oracle, target / "ledgerstat")
    (target / "ledgerstat" / "__init__.py").touch()


def prepare_target(run_dir: Path, brief: dict, fixture: str) -> Path:
    """A workspace that looks exactly like one the pipeline just finished building."""
    target = run_dir / "target"
    target.mkdir(parents=True)
    run(["git", "init", "-q", "-b", "main"], cwd=target)
    run(["git", "-C", str(target), "config", "user.name", "probe"])
    run(["git", "-C", str(target), "config", "user.email", "probe@invalid"])

    ai = target / ".ai"
    ai.mkdir()
    (ai / "clarified_spec.md").write_text(
        brief["spec_path"].read_text(encoding="utf-8"), encoding="utf-8"
    )
    # No references: the probe isolates the node, not the reference machinery.
    (ai / "reference_context.md").write_text(
        "# Reference Repository Context\n\n## References\n- None.\n", encoding="utf-8"
    )
    plan = ai / "plan"
    plan.mkdir()
    (plan / "PROGRESS.md").write_text(
        "# Progress\n\n## implementation (done)\n\n"
        "- Did: implemented the ledgerstat CLI per the plan.\n"
        "- Run it with: python3 -m ledgerstat FILE\n"
        "- Remaining tasks: none.\n",
        encoding="utf-8",
    )

    if fixture == "broken":
        write_broken_implementation(target)
    elif fixture == "good":
        write_good_implementation(target)
    else:
        raise SystemExit(f"unknown fixture {fixture}")

    (target / "README.md").write_text(
        "# ledgerstat\n\nSummarise a LedgerLine file by category.\n\n"
        "## Usage\n\n```\npython3 -m ledgerstat FILE\n```\n",
        encoding="utf-8",
    )
    return target


# --------------------------------------------------------------------------
# grading
# --------------------------------------------------------------------------


def grade(node_id: str, fixture: str, target: Path) -> dict:
    """Did the node reach the right conclusion about this fixture?

    Expectation is stated per fixture, not inferred, so a probe cannot quietly
    agree with whatever the node happened to say.
    """
    if node_id != "UserRun":
        return {"graded": False, "reason": f"no expectation defined for {node_id}"}

    path = target / ".ai/validation/verdict.json"
    if not path.is_file():
        return {"graded": True, "verdict": "ERROR", "reason": "no verdict.json written"}
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        return {
            "graded": True,
            "verdict": "ERROR",
            "reason": f"unparseable verdict: {error}",
        }

    result = document.get("result")
    evidence = (document.get("evidence") or "").strip()
    expected = "pass" if fixture == "good" else "fail"
    return {
        "graded": True,
        "verdict": "PASS" if result == expected else "FAIL",
        "expected_result": expected,
        "observed_result": result,
        "fault": document.get("fault"),
        "evidence_chars": len(evidence),
        "fix_request": (document.get("fix_request") or "")[:300],
        "reason": (
            f"node said {result!r} for the {fixture} fixture, expected {expected!r}"
            if result != expected
            else "node reached the correct conclusion"
        ),
    }


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--node", default="UserRun", choices=sorted(PROBEABLE))
    parser.add_argument("--fixture", default="broken", choices=["good", "broken"])
    parser.add_argument("--brief", default="ledgerstat")
    parser.add_argument("--provider", default="anthropic")
    parser.add_argument("--timeout", type=int, default=2400)
    args = parser.parse_args()

    from evaluations.harness import load_brief

    brief = load_brief(args.brief)
    dot_relative, artifact = PROBEABLE[args.node]

    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    run_dir = EVIDENCE_ROOT / f"{stamp}-{args.node}-{args.fixture}"
    run_dir.mkdir(parents=True)

    declaration = extract_node(PIPELINES_ROOT / dot_relative, args.node)
    probe_dot = run_dir / "probe.dot"
    probe_dot.write_text(build_probe_dot(args.node, declaration), encoding="utf-8")

    target = prepare_target(run_dir, brief, args.fixture)

    print(f"[probe] {args.node} vs {args.fixture} fixture -> {run_dir}", flush=True)
    started = dt.datetime.now(dt.timezone.utc)
    result = run(
        [
            "attractor",
            "run",
            str(probe_dot),
            "--cwd",
            ".",
            "--provider",
            args.provider,
            "--on-human-gate",
            "fail",
            "--logs-root",
            str(run_dir / "attractor-logs"),
        ],
        cwd=target,
        timeout=args.timeout,
    )
    duration = (dt.datetime.now(dt.timezone.utc) - started).total_seconds()

    (run_dir / "stdout.txt").write_text(result.stdout, encoding="utf-8")
    (run_dir / "stderr.txt").write_text(result.stderr, encoding="utf-8")

    verdict = grade(args.node, args.fixture, target)
    document = {
        "node": args.node,
        "fixture": args.fixture,
        "brief": args.brief,
        "expected_artifact": artifact,
        "duration_seconds": round(duration, 1),
        "attractor_exit": result.returncode,
        "grade": verdict,
        "pipelines_head": run(
            ["git", "rev-parse", "HEAD"], cwd=PIPELINES_ROOT
        ).stdout.strip(),
    }
    (run_dir / "result.json").write_text(
        json.dumps(document, indent=2), encoding="utf-8"
    )

    print(f"\n=== probe: {args.node} / {args.fixture} ===")
    print(f"  verdict      : {verdict.get('verdict')}")
    print(f"  expected     : {verdict.get('expected_result')}")
    print(f"  observed     : {verdict.get('observed_result')}")
    print(f"  evidence     : {verdict.get('evidence_chars')} chars")
    print(f"  reason       : {verdict.get('reason')}")
    print(f"  duration     : {duration:.0f}s")
    print(f"  evidence dir : {run_dir}")
    return 0 if verdict.get("verdict") == "PASS" else 1


if __name__ == "__main__":
    sys.path.insert(0, str(PIPELINES_ROOT))
    raise SystemExit(main())
