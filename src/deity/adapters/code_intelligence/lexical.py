"""Bounded lexical symbol and reference search for Python, Java and TypeScript."""

import asyncio
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Literal, cast

from deity.core.contracts.code_intelligence import (
    CodeSearchResult,
    ReferenceQuery,
    SymbolLocation,
)

Language = Literal["python", "java", "typescript", "unknown"]
_LANGUAGE: dict[str, Language] = {
    ".py": "python",
    ".java": "java",
    ".ts": "typescript",
    ".tsx": "typescript",
}
_PYTHON = re.compile(r"^\s*(?:(async)\s+)?(class|def)\s+([A-Za-z_]\w*)\b")
_JAVA_TYPE = re.compile(
    r"^\s*(?:(?:public|protected|private|abstract|final|static|sealed|non-sealed)\s+)*"
    r"(class|interface|enum|record)\s+([A-Za-z_]\w*)\b"
)
_JAVA_METHOD = re.compile(
    r"^\s*(?:(?:public|protected|private|abstract|final|static|synchronized|native)\s+)*"
    r"[A-Za-z_$][\w$<>,.?\[\] ]*\s+([A-Za-z_$][\w$]*)\s*\([^;]*\)\s*(?:\{|throws\b)"
)
_TS_TYPE = re.compile(
    r"^\s*(?:(?:export|default|declare|abstract|async)\s+)*"
    r"(class|interface|type|function|enum)\s+([A-Za-z_$][\w$]*)\b"
)
_TS_VARIABLE = re.compile(r"^\s*(?:(?:export|declare)\s+)*(?:const|let|var)\s+([A-Za-z_$][\w$]*)\b")


class LexicalCodeSearch:
    """Return candidates with explicit lexical provenance; never claim semantic certainty."""

    backend_version = "lexical-v1"

    def __init__(
        self,
        workspace: Path,
        *,
        cache_path: Path | None,
        max_files: int = 20_000,
        max_file_bytes: int = 2_000_000,
        result_limit: int = 200,
    ) -> None:
        self.workspace = workspace.resolve(strict=True)
        self.cache_path = cache_path
        self.max_files = max_files
        self.max_file_bytes = max_file_bytes
        self.result_limit = result_limit
        self._last_diagnostics: tuple[str, ...] = ()
        self._last_partial = False

    async def find_symbol(self, query: ReferenceQuery) -> CodeSearchResult:
        definitions, diagnostics, partial = await self._index(query)
        matches = [item for item in definitions if item.symbol == query.symbol]
        matches.sort(key=lambda item: (item.path, item.line, item.column or 0, item.kind))
        if len(matches) > min(query.limit, self.result_limit):
            partial = True
            matches = matches[: min(query.limit, self.result_limit)]
        return CodeSearchResult(tuple(matches), partial, tuple(diagnostics))

    async def find_references(self, query: ReferenceQuery) -> CodeSearchResult:
        paths, diagnostics, partial = await self._source_paths(query)
        pattern = re.compile(rf"(?<![\w$]){re.escape(query.symbol)}(?![\w$])")
        matches: list[SymbolLocation] = []
        limit = min(query.limit, self.result_limit)
        for path, language, text, _digest in paths:
            for line_number, line in enumerate(text.splitlines(), 1):
                for match in pattern.finditer(line):
                    matches.append(
                        SymbolLocation(
                            path,
                            line_number,
                            match.start() + 1,
                            query.symbol,
                            language,
                            "reference",
                            "lexical",
                            "textual_reference",
                        )
                    )
                    if len(matches) >= limit:
                        partial = True
                        return CodeSearchResult(tuple(matches), partial, tuple(diagnostics))
        return CodeSearchResult(tuple(matches), partial, tuple(diagnostics))

    async def index_status(self) -> dict[str, object]:
        cache = self._read_cache()
        return {
            "backend": self.backend_version,
            "workspace": str(self.workspace),
            "cached_files": len(cache.get("files", {})),
            "partial": self._last_partial,
            "diagnostics": self._last_diagnostics,
        }

    async def _index(self, query: ReferenceQuery) -> tuple[list[SymbolLocation], list[str], bool]:
        paths, diagnostics, partial = await self._source_paths(query)
        cache = self._read_cache()
        old_files = cache.get("files", {}) if isinstance(cache.get("files"), dict) else {}
        new_files: dict[str, Any] = {}
        found: list[SymbolLocation] = []
        for path, language, text, digest in paths:
            source = self.workspace / path
            stat = source.stat()
            old = old_files.get(path) if isinstance(old_files, dict) else None
            if (
                isinstance(old, dict)
                and old.get("sha256") == digest
                and old.get("size") == stat.st_size
                and old.get("version") == self.backend_version
            ):
                definitions = self._decode_locations(old.get("definitions"), path, language)
            else:
                definitions = self._definitions(path, language, text)
            new_files[path] = {
                "size": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
                "sha256": digest,
                "version": self.backend_version,
                "definitions": [self._encode_location(item) for item in definitions],
            }
            found.extend(definitions)
        self._write_cache({"version": 1, "workspace": str(self.workspace), "files": new_files})
        self._last_diagnostics = tuple(diagnostics)
        self._last_partial = partial
        return found, diagnostics, partial

    async def _source_paths(
        self, query: ReferenceQuery
    ) -> tuple[list[tuple[str, Language, str, str]], list[str], bool]:
        process = await asyncio.create_subprocess_exec(
            "rg",
            "--files",
            "-0",
            "-g",
            "*.py",
            "-g",
            "*.java",
            "-g",
            "*.ts",
            "-g",
            "*.tsx",
            cwd=self.workspace,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await process.communicate()
        if process.returncode not in {0, 1}:
            raise OSError("ripgrep file discovery failed")
        raw_paths = sorted(item for item in stdout.decode().split("\0") if item)
        diagnostics: list[str] = []
        partial = len(raw_paths) > self.max_files
        raw_paths = raw_paths[: self.max_files]
        selected: list[tuple[str, Language, str, str]] = []
        for relative in raw_paths:
            language = _LANGUAGE.get(Path(relative).suffix.lower(), "unknown")
            if query.language is not None and query.language != language:
                continue
            if query.path is not None and not Path(relative).is_relative_to(Path(query.path)):
                continue
            source = (self.workspace / relative).resolve(strict=True)
            if not source.is_relative_to(self.workspace) or source.is_symlink():
                diagnostics.append(f"symlink skipped: {relative}")
                continue
            if source.stat().st_size > self.max_file_bytes:
                diagnostics.append(f"oversized skipped: {relative}")
                continue
            content = source.read_bytes()
            if b"\0" in content:
                diagnostics.append(f"binary skipped: {relative}")
                continue
            try:
                text = content.decode("utf-8-sig")
            except UnicodeDecodeError:
                diagnostics.append(f"encoding skipped: {relative}")
                continue
            selected.append((relative, language, text, hashlib.sha256(content).hexdigest()))
        return selected, diagnostics, partial

    @staticmethod
    def _definitions(path: str, language: Language, text: str) -> list[SymbolLocation]:
        found: list[SymbolLocation] = []
        for line_number, line in enumerate(text.splitlines(), 1):
            match: re.Match[str] | None = None
            kind = "unknown"
            confidence: Literal["exact_definition", "candidate_definition"] = "exact_definition"
            if language == "python" and (match := _PYTHON.match(line)):
                kind = "async_function" if match.group(1) else match.group(2)
                symbol = match.group(3)
            elif language == "java" and (match := _JAVA_TYPE.match(line)):
                kind, symbol = match.group(1), match.group(2)
            elif language == "java" and (match := _JAVA_METHOD.match(line)):
                kind, symbol, confidence = "method", match.group(1), "candidate_definition"
            elif language == "typescript" and (match := _TS_TYPE.match(line)):
                kind, symbol = match.group(1), match.group(2)
            elif language == "typescript" and (match := _TS_VARIABLE.match(line)):
                kind, symbol, confidence = "variable", match.group(1), "candidate_definition"
            else:
                continue
            column = line.find(symbol) + 1
            found.append(
                SymbolLocation(
                    path,
                    line_number,
                    column,
                    symbol,
                    language,
                    kind,
                    "lexical",
                    confidence,
                )
            )
        return found

    @staticmethod
    def _encode_location(item: SymbolLocation) -> dict[str, object]:
        return {
            "line": item.line,
            "column": item.column,
            "symbol": item.symbol,
            "kind": item.kind,
            "confidence": item.confidence,
        }

    @staticmethod
    def _decode_locations(raw: object, path: str, language: Language) -> list[SymbolLocation]:
        if not isinstance(raw, list):
            return []
        decoded: list[SymbolLocation] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            try:
                confidence = item["confidence"]
                if confidence not in {"exact_definition", "candidate_definition"}:
                    return []
                decoded.append(
                    SymbolLocation(
                        path,
                        int(item["line"]),
                        int(item["column"]) if item.get("column") is not None else None,
                        str(item["symbol"]),
                        language,
                        str(item["kind"]),
                        "lexical",
                        cast(Literal["exact_definition", "candidate_definition"], confidence),
                    )
                )
            except (KeyError, TypeError, ValueError):
                return []
        return decoded

    def _read_cache(self) -> dict[str, Any]:
        if self.cache_path is None or not self.cache_path.is_file():
            return {}
        try:
            data = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return {}
        if (
            not isinstance(data, dict)
            or data.get("version") != 1
            or data.get("workspace") != str(self.workspace)
        ):
            return {}
        return data

    def _write_cache(self, payload: dict[str, Any]) -> None:
        if self.cache_path is None:
            return
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.cache_path.with_name(f".{self.cache_path.name}.{os.getpid()}.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        os.replace(temporary, self.cache_path)
