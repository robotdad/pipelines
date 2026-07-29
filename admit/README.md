# admit

An admission brick. It answers one question about a submitted brief:

> Is there enough here to plan and build against, and will we be able to tell whether it worked?

It **rejects; it never asks.** No human gate, so it runs headless.

## Why it exists

A build pipeline whose front door only checks "is the input non-empty" will happily burn a long,
expensive run on a one-line wish list, produce something worthless, and report SUCCESS. That is the
failure this gate exists to prevent, and it is the *only* one it tries to prevent.

It is deliberately **not** a readiness grader. It does not score, rank, or improve a brief. Scoring
is a different job with a different cost profile, and conflating the two produces a gate too
expensive to put in front of everything.

## Usage

```sh
# one file
attractor run admit.dot --param inputs=brief.md --cwd .

# a directory -- every *.md under it, recursively, sorted
attractor run admit.dot --param inputs=docs/designs --cwd .
```

Pass the **path**, not the contents. Do not use attractor's `@file` form here; `CollectInputs`
does the reading so it can handle one file or many identically.

Composed as a brick, the parent owns the routing:

```dot
Admit      [shape=folder, dot_file="admit.dot", outputs="admit_state"];
CheckAdmit [shape=diamond, label="Admitted?"];

Admit      -> CheckAdmit;
CheckAdmit -> Plan [condition="context.admit_state=admitted", label="admit"];
CheckAdmit -> done [condition="context.admit_state=rejected", label="reject"];
```

## Criteria

All three must hold to admit:

1. **Discernible goal** — it is clear what is to be built, and roughly for whom.
2. **Observable done** — at least one success statement concrete enough that someone who did not
   write the brief could check it. Without this, a run is unfalsifiable.
3. **Not self-contradictory** — where documents declare a precedence between themselves, a
   disagreement resolved by that precedence is not a contradiction.

Explicitly **not** grounds for rejection: missing architecture or tech choices; *present*
architecture, design detail, or an implementation plan; missing constraints, edge cases, or scope;
brevity. Borderline cases admit — a wrong reject blocks a human at the door, while a wrong admit is
bounded by the stages that follow.

## Outputs

| Path | Written on | Contents |
|---|---|---|
| `.ai/brief.md` | always | inputs concatenated with per-file delimiters |
| `.ai/admit/manifest.txt` | always | the files that were read |
| `.ai/admit/assessment.md` | admit **and** reject | per-criterion PASS/FAIL with reasons; on reject, what would fix it |
| `.ai/admit/summary.md` | admit **and** reject | run summary |
| `admit_state` (context key) | always | `admitted` \| `rejected` |

## Design notes

**File-verdict triple.** `AdmitSpec` (LLM) writes a verdict *file* as its final action;
`ReadAdmitVerdict` (deterministic) reads it with a default-closed `case` statement and `rm -f`s it;
edges route on `context.tool.last_line`. Prose is never load-bearing. The box carries no `goal_gate`
— an LLM box's `outcome=` prose fail-opens to SUCCESS, which would make the reject path
structurally unreachable.

**Fail-closed throughout.** No param, missing path, no files, or blank content exits 1 *before* any
model is called. Any verdict token other than exactly `admit` is a reject. An unreadable state file
reports `rejected`.

**Rejection is reported, not just routed.** Both outcomes pass through `Report`, so a rejected run
emits the same structured summary an admitted one does. A gate that rejects loudly to the engine and
silently to the submitter is a wall, not a door.

**No structural requirements.** The gate does not demand sections, headings, roles, or resolvable
cross-links. Those assume an authoring shape the submitter never agreed to. "Files exist and are
non-empty" is the only mechanical floor that is honestly universal.

## Verified

Run against the `examples/` fixtures with the attractor CLI:

| Fixture | Expected | Result |
|---|---|---|
| `examples/thin.md` | reject | rejected — goal and observable-done both FAIL, with concrete fixes named |
| `examples/pass.md` | admit | admitted — all three PASS |
| `examples/multi/` | admit | admitted — two files read; criterion 3 resolved via the docs' declared precedence |
| non-existent path | hard fail | exit 1 at `CollectInputs`, before any model call |

The `multi/` fixture carries a design document on purpose: detail is more signal, and a brief must
never be rejected for having it.
