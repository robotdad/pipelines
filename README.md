# pipelines

Attractor pipelines — DOT-format graph pipelines for automated, gated workflows.

These run on the [attractor](https://github.com/microsoft/amplifier-bundle-attractor) engine
directly. They carry no hosting-platform coupling: no viewports, no platform events, no
plugin-specific glue.

## What's here

| Pipeline | Status | Summary |
|---|---|---|
| [`expert_builder/`](expert_builder/) | working-tree closure verified; remote CI gate on push/PR | Greenfield build-from-spec spine: admit → plan → implement → validate → reality-check → deliver |

## Conventions

One folder per pipeline. Each published entrypoint is a closed package: its `.dot` graph, every
static subgraph it invokes, and supporting data live at or below that entrypoint's directory.
`expert_builder/admit/admit.dot` is an internal admission brick, not a sibling package.

### Running a pipeline

A Resolve entrypoint names only the root DOT at a pinned Git commit:

```sh
git+https://github.com/robotdad/pipelines@<40-char-sha>#subdirectory=expert_builder/expert_builder.dot
```

Resolve recursively hydrates that root and its `dot_file` closure. Do not use sibling `../` paths
or flatten copied DOT files as package validation: both bypass the published boundary. Before a
push or pull request, run the working-tree closure test; CI runs that test and remote hydration
against the pushed or PR commit:

```sh
uv run pytest -q -m 'not remote'
PIPELINES_REMOTE_SHA=<40-char-sha> uv run pytest -q -m remote
```

### Evaluating `expert_builder`

Run the deterministic controlled suite during development, then the full acceptance suite (including
the real DTU validation-reference canary) before publication:

```sh
uv run python -m evaluations.expert_builder.run --suite controlled
uv run python -m evaluations.expert_builder.run --suite acceptance
```

Each run writes its summary, per-case observations, and captured artifacts under
`.work/evaluations/expert-builder/<timestamp>/`.

### `expert_builder` references

`expert_builder` accepts an optional trusted `references` JSON input:

```json
[
  {"id": "example", "path": "/project/workspace/example", "use_in_validation": true}
]
```

Each entry names an already-present Git worktree. `id` and `path` are required;
`use_in_validation` is optional and defaults to `false`. The current working directory
remains the one authorized target for modification, staging, commit, and delivery.
Zero or more references are advisory read-only context. Git-visible point-in-time
integrity gates detect changes to them throughout the run.

References marked for validation are installed in the DTU through their documented public
installation path; they are not copied into it. Reference artifacts under `.ai/` and
`.rc/` are transient and excluded from delivery. References are trusted inputs: this
does not sandbox untrusted repository documentation.

This feature does not clone or provision repositories, transport exact checkouts, add
another writer, or schedule work across repositories. `references/prepare.dot` and
`references/verify.dot` deliberately duplicate their fingerprint logic because Resolve
hydrates only static DOT dependencies in the published package.

Node shapes carry meaning:

| Shape | Meaning |
|---|---|
| `box` | LLM-executed node |
| `parallelogram` | deterministic tool node; routes on a verdict file or last stdout line |
| `diamond` | conditional branch |
| `folder` | invokes a child `.dot` subgraph |
| `hexagon` | human gate the engine cannot route past |
| `Mdiamond` / `Msquare` | entry / exit |

Two rules the pipelines here are held to:

- **Never route on LLM prose.** Branch on a deterministic verdict — a file, an exit code, a
  marker line — never on a model's free text.
- **Fail closed.** A gate that cannot reject is not a gate. Ambiguity resolves to the safe
  outcome, and an exhausted budget is a loud failure with a destination, not a silent continue.
