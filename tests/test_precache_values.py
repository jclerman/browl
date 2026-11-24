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


def counts(*words: int) -> Counter[int]:
    """Return a histogram of values' word counts."""
    return Counter(words)


@pytest.mark.parametrize(
    ("iri", "words", "supers", "kind"),
    [
        (DEFINITION, counts(1), {}, (True, False)),  # in the prose list, however short
        (COMMENT, counts(1), {}, (True, False)),
        (SYNONYM, counts(20), {}, (False, True)),  # in the label list, however long
        (f"{EX}sub", counts(20), {f"{EX}sub": {f"{EX}mid", LABEL}}, (False, False)),  # under a label property
        (f"{EX}short", counts(PROSE_WORDS - 1, PROSE_WORDS - 1, 30), {}, (False, False)),
        (f"{EX}long", counts(1, PROSE_WORDS, PROSE_WORDS), {}, (True, False)),
        (f"{EX}even", counts(PROSE_WORDS - 1, PROSE_WORDS + 1), {}, (True, False)),  # median 8
    ],
    ids=["prose list", "comment", "label list", "sub-property", "median short", "median long", "median of two"],
)
def test_classify(iri: str, words: Counter[int], supers: dict[str, set[str]], kind: tuple[bool, bool]) -> None:
    """Check a property's kind, (prose, searched by default), by the lists, the hierarchy, then the median rule."""
    assert classify([LABEL], {iri: words}, supers)[iri] == kind


def test_classify_name_property_first() -> None:
    """Check that a name property is name-like and searched by default, even if it's in the prose list."""
    assert classify([DEFINITION], {DEFINITION: counts(30)}, {})[DEFINITION] == (False, True)


def test_classify_defaults_without_values() -> None:
    """Check that the name, label and synonym properties are classified even without values.

    So a default search reaches their sub-properties.
    """
    kinds = classify([f"{EX}name"], {}, {})
    assert set(kinds) == {f"{EX}name", *LABEL_PROPERTIES}
    assert set(kinds.values()) == {(False, True)}


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
        LABEL: (False, True, 2),
        COMMENT: (True, False, 1),
        SYNONYM: (False, True, 1),
        XREF: (False, False, 1),
        DEFINITION: (True, False, 1),
        f"{EX}note": (True, False, 1),
        f"{EX}code": (False, False, 1),
        f"{EX}myLabel": (False, False, 1),
        f"{EX}deepLabel": (False, False, 1),
        f"{EX}loopA": (False, False, 1),
        f"{EX}typed": (False, False, 1),
    } | {iri: (False, True, 0) for iri in LABEL_PROPERTIES if iri not in (LABEL, SYNONYM)}
    assert sorted(subs) == sorted(
        [(LABEL, f"{EX}myLabel"), (LABEL, f"{EX}deepLabel"), (f"{EX}myLabel", f"{EX}deepLabel")]
    )


@pytest.mark.parametrize(
    ("text", "filters", "found"),
    [
        ("hear", {}, [LABEL]),
        ("cardi", {}, [SYNONYM]),
        ("tick", {}, [f"{EX}myLabel"]),  # entailed through a sub-property
        ("pum", {}, [f"{EX}deepLabel"]),  # and through its sub-property
        ("pum", {"entailed": False}, []),
        ("pum", {"properties": frozenset({f"{EX}myLabel"})}, [f"{EX}deepLabel"]),
        ("fma", {}, []),  # not searched by default
        ("fma", {"properties": frozenset({XREF})}, [XREF]),
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
    """Check that the properties in a query's order come first, in it, then the others by name, else IRI (rule 3)."""
    settings, generation = built
    with closing(connect(settings, generation, HOOKS_USED)) as connection:
        walked = [found.iri for found in search_properties(connection, [SYNONYM, LABEL, SYNONYM, f"{EX}none"])]
        best = [search(connection, SearchQuery("c", order=order)).hits[0].value for order in ((SYNONYM,), (LABEL,))]
    others = sorted(set(walked) - {SYNONYM, LABEL}, key=lambda iri: (iri.casefold(), iri))
    assert walked == [SYNONYM, LABEL, *others]
    assert best == ["cardium", "cœur"]  # the heart's best value is by the order given
