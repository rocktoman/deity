"""Deterministic, provider-neutral context budgeting and compaction."""

import hashlib
import json
import math
import re
from dataclasses import dataclass
from typing import Any, Protocol


class ContextOverflow(ValueError):
    """Required context cannot fit the configured model window."""


def normalize_unicode(value: str) -> str:
    """Join valid surrogate pairs and replace isolated surrogates at text boundaries."""
    if not any(0xD800 <= ord(char) <= 0xDFFF for char in value):
        return value
    return value.encode("utf-16", "surrogatepass").decode("utf-16", "replace")


def sanitize_text(value: str) -> str:
    """Redact credential-shaped assignments before model or storage use."""
    return re.sub(
        r"(?i)\b(api_key|token|secret|password)\s*[:=]\s*[^\s,;]+",
        r"\1=[redacted]",
        normalize_unicode(value),
    )


@dataclass(frozen=True)
class ContextBudget:
    """Configured model input and output reserve, in estimated tokens."""

    model_window: int
    reserved_output: int
    safety_margin: int

    def __post_init__(self) -> None:
        if (
            self.model_window <= 0
            or self.reserved_output <= 0
            or self.safety_margin <= 0
            or self.reserved_output + self.safety_margin >= self.model_window
        ):
            raise ValueError("invalid context budget")

    @property
    def soft_limit(self) -> int:
        """Return the threshold that starts compaction."""
        return int(self.model_window * 0.75)

    @property
    def input_limit(self) -> int:
        """Return the hard maximum for input messages."""
        return min(
            self.model_window - self.reserved_output - self.safety_margin,
            int(self.model_window * 0.90),
        )


class TokenEstimator(Protocol):
    """Estimate provider message tokens without a vendor SDK."""

    def estimate(self, messages: list[dict[str, Any]], tool_schema_tokens: int = 0) -> int: ...


class ConservativeEstimator:
    """Estimate from both UTF-8 byte count and Unicode character count."""

    version = 1

    def estimate(self, messages: list[dict[str, Any]], tool_schema_tokens: int = 0) -> int:
        """Return a conservative preflight count, not provider-reported usage."""
        count = tool_schema_tokens
        for message in messages:
            encoded = json.dumps(message, ensure_ascii=False, sort_keys=True)
            count += max(math.ceil(len(encoded.encode("utf-8")) / 3), math.ceil(len(encoded) / 2))
            count += 8
        return count


@dataclass(frozen=True)
class ArtifactRef:
    """Pointer to an original committed message after context clipping."""

    message_index: int
    sha256: str
    byte_count: int
    truncated: bool = True

    @property
    def id(self) -> str:
        """Use the content digest as a stable local artifact ID."""
        return self.sha256


@dataclass(frozen=True)
class ContextSnapshot:
    """Hashed model-visible projection and its original-message references."""

    messages: tuple[dict[str, Any], ...]
    estimated_tokens: int
    sha256: str
    artifacts: tuple[ArtifactRef, ...] = ()


def canonical_json(value: Any) -> str:
    """Serialize a stable representation for checkpoint hashes."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class ContextCompactor:
    """Keep required facts and recent turns; summarize old typed facts only."""

    def __init__(self, estimator: TokenEstimator | None = None) -> None:
        self.estimator = estimator or ConservativeEstimator()

    def compact(
        self,
        messages: list[dict[str, Any]],
        budget: ContextBudget,
        tool_schema_tokens: int = 0,
    ) -> ContextSnapshot:
        """Return a deterministic safe projection or fail explicitly."""
        count = self.estimator.estimate(messages, tool_schema_tokens)
        if count <= min(budget.soft_limit, budget.input_limit):
            return self._snapshot(messages, count)

        fixed: list[dict[str, Any]] = []
        turns: list[list[tuple[int, dict[str, Any]]]] = []
        for index, message in enumerate(messages):
            role = message.get("role")
            if role in {"system", "user"}:
                fixed.append(message)
            elif role == "assistant":
                turns.append([(index, message)])
            elif role == "tool" and turns:
                turns[-1].append((index, message))
            else:
                raise ContextOverflow("incomplete or unsupported message sequence")

        if self.estimator.estimate(fixed, tool_schema_tokens) > budget.input_limit:
            raise ContextOverflow("required user and system instructions exceed context")
        old = turns[:-4]
        recent = turns[-4:]
        failures: list[dict[str, Any]] = []
        evidence_ids: set[str] = set()
        modified_files: set[str] = set()
        source_indices: list[int] = []
        old_turn_count = 0
        for turn in old:
            old_turn_count += 1
            for index, message in turn:
                source_indices.append(index)
                content = message.get("content")
                if isinstance(content, str) and message.get("role") == "assistant":
                    try:
                        prior = json.loads(content)
                    except json.JSONDecodeError:
                        prior = None
                    if isinstance(prior, dict) and prior.get("kind") == "compaction_ledger_v1":
                        old_turn_count += int(prior.get("older_turns", 0)) - 1
                        source_indices.extend(prior.get("source_message_indices", []))
                        failures.extend(prior.get("unresolved", []))
                        evidence_ids.update(prior.get("evidence_ids", []))
                        modified_files.update(prior.get("modified_files", []))
                for call in message.get("tool_calls", []):
                    if call.get("name") in {"write_file", "apply_patch"}:
                        path = call.get("arguments", {}).get("path")
                        if isinstance(path, str):
                            modified_files.add(path)
                if isinstance(message.get("evidence_id"), str):
                    evidence_ids.add(message["evidence_id"])
                if message.get("status") in {"error", "denied", "timeout", "cancelled", "unknown"}:
                    failures.append(
                        {
                            "call_id": message.get("call_id"),
                            "status": message.get("status"),
                            "evidence_id": message.get("evidence_id"),
                        }
                    )
        ledger: dict[str, Any] = {
            "role": "assistant",
            "content": canonical_json(
                {
                    "kind": "compaction_ledger_v1",
                    "older_turns": old_turn_count,
                    "source_message_indices": sorted(set(source_indices)),
                    "unresolved": failures,
                    "evidence_ids": sorted(evidence_ids),
                    "modified_files": sorted(modified_files),
                }
            ),
        }
        candidate = list(fixed)
        if old:
            candidate.append(ledger)
        artifacts: list[ArtifactRef] = []
        for turn in recent:
            for index, message in turn:
                clipped = dict(message)
                content = clipped.get("content")
                if (
                    clipped.get("role") == "tool"
                    and isinstance(content, str)
                    and len(content) > 256
                ):
                    raw = content.encode("utf-8")
                    digest = hashlib.sha256(raw).hexdigest()
                    clipped["content"] = (
                        content[:128]
                        + f"\n[truncated artifact={digest}; rerun the source read tool if more detail is needed]\n"
                        + content[-128:]
                    )
                    clipped["artifact_ref"] = {
                        "id": digest,
                        "sha256": digest,
                        "byte_count": len(raw),
                        "truncated": True,
                    }
                    clipped["truncated"] = True
                    artifacts.append(ArtifactRef(index, digest, len(raw)))
                candidate.append(clipped)
        final_count = self.estimator.estimate(candidate, tool_schema_tokens)
        if final_count > budget.input_limit:
            raise ContextOverflow("required recent turns and ledger exceed context")
        return self._snapshot(candidate, final_count, tuple(artifacts))

    @staticmethod
    def _snapshot(
        messages: list[dict[str, Any]], count: int, artifacts: tuple[ArtifactRef, ...] = ()
    ) -> ContextSnapshot:
        payload = canonical_json(messages).encode("utf-8")
        return ContextSnapshot(
            tuple(messages), count, hashlib.sha256(payload).hexdigest(), artifacts
        )
