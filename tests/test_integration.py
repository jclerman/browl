"""Integration tests: queries to a real Fuseki holding `tests/data/family.ttl`, and fragments parsed by the OWLAPI."""

from collections.abc import AsyncIterator

import httpx2
import pytest

from browl import owlapi
from browl.fuseki import FusekiClient, SparqlError
from browl.rdf import IRI, RDF_LANG_STRING, XSD, BlankNode, Literal

pytestmark = [pytest.mark.jvm, pytest.mark.fuseki, pytest.mark.usefixtures("jvm"), pytest.mark.anyio]

FAMILY = "http://example.org/family#"
PREFIXES = f"""PREFIX : <{FAMILY}>
PREFIX owl: <http://www.w3.org/2002/07/owl#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
"""


@pytest.fixture
def anyio_backend() -> str:
    """Run the async tests on asyncio, as the app does."""
    return "asyncio"


@pytest.fixture
async def sparql(fuseki_url: str) -> AsyncIterator[FusekiClient]:
    """Yield a client for the test Fuseki."""
    async with httpx2.AsyncClient(base_url=fuseki_url) as http:
        yield FusekiClient(http)


async def test_select_terms(sparql: FusekiClient, family_dataset: str) -> None:
    """Check that every kind of term in Fuseki's TSV, escapes included, comes back as the ontology states it."""
    result = await sparql.select(family_dataset, PREFIXES + "SELECT ?p ?o WHERE { :Person ?p ?o }")
    assert result.variables == ("p", "o")
    assert set(result.rows) >= {
        (IRI(f"{FAMILY}rank"), Literal("1", f"{XSD}integer")),
        (IRI("http://www.w3.org/2000/01/rdf-schema#label"), Literal("person", RDF_LANG_STRING, "en")),
        (IRI("http://www.w3.org/2000/01/rdf-schema#label"), Literal("personne", RDF_LANG_STRING, "fr")),
        (
            IRI("http://www.w3.org/2000/01/rdf-schema#comment"),
            Literal('Tab\there, newline\nhere, "quotes", a backslash \\, é, and a line separator\u2028here.'),
        ),
    }


async def test_select_blank_nodes_and_unbound(sparql: FusekiClient, family_dataset: str) -> None:
    """Check that blank nodes and unbound variables come through."""
    query = "SELECT ?e ?label WHERE { :Parent owl:equivalentClass ?e OPTIONAL { ?e rdfs:label ?label } }"
    result = await sparql.select(family_dataset, PREFIXES + query)
    [(equivalent, label)] = result.rows
    assert isinstance(equivalent, BlankNode)
    assert label is None


async def test_select_stream(sparql: FusekiClient, family_dataset: str) -> None:
    """Check that a streamed result has the same rows as a buffered one."""
    query = "SELECT ?s ?p ?o WHERE { ?s ?p ?o }"
    async with sparql.select_stream(family_dataset, query) as stream:
        streamed = [row async for row in stream]
    assert sorted(map(str, streamed)) == sorted(map(str, (await sparql.select(family_dataset, query)).rows))
    assert len(streamed) > 30


async def test_fragment(sparql: FusekiClient, family_dataset: str) -> None:
    """Check that an entity's DESCRIBE fragment, with its blank-node closure, parses as its logical axioms."""
    ntriples = await sparql.construct(family_dataset, f"DESCRIBE <{FAMILY}Parent>")

    def logical_axioms() -> list[str]:
        with owlapi.ntriples_ontology(ntriples) as fragment:
            return sorted(str(axiom) for axiom in fragment.getLogicalAxioms())

    [axiom] = await owlapi.run_java(logical_axioms)
    assert axiom.startswith("EquivalentClasses(")
    assert f"ObjectSomeValuesFrom(<{FAMILY}hasChild> <{FAMILY}Person>)" in axiom


async def test_fragment_with_imports(sparql: FusekiClient, family_dataset: str) -> None:
    """Check that the ontology's own fragment, with an import that can't be fetched, parses without fetching it."""
    ntriples = await sparql.construct(family_dataset, "DESCRIBE <http://example.org/family>")

    def imports() -> list[str]:
        with owlapi.ntriples_ontology(ntriples) as fragment:
            return [str(declaration.getIRI()) for declaration in fragment.getImportsDeclarations()]

    assert await owlapi.run_java(imports) == ["http://example.org/not-fetched.owl"]


async def test_construct_stream(sparql: FusekiClient, family_dataset: str) -> None:
    """Check that a streamed CONSTRUCT gives the same triples as a buffered one."""
    query = "CONSTRUCT { ?s ?p ?o } WHERE { ?s ?p ?o }"
    async with sparql.construct_stream(family_dataset, query) as lines:
        streamed = [line async for line in lines]
    assert "\n".join(streamed) + "\n" == (await sparql.construct(family_dataset, query)).decode()


async def test_query_syntax_error(sparql: FusekiClient, family_dataset: str) -> None:
    """Check that Fuseki's 400 for a malformed query fails with its message."""
    with pytest.raises(SparqlError, match=r"Fuseki answered 400: .*Parse error"):
        await sparql.select(family_dataset, "SELECT ?s WHERE {")


async def test_unknown_dataset(sparql: FusekiClient) -> None:
    """Check that a query to a dataset Fuseki doesn't have fails (Fuseki answers 405 for its path)."""
    with pytest.raises(SparqlError, match=r"Fuseki answered 4[0-9][0-9]"):
        await sparql.select("no-such-dataset", "SELECT * WHERE { ?s ?p ?o }")
