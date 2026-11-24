"""Name values (DESIGN.md §2.6, §2.7): the name properties, language ranking, normalization and CURIE expansion."""

import logging
import sqlite3
import unicodedata
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager

from browl.rdf import IRI, OWL, RDF, RDFS, XSD, Literal, RDFSyntaxError
from browl.settings import UNTAGGED, Settings

logger = logging.getLogger(__name__)

BUILTIN_PREFIXES = {
    "rdf": RDF,
    "rdfs": RDFS,
    "owl": OWL,
    "xsd": XSD,
    "skos": "http://www.w3.org/2004/02/skos/core#",
    "obo": "http://purl.obolibrary.org/obo/",
    "dc": "http://purl.org/dc/elements/1.1/",
    "dcterms": "http://purl.org/dc/terms/",
}
"""The built-in CURIE prefixes (§2.7), which `EXTRA_PREFIXES` overrides."""

BLANK = " \t\n\r"
"""The characters of a blank name value or title, which is never a display name: its links would have no text."""

LAST_CHAR = "\U0010ffff"
"""Sorts after any other character, as UTF-8 bytes too: strings that start with `p` sort before `p + LAST_CHAR`."""


def expand_curie(name: str, prefixes: Mapping[str, str]) -> str:
    """Return the IRI a CURIE stands for, or the name itself if its prefix isn't known (it's taken to be an IRI)."""
    prefix, colon, local = name.partition(":")
    if colon and prefix in prefixes and not local.startswith("//"):
        return prefixes[prefix] + local
    return name


def curie_prefixes(declared: Mapping[str, str], configured: Mapping[str, str]) -> list[tuple[str, str]]:
    """Return the CURIE prefixes (§2.7), as (name, namespace), in the order an IRI that several cover takes them.

    They're the built-in ones, overridden by those the ontology file declares, overridden by `EXTRA_PREFIXES`. The
    longest namespace comes first; of several prefixes for one namespace, the one of the best source (configured, then
    declared, then built in), then a named one rather than the default (empty) prefix, then by name.

    Args:
        declared: The prefixes the ontology file declares.
        configured: `EXTRA_PREFIXES`.
    """
    prefixes = {**BUILTIN_PREFIXES, **declared, **configured}
    source_rank = {name: 0 if name in configured else 1 if name in declared else 2 for name in prefixes}

    def preference(item: tuple[str, str]) -> tuple[int, int, bool, str]:
        name, namespace = item
        return -len(namespace), source_rank[name], name == "", name

    return sorted(prefixes.items(), key=preference)


def curie(iri: str, prefixes: Iterable[tuple[str, str]]) -> str | None:
    """Return an IRI's CURIE, by the first prefix whose namespace is a proper prefix of it; None if none is.

    Examples:
        >>> prefixes = curie_prefixes({"ex": "http://example.org/", "a": "http://example.org/a/"}, {})
        >>> curie("http://example.org/a/b", prefixes), curie("http://example.org/", prefixes)
        ('a:b', None)
    """
    for name, namespace in prefixes:
        if iri.startswith(namespace) and len(iri) > len(namespace):
            return f"{name}:{iri[len(namespace) :]}"
    return None


def name_properties(settings: Settings) -> dict[str, str]:
    """Return the name properties (`NAME_PROPERTIES`, O-4), best first, by IRI, each with the first name given for it.

    A CURIE is expanded with the built-in prefixes and `EXTRA_PREFIXES`, not the ontology's own, so a setting means the
    same whatever is loaded. A name that's still not an IRI is left out, with a warning.
    """
    prefixes = BUILTIN_PREFIXES | settings.extra_prefixes
    names: dict[str, str] = {}
    for name in settings.name_properties:
        try:
            names.setdefault(IRI(expand_curie(name, prefixes)), name)
        except RDFSyntaxError:
            logger.warning("The name property %r isn't an IRI or a known CURIE; it's left out", name)
    return names


def name_property_iris(settings: Settings) -> list[str]:
    """Return the IRIs of the name properties, best first, without repeats (`name_properties`)."""
    return list(name_properties(settings))


def normalize(text: str) -> str:
    """Return a name value as search compares it (§2.6): NFKD, without accents or leading whitespace, case folded.

    Search's queries are normalized with it too, so a query matches a value whatever its accents and case.
    """
    # Unicode's compatibility caseless matching (D146) normalizes again after folding, as folding can leave a string
    # unnormalized (U+0345, a combining mark, folds to a letter that may need reordering against the marks around it).
    folded = unicodedata.normalize("NFKD", unicodedata.normalize("NFKD", text).casefold())
    return "".join(char for char in folded if not unicodedata.combining(char)).lstrip()


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


@contextmanager
def rank_tables(connection: sqlite3.Connection, properties: Sequence[str], ranking: Sequence[str]) -> Iterator[None]:
    """Create temporary tables of name values' ranks for the duration, for display names.

    `property_rank (property, rank)` gives each name property's position, and `language_rank` each language tag in
    `name_value` its rank (`language_ranks`).

    Args:
        connection: A connection to the derived database, with `name_value` filled.
        properties: The name properties' IRIs, best first (`name_property_iris`).
        ranking: The language ranking, lowercased (`Settings.language_ranking`).
    """
    tags = [row[0] for row in connection.execute("SELECT DISTINCT language FROM name_value")]
    connection.execute("CREATE TEMP TABLE property_rank (property TEXT PRIMARY KEY, rank INTEGER NOT NULL)")
    connection.executemany("INSERT INTO property_rank VALUES (?, ?)", [(iri, n) for n, iri in enumerate(properties)])
    try:
        with language_ranks(connection, tags, ranking):
            yield
    finally:
        connection.execute("DROP TABLE property_rank")


@contextmanager
def language_ranks(
    connection: sqlite3.Connection, tags: Iterable[str | None], ranking: Sequence[str]
) -> Iterator[None]:
    """Create a temporary table of language tags' ranks for the duration, for display names and search.

    `language_rank (language, rank)` gives each tag its rank by `language_rank`, as dense integers from 0: tags that
    rank alike share one.

    Args:
        connection: A connection to the derived database.
        tags: The language tags to rank; None for untagged values.
        ranking: The language ranking, lowercased (`Settings.language_ranking`).
    """
    ranks = {tag: language_rank(tag, ranking) for tag in tags}
    dense = {rank: n for n, rank in enumerate(sorted(set(ranks.values())))}
    connection.execute("CREATE TEMP TABLE language_rank (language TEXT, rank INTEGER NOT NULL)")
    connection.executemany(
        "INSERT INTO language_rank VALUES (?, ?)", [(tag, dense[rank]) for tag, rank in ranks.items()]
    )
    try:
        yield
    finally:
        connection.execute("DROP TABLE language_rank")
