"""Tests for the annotation values hook (P-42): stored values, the properties' kinds, and their sub-properties."""

import uuid
from collections import Counter
from collections.abc import AsyncIterator, Sequence
from contextlib import closing
from pathlib import Path

import httpx2
import pytest

from browl.jobs import JobContext
from browl.jobtypes import Reasoner
from browl.loading import dataset_name
from browl.precache.core import Hook
from browl.precache.entities import entities
from browl.precache.search import search as search_hook
from browl.precache.stage import pre_caching_stage
from browl.precache.values import (
    IAO,
    LABEL_PROPERTIES,
    OBO_IN_OWL,
    PROSE_WORDS,
    Stats,
    annotation_values,
    classify,
)
from browl.prefixes import read_prefixes
from browl.rdf import RDFS
from browl.search import SearchQuery, search, search_properties
from browl.settings import Settings
from tests.conftest import DATA
from tests.helpers import connect, make_settings

pytestmark = pytest.mark.anyio

EX = "http://example.org/values#"
HOOKS_USED: Sequence[Hook] = (entities, annotation_values, search_hook)
LABEL = f"{RDFS}label"
COMMENT = f"{RDFS}comment"
SYNONYM = f"{OBO_IN_OWL}hasExactSynonym"
XREF = f"{OBO_IN_OWL}hasDbXref"
DEFINITION = f"{IAO}0000115"


def stats(*words: int, values: int = 1, unique: int = 1, uses: int | None = None) -> Stats:
    """Return a property's statistics: its values' word counts, how many values and distinct ones it has, and uses.

    Its uses, (entity, value) pairs, are its values unless given.
    """
    return Stats(Counter(words), values, unique, values if uses is None else uses)


@pytest.mark.parametrize(
    ("iri", "found", "supers", "kind"),
    [
        (DEFINITION, stats(1), {}, (True, False)),  # in the prose list, however short
        (COMMENT, stats(1), {}, (True, False)),
        (SYNONYM, stats(20, values=10), {}, (False, True)),  # in the label list, however long or repeated
        (f"{EX}sub", stats(20), {f"{EX}sub": {f"{EX}mid", LABEL}}, (False, True)),  # under a label property
        (f"{EX}short", stats(PROSE_WORDS - 1, PROSE_WORDS - 1, 30), {}, (False, True)),
        (f"{EX}long", stats(1, PROSE_WORDS, PROSE_WORDS), {}, (True, False)),
        (f"{EX}even", stats(PROSE_WORDS - 1, PROSE_WORDS + 1), {}, (True, False)),  # median 8
        (f"{EX}id", stats(1, values=1000, unique=1000), {}, (False, True)),  # each value on one entity
        (f"{EX}xref", stats(1, values=1999, unique=1000), {}, (False, True)),  # on just under 2, on average
        (f"{EX}source", stats(1, values=2000, unique=1000), {}, (False, False)),  # on 2
        (f"{EX}languages", stats(1, values=2000, unique=1000, uses=1000), {}, (False, True)),  # each in 2 languages
        (f"{EX}namespace", stats(1, values=3_000_000, unique=1), {}, (False, False)),
    ],
    ids=[
        "prose list",
        "comment",
        "label list",
        "sub-property",
        "median short",
        "median long",
        "median of two",
        "unique",
        "nearly unique",
        "repeated",
        "two languages",
        "one value",
    ],
)
def test_classify(iri: str, found: Stats, supers: dict[str, set[str]], kind: tuple[bool, bool]) -> None:
    """Check a property's kind, (prose, searched by default): the lists, the hierarchy, words, then reuse."""
    assert classify([LABEL], {iri: found}, supers)[iri] == kind


def test_classify_name_property_first() -> None:
    """Check that a name property is name-like and searched by default, even if it's in the prose list."""
    assert classify([DEFINITION], {DEFINITION: stats(30, values=5)}, {})[DEFINITION] == (False, True)


def test_classify_defaults_without_values() -> None:
    """Check that the name, label and synonym properties are classified even without values.

    So a default search reaches their sub-properties.
    """
    kinds = classify([f"{EX}name"], {}, {})
    assert set(kinds) == {f"{EX}name", *LABEL_PROPERTIES}
    assert set(kinds.values()) == {(False, True)}


def test_classify_ancestors() -> None:
    """Check a super-property of ones with values: name-like unless all are prose, a default if all are defaults."""
    supers = {
        f"{EX}a": {f"{EX}names", f"{EX}any", f"{EX}mixed"},
        f"{EX}b": {f"{EX}notes", f"{EX}any"},
        f"{EX}c": {f"{EX}mixed"},
    }
    found = {f"{EX}a": stats(1), f"{EX}b": stats(30), f"{EX}c": stats(1, values=10, unique=1)}
    kinds = classify([LABEL], found, supers)
    assert {iri: kinds[iri] for iri in (f"{EX}names", f"{EX}notes", f"{EX}any", f"{EX}mixed")} == {
        f"{EX}names": (False, True),  # only a, a default
        f"{EX}notes": (True, False),
        f"{EX}any": (False, False),  # b is prose
        f"{EX}mixed": (False, False),  # c repeats its value
    }


@pytest.fixture
async def built(fuseki_url: str, data_dir: Path) -> AsyncIterator[tuple[Settings, int]]:
    """Pre-cache `values.ttl` on a real Fuseki, `rdfs:label` the name property; yield the settings and generation."""
    settings = make_settings()
    generation = 900_000 + uuid.uuid4().int % 100_000
    name = dataset_name(generation)
    async with httpx2.AsyncClient(base_url=fuseki_url, timeout=60) as http:
        (await http.post("/$/datasets", data={"dbName": name, "dbType": "mem"})).raise_for_status()
        try:
            content = (DATA / "values.ttl").read_bytes()
            response = await http.post(f"/{name}/data", content=content, headers={"Content-Type": "text/turtle"})
            response.raise_for_status()
            context = JobContext(
                generation,
                settings,
                Reasoner.NONE,
                upload=DATA / "values.ttl",
                prefixes=read_prefixes(DATA / "values.ttl"),
            )
            await pre_caching_stage(context, http, hooks=HOOKS_USED)
        finally:
            await http.delete(f"/$/datasets/{name}")
    yield settings, generation


async def test_values_on_real_fuseki(built: tuple[Settings, int]) -> None:
    """Check the stored values: strings and language-tagged strings on declared entities, under the property asserted.

    Dates, booleans, URIs and IRIs aren't stored, nor values on undeclared IRIs; the built-in `rdfs:label` and
    `rdfs:comment` are read though undeclared, each value once.
    """
    settings, generation = built
    with closing(connect(settings, generation, HOOKS_USED)) as connection:
        stored = connection.execute(
            "SELECT entity.iri, p.iri, value, language FROM annotation_value AS v"
            " JOIN entity ON entity.id = v.entity JOIN search_property AS p ON p.id = v.property"
        ).fetchall()
    heart = f"{EX}Heart"
    assert sorted(stored) == sorted(
        [
            (
                "http://example.org/values",
                COMMENT,
                "An ontology for testing which annotation values are stored and how.",
                None,
            ),
            (heart, LABEL, "heart", "en"),
            (heart, LABEL, "cœur", "fr"),
            (heart, SYNONYM, "cardium", None),
            (heart, XREF, "FMA:7088", None),
            (heart, DEFINITION, "A hollow organ.", None),
            (heart, f"{EX}note", "This note has more than eight words, so it is prose.", None),
            (heart, f"{EX}code", "AB-12", None),
            (heart, f"{EX}myLabel", "ticker", None),
            (heart, f"{EX}deepLabel", "pump", None),
            (heart, f"{EX}loopA", "loop", None),
            (heart, f"{EX}typed", "typed", None),
            (heart, f"{EX}acronym", "HT", None),
            (heart, f"{EX}source", "Atlas", None),
            (f"{EX}Lung", LABEL, "lung", "en"),
            (f"{EX}Lung", f"{EX}source", "Atlas", None),
            (f"{EX}Liver", LABEL, "liver", "en"),
            (f"{EX}Liver", f"{EX}source", "Atlas", None),
        ]
    )


async def test_properties_on_real_fuseki(built: tuple[Settings, int]) -> None:
    """Check each property's kind and value count, and the sub-properties, transitively, among those stored."""
    settings, generation = built
    with closing(connect(settings, generation, HOOKS_USED)) as connection:
        properties = {
            iri: (bool(prose), bool(by_default), count)
            for iri, prose, by_default, count in connection.execute(
                "SELECT iri, prose, by_default, value_count FROM search_property"
            )
        }
        subs = connection.execute(
            "SELECT p.iri, s.iri FROM sub_property JOIN search_property AS p ON p.id = sub_property.property"
            " JOIN search_property AS s ON s.id = sub_property.sub"
        ).fetchall()
    assert properties == {
        LABEL: (False, True, 4),
        COMMENT: (True, False, 1),
        SYNONYM: (False, True, 1),
        XREF: (False, True, 1),  # each value on one entity
        DEFINITION: (True, False, 1),
        f"{EX}note": (True, False, 1),
        f"{EX}code": (False, True, 1),
        f"{EX}myLabel": (False, True, 1),
        f"{EX}deepLabel": (False, True, 1),
        f"{EX}loopA": (False, True, 1),
        f"{EX}typed": (False, True, 1),
        f"{EX}acronym": (False, True, 1),
        f"{EX}source": (False, False, 3),  # one value on 3 entities
        f"{EX}mappingTerm": (False, True, 0),  # no values, but a sub-property with some, a default
        f"{EX}loopB": (False, True, 0),  # likewise, in a cycle with it
    } | {iri: (False, True, 0) for iri in LABEL_PROPERTIES if iri not in (LABEL, SYNONYM)}
    assert sorted(subs) == sorted(
        [
            (LABEL, f"{EX}myLabel"),
            (LABEL, f"{EX}deepLabel"),
            (f"{EX}myLabel", f"{EX}deepLabel"),
            (f"{EX}mappingTerm", f"{EX}acronym"),
            (f"{EX}loopA", f"{EX}loopB"),
            (f"{EX}loopB", f"{EX}loopA"),
        ]
    )


@pytest.mark.parametrize(
    ("text", "filters", "found"),
    [
        ("hear", {}, [LABEL]),
        ("cardi", {}, [SYNONYM]),
        ("tick", {}, [f"{EX}myLabel"]),  # entailed through a sub-property
        ("pum", {}, [f"{EX}deepLabel"]),  # and through its sub-property
        ("pum", {"properties": frozenset({LABEL}), "entailed": False}, []),
        ("pum", {"properties": frozenset({f"{EX}myLabel"})}, [f"{EX}deepLabel"]),
        ("fma", {}, [XREF]),  # an ID, searched by default
        ("atlas", {}, []),  # repeated, so not searched by default
        ("atlas", {"properties": frozenset({f"{EX}source"})}, [f"{EX}source"] * 3),
        ("ht", {"properties": frozenset({f"{EX}mappingTerm"})}, [f"{EX}acronym"]),  # through a super-property
        ("hollow", {}, []),  # prose isn't in this index
        ("hollow", {"properties": frozenset({DEFINITION})}, []),
        ("nobody", {}, []),
    ],
)
async def test_search_on_real_fuseki(
    built: tuple[Settings, int], text: str, filters: dict[str, object], found: list[str]
) -> None:
    """Check what search finds of the stored values: by default the name and synonym properties, with entailment."""
    settings, generation = built
    with closing(connect(settings, generation, HOOKS_USED)) as connection:
        results = search(connection, SearchQuery(text, **filters))  # type: ignore[arg-type]
    assert [hit.property for hit in results.hits] == found


async def test_walk_order(built: tuple[Settings, int]) -> None:
    """Check the walk (rule 3): the query's order first, in it, then the defaults, then the others, by label.

    Without the names hook, the properties have no display names or CURIEs, so their labels are their IRIs.
    """
    settings, generation = built
    with closing(connect(settings, generation, HOOKS_USED)) as connection:
        walked = search_properties(connection, [SYNONYM, LABEL, SYNONYM, f"{EX}none"])
        best = [search(connection, SearchQuery("c", order=order)).hits[0].value for order in ((SYNONYM,), (LABEL,))]
    rest = [found for found in walked if found.iri not in (SYNONYM, LABEL)]

    def by_label(group: list[str]) -> list[str]:
        return sorted(group, key=lambda iri: (iri.casefold(), iri))

    defaults = by_label([found.iri for found in rest if found.by_default])
    others = by_label([found.iri for found in rest if not found.by_default])
    assert [found.iri for found in walked] == [SYNONYM, LABEL, *defaults, *others]
    assert defaults and others
    assert best == ["cardium", "cœur"]  # the heart's best value is by the order given
