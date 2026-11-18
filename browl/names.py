"""Choosing among name values (DESIGN.md §2.7): language ranking and CURIE expansion."""

from collections.abc import Iterable, Mapping, Sequence

from browl.rdf import Literal
from browl.settings import UNTAGGED

BUILTIN_PREFIXES = {
    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "rdfs": "http://www.w3.org/2000/01/rdf-schema#",
    "owl": "http://www.w3.org/2002/07/owl#",
    "xsd": "http://www.w3.org/2001/XMLSchema#",
    "skos": "http://www.w3.org/2004/02/skos/core#",
    "obo": "http://purl.obolibrary.org/obo/",
    "dc": "http://purl.org/dc/elements/1.1/",
    "dcterms": "http://purl.org/dc/terms/",
}
"""The built-in CURIE prefixes (§2.7), which `EXTRA_PREFIXES` overrides."""


def expand_curie(name: str, prefixes: Mapping[str, str]) -> str:
    """Return the IRI a CURIE stands for, or the name itself if its prefix isn't known (it's taken to be an IRI)."""
    prefix, colon, local = name.partition(":")
    if colon and prefix in prefixes and not local.startswith("//"):
        return prefixes[prefix] + local
    return name


def language_rank(language: str | None, ranking: Sequence[str]) -> float:
    """Return a language tag's position in the language ranking (O-5), lower first, by §2.7's matching rules.

    The tag is lowercased, and falls back as RFC 4647 "lookup" does, dropping subtags from the end (and a singleton left
    last) until one is listed. An untagged value matches `UNTAGGED`. A tag that matches nothing ranks after every
    listed language: just before `UNTAGGED` when it's last, otherwise last.

    Args:
        language: The literal's language tag; None if it has none.
        ranking: The language ranking, lowercased (`Settings.language_ranking`).
    """
    positions = {tag: index for index, tag in enumerate(ranking)}
    if language is None:
        return positions.get(UNTAGGED, len(ranking))
    subtags = language.lower().split("-")
    while subtags:
        position = positions.get("-".join(subtags))
        if position is not None:
            return position
        subtags.pop()
        while subtags and len(subtags[-1]) == 1:
            subtags.pop()
    return len(ranking) - 1.5 if ranking and ranking[-1] == UNTAGGED else len(ranking)


def best_values(values: Iterable[Literal], ranking: Sequence[str]) -> list[str]:
    """Return the distinct lexical forms of the values whose language ranks best (`language_rank`), sorted.

    The first is the one to take; more than one is a tie that only lexical order breaks. Empty if there are no values.
    """
    ranked: dict[str, float] = {}
    for literal in values:
        rank = language_rank(literal.language, ranking)
        ranked[literal.value] = min(rank, ranked.get(literal.value, rank))
    if not ranked:
        return []
    best = min(ranked.values())
    return sorted(value for value, rank in ranked.items() if rank == best)
