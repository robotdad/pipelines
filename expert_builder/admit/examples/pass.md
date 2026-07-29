# wordfreq

A command-line tool that counts word frequencies in a text file.

## Goal

Give writers a quick way to see which words they lean on, without installing
anything heavier than a single binary.

## Done looks like

- `wordfreq notes.txt` prints one `count word` pair per line, highest count first.
- Ties are broken alphabetically, so output is stable across runs.
- `wordfreq --top 10 notes.txt` prints only the ten most frequent words.
- Words are compared case-insensitively: `The` and `the` count as one word.
- Running it on an empty file prints nothing and exits 0.
