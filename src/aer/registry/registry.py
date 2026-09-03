"""Deterministic lexical and fuzzy capability discovery."""

from __future__ import annotations

import re
from difflib import SequenceMatcher

from aer.errors import AerError
from aer.registry.catalog import CAPABILITIES
from aer.registry.models import Capability

_TOKEN_RE = re.compile(r"[\w.+:-]+", re.UNICODE)


class CapabilityRegistry:
    def __init__(self, capabilities: tuple[Capability, ...]) -> None:
        by_name = {capability.name: capability for capability in capabilities}
        if len(by_name) != len(capabilities):
            raise ValueError("Capability names must be unique.")
        self._by_name = by_name

    def list_names(self) -> dict[str, list[str]]:
        return {"names": sorted(self._by_name)}

    def schema(
        self, name: str, *, compact: bool = False, example: bool = False
    ) -> dict[str, object]:
        capability = self._by_name.get(name)
        if capability is None:
            raise AerError(
                "NOT_FOUND",
                "Capability was not found.",
                operation="schema",
                target=name,
                suggested_action="Use `aer schema --list-names` to inspect available names.",
            )
        return capability.schema_record(compact=compact, include_example=example)

    def discover(self, query: str, *, limit: int = 5) -> dict[str, object]:
        query = query.strip()
        if not query:
            raise AerError(
                "INVALID_ARGUMENT",
                "Discovery query must not be empty.",
                operation="discover",
            )
        if limit < 1 or limit > 20:
            raise AerError(
                "INVALID_ARGUMENT",
                "Discovery limit must be between 1 and 20.",
                operation="discover",
                target=str(limit),
            )
        ranked = sorted(
            (
                (self._score(capability, query), capability.name, capability)
                for capability in self._by_name.values()
            ),
            key=lambda item: (-item[0], item[1]),
        )
        matches = [capability.discovery_record() for score, _, capability in ranked if score > 0]
        return {"query": query, "capabilities": matches[:limit]}

    @staticmethod
    def _score(capability: Capability, query: str) -> int:
        normalized_query = query.casefold()
        query_tokens = _tokens(query)
        name_tokens = _tokens(capability.name.replace(".", " "))
        keyword_tokens = {token for keyword in capability.keywords for token in _tokens(keyword)}
        summary_tokens = _tokens(capability.summary)
        all_tokens = name_tokens | keyword_tokens | summary_tokens
        searchable = " ".join(
            (capability.name, capability.summary, *capability.keywords)
        ).casefold()

        score = 0
        if normalized_query == capability.name.casefold():
            score += 1_000
        if normalized_query in searchable:
            score += 120
        matched = 0
        for token in query_tokens:
            if token in name_tokens:
                score += 45
                matched += 1
            elif token in keyword_tokens:
                score += 30
                matched += 1
            elif token in summary_tokens:
                score += 15
                matched += 1
            elif any(token in candidate or candidate in token for candidate in all_tokens):
                score += 8
                matched += 1
            else:
                similarity = max(
                    (SequenceMatcher(None, token, candidate).ratio() for candidate in all_tokens),
                    default=0.0,
                )
                if similarity >= 0.72:
                    score += round(similarity * 6)
                    matched += 1
        if query_tokens and matched == len(query_tokens):
            score += 40
        return score


def _tokens(value: str) -> set[str]:
    return {match.group(0).casefold() for match in _TOKEN_RE.finditer(value)}


REGISTRY = CapabilityRegistry(CAPABILITIES)


def discover(query: str, *, limit: int = 5) -> dict[str, object]:
    return REGISTRY.discover(query, limit=limit)


def schema(name: str, *, compact: bool = False, example: bool = False) -> dict[str, object]:
    return REGISTRY.schema(name, compact=compact, example=example)


def list_names() -> dict[str, list[str]]:
    return REGISTRY.list_names()
