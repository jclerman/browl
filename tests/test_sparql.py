"""Checks of the SPARQL files browl runs (`browl/sparql/`): each parses, and names only W3C-standard terms.

browl serves any OWL ontology, so its queries can rely only on the RDF, RDFS, OWL and XSD vocabularies. Cases ROBOT's
output can't show, such as a shared restriction node, run on rdflib; the rest are tested through ROBOT (test_robot.py).
"""

from collections.abc import Iterator
from pathlib import Path

import pytest
from rdflib import Graph, Literal, URIRef, namespace
from rdflib.paths import Path as PropertyPath
from rdflib.plugins.sparql import prepareQuery, prepareUpdate

import browl

SPARQL_DIR = Path(browl.__file__).parent / "sparql"
FILES = sorted(SPARQL_DIR.glob("*.rq"))
# Reached through `namespace`: imported into this module, pytest probes them and rdflib warns.
STANDARD = (namespace.RDF, namespace.RDFS, namespace.OWL, namespace.XSD)


def algebra(text: str) -> object:
    """Return the algebra of a SPARQL query or update.

    Raises:
        ValueError: The text is neither, saying why each parse failed.
    """
    errors = []
    for prepare in (prepareQuery, prepareUpdate):
        try:
            return prepare(text).algebra
        except Exception as error:  # noqa: BLE001  # rdflib raises pyparsing's and its own errors
            errors.append(f"as {'a query' if prepare is prepareQuery else 'an update'}: {error}")
    raise ValueError("; ".join(errors))


def iris(node: object) -> Iterator[URIRef]:
    """Yield every IRI in a SPARQL algebra tree, including those in property paths and literals' datatypes."""
    if isinstance(node, URIRef):
        yield node
    elif isinstance(node, Literal):
        if node.datatype is not None:
            yield node.datatype
    elif isinstance(node, str):
        return
    elif isinstance(node, dict):
        for value in node.values():
            yield from iris(value)
    elif isinstance(node, list | tuple | set):
        for item in node:
            yield from iris(item)
    elif isinstance(node, PropertyPath):
        yield from iris(vars(node))


def nonstandard(iris: set[URIRef]) -> list[str]:
    """Return the IRIs that aren't terms of the standard vocabularies, sorted."""
    return sorted(str(iri) for iri in iris if not any(iri in vocabulary for vocabulary in STANDARD))


def test_files_found() -> None:
    """Check that there are SPARQL files to check, so the tests below can't pass vacuously."""
    assert FILES


@pytest.mark.parametrize("path", FILES, ids=lambda path: path.name)
def test_parses(path: Path) -> None:
    """Check that the file is a valid SPARQL 1.1 query or update."""
    algebra(path.read_text())


@pytest.mark.parametrize("path", FILES, ids=lambda path: path.name)
def test_standard_terms_only(path: Path) -> None:
    """Check that every IRI the file names is a term defined in the RDF, RDFS, OWL or XSD vocabulary."""
    found = set(iris(algebra(path.read_text())))
    assert found
    assert nonstandard(found) == []


@pytest.mark.parametrize(
    ("text", "bad"),
    [
        (
            "PREFIX owl: <http://www.w3.org/2002/07/owl#>\nSELECT * { ?r owl:someValueFrom ?c }",
            f"{namespace.OWL}someValueFrom",
        ),
        ("SELECT * { ?s <http://example.org/p> ?o }", "http://example.org/p"),
        ('SELECT * { ?s ?p "x"^^<http://example.org/t> }', "http://example.org/t"),
        (
            (
                "PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>\n"
                "DELETE { ?a rdfs:subClassOf ?b } WHERE { ?a rdfs:subClassOf/<http://example.org/p> ?b }"
            ),
            "http://example.org/p",
        ),
    ],
)
def test_nonstandard_terms_found(text: str, bad: str) -> None:
    """Check that the walk finds non-standard IRIs and misspelt terms, in paths, datatypes and updates too."""
    assert nonstandard(set(iris(algebra(text)))) == [bad]


def test_invalid_syntax() -> None:
    """Check that text that's neither a query nor an update is rejected, with both parsers' errors."""
    with pytest.raises(ValueError, match=r"as a query: .*; as an update: "):
        algebra("PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>\nDELETE { ?a rdfs:subClassOf ?b ; ?b } WHERE {}")


@pytest.mark.parametrize(("filler", "deleted"), [("someValuesFrom", True), ("allValuesFrom", False)])
def test_shortcuts_need_some_restriction(filler: str, deleted: bool) -> None:
    """Check that `X ⊑ p some W`, `W ⊑ R` and `X ⊑ R` delete `X ⊑ R` only when R is `p some T`, not `p only T`.

    R is one node shared by X and W, which ROBOT's output never has.
    """
    graph = Graph().parse(
        format="turtle",
        data=f"""
            @prefix : <http://example.org/s/> .
            @prefix owl: <{namespace.OWL}> .
            @prefix rdfs: <{namespace.RDFS}> .
            :p a owl:TransitiveProperty .
            :X rdfs:subClassOf [ a owl:Restriction ; owl:onProperty :p ; owl:someValuesFrom :W ], _:r .
            :W rdfs:subClassOf _:r .
            _:r a owl:Restriction ; owl:onProperty :p ; owl:{filler} :T .
        """,
    )
    shared = graph.value(URIRef("http://example.org/s/W"), namespace.RDFS.subClassOf)
    graph.update((SPARQL_DIR / "remove-superclass-shortcuts.rq").read_text())
    assert ((URIRef("http://example.org/s/X"), namespace.RDFS.subClassOf, shared) in graph) is not deleted
