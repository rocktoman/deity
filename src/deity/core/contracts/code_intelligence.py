"""Code intelligence DTOs and ports."""

from dataclasses import dataclass
from typing import Literal, Protocol


@dataclass(frozen=True)
class SymbolLocation:
    path: str
    line: int
    column: int | None
    symbol: str
    language: Literal["python", "java", "typescript", "unknown"]
    kind: str
    source: Literal["lexical", "tree_sitter", "lsp"]
    confidence: Literal["exact_definition", "candidate_definition", "textual_reference"]


@dataclass(frozen=True)
class ReferenceQuery:
    symbol: str
    language: Literal["python", "java", "typescript", "unknown"] | None = None
    path: str | None = None
    limit: int = 200


@dataclass(frozen=True)
class CodeSearchResult:
    locations: tuple[SymbolLocation, ...]
    partial: bool = False
    diagnostics: tuple[str, ...] = ()


class CodeSearchPort(Protocol):
    async def find_symbol(self, query: ReferenceQuery) -> CodeSearchResult: ...
    async def find_references(self, query: ReferenceQuery) -> CodeSearchResult: ...
    async def index_status(self) -> dict[str, object]: ...


class IndexCache(Protocol):
    async def get(self, key: str) -> dict[str, object] | None: ...
    async def put(self, key: str, value: dict[str, object]) -> None: ...
    async def invalidate(self, workspace_id: str) -> None: ...
