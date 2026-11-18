"""Name values (DESIGN.md §2.6, §2.7): the name properties, language ranking, normalization and CURIE expansion."""

import logging
import unicodedata
from collections.abc import Iterable, Mapping, Sequence

from browl.rdf import IRI, Literal, RDFSyntaxError
from browl.settings import UNTAGGED, Settings

logger = logging.getLogger(__name__)

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


def name_property_iris(settings: Settings) -> list[str]:
    """Return the IRIs of the name properties (`NAME_PROPERTIES`, O-4), best first, without repeats.

    A CURIE is expanded with the built-in prefixes and `EXTRA_PREFIXES`, not the ontology's own, so a setting means the
    same whatever is loaded. A name that's still not an IRI is left out, with a warning.
    """
    prefixes = BUILTIN_PREFIXES | settings.extra_prefixes
    iris = []
    for name in settings.name_properties:
        try:
            iris.append(IRI(expand_curie(name, prefixes)))
        except RDFSyntaxError:
            logger.warning("The name property %r isn't an IRI or a known CURIE; it's left out", name)
    return list(dict.fromkeys(iris))


def normalize(text: str) -> str:
    """Return a name value as search compares it (§2.6): Unicode NFKD, accents removed, case folded.

    Search's queries are normalized with it too, so a query matches a value whatever its accents and case.
    """
    folded = unicodedata.normalize("NFKD", unicodedata.normalize("NFKD", text).casefold())
    return "".join(char for char in folded if not unicodedata.combining(char))


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
