"""Strict plan parsing, canonicalization and drift hashes."""

import hashlib
import json
import unicodedata
from collections.abc import Mapping
from dataclasses import asdict
from typing import Any

from deity.core.contracts.planning import PlanDraft, PlanStep

_DRAFT_KEYS = {"schema_version", "goal", "assumptions", "steps", "risks", "verification"}
_STEP_KEYS = {"ordinal", "title", "description", "files", "verification"}


def _text(value: object, *, maximum: int) -> str:
    if not isinstance(value, str):
        raise ValueError("plan text must be a string")  # noqa: TRY004
    normalized = unicodedata.normalize("NFC", value).strip()
    if not normalized or len(normalized) > maximum:
        raise ValueError("plan text length is invalid")
    return normalized


def _texts(value: object, *, maximum_items: int, maximum_length: int) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > maximum_items:
        raise ValueError("plan list is invalid")
    return tuple(_text(item, maximum=maximum_length) for item in value)


def parse_plan_draft(payload: Mapping[str, object]) -> PlanDraft:
    """Reject partial, extra or oversized model plan fields."""
    if set(payload) != _DRAFT_KEYS or payload.get("schema_version") != 1:
        raise ValueError("invalid plan schema")
    raw_steps = payload["steps"]
    if not isinstance(raw_steps, list) or not 1 <= len(raw_steps) <= 100:
        raise ValueError("invalid plan steps")
    steps: list[PlanStep] = []
    ordinals: set[int] = set()
    for raw in raw_steps:
        if not isinstance(raw, dict) or set(raw) != _STEP_KEYS:
            raise ValueError("invalid plan step")
        ordinal = raw["ordinal"]
        if type(ordinal) is not int or ordinal < 1 or ordinal in ordinals:
            raise ValueError("invalid plan step ordinal")
        ordinals.add(ordinal)
        steps.append(
            PlanStep(
                ordinal,
                _text(raw["title"], maximum=200),
                _text(raw["description"], maximum=4000),
                _texts(raw["files"], maximum_items=100, maximum_length=1000),
                _texts(raw["verification"], maximum_items=30, maximum_length=1000),
            )
        )
    if sorted(ordinals) != list(range(1, len(steps) + 1)):
        raise ValueError("plan step ordinals must be contiguous")
    return PlanDraft(
        1,
        _text(payload["goal"], maximum=4000),
        _texts(payload["assumptions"], maximum_items=50, maximum_length=1000),
        tuple(sorted(steps, key=lambda step: step.ordinal)),
        _texts(payload["risks"], maximum_items=50, maximum_length=1000),
        _texts(payload["verification"], maximum_items=50, maximum_length=1000),
    )


def canonical_plan_json(draft: PlanDraft) -> str:
    """Serialize a validated plan without timestamps or environment paths."""
    return json.dumps(asdict(draft), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def plan_sha256(draft: PlanDraft) -> str:
    return hashlib.sha256(canonical_plan_json(draft).encode("utf-8")).hexdigest()


def requirements_sha256(user_text: str, constraints: tuple[str, ...] = ()) -> str:
    payload: dict[str, Any] = {
        "user_text": unicodedata.normalize("NFC", user_text),
        "constraints": [unicodedata.normalize("NFC", item) for item in constraints],
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
