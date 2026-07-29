# pipelines

Attractor pipelines — DOT-format graph pipelines for automated, gated workflows.

These run on the [attractor](https://github.com/microsoft/amplifier-bundle-attractor) engine
directly. They carry no hosting-platform coupling: no viewports, no platform events, no
plugin-specific glue.

## What's here

| Pipeline | Status | Summary |
|---|---|---|
| [`expert_builder/`](expert_builder/) | imported, unmodified | Greenfield build-from-spec spine: plan → implement → reality-check → deliver |

## Conventions

One folder per pipeline. Each holds its `.dot` graph, any subgraph `.dot` files it invokes,
and its supporting scripts and default data files.

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
