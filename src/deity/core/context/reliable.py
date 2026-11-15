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


SUMMARY_KIND = "compaction_summary_v1"
LEDGER_KIND = "compaction_ledger_v1"
_SUMMARY_SOURCE_CAP = 48_000


def build_summary_prompt(source: str) -> str:
    """构建结构化摘要提示（ADR-019 A5）。

    章节结构参照 openai/codex 的 compact 摘要模板
    （prompts/templates/compact/，Apache-2.0）重新编写：只要叙事摘
    要，不要复述原文；事实性清单（已改文件、未解决失败、证据）由
    确定性账本另行承载，不在摘要里重复计数。
    """
    return (
        "你正在为一个编程 Agent 的长会话生成上下文压缩摘要。后续会话只会看到"
        "这份摘要（外加一份确定性事实账本），因此必须保留继续任务所需的全部"
        "脉络。用中文输出，严格按以下章节，不要输出章节之外的内容：\n"
        "## 目标\n用户要达成的最终目标与验收标准。\n"
        "## 进展\n已经完成了什么、验证到了什么程度。\n"
        "## 关键决定\n已做出的技术/方案决定及其理由。\n"
        "## 未解决\n失败、阻塞、待确认事项（含具体报错要点）。\n"
        "## 下一步\n紧接着应该做的事，按顺序列出。\n\n"
        "以下是将被压缩的早期对话（工具输出已截断）：\n\n" + source
    )


def _parse_typed(content: Any) -> dict[str, Any] | None:
    if not isinstance(content, str):
        return None
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


class ContextCompactor:
    """Keep required facts and recent turns; summarize old typed facts only."""

    def __init__(self, estimator: TokenEstimator | None = None) -> None:
        self.estimator = estimator or ConservativeEstimator()

    @staticmethod
    def _split(
        messages: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], list[list[tuple[int, dict[str, Any]]]]]:
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
        return fixed, turns

    def summary_source(
        self,
        messages: list[dict[str, Any]],
        budget: ContextBudget,
        tool_schema_tokens: int = 0,
    ) -> str | None:
        """将被折叠的旧轮次渲染为摘要素材；无需压缩或无旧轮次时返回 None。

        渲染规则：角色标注；工具内容截断到 800 字符；上一代账本只带
        事实要点，上一代摘要全文带入（供新摘要承接）。总量上限
        48K 字符，超限时从最早的轮次开始省略并标注。
        """
        count = self.estimator.estimate(messages, tool_schema_tokens)
        if count <= min(budget.soft_limit, budget.input_limit):
            return None
        _, turns = self._split(messages)
        old = turns[:-4]
        if not old:
            return None
        rendered: list[str] = []
        for turn in old:
            lines: list[str] = []
            for _, message in turn:
                role = message.get("role")
                content = message.get("content")
                typed = _parse_typed(content)
                if typed is not None and typed.get("kind") == SUMMARY_KIND:
                    lines.append(f"[此前摘要]\n{typed.get('summary', '')}")
                elif typed is not None and typed.get("kind") == LEDGER_KIND:
                    files = typed.get("modified_files", [])
                    names = (
                        [item for item in files if isinstance(item, str)]
                        if isinstance(files, list)
                        else []
                    )
                    unresolved = typed.get("unresolved", [])
                    lines.append(
                        "[此前账本] 已改文件: "
                        + ", ".join(names)
                        + f"；未解决: {len(unresolved) if isinstance(unresolved, list) else 0} 项"
                    )
                elif isinstance(content, str) and content:
                    clipped = content if role != "tool" else content[:800]
                    lines.append(f"[{role}] {clipped}")
                for call in message.get("tool_calls", []):
                    args = json.dumps(call.get("arguments", {}), ensure_ascii=False)
                    lines.append(f"[调用 {call.get('name')}] {args[:300]}")
            if lines:
                rendered.append("\n".join(lines))
        while rendered and sum(len(part) for part in rendered) > _SUMMARY_SOURCE_CAP:
            rendered.pop(0)
            if rendered and not rendered[0].startswith("（更早"):
                rendered.insert(0, "（更早的轮次已省略）")
        return "\n\n".join(rendered) or None

    def compact(
        self,
        messages: list[dict[str, Any]],
        budget: ContextBudget,
        tool_schema_tokens: int = 0,
        summary: str | None = None,
    ) -> ContextSnapshot:
        """Return a deterministic safe projection or fail explicitly.

        summary 是可选的 LLM 叙事摘要（ADR-019 A5）：给出时在账本后
        追加一条 compaction_summary_v1 消息；未给出时若旧轮次中已
        有上一代摘要则原样结转。账本审计层不因摘要存在而改变。
        """
        count = self.estimator.estimate(messages, tool_schema_tokens)
        if count <= min(budget.soft_limit, budget.input_limit):
            return self._snapshot(messages, count)

        fixed, turns = self._split(messages)

        if self.estimator.estimate(fixed, tool_schema_tokens) > budget.input_limit:
            raise ContextOverflow("required user and system instructions exceed context")
        old = turns[:-4]
        recent = turns[-4:]
        failures: list[dict[str, Any]] = []
        evidence_ids: set[str] = set()
        modified_files: set[str] = set()
        source_indices: list[int] = []
        old_turn_count = 0
        prior_summary: str | None = None
        for turn in old:
            old_turn_count += 1
            for index, message in turn:
                source_indices.append(index)
                content = message.get("content")
                if message.get("role") == "assistant":
                    prior = _parse_typed(content)
                    if prior is not None and prior.get("kind") == LEDGER_KIND:
                        old_turn_count += int(prior.get("older_turns", 0)) - 1
                        source_indices.extend(prior.get("source_message_indices", []))
                        failures.extend(prior.get("unresolved", []))
                        evidence_ids.update(prior.get("evidence_ids", []))
                        modified_files.update(prior.get("modified_files", []))
                    elif prior is not None and prior.get("kind") == SUMMARY_KIND:
                        carried = prior.get("summary")
                        if isinstance(carried, str) and carried:
                            prior_summary = carried
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
                    "kind": LEDGER_KIND,
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
            effective_summary = summary if summary is not None else prior_summary
            if effective_summary:
                candidate.append(
                    {
                        "role": "assistant",
                        "content": canonical_json(
                            {
                                "kind": SUMMARY_KIND,
                                "summary": sanitize_text(effective_summary),
                            }
                        ),
                    }
                )
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
