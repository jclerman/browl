"""Tests for the names pre-caching hook (P-11): prefixes, name values, CURIEs and display names."""

import uuid
from collections.abc import Sequence
from contextlib import closing
from pathlib import Path

import httpx2
import pytest

from browl.fuseki import UNION_FROM
from browl.jobs import JobContext
from browl.jobtypes import ImportResult, Reasoner
from browl.loading import dataset_name, read_upload_prefixes
from browl.names import BUILTIN_PREFIXES
from browl.precache.core import Hook
from browl.precache.entities import entities
from browl.precache.names import names
from browl.precache.stage import HOOKS, pre_caching_stage
from browl.prefixes import read_prefixes
from browl.settings import Settings
from tests.conftest import DATA
from tests.helpers import (
    DC_TITLE,
    DCTERMS_TITLE,
    END,
    ENTITY_END,
    ENTITY_HEADER,
    HEADER,
    LABEL,
    NAME_END,
    NAME_HEADER,
    ONTOLOGY_RESULT,
    OWL,
    connect,
    fuseki,
    job,
    make_settings,
)

pytestmark = pytest.mark.anyio

EX = "http://example.org/"
SKOS = "http://www.w3.org/2004/02/skos/core#"
PREF_LABEL = f"<{SKOS}prefLabel>"
HOOKS_USED: Sequence[Hook] = (entities, names)


def entity_names(settings: Settings, generation: int) -> dict[tuple[str, str], tuple[str, str | None]]:
    """Return each entity's display name and CURIE, by its IRI and type."""
    with closing(connect(settings, generation, HOOKS_USED)) as connection:
        found = connection.execute("SELECT iri, type, display_name, curie FROM entity").fetchall()
    return {(str(iri), str(kind)): (str(name), curie) for iri, kind, name, curie in found}  # type: ignore[misc]


def name_values(settings: Settings, generation: int) -> list[tuple[object, ...]]:
    """Return each `name_value` row with its entity's IRI and type, sorted."""
    with closing(connect(settings, generation, HOOKS_USED)) as connection:
        return sorted(
            connection.execute(
                "SELECT iri, type, property, value, normalized, language, direct"
                " FROM name_value JOIN entity ON entity.id = name_value.entity",
            ),
            key=repr,
        )


def entity_result(*entities: tuple[str, str]) -> str:
    """Return the entity list's result for these (IRI, OWL class name) pairs."""
    return ENTITY_HEADER + "".join(f"<{iri}>\t<{OWL}{kind}>\t\n" for iri, kind in entities) + ENTITY_END


def name_result(*values: tuple[str, str, str, str]) -> str:
    """Return the name values query's result for these (entity IRI, name property, property, literal) rows."""
    return NAME_HEADER + "".join(f"<{e}>\t{n}\t{p}\t{v}\t\n" for e, n, p, v in values) + NAME_END


async def test_display_names(data_dir: Path) -> None:
    """Check the display name ranking: property, language, direct, length, then lexical order; then CURIE, then IRI."""
    settings = make_settings(NAME_PROPERTIES=["rdfs:label", "skos:prefLabel"], LANGUAGE_RANKING=["fr", "en"])
    sub = f"<{EX}sub>"
    names = ["property", "language", "direct", "length", "lexical", "blank", "x"]
    classes = [(f"{EX}{name}", "Class") for name in names]
    values = [
        (f"{EX}property", PREF_LABEL, PREF_LABEL, '"a"@fr'),
        (f"{EX}property", LABEL, LABEL, '"second property"@de'),
        (f"{EX}language", LABEL, LABEL, '"anglais"@en'),
        (f"{EX}language", LABEL, sub, '"français"@fr-CA'),
        (f"{EX}direct", LABEL, sub, '"a"@en'),
        (f"{EX}direct", LABEL, LABEL, '"direct"@en'),
        (f"{EX}length", LABEL, LABEL, '"longer"'),
        (f"{EX}length", LABEL, LABEL, '"short"'),
        (f"{EX}lexical", LABEL, LABEL, '"b"@en'),
        (f"{EX}lexical", LABEL, LABEL, '"a"@en-US'),
        (f"{EX}blank", LABEL, LABEL, '""@fr'),  # blank values are skipped (C1)
        (f"{EX}blank", LABEL, LABEL, '" \\t\\n"@fr'),
        (f"{EX}blank", PREF_LABEL, PREF_LABEL, '"not blank"'),
        ("http://example.org/undeclared", LABEL, LABEL, '"left out"'),
    ]
    queries: list[str] = []
    async with fuseki(ONTOLOGY_RESULT, entity_result(*classes), name_result(*values), queries=queries) as http:
        await pre_caching_stage(job(settings, 1, [ImportResult(f"{EX}i", triples=1)]), http, hooks=HOOKS_USED)
    query = queries[2]
    assert UNION_FROM in query  # the imports' values too
    assert f"VALUES ?name {{ <http://www.w3.org/2000/01/rdf-schema#label> {PREF_LABEL} }}" in query
    assert "?property rdfs:subPropertyOf* ?name" in query
    found = entity_names(settings, 1)
    assert {iri: name for (iri, _), (name, _) in found.items()} == {
        f"{EX}onto": "Onto",  # the ontology's title, from the header
        f"{EX}property": "second property",
        f"{EX}language": "français",
        f"{EX}direct": "direct",
        f"{EX}length": "short",
        f"{EX}lexical": "a",
        f"{EX}blank": "not blank",
        f"{EX}x": f"{EX}x",  # no CURIE: example.org isn't a built-in prefix
    }
    assert (f"{EX}undeclared", "class") not in found


async def test_name_values_deduplicated(data_dir: Path) -> None:
    """Check that a value under a name property directly and through sub-properties is stored once, as direct."""
    settings = make_settings()
    sub, other = f"<{EX}sub>", f"<{EX}other>"
    values = [
        (f"{EX}A", LABEL, sub, '"Café"@en'),
        (f"{EX}A", LABEL, LABEL, '"Café"@en'),
        (f"{EX}A", LABEL, other, '"Café"@en'),
        (f"{EX}A", LABEL, other, '"Café"@fr'),
        (f"{EX}A", LABEL, sub, '"ÅNGSTRÖM"'),
        (f"{EX}A", LABEL, other, '"ÅNGSTRÖM"'),
    ]
    async with fuseki(HEADER + END, entity_result((f"{EX}A", "Class")), name_result(*values)) as http:
        await pre_caching_stage(job(settings, 1), http, hooks=HOOKS_USED)
    label = "http://www.w3.org/2000/01/rdf-schema#label"
    assert name_values(settings, 1) == sorted(
        [
            (f"{EX}A", "class", label, "Café", "cafe", "en", 1),
            (f"{EX}A", "class", label, "Café", "cafe", "fr", 0),
            (f"{EX}A", "class", label, "ÅNGSTRÖM", "angstrom", None, 0),
        ],
        key=repr,
    )


async def test_curies(data_dir: Path) -> None:
    """Check that a CURIE comes from the longest namespace, preferring configured, then declared, then named prefixes.

    `EXTRA_PREFIXES` overrides the ontology's prefixes, which override the built-in ones; an IRI that is a namespace
    itself gets no CURIE.
    """
    settings = make_settings(EXTRA_PREFIXES={"mine": f"{EX}a/", "obo": f"{EX}obo/"})
    iris = [f"{EX}a/b/c", f"{EX}a/x", f"{EX}a/", f"{EX}d#e", f"{EX}obo/f", "http://purl.obolibrary.org/obo/g", "urn:x"]
    iris.append(f"{SKOS}x")
    context = job(settings, 1)
    context.upload.write_text(  # type: ignore[union-attr]
        f"@prefix ab: <{EX}a/b/> .\n@prefix : <{EX}d#> .\n@prefix d: <{EX}d#> .\n@prefix mine: <{EX}ignored/> .\n"
        "@prefix owl: <http://example.org/not-owl#> .\n"
        f"@prefix zz: <{SKOS}> .\n"  # declared, for a built-in prefix's namespace
    )
    context.prefixes = read_upload_prefixes(settings.data_dir, 1)  # as the loading stage does
    async with fuseki(HEADER + END, entity_result(*((iri, "Class") for iri in iris)), name_result()) as http:
        await pre_caching_stage(context, http, hooks=HOOKS_USED)
    assert context.prefixes == {
        "ab": f"{EX}a/b/",
        "": f"{EX}d#",
        "d": f"{EX}d#",
        "mine": f"{EX}ignored/",
        "owl": "http://example.org/not-owl#",
        "zz": SKOS,
    }
    found = entity_names(settings, 1)
    assert {iri: curie for (iri, _), (_, curie) in found.items()} == {
        f"{EX}a/b/c": "ab:c",
        f"{EX}a/x": "mine:x",
        f"{EX}a/": None,
        f"{EX}d#e": "d:e",
        f"{EX}obo/f": "obo:f",
        "http://purl.obolibrary.org/obo/g": None,  # the built-in obo prefix is overridden
        "urn:x": None,
        f"{SKOS}x": "zz:x",  # the declared prefix, not the built-in skos, though it's later alphabetically
    }
    assert found[(f"{EX}d#e", "class")] == ("d:e", "d:e")  # the CURIE is the display name without a name value
    with closing(connect(settings, 1, HOOKS_USED)) as connection:
        stored = dict(connection.execute("SELECT prefix, namespace FROM prefix"))  # type: ignore[arg-type]
    assert stored == BUILTIN_PREFIXES | context.prefixes | settings.extra_prefixes
    assert stored["owl"] == "http://example.org/not-owl#"


async def test_no_valid_name_property(data_dir: Path) -> None:
    """Check that without a name property that's an IRI, no name values are queried, and names fall back."""
    settings = make_settings(NAME_PROPERTIES=["not an IRI"])
    queries: list[str] = []
    async with fuseki(HEADER + END, entity_result((f"{EX}A", "Class")), queries=queries) as http:
        await pre_caching_stage(job(settings, 1), http, hooks=HOOKS_USED)
    assert len(queries) == 2
    assert entity_names(settings, 1) == {(f"{EX}A", "class"): (f"{EX}A", None)}


@pytest.mark.fuseki
async def test_names_on_real_fuseki(fuseki_url: str, data_dir: Path) -> None:
    """Check the names hook on a real Fuseki: sub-property chains and cycles, languages, punning, and the imports.

    Of two name properties, one a sub-property of the other, each stores its own values; a value that isn't a literal
    isn't a name.
    """
    settings = make_settings(NAME_PROPERTIES=["rdfs:label", "skos:prefLabel"])
    generation = 900_000 + uuid.uuid4().int % 100_000
    name = dataset_name(generation)
    ns = "http://example.org/names#"
    imported = f'<{EX}imported#C> a <{OWL}Class> ; <http://www.w3.org/2000/01/rdf-schema#label> "imported"@en .\n'
    async with httpx2.AsyncClient(base_url=fuseki_url, timeout=60) as http:
        (await http.post("/$/datasets", data={"dbName": name, "dbType": "mem"})).raise_for_status()
        try:
            for graph, content in [(None, (DATA / "names.ttl").read_bytes()), (f"{EX}imported", imported.encode())]:
                response = await http.post(
                    f"/{name}/data",
                    params={} if graph is None else {"graph": graph},
                    content=content,
                    headers={"Content-Type": "text/turtle"},
                )
                response.raise_for_status()
            context = JobContext(
                generation,
                settings,
                Reasoner.NONE,
                upload=DATA / "names.ttl",
                prefixes=read_prefixes(DATA / "names.ttl"),
                imports=[ImportResult(f"{EX}imported", triples=2)],
            )
            await pre_caching_stage(context, http, hooks=HOOKS)
        finally:
            await http.delete(f"/$/datasets/{name}")

    label = "http://www.w3.org/2000/01/rdf-schema#label"
    pref = f"{SKOS}prefLabel"
    with closing(connect(settings, generation, HOOKS)) as connection:
        stored = sorted(
            connection.execute(
                "SELECT iri, type, property, value, normalized, language, direct"
                " FROM name_value JOIN entity ON entity.id = name_value.entity",
            ),
            key=repr,
        )
        found = {
            (iri, kind): (display, curie)
            for iri, kind, display, curie in connection.execute("SELECT iri, type, display_name, curie FROM entity")
        }
    assert stored == sorted(
        [
            ("http://example.org/names", "ontology", label, "Names", "names", None, 1),
            (f"{ns}Heart", "class", label, "Heart", "heart", "en", 1),
            (f"{ns}Heart", "class", label, "Herz", "herz", "de", 1),
            (f"{ns}Heart", "class", label, "cardiac organ", "cardiac organ", "en", 0),  # through a chain
            (f"{ns}Bee", "class", label, "Bee", "bee", "en-gb", 0),
            (f"{ns}Cafe", "class", label, "Café", "cafe", None, 1),
            (f"{ns}Dumpling", "class", pref, "餃子", "餃子", "zh-hant-tw", 0),  # through a cycle
            (f"{ns}Dumpling", "class", label, "餃子", "餃子", "zh-hant-tw", 0),  # prefLabel ⊑ label, both configured
            (f"{ns}Pun", "class", pref, "pun", "pun", "en", 1),
            (f"{ns}Pun", "class", label, "pun", "pun", "en", 0),
            (f"{ns}Pun", "object_property", pref, "pun", "pun", "en", 1),
            (f"{ns}Pun", "object_property", label, "pun", "pun", "en", 0),
            (f"{EX}imported#C", "class", label, "imported", "imported", "en", 1),
        ],
        key=repr,
    )
    assert found == {
        ("http://example.org/names", "ontology"): ("Names", None),
        (f"{ns}Heart", "class"): ("Heart", "ex:Heart"),  # a named prefix, not the default one
        (f"{ns}Bee", "class"): ("Bee", "ex:Bee"),
        (f"{ns}Cafe", "class"): ("Café", "ex:Cafe"),
        (f"{ns}Dumpling", "class"): ("餃子", "ex:Dumpling"),
        (f"{ns}Nameless", "class"): ("ex:Nameless", "ex:Nameless"),  # its rdfs:label isn't a literal
        (f"{ns}Pun", "class"): ("pun", "ex:Pun"),
        (f"{ns}Pun", "object_property"): ("pun", "ex:Pun"),
        (f"{ns}label2", "annotation_property"): ("ex:label2", "ex:label2"),
        (f"{ns}label3", "annotation_property"): ("ex:label3", "ex:label3"),
        (f"{ns}alias", "annotation_property"): ("ex:alias", "ex:alias"),
        (f"{EX}imported#C", "class"): ("imported", None),
    }


async def test_blank_title_skipped(data_dir: Path) -> None:
    """Check that a blank title isn't the ontology's display name: the next title property's is (C1)."""
    settings = make_settings()
    header = f'{HEADER}<{EX}onto>\t{DCTERMS_TITLE}\t" "@en\t\n<{EX}onto>\t{DC_TITLE}\t"Onto"\t\n{END}'
    async with fuseki(header, entity_result(), name_result()) as http:
        await pre_caching_stage(job(settings, 1), http, hooks=HOOKS_USED)
    assert entity_names(settings, 1)[(f"{EX}onto", "ontology")] == ("Onto", None)
