"""Tests for entities' usages and their counts (P-27a): the blank-node owner map, against the OWLAPI's usages."""

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import closing
from pathlib import Path
from typing import Any

import httpx2
import jpype
import pytest

from browl import axioms, owlapi
from browl.derived import DerivedReader, NotCurrentError, UsageRootKind
from browl.fuseki import FusekiClient, SparqlError
from browl.jobs import JobContext
from browl.jobtypes import Reasoner
from browl.loading import dataset_name
from browl.precache.core import PREFIXES
from browl.precache.keys import build_keys
from browl.precache.stage import HOOKS, pre_cache
from browl.rdf import IRI, RDFS, BlankNode, Literal, Term, ntriples_iris
from browl.settings import Settings
from browl.usages import CACHED_COUNT_S, Usage, UsageCounts, UsageSource, UsageStore, count_usages, read_usages
from tests.helpers import connect, make_settings

pytestmark = [pytest.mark.anyio, pytest.mark.fuseki, pytest.mark.jvm]

ONTOLOGY = Path(__file__).parent / "data" / "axioms.ofn"
NS = "http://example.org/axioms#"
DECLARATION_TYPES = (
    "owl:Class owl:ObjectProperty owl:DatatypeProperty owl:AnnotationProperty owl:NamedIndividual rdfs:Datatype"
    " <http://www.w3.org/2003/11/swrl#Variable>"
)
"""The types a usage's fragment needs of the IRIs it mentions: declarations only, or it would gain the other entities'
axioms, such as their class assertions and property characteristics (S-7)."""
ANNOTATION_DOMAIN_RANGE = {"AnnotationPropertyDomain": "getDomain", "AnnotationPropertyRangeOf": "getRange"}
"""The OWLAPI's names of annotation properties' domain and range axioms (one misspelt), and their IRIs' getters."""


@pytest.fixture(scope="module")
def anyio_backend() -> str:
    """Run the module's async tests and fixtures on asyncio, as the module-scoped fixtures need one backend."""
    return "asyncio"


def ontology_ntriples() -> bytes:
    """Return the test ontology as N-Triples, as the OWLAPI writes it: the RDF that browl's store would hold."""
    manager = jpype.JClass("org.semanticweb.owlapi.apibinding.OWLManager").createOWLOntologyManager()
    ontology = manager.loadOntologyFromOntologyDocument(jpype.JClass("java.io.File")(str(ONTOLOGY)))
    out = jpype.JClass("java.io.ByteArrayOutputStream")()
    manager.saveOntology(ontology, jpype.JClass("org.semanticweb.owlapi.formats.NTriplesDocumentFormat")(), out)
    return bytes(out.toByteArray())


@pytest.fixture(scope="module")
def ntriples(jvm: None) -> bytes:
    """Return the test ontology as N-Triples."""
    return asyncio.run(owlapi.run_java(ontology_ntriples))


@pytest.fixture(scope="module")
def settings(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Settings]:
    """Yield settings with a data directory of the module's own, as `DATA_DIR` too."""
    data_dir = tmp_path_factory.mktemp("data")
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("DATA_DIR", str(data_dir))
        yield make_settings()


@pytest.fixture(scope="module")
async def store(fuseki_url: str, ntriples: bytes, settings: Settings) -> AsyncIterator[UsageStore]:
    """Yield a store of the test ontology: an in-memory Fuseki dataset, and the derived database built from it."""
    generation = 900_000 + uuid.uuid4().int % 100_000
    name = dataset_name(generation)
    async with httpx2.AsyncClient(base_url=fuseki_url, timeout=60) as http:
        (await http.post("/$/datasets", data={"dbName": name, "dbType": "mem"})).raise_for_status()
        try:
            response = await http.post(
                f"/{name}/data", content=ntriples, headers={"Content-Type": "application/n-triples"}
            )
            response.raise_for_status()
            await pre_cache(JobContext(generation, settings, Reasoner.NONE), http)
            reader = DerivedReader(settings.data_dir, build_keys(settings, HOOKS))
            try:
                yield UsageStore(FusekiClient(http), reader, generation)
            finally:
                reader.close()
        finally:
            await http.delete(f"/$/datasets/{name}")


def normal(axiom: Any) -> str:
    """Return an axiom's functional syntax without its annotations."""
    return str(axiom.getAxiomWithoutAnnotations())


def parsed(ntriples: bytes) -> set[str]:
    """Return the axioms the OWLAPI parses from N-Triples, but declarations, without annotations."""
    with owlapi.ntriples_ontology(ntriples) as ontology:
        return {normal(each) for each in ontology.getAxioms() if str(each.getAxiomType().getName()) != "Declaration"}


def owlapi_usages(ntriples: bytes, described: dict[str, bytes], subjects: bytes) -> dict[str, set[str]]:
    """Return each entity's usages as the OWLAPI finds them, by IRI.

    That's its referencing axioms, and the annotation assertions, annotated axioms, and annotation properties' domains
    and ranges with it as value, which the OWLAPI holds as IRIs, not entities; less its own:
    those stated with it as subject (`described`, each IRI's `DESCRIBE`), and the axioms its Overview lists
    (`browl.axioms.entity_closure`) that have no single subject: those `subjects`, every IRI subject's `DESCRIBE`,
    doesn't give.
    """
    excluded = jpype.JClass("org.semanticweb.owlapi.model.parameters.Imports").EXCLUDED
    create = jpype.JClass("org.semanticweb.owlapi.model.IRI").create
    with owlapi.ntriples_ontology(ntriples) as ontology:
        every = {normal(each) for each in ontology.getAxioms() if str(each.getAxiomType().getName()) != "Declaration"}
        unowned = every - parsed(subjects)
        by_value: dict[str, set[str]] = {}

        def values(annotations: Any) -> Iterator[str]:
            for annotation in annotations:
                if annotation.getValue().isIRI():
                    yield str(annotation.getValue())
                yield from values(annotation.getAnnotations())

        for axiom in ontology.getAxioms():
            kind = str(axiom.getAxiomType().getName())
            if kind == "AnnotationAssertion" and axiom.getValue().isIRI():
                by_value.setdefault(str(axiom.getValue()), set()).add(normal(axiom))
            if kind in ANNOTATION_DOMAIN_RANGE:  # an IRI to the OWLAPI, not an entity
                by_value.setdefault(str(getattr(axiom, ANNOTATION_DOMAIN_RANGE[kind])()), set()).add(normal(axiom))
            for iri in values(axiom.getAnnotations()):
                by_value.setdefault(iri, set()).add(normal(axiom))
        found = {}
        for iri, own in described.items():
            referencing = set(by_value.get(iri, ()))
            for entity in ontology.getEntitiesInSignature(create(iri)):
                referencing.update(
                    normal(axiom)
                    for axiom in ontology.getReferencingAxioms(entity, excluded)
                    if str(axiom.getAxiomType().getName()) != "Declaration"
                )
            overview = {normal(axiom) for axiom in axioms.entity_closure(ontology, iri)}
            found[iri] = referencing - parsed(own) - (overview & unowned)
        return found


def ntriples_term(term: Term) -> str:
    """Return an IRI or a literal in N-Triples syntax."""
    if isinstance(term, IRI):
        return f"<{term}>"
    assert isinstance(term, Literal)
    text = json.dumps(term.value, ensure_ascii=False)  # escapes '"', '\\' and line breaks as N-Triples does
    return text + (f"@{term.language}" if term.language else f"^^<{term.datatype}>")


async def usage_triples(store: UsageStore, iri: str, usage: Usage) -> bytes:
    """Return the triples of a usage's axiom, with the declaration types of the IRIs they mention."""
    sparql, dataset = store.sparql, store.dataset
    queries = []
    triples = b""
    match usage.source:
        case UsageSource.OBJECT:
            triples = f"<{usage.subject}> <{usage.predicate}> <{iri}> .\n".encode()
        case UsageSource.PREDICATE if isinstance(usage.object, BlankNode):
            node = f"<_:{usage.object}>"
            triple = f"<{usage.subject}> <{iri}> ?b"
            queries += [f"CONSTRUCT {{ {triple} }} WHERE {{ BIND ({node} AS ?b) {triple} }}", f"DESCRIBE {node}"]
        case UsageSource.PREDICATE:
            assert usage.object is not None
            triples = f"<{usage.subject}> <{iri}> {ntriples_term(usage.object)} .\n".encode()
        case UsageSource.ROOT:
            assert usage.root is not None
            node = f"<_:{usage.root.label}>"
            queries.append(f"DESCRIBE {node}")
            if usage.root.kind == UsageRootKind.TRIPLE:
                where = f"BIND ({node} AS ?b) <{usage.subject}> <{usage.predicate}> ?b"
                queries.append(f"CONSTRUCT {{ <{usage.subject}> <{usage.predicate}> ?b }} WHERE {{ {where} }}")
            elif usage.root.kind == UsageRootKind.AXIOM:
                where = f"{node} owl:annotatedSource ?s ; owl:annotatedProperty ?p ; owl:annotatedTarget ?t"
                queries.append(f"CONSTRUCT {{ ?s ?p ?t }} WHERE {{ {where} }}")
    for query in queries:
        triples += await sparql.construct(dataset, PREFIXES + query)
    return await with_types(store, triples)


async def with_types(store: UsageStore, triples: bytes) -> bytes:
    """Return triples with the declaration types of the IRIs they mention, so the OWLAPI can parse their axioms."""
    values = " ".join(f"<{each}>" for each in sorted(ntriples_iris(triples)))
    where = f"VALUES ?iri {{ {values} }} VALUES ?type {{ {DECLARATION_TYPES} }} ?iri a ?type"
    return triples + await store.sparql.construct(
        store.dataset, f"{PREFIXES}CONSTRUCT {{ ?iri a ?type }} WHERE {{ {where} }}"
    )


async def test_usages_match_owlapi(store: UsageStore, ntriples: bytes, settings: Settings) -> None:
    """Check every entity's usages against the OWLAPI's, by value, less its own axioms; and its count against them.

    Each usage is one axiom, none twice, so the count, the usages listed and their axioms all agree.
    """
    with closing(connect(settings, store.generation)) as connection:
        iris = sorted({iri for (iri,) in connection.execute("SELECT iri FROM entity")})
    described = {
        iri: await with_types(store, await store.sparql.construct(store.dataset, f"DESCRIBE <{iri}>")) for iri in iris
    }
    subjects = await store.sparql.construct(store.dataset, "DESCRIBE ?s WHERE { ?s ?p ?o FILTER isIRI(?s) }")
    expected = await owlapi.run_java(owlapi_usages, ntriples, described, subjects)
    for iri in iris:
        usages = await read_usages(store, iri)
        found = []
        for usage in usages:
            axioms_found = await owlapi.run_java(parsed, await usage_triples(store, iri, usage))
            assert len(axioms_found) == 1, (iri, usage, axioms_found)
            found.extend(axioms_found)
        assert sorted(found) == sorted(expected[iri]), iri
        assert await count_usages(store, iri) == len(usages), iri


async def test_usages_kinds(store: UsageStore) -> None:
    """Check some entities' usages: their sources, roots and subjects ("" for none).

    A property's general class axioms and negative assertions are its usages, with no subject; a class's general class
    axioms aren't, as its Overview lists them.
    """

    async def usages(name: str) -> list[tuple[str, str, str]]:
        found = await read_usages(store, f"{NS}{name}")
        return sorted(
            (usage.source, usage.root.kind if usage.root else "", (usage.subject or "").removeprefix(NS))
            for usage in found
        )

    triple, axiom, general = UsageRootKind.TRIPLE, UsageRootKind.AXIOM, UsageRootKind.GENERAL
    # Through a class expression, and as an annotation's value on a reified axiom; not in either general class axiom.
    assert await usages("Fungus") == [("root", axiom, "Tricky"), ("root", triple, "Liver")]
    # Cat DisjointWith Dog and an annotation by value; rex's class assertion. Its general class axiom is its own.
    assert await usages("Dog") == [("object", "", "Cat"), ("object", "", "Cat"), ("object", "", "rex")]
    assert await usages("hasPart") == [("root", general, ""), ("root", general, "")]  # two general class axioms
    assert await usages("hates") == [("root", general, "")]  # a negative property assertion
    # On an annotated general class axiom, on Cat's annotated axiom, and on the n-ary DisjointClasses.
    assert await usages("note") == [("root", axiom, ""), ("root", axiom, "Cat"), ("root", general, "")]
    assert await usages("age") == [
        ("predicate", "", "alice"),
        ("root", general, ""),  # its negative assertion; not DisjointDataProperties(age, weight), its own
        ("root", triple, "Person"),
        ("root", triple, "Person"),
    ]
    assert await usages("remark") == [("predicate", "", "Cat"), ("root", axiom, "Tricky")]
    [annotation] = [usage for usage in await read_usages(store, f"{NS}remark") if usage.source == "predicate"]
    assert annotation == Usage(UsageSource.PREDICATE, IRI(f"{NS}Cat"), object=IRI(f"{NS}Dog"))
    [subclass] = [usage for usage in await read_usages(store, f"{NS}Parent") if usage.root]
    assert (subclass.predicate, subclass.root and subclass.root.kind) == (f"{RDFS}subClassOf", triple)


async def test_count_kept(store: UsageStore, monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that a count that takes longer than `CACHED_COUNT_S` is kept for its generation, and only then."""
    cache = UsageCounts()
    iri = f"{NS}Animal"
    count = await count_usages(store, iri, cache)
    assert cache.generation(store.generation) == {}  # quick
    assert CACHED_COUNT_S > 0
    monkeypatch.setattr("browl.usages.CACHED_COUNT_S", 0.0)
    assert await count_usages(store, iri, cache) == count
    assert cache.generation(store.generation) == {iri: count}
    cache.generation(store.generation)[iri] = -1
    assert await count_usages(store, iri, cache) == -1  # read from the cache
    assert cache.generation(store.generation - 1) == {}  # an older generation's aren't kept
    assert cache.generation(store.generation + 1) == {}  # a newer one's replace them
    assert cache.generation(store.generation) == {}


async def test_not_an_entity(store: UsageStore) -> None:
    """Check that an IRI that isn't in `entity`, an individual, has only its triples' usages, and a count of them all.

    Its count includes triples with a blank subject, such as a negative property assertion's, which the owner map
    doesn't count for an IRI that's no entity.
    """
    usages = await read_usages(store, f"{NS}rex")
    assert usages == [Usage(UsageSource.OBJECT, IRI(f"{NS}alice"), predicate=IRI(f"{NS}likes"))]
    assert await count_usages(store, f"{NS}rex") > len(usages)  # also in ObjectOneOf(rex tom) and hates's assertion


async def test_query_fails(store: UsageStore) -> None:
    """Check that a failed query, or a derived database that isn't current, fails the read and the count."""
    async with httpx2.AsyncClient(base_url="http://127.0.0.1:9") as http:  # nothing listens there
        unreachable = UsageStore(FusekiClient(http), store.derived, store.generation)
        with pytest.raises(SparqlError):
            await read_usages(unreachable, f"{NS}Animal")
        with pytest.raises(SparqlError):
            await count_usages(unreachable, f"{NS}Animal")

    async def never(request: httpx2.Request) -> httpx2.Response:
        await asyncio.Event().wait()  # so the derived read fails first, and the Fuseki reads are cancelled
        raise AssertionError

    async with httpx2.AsyncClient(base_url="http://fuseki", transport=httpx2.MockTransport(never)) as http:
        missing = UsageStore(FusekiClient(http), store.derived, store.generation + 1)  # no such generation's database
        with pytest.raises(NotCurrentError):
            await read_usages(missing, f"{NS}Animal")
        with pytest.raises(NotCurrentError):
            await count_usages(missing, f"{NS}Animal")
