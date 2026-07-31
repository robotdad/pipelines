"""Oracle ledgerstat. Validates the hidden acceptance suite; not the subject."""

from __future__ import annotations

import sys

ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyz"


def split_fields(line):
    fields, current, i = [], [], 0
    while i < len(line):
        c = line[i]
        if c == "\\":
            if i + 1 >= len(line):
                return None
            nxt = line[i + 1]
            if nxt == "p":
                current.append("|")
            elif nxt == "\\":
                current.append("\\")
            else:
                return None
            i += 2
            continue
        if c == "|":
            fields.append("".join(current))
            current = []
            i += 1
            continue
        current.append(c)
        i += 1
    fields.append("".join(current))
    return fields


def prefix_of(line):
    seen, i = 0, 0
    while i < len(line):
        if line[i] == "\\":
            i += 2
            continue
        if line[i] == "|":
            seen += 1
            if seen == 4:
                return line[:i]
        i += 1
    return None


def valid_category(value):
    if not value:
        return False
    return all(("a" <= c <= "z") or c.isdigit() or c == "_" for c in value)


def parse_oday(value):
    if value.count(".") != 1:
        return False
    day, year = value.split(".")
    if len(year) != 2 or not year.isdigit():
        return False
    if not day.isdigit() or (len(day) > 1 and day.startswith("0")):
        return False
    return 1 <= int(day) <= 366


def parse_amount(value):
    if not value:
        return None
    negative = value.endswith("-")
    digits = value[:-1] if negative else value
    if not digits or not digits.isdigit():
        return None
    n = int(digits)
    return -n if negative else n


def main(argv):
    if len(argv) != 2:
        print("usage: python3 -m ledgerstat FILE", file=sys.stderr)
        return 2
    try:
        with open(argv[1], encoding="utf-8") as handle:
            text = handle.read()
    except OSError as error:
        print(f"cannot read {argv[1]}: {error}", file=sys.stderr)
        return 2

    totals, rejected = {}, 0
    for line in text.splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        fields = split_fields(line)
        if fields is None or len(fields) != 5:
            rejected += 1
            continue
        oday, category, amount_text, _memo, check = fields
        amount = parse_amount(amount_text)
        prefix = prefix_of(line)
        if (
            not parse_oday(oday)
            or not valid_category(category)
            or amount is None
            or prefix is None
            or len(check) != 1
            or check not in ALPHABET
            or ALPHABET[sum(prefix.encode("utf-8")) % 36] != check
        ):
            rejected += 1
            continue
        totals[category] = totals.get(category, 0) + amount

    out = []
    for category in sorted(totals):
        minor = totals[category]
        sign = "-" if minor < 0 else ""
        magnitude = abs(minor)
        out.append(f"{category}\t{sign}{magnitude // 100}.{magnitude % 100:02d}")
    out.append(f"rejected\t{rejected}")
    sys.stdout.write("\n".join(out) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
