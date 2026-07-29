# wordfreq -- requirements

What the tool must do. This document is authoritative on *what*; where the design
document disagrees with it, this document wins.

## Goal

Give writers a quick way to see which words they lean on.

## Done looks like

- `wordfreq notes.txt` prints one `count word` pair per line, highest count first.
- Ties break alphabetically, so output is stable across runs.
- `--top N` limits output to the N most frequent words.
- Comparison is case-insensitive.
- An empty input file prints nothing and exits 0.
