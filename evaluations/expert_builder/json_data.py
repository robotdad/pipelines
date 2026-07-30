"""Validated dynamic JSON boundaries for deterministic evaluation evidence."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any, TypeAlias, cast

JsonObject: TypeAlias = dict[str, Any]
JsonMapping: TypeAlias = Mapping[str, Any]


def parse_object(text: str, *, source: str) -> JsonObject:
    """Decode external JSON only when its root value is an object."""
    raw: Any = json.loads(text)
    if not isinstance(raw, dict) or not all(isinstance(key, str) for key in raw):
        raise ValueError(f"{source} must contain a JSON object")
    return cast(JsonObject, raw)


def as_object(value: object) -> JsonMapping | None:
    """Narrow an already-decoded dynamic value to a JSON object."""
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        return None
    return cast(JsonMapping, value)


def as_object_list(value: object) -> list[JsonMapping] | None:
    """Narrow an already-decoded dynamic value to a list of JSON objects."""
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return None
    objects = [as_object(item) for item in value]
    if any(item is None for item in objects):
        return None
    return [item for item in objects if item is not None]
