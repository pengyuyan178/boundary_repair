"""Strict JSON and dataclass serialization shared by algorithms, without adapter dependencies."""
from __future__ import annotations
import json
from dataclasses import fields, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from boundary_repair.domain.errors import ValidationError


def plain(value: object) -> Any:
    """Convert immutable domain objects to JSON data, preserving all unresolved fields."""
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: plain(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (tuple, list)):
        return [plain(v) for v in value]
    if isinstance(value, dict):
        return {str(k): plain(v) for k, v in value.items()}
    return value


def _unique_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Reject duplicate JSON keys instead of silently accepting the last model value."""
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValidationError('duplicate_json_key')
        result[key] = value
    return result


def strict_json(text: str, maximum: int = 2000000) -> dict[str, Any]:
    """Require a single JSON object, bounded size, finite values and no Markdown extraction."""
    if len(text.encode('utf-8')) > maximum:
        raise ValidationError('json_size_limit')
    try:
        data = json.loads(text, object_pairs_hook=_unique_pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValidationError('nonfinite_json')))
    except (ValueError, RecursionError) as exc:
        raise ValidationError('invalid_json') from exc
    if not isinstance(data, dict):
        raise ValidationError('json_object_required')
    return data


def require_keys(data: object, required: set[str], optional: set[str] | None = None) -> dict[str, Any]:
    """Validate exact object fields at an external boundary."""
    if not isinstance(data, dict) or not required <= data.keys() or data.keys() - required - (optional or set()):
        raise ValidationError('invalid_object_fields')
    return data


def text_field(value: object, maximum: int = 200000) -> str:
    """Return nonempty bounded text without coercing nonstrings."""
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ValidationError('invalid_text_field')
    return value
