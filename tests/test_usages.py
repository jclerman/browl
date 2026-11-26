"""Tests for entities' usages and their counts (P-27a): the blank-node owner map, against the OWLAPI's usages."""

import asyncio
import json
import re
from collections.abc import AsyncIterator, Iterator
from contextlib import closing
from typing import Any

import httpx2
import jpype
import pytest

from browl import axioms, owlapi
from browl import usages as usages_module
from browl.derived import NotCurrentError, UsageRootKind, entities_with_iri
from browl.fuseki import UNION_FROM, FusekiClient, SelectResult, SparqlError
from browl.precache.core import PREFIXES
from browl.rdf import IRI, RDF, RDFS, BlankNode, Literal, Term, ntriples_iris
from browl.settings import Settings
from browl.usage_tab import sorted_usages
from browl.usages import (
    CACHED_COUNT_S,
    TooManyUsagesError,
    Usage,
    UsageCounts,
    UsageSource,
    UsageStore,
    count_usages,
    read_usages,
    read_values,
)
from tests.helpers import connect, loaded, make_settings, ontology_ntriples

pytestmark = pytest.mark.anyio

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
    """Yield a store of the test ontology."""
    async with loaded(fuseki_url, settings, {None: ntriples}, "") as found:
        yield found


SPLIT = (f"<{NS}Dog> ", f"<{NS}Cat> ", f"<{NS}likes> ", f"<{NS}Liver> ")
"""The subjects of the triples `split_store` puts in a named graph, as an import's, but those with a blank object: blank
nodes stay in the default graph, since each load names its own."""


@pytest.fixture(scope="module")
async def split_store(fuseki_url: str, ntriples: bytes, settings: Settings) -> AsyncIterator[UsageStore]:
    """Yield a store of the test ontology with `SPLIT`'s triples in a named graph, read with the imports' clause."""
    lines = ntriples.decode().splitlines(keepends=True)
    split = [line.startswith(SPLIT) and " _:" not in line for line in lines]
    named = "".join(line for line, moved in zip(lines, split, strict=True) if moved).encode()
    rest = "".join(line for line, moved in zip(lines, split, strict=True) if not moved).encode()
    assert named.count(b"\n") >= 10
    async with loaded(fuseki_url, settings, {None: rest, "http://example.org/imported": named}, UNION_FROM) as found:
        yield found


def normal(axiom: Any) -> str:
    """Return an axiom's functional syntax without its annotations, nor its anonymous individuals' labels.

    The OWLAPI gives those anew on every parse.
    """
    return re.sub(r"_:genid[0-9]+", "_:", str(axiom.getAxiomWithoutAnnotations()))


def parsed(ntriples: bytes, closure: bool = False) -> set[str]:
    """Return the axioms the OWLAPI parses from N-Triples, but unannotated declarations, without annotations.

    With `closure`, the triples are a root's closure, which brings along the assertions of any anonymous individual in
    it, which are roots of their own, so they're left out, as the Usage tab leaves them out (`browl.usage_tab`).
    """
    with owlapi.ntriples_ontology(ntriples) as ontology:
        return {
            normal(each)
            for each in ontology.getAxioms()
            if (each.isAnnotated() or not axioms.is_declaration(each))
            and not (closure and is_anonymous_assertion(each))
        }


def is_anonymous_assertion(axiom: Any) -> bool:
    """Return whether an OWLAPI axiom is an assertion that names an anonymous individual."""
    kinds = {"ClassAssertion", "ObjectPropertyAssertion", "DataPropertyAssertion", "AnnotationAssertion"}
    return str(axiom.getAxiomType().getName()) in kinds and not axiom.getAnonymousIndividuals().isEmpty()


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
        case UsageSource.PREDICATE:
            assert usage.subject is not None
            value = (await read_values(store, iri, [usage.subject]))[usage.subject][usage.index]
            if isinstance(value, BlankNode):  # an anonymous individual, which has no closure of its own
                node = f"<_:{value}>"
                triple = f"<{usage.subject}> <{iri}> ?b"
                queries.append(f"CONSTRUCT {{ {triple} }} WHERE {{ BIND ({node} AS ?b) {triple} }}")
            else:
                triples = f"<{usage.subject}> <{iri}> {ntriples_term(value)} .\n".encode()
        case UsageSource.ROOT if usage.root is not None and usage.root.kind == UsageRootKind.ASSERTION:
            queries += assertion_queries(usage)
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


def assertion_queries(usage: Usage) -> list[str]:
    """Return the queries for an anonymous individual's assertion's triple, and its class expression's closure.

    Its object is an IRI or a blank node, or a literal by its digest (`usage_root.object`).
    """
    assert usage.root is not None and usage.root.object is not None
    node, predicate, term = f"<_:{usage.root.label}>", f"<{usage.predicate}>", usage.root.object
    where = f"BIND ({node} AS ?s) ?s {predicate} ?o"
    if term.startswith('"'):
        digest = 'SHA256(CONCAT(STR(?o), " ", LCASE(LANG(?o)), " ", STR(DATATYPE(?o))))'
        where += f" FILTER (isLiteral(?o) && SUBSTR({digest}, 1, {len(term) - 2}) = {term})"
    else:
        where = f"BIND ({term} AS ?o) {where}"
    queries = [f"CONSTRUCT {{ ?s {predicate} ?o }} WHERE {{ {where} }}"]
    if term.startswith("<_:") and usage.predicate == f"{RDF}type":
        queries.append(f"DESCRIBE {term}")
    return queries


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
            closure = usage.root is not None and usage.root.kind != UsageRootKind.ASSERTION
            axioms_found = await owlapi.run_java(parsed, await usage_triples(store, iri, usage), closure)
            assert len(axioms_found) == 1, (iri, usage, axioms_found)
            found.extend(axioms_found)
        assert sorted(found) == sorted(expected[iri]), iri
        assert await count_usages(store, iri) == len(usages), iri


async def test_usages_kinds(store: UsageStore) -> None:
    """Check some entities' usages: their sources, roots and subjects ("" for none).

    A property's general class axioms and negative assertions are its usages, with no subject; a class's general class
    axioms aren't, as its Overview lists them. An anonymous individual's assertions are usages with no subject, one
    each; an n-ary `DifferentIndividuals` is a named member's own axiom, as is a negative assertion its source's.
    """

    async def usages(name: str) -> list[tuple[str, str, str]]:
        found = await read_usages(store, f"{NS}{name}")
        return sorted(
            (usage.source, usage.root.kind if usage.root else "", (usage.subject or "").removeprefix(NS))
            for usage in found
        )

    triple, axiom, general = UsageRootKind.TRIPLE, UsageRootKind.AXIOM, UsageRootKind.GENERAL
    assertion = UsageRootKind.ASSERTION
    # Through a class expression, and as an annotation's value on a reified axiom, and an anonymous individual's; not
    # in either general class axiom.
    assert await usages("Fungus") == [("root", assertion, ""), ("root", axiom, "Tricky"), ("root", triple, "Liver")]
    # Cat DisjointWith Dog and an annotation by value; rex's class assertion; a SWRL rule; Pet's union. Its general
    # class axiom is its own.
    assert await usages("Dog") == [
        ("object", "", "Cat"),
        ("object", "", "Cat"),
        ("object", "", "rex"),
        ("root", general, ""),
        ("root", triple, "Pet"),
    ]
    # In an annotation on an axiom annotation: the annotated axiom's subject's usage.
    assert await usages("Plant") == [
        ("root", axiom, "Liver"),
        ("root", triple, "Heart"),
        ("root", triple, "LivingThing"),
    ]
    # Only in an annotation of an n-ary DisjointClasses, so not its own; its triple about itself isn't a usage.
    assert await usages("Mineral") == [("root", general, "")]
    assert await usages("hasPart") == [("root", general, ""), ("root", general, "")]  # two general class axioms
    # Two negative property assertions, one an anonymous individual's, and an anonymous individual's assertion.
    assert await usages("hates") == [("root", assertion, ""), ("root", general, ""), ("root", general, "")]
    # On Cat's and Liver's annotated axioms, an annotated general class axiom, the n-ary DisjointClasses, and the
    # axiom annotations on Kidney's comments and a restriction, Bladder's comment and disjointness, and carol's and an
    # anonymous individual's assertions.
    assert await usages("note") == [
        ("root", assertion, ""),
        ("root", axiom, "Bladder"),
        ("root", axiom, "Bladder"),
        ("root", axiom, "Cat"),
        ("root", axiom, "Kidney"),
        ("root", axiom, "Kidney"),
        ("root", axiom, "Kidney"),
        ("root", axiom, "Liver"),
        ("root", axiom, "carol"),
        ("root", axiom, "carol"),
        ("root", general, ""),
        ("root", general, ""),
    ]
    assert await usages("age") == [
        ("predicate", "", "alice"),
        ("root", assertion, ""),  # an anonymous individual's two values
        ("root", assertion, ""),
        ("root", general, ""),  # its negative assertion; not DisjointDataProperties(age, weight), its own
        ("root", triple, "Person"),
        ("root", triple, "Person"),
    ]
    assert await usages("remark") == [
        ("predicate", "", "Cat"),
        ("root", assertion, ""),  # an anonymous individual's annotation
        ("root", axiom, "Kidney"),  # on the annotation of its comment's axiom
        ("root", axiom, "Kidney"),  # on its rdfs:seeAlso's axiom
        ("root", axiom, "Liver"),  # on the annotation of Liver's axiom
        ("root", axiom, "Tricky"),
        ("root", general, ""),  # on the n-ary DisjointClasses
    ]
    [annotation] = [usage for usage in await read_usages(store, f"{NS}remark") if usage.source == "predicate"]
    assert annotation == Usage(UsageSource.PREDICATE, IRI(f"{NS}Cat"))
    assert await read_values(store, f"{NS}remark", [f"{NS}Cat"]) == {f"{NS}Cat": [IRI(f"{NS}Dog")]}
    # The target of a negative assertion whose source is anonymous, and in a ObjectOneOf; not the n-ary
    # DifferentIndividuals, its own; its anonymous members' assertions aren't in it.
    assert await usages("tom") == [("root", general, ""), ("root", triple, "Pet")]
    # alice's hasChild and DifferentFrom; not the assertions of the anonymous individual bob loves.
    assert await usages("bob") == [("object", "", "alice"), ("object", "", "alice")]
    assert (await usages("Pet")).count(("root", assertion, "")) == 1  # an anonymous individual's type
    [subclass] = [usage for usage in await read_usages(store, f"{NS}Parent") if usage.root]
    assert (subclass.predicate, subclass.root and subclass.root.kind) == (f"{RDFS}subClassOf", triple)


async def test_count_kept(store: UsageStore, monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that an entity's count that takes longer than `CACHED_COUNT_S` is kept, and only then.

    An IRI that's no entity's never is.
    """
    assert CACHED_COUNT_S == 0.1
    cache = UsageCounts()
    iri, generation = f"{NS}Animal", store.generation
    monkeypatch.setattr("browl.usages.CACHED_COUNT_S", 3600.0)
    count = await count_usages(store, iri, cache)
    assert cache.get(generation, iri) is None
    monkeypatch.setattr("browl.usages.CACHED_COUNT_S", 0.0)
    assert await count_usages(store, iri, cache) == count
    assert cache.get(generation, iri) == count
    await count_usages(store, f"{NS}nothing", cache)
    assert cache.get(generation, f"{NS}nothing") is None
    cache.keep(generation, iri, -1)
    assert await count_usages(store, iri, cache) == -1  # read from the cache
    assert cache.get(generation - 1, iri) is None


def test_counts_kept_per_generation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that counts are kept for the newest generation only, at most `MAX_KEPT_COUNTS`, the oldest dropped."""
    monkeypatch.setattr("browl.usages.MAX_KEPT_COUNTS", 2)
    cache = UsageCounts()
    cache.keep(5, "a", 1)
    cache.keep(4, "b", 2)  # older: not kept
    assert (cache.get(5, "a"), cache.get(4, "b"), cache.get(5, "b")) == (1, None, None)
    cache.keep(5, "b", 2)
    cache.keep(5, "a", 3)  # replaced, still the oldest
    cache.keep(5, "c", 4)
    assert [cache.get(5, iri) for iri in "abc"] == [None, 2, 4]
    cache.keep(6, "d", 5)  # newer: replaces them all
    assert [cache.get(6, iri) for iri in "bcd"] == [None, None, 5]
    assert cache.get(5, "d") is None


async def test_limit(store: UsageStore) -> None:
    """Check that an entity with more usages than the limit fails the read, from each source."""
    for name in ("Animal", "age", "remark"):
        usages = await read_usages(store, f"{NS}{name}")
        assert await read_usages(store, f"{NS}{name}", len(usages)) == usages
        with pytest.raises(TooManyUsagesError, match=f"^the entity has more than {len(usages) - 1} usages$"):
            await read_usages(store, f"{NS}{name}", len(usages) - 1)
    with pytest.raises(TooManyUsagesError):  # every source over the limit at once
        await read_usages(store, f"{NS}age", 0)
    # Each source reads no more than it needs to tell.
    animal = f"{NS}Animal"
    assert len(await usages_module._object_usages(store, animal, 2)) == 2
    assert len(await usages_module._predicate_usages(store, f"{NS}remark", 0)) == 0
    assert len(await store.derived.read(store.generation, lambda c: usages_module._root_usages(c, animal, 1))) == 1


def normalized(usages: list[Usage]) -> list[tuple[str, ...]]:
    """Return usages without their store's blank node labels and root numbers."""
    return sorted(
        (
            usage.source,
            str(usage.subject),
            str(usage.predicate),
            str(usage.index),
            usage.root.kind if usage.root else "",
        )
        for usage in usages
    )


async def test_imports(store: UsageStore, split_store: UsageStore, settings: Settings) -> None:
    """Check that with some triples in a named graph, as an import's, each entity has the same usages and count."""
    with closing(connect(settings, store.generation)) as connection:
        iris = sorted({iri for (iri,) in connection.execute("SELECT iri FROM entity")})
    for iri in iris:
        usages = await read_usages(split_store, iri)
        assert normalized(usages) == normalized(await read_usages(store, iri)), iri
        assert await count_usages(split_store, iri) == len(usages), iri


async def test_anonymous_individuals(fuseki_url: str, settings: Settings) -> None:
    """Check that anonymous individuals' assertions are listed and counted, each a usage with no subject (P-47).

    An anonymous individual is found by its type, or as its property assertion's subject, or by `owl:sameAs`; one with
    annotation assertions alone isn't, so they're counted, not listed. An anonymous individual's `owl:sameAs` a named
    one is the named one's own axiom, which its Overview lists, not its usage.
    """
    owl = "http://www.w3.org/2002/07/owl#"
    pet, heart, likes, age, note, rex = (f"<{NS}{name}>" for name in ("Pet", "Heart", "likes", "age", "note", "rex"))
    declared = {pet: "Class", heart: "Class", likes: "ObjectProperty", age: "DatatypeProperty"}
    declared |= {note: "AnnotationProperty", rex: "NamedIndividual"}
    triples = "".join(f"{iri} <{RDF}type> <{owl}{kind}> .\n" for iri, kind in declared.items()) + (
        f"_:a <{RDF}type> {pet} .\n_:a {likes} {heart} .\n"  # ClassAssertion(Pet _:a), likes(_:a Heart)
        f"_:b {likes} _:a .\n_:a {likes} _:b .\n"  # a cycle, _:b found by its property only
        f'_:c <{owl}sameAs> {rex} .\n_:e {age} "5"^^<http://www.w3.org/2001/XMLSchema#integer> .\n'
        f"_:d {note} {heart} .\n"  # annotations alone
    )
    async with loaded(fuseki_url, settings, {None: triples.encode()}, "") as anonymous:
        for name, listed, count in (("Pet", 1, 1), ("Heart", 1, 2), ("likes", 3, 3), ("age", 1, 1), ("note", 0, 1)):
            usages = await read_usages(anonymous, f"{NS}{name}")
            assert [(usage.subject, usage.root and usage.root.kind) for usage in usages] == [
                (None, UsageRootKind.ASSERTION)
            ] * listed, name
            assert await count_usages(anonymous, f"{NS}{name}") == count, name
        assert await read_usages(anonymous, f"{NS}rex") == []
        assert await count_usages(anonymous, f"{NS}rex") == 0


async def test_undeclared_individual(fuseki_url: str, settings: Settings) -> None:
    """Check that an individual typed but not declared is no entity, so its axioms are under the subjects not in it.

    They're after a declared one's, which are under it (DESIGN.md §2.8).

    Its own usages are only its triples', with a count of them all, which includes those with a blank subject, as the
    owner map counts them only for entities.
    """
    owl = "http://www.w3.org/2002/07/owl#"
    person, likes, alice, aaron = (f"<{NS}{name}>" for name in ("Person", "likes", "alice", "aaron"))
    triples = (
        f"{person} <{RDF}type> <{owl}Class> .\n{likes} <{RDF}type> <{owl}ObjectProperty> .\n"
        f"{alice} <{RDF}type> <{owl}NamedIndividual> .\n{alice} <{RDF}type> {person} .\n"
        f"{aaron} <{RDF}type> {person} .\n"
        f"{alice} {likes} {aaron} .\n_:x {likes} {aaron} .\n"
    )
    async with loaded(fuseki_url, settings, {None: triples.encode()}, "") as found:
        usages = await read_usages(found, f"{NS}Person")
        ordered = await found.derived.read(found.generation, lambda c: sorted_usages(c, usages))
        # aaron's name sorts first, but it's no entity's.
        assert [str(usage.subject) for usage in ordered] == [f"{NS}alice", f"{NS}aaron"]
        with closing(connect(settings, found.generation)) as connection:
            assert [entity.type for entity in entities_with_iri(connection, f"{NS}alice")] == ["named_individual"]
            assert entities_with_iri(connection, f"{NS}aaron") == []
        undeclared = await read_usages(found, f"{NS}aaron")
        assert undeclared == [Usage(UsageSource.OBJECT, IRI(f"{NS}alice"), predicate=IRI(f"{NS}likes"))]
        assert await count_usages(found, f"{NS}aaron") == 2  # and _:x's assertion


async def test_query_fails(store: UsageStore) -> None:
    """Check that a failed query, or a derived database that isn't current, fails the read and the count."""
    async with httpx2.AsyncClient(base_url="http://127.0.0.1:9") as http:  # nothing listens there
        unreachable = UsageStore(FusekiClient(http), store.derived, store.generation, "")
        with pytest.raises(SparqlError):
            await read_usages(unreachable, f"{NS}Animal")
        with pytest.raises(SparqlError):
            await count_usages(unreachable, f"{NS}Animal")

    async def never(request: httpx2.Request) -> httpx2.Response:
        await asyncio.Event().wait()  # so the derived read fails first, and the Fuseki reads are cancelled
        raise AssertionError

    async with httpx2.AsyncClient(base_url="http://fuseki", transport=httpx2.MockTransport(never)) as http:
        missing = UsageStore(FusekiClient(http), store.derived, store.generation + 1, "")  # no generation's database
        with pytest.raises(NotCurrentError):
            await read_usages(missing, f"{NS}Animal")
        with pytest.raises(NotCurrentError):
            await count_usages(missing, f"{NS}Animal")


async def test_values_checked(store: UsageStore) -> None:
    """Check that a values query whose row has no value fails, and that no subjects need no query."""
    assert await read_values(store, f"{NS}remark", []) == {}

    class Unbound:
        async def select(self, *args: object, **kwargs: object) -> SelectResult:
            return SelectResult(("subject", "object", "blank"), [(IRI(f"{NS}Cat"), None, None)])

    broken = UsageStore(Unbound(), store.derived, store.generation, store.dataset_clause)  # type: ignore[arg-type]
    with pytest.raises(SparqlError, match="gave no value"):
        await read_values(broken, f"{NS}remark", [f"{NS}Cat"])
