# pipelines

Attractor pipelines — DOT-format graph pipelines for automated, gated workflows.

These run on the [attractor](https://github.com/microsoft/amplifier-bundle-attractor) engine
directly. They carry no hosting-platform coupling: no viewports, no platform events, no
plugin-specific glue.

## What's here

| Pipeline | Status | Summary |
|---|---|---|
| [`admit/`](admit/) | working, verified | Admission gate: rejects a brief that cannot be built against. Headless, no human gate |
| [`expert_builder/`](expert_builder/) | verified end-to-end | Greenfield build-from-spec spine: admit → plan → implement → validate → reality-check → deliver |

## Conventions

One folder per pipeline. Each holds its `.dot` graph, any subgraph `.dot` files it invokes,
and its supporting scripts and default data files.

### Running a pipeline

A relative `dot_file=` on a `folder` node resolves against the **run working directory**, and box
(LLM) nodes require the process working directory to equal `--cwd`. So run a pipeline from a
directory that holds its bricks:

```sh
cd <workdir> && attractor run <pipeline>.dot --cwd .
```

For a build pipeline, copy the bricks into a scratch workdir first so generated output does not
land in this repo. Cross-pipeline references work the same way — `dot_file="../admit/admit.dot"`
resolves when the run workdir is the pipeline's own folder.

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
