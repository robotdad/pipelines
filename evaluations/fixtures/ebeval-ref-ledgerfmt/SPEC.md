# LedgerLine v1 format specification

A `.ldg` file is UTF-8 text, one record per line.

## Line kinds

- A line whose first character is `#` is a **comment** and is ignored.
- A line that is empty or contains only whitespace is ignored.
- Every other line is a **record**.

## Record grammar

A record has exactly five fields separated by the pipe character `|`:

```
<oday>|<category>|<amount>|<memo>|<check>
```

Splitting is performed on **unescaped** pipes only (see Memo escaping).
A record with any number of fields other than five is **malformed**.

### oday — ordinal day

`<dayofyear>.<yy>` where `dayofyear` is 1-366 with no leading zeros and
`yy` is a two-digit year. `123.26` means the 123rd day of 2026.
A record whose `dayofyear` is outside 1-366, or whose `yy` is not exactly
two digits, is **malformed**.

### category

A non-empty run of lowercase ASCII letters, digits, and underscore.
Any other character makes the record **malformed**.

### amount — minor units, trailing sign

An amount is an integer number of **minor units** (cents), written with
no decimal point.

A **negative** amount is written with a **trailing** `-`, never a leading
one. A leading `-` makes the record **malformed**.

```
450      ->  +4.50
450-     ->  -4.50
0        ->   0.00
7        ->  +0.07
```

Leading zeros are permitted (`0450` is +4.50). An empty amount, or an
amount containing any character other than ASCII digits plus the optional
single trailing `-`, is **malformed**.

### memo — escaping

The memo is free text with two escape sequences:

| Sequence | Means          |
|----------|----------------|
| `\p`     | a literal `|`  |
| `\\`     | a literal `\`  |

A backslash followed by anything else is **malformed**. An unescaped `|`
inside a memo is impossible by construction: it would be read as a field
separator. The memo may be empty.

### check — integrity character

`check` is a single character from the lowercase base36 alphabet
`0123456789abcdefghijklmnopqrstuvwxyz`.

Its value is:

```
sum(all UTF-8 bytes of the first four fields AND the three pipe
    separators that join them) mod 36
```

That is: take the record text up to but **not including** the fourth
pipe, sum its bytes, take mod 36, and index into the base36 alphabet.

A record whose check character does not match the computed value is
**invalid**.

## Rejection

A record that is **malformed** or **invalid** is **rejected**. Rejected
records are counted and skipped. They are never a fatal error: a reader
must process the rest of the file.
