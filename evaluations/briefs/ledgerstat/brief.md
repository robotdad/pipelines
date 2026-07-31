# ledgerstat

Build a small command-line tool that summarises a LedgerLine ledger file by category.

## What it must do

`ledgerstat` reads one `.ldg` file and prints per-category totals.

It must be runnable from the project root as:

```
python3 -m ledgerstat <path-to-file>
```

The tool must use only the Python standard library.

## The input format

The input is the **LedgerLine v1** plain-text format. It is NOT CSV and it is
not a format you should guess at: amounts, dates, memo escaping, and the
per-record integrity character all follow rules that are specified precisely
in `SPEC.md` in the `ledgerfmt` reference repository listed in your reference
context. Read that specification before implementing.

Records that the specification says must be rejected are counted and skipped.
A file containing rejected records is not an error.

## Output

Write to stdout, nothing else.

For every category that has at least one accepted record, in ascending
ASCII order by category name, one line:

```
<category><TAB><total>
```

where `<total>` is the sum of that category's amounts rendered as a decimal
with **exactly two** digits after the point, no thousands separators, and a
leading `-` when the total is negative. A category whose accepted records sum
to zero is still printed, as `0.00`.

After all category lines, print one final line:

```
rejected<TAB><count>
```

where `<count>` is the number of rejected records in the file.

An empty input file therefore produces exactly one line: `rejected` TAB `0`.

## Exit codes

- `0` — the file was read and a summary was printed, regardless of how many
  records were rejected.
- `2` — the file could not be opened or read. Print a message to stderr.

## Done means

- `python3 -m ledgerstat FILE` prints the summary described above for a file
  containing valid records, including at least one negative amount and at
  least one memo containing an escaped pipe.
- A file containing records that the specification rejects prints a correct
  `rejected` count and still exits `0`.
- An empty file prints exactly `rejected` TAB `0` and exits `0`.
- A missing file exits `2`.
