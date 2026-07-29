# wordfreq -- design

How the tool is built. The requirements document is authoritative on *what*;
where this document appears to disagree, the requirements win.

## Approach

Single-file Python script, no third-party dependencies, stdlib only.

## Structure

- `tokenize(text) -> list[str]` -- lowercase, split on non-alphanumeric runs.
- `count(tokens) -> dict[str, int]` -- a `collections.Counter`.
- `render(counts, top) -> str` -- sort by `(-count, word)` so ties are alphabetical.
- `main(argv)` -- argument parsing via `argparse`; reads the file, prints the render.

## Why sorting this way

Sorting on the tuple `(-count, word)` gets both orderings in one pass and makes the
stable-ties requirement fall out of the sort rather than needing a second step.
