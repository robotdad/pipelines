"""LedgerLine v1 reference parser.

This module is the normative reference for SPEC.md. Where the prose and this
code disagree, the prose is authoritative and this code is a bug.
"""

from __future__ import annotations

from dataclasses import dataclass

ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyz"
CATEGORY_EXTRA = "_"


@dataclass(frozen=True)
class Record:
    """One accepted ledger record."""

    day_of_year: int
    year_suffix: str
    category: str
    minor_units: int
    memo: str


@dataclass(frozen=True)
class RejectedRecord:
    """One rejected line, with the reason it was rejected."""

    line_number: int
    raw: str
    reason: str


def check_char(prefix: str) -> str:
    """Return the base36 check character for the first four joined fields.

    `prefix` is the record text up to but not including the fourth pipe.
    """
    return ALPHABET[sum(prefix.encode("utf-8")) % 36]


def _split_fields(line: str) -> list[str] | None:
    """Split on unescaped pipes. Returns None if a bad escape is present."""
    fields: list[str] = []
    current: list[str] = []
    index = 0
    length = len(line)
    while index < length:
        character = line[index]
        if character == "\\":
            if index + 1 >= length:
                return None
            following = line[index + 1]
            if following == "p":
                current.append("|")
            elif following == "\\":
                current.append("\\")
            else:
                return None
            index += 2
            continue
        if character == "|":
            fields.append("".join(current))
            current = []
            index += 1
            continue
        current.append(character)
        index += 1
    fields.append("".join(current))
    return fields


def _valid_category(value: str) -> bool:
    if not value:
        return False
    return all(c.islower() and c.isascii() or c.isdigit() or c in CATEGORY_EXTRA for c in value)


def _parse_oday(value: str) -> tuple[int, str] | None:
    if value.count(".") != 1:
        return None
    day_text, year_text = value.split(".")
    if len(year_text) != 2 or not year_text.isdigit():
        return None
    if not day_text.isdigit() or (len(day_text) > 1 and day_text.startswith("0")):
        return None
    day = int(day_text)
    if not 1 <= day <= 366:
        return None
    return day, year_text


def _parse_amount(value: str) -> int | None:
    if not value:
        return None
    negative = value.endswith("-")
    digits = value[:-1] if negative else value
    if not digits or not digits.isdigit():
        return None
    magnitude = int(digits)
    return -magnitude if negative else magnitude


def parse_line(line: str, line_number: int = 0) -> Record | RejectedRecord | None:
    """Parse one line. Returns None for comments and blank lines."""
    if not line.strip():
        return None
    if line.startswith("#"):
        return None

    fields = _split_fields(line)
    if fields is None:
        return RejectedRecord(line_number, line, "malformed: bad escape sequence")
    if len(fields) != 5:
        return RejectedRecord(line_number, line, f"malformed: {len(fields)} fields, expected 5")

    raw_oday, raw_category, raw_amount, memo, check = fields

    oday = _parse_oday(raw_oday)
    if oday is None:
        return RejectedRecord(line_number, line, "malformed: bad ordinal day")
    if not _valid_category(raw_category):
        return RejectedRecord(line_number, line, "malformed: bad category")
    amount = _parse_amount(raw_amount)
    if amount is None:
        return RejectedRecord(line_number, line, "malformed: bad amount")

    prefix_source = _unsplit_prefix(line)
    if prefix_source is None:
        return RejectedRecord(line_number, line, "malformed: cannot locate check field")
    if len(check) != 1 or check not in ALPHABET:
        return RejectedRecord(line_number, line, "malformed: bad check character")
    if check_char(prefix_source) != check:
        return RejectedRecord(line_number, line, "invalid: check character mismatch")

    day, year_suffix = oday
    return Record(day, year_suffix, raw_category, amount, memo)


def _unsplit_prefix(line: str) -> str | None:
    """Return raw line text up to (not including) the fourth unescaped pipe."""
    seen = 0
    index = 0
    length = len(line)
    while index < length:
        character = line[index]
        if character == "\\":
            index += 2
            continue
        if character == "|":
            seen += 1
            if seen == 4:
                return line[:index]
        index += 1
    return None


def parse_text(text: str) -> tuple[list[Record], list[RejectedRecord]]:
    """Parse a whole file body into accepted records and rejected lines."""
    accepted: list[Record] = []
    rejected: list[RejectedRecord] = []
    for number, line in enumerate(text.splitlines(), start=1):
        result = parse_line(line, number)
        if result is None:
            continue
        if isinstance(result, Record):
            accepted.append(result)
        else:
            rejected.append(result)
    return accepted, rejected
