"""Application use case for deterministic code location."""

from deity.core.contracts.code_intelligence import (
    CodeSearchPort,
    CodeSearchResult,
    ReferenceQuery,
)


class CodeIntelligenceService:
    def __init__(self, search: CodeSearchPort) -> None:
        self.search = search

    async def find_symbol(self, query: ReferenceQuery) -> CodeSearchResult:
        return await self.search.find_symbol(query)

    async def find_references(self, query: ReferenceQuery) -> CodeSearchResult:
        return await self.search.find_references(query)

    async def status(self) -> dict[str, object]:
        return await self.search.index_status()
