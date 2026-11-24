"""Tests for the axiom counts (`browl.precache.axiom_counts`): checked against the OWLAPI's counts of the same RDF."""

from collections import Counter
from collections.abc import Iterator
from contextlib import closing

import jpype
import pytest

from browl import owlapi
from browl.derived import AxiomKind, read_axiom_counts
from browl.precache.axiom_counts import count_axioms
from browl.rdf import OWL, RDF, RDFS
from browl.settings import Settings
from tests.helpers import connect, loaded, make_settings, ontology_ntriples

pytestmark = pytest.mark.anyio

EX = "http://example.org/x#"

OWLAPI_KINDS = {
    AxiomKind.DECLARATION: ("DECLARATION",),
    AxiomKind.ANNOTATION: (
        "ANNOTATION_ASSERTION",
        "SUB_ANNOTATION_PROPERTY_OF",
        "ANNOTATION_PROPERTY_DOMAIN",
        "ANNOTATION_PROPERTY_RANGE",
    ),
    AxiomKind.OTHER: ("DATATYPE_DEFINITION",),
}
"""The OWLAPI's axiom types of each kind but logical, which is every other type (its `getLogicalAxiomCount` counts
datatype definitions too)."""


@pytest.fixture(scope="module")
def anyio_backend() -> str:
    """Run the module's async tests and fixtures on asyncio, as the module-scoped fixtures need one backend."""
    return "asyncio"


@pytest.fixture(scope="module")
def settings(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Settings]:
    """Yield settings with a data directory of the module's own, as `DATA_DIR` too."""
    data_dir = tmp_path_factory.mktemp("data")
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("DATA_DIR", str(data_dir))
        yield make_settings()


def owlapi_counts(ntriples: bytes) -> Counter[AxiomKind]:
    """Return the OWLAPI's axioms of each kind in an ontology's N-Triples."""
    manager = jpype.JClass("org.semanticweb.owlapi.apibinding.OWLManager").createOWLOntologyManager()
    source = jpype.JClass("org.semanticweb.owlapi.io.StringDocumentSource")(ntriples.decode())
    ontology = manager.loadOntologyFromOntologyDocument(source)
    axiom_type = jpype.JClass("org.semanticweb.owlapi.model.AxiomType")
    kinds = {str(getattr(axiom_type, name)): kind for kind, names in OWLAPI_KINDS.items() for name in names}
    counts: Counter[AxiomKind] = Counter()
    for axiom in ontology.getAxioms():
        counts[kinds.get(str(axiom.getAxiomType()), AxiomKind.LOGICAL)] += 1
    assert sum(counts.values()) == int(ontology.getAxiomCount())
    return counts


async def stored_counts(fuseki_url: str, settings: Settings, ntriples: bytes) -> Counter[AxiomKind]:
    """Return the axiom counts the pre-caching stage stores for an ontology's N-Triples."""
    async with loaded(fuseki_url, settings, {None: ntriples}, "") as store:
        with closing(connect(settings, store.generation)) as connection:
            return Counter(read_axiom_counts(connection))


async def test_axioms_ofn(jvm: None, fuseki_url: str, settings: Settings) -> None:
    """Check the counts of every kind of OWL 2 axiom (`tests/data/axioms.ofn`) against the OWLAPI's."""
    ntriples = await owlapi.run_java(ontology_ntriples)
    expected = await owlapi.run_java(owlapi_counts, ntriples)
    assert await stored_counts(fuseki_url, settings, ntriples) == expected


ANONYMOUS = f"""\
<http://example.org/x> <{RDF}type> <{OWL}Ontology> .
<{EX}A> <{RDF}type> <{OWL}Class> .
<{EX}p> <{RDF}type> <{OWL}ObjectProperty> .
<{EX}a> <{RDF}type> <{OWL}NamedIndividual> .
_:i <{RDF}type> <{EX}A> .
_:i <{EX}p> <{EX}a> .
<{EX}a> <{EX}p> _:i .
_:i <{RDFS}comment> "An anonymous individual's annotation." .
""".encode()


async def test_anonymous_individuals(jvm: None, fuseki_url: str, settings: Settings) -> None:
    """Check the counts of anonymous individuals' assertions, whose subject is blank, against the OWLAPI's."""
    expected = await owlapi.run_java(owlapi_counts, ANONYMOUS)
    assert await stored_counts(fuseki_url, settings, ANONYMOUS) == expected


SYMMETRIC_AND_HEADER = f"""\
<http://example.org/x> <{RDF}type> <{OWL}Ontology> .
<http://example.org/x> <{OWL}imports> <http://example.org/y> .
<http://example.org/x> <{RDFS}comment> "Not an axiom." .
<{EX}A> <{RDF}type> <{OWL}Class> .
<{EX}B> <{RDF}type> <{OWL}Class> .
<{EX}A> <{OWL}equivalentClass> <{EX}B> .
<{EX}B> <{OWL}equivalentClass> <{EX}A> .
<{EX}A> <{OWL}disjointWith> <{EX}A> .
<{EX}A> <{RDFS}comment> "An annotation assertion." .
<{EX}A> <{RDF}type> <{RDFS}Class> .
""".encode()


async def test_symmetric_and_header(fuseki_url: str, settings: Settings) -> None:
    """Check that a symmetric axiom stated both ways counts once, and the header and RDFS typing not at all."""
    counts = await stored_counts(fuseki_url, settings, SYMMETRIC_AND_HEADER)
    assert counts == Counter({AxiomKind.LOGICAL: 2, AxiomKind.DECLARATION: 2, AxiomKind.ANNOTATION: 1})


@pytest.mark.parametrize(
    ("types", "kind"),
    [
        (set(), AxiomKind.ANNOTATION),  # undeclared: an annotation property, as the OWLAPI reads one
        ({f"{OWL}AnnotationProperty"}, AxiomKind.ANNOTATION),
        ({f"{OWL}AnnotationProperty", f"{OWL}ObjectProperty"}, AxiomKind.ANNOTATION),
        ({f"{OWL}ObjectProperty"}, AxiomKind.LOGICAL),
        ({f"{OWL}TransitiveProperty"}, AxiomKind.LOGICAL),
    ],
)
def test_property_assertion_kind(types: set[str], kind: AxiomKind) -> None:
    """Check that a triple's predicate makes it an annotation assertion or a property assertion as its types say."""
    counts = count_axioms({(f"{EX}p", None): 3}, {}, {}, {f"{EX}p": types})
    assert counts == Counter({kind: 3})


def test_adjustments() -> None:
    """Check the corrections: annotation property axioms and datatype definitions move; the others add or subtract."""
    triples = {(f"{RDFS}subPropertyOf", None): 5, (f"{OWL}equivalentClass", None): 4}
    adjustments = {"annotation_property": 2, "datatype_definition": 1, "symmetric": 1, "node": 3}
    counts = count_axioms(triples, {}, adjustments, {})
    assert counts == Counter({AxiomKind.LOGICAL: 9 - 2 - 1 - 1 + 3, AxiomKind.ANNOTATION: 2, AxiomKind.OTHER: 1})


def test_vocabulary_not_axioms() -> None:
    """Check that the vocabularies' other predicates and classes make no axiom, nor the excluded subjects' triples."""
    triples = {
        (f"{OWL}imports", None): 1,
        (f"{RDF}type", f"{RDFS}Class"): 1,
        (f"{RDF}type", f"{OWL}Ontology"): 1,
        (f"{RDFS}comment", None): 2,
    }
    counts = count_axioms(triples, {(f"{RDFS}comment", None): 1}, {}, {})
    assert counts == Counter({AxiomKind.ANNOTATION: 1})


async def test_imports_not_counted(fuseki_url: str, settings: Settings) -> None:
    """Check that only the ontology's own axioms are counted, not an import's, in its named graph."""
    imported = f"<{EX}C> <{RDF}type> <{OWL}Class> .\n<{EX}C> <{RDFS}subClassOf> <{EX}A> .\n".encode()
    graphs = {None: SYMMETRIC_AND_HEADER, "http://example.org/y": imported}
    async with loaded(fuseki_url, settings, graphs, "") as store:
        with closing(connect(settings, store.generation)) as connection:
            counts = Counter(read_axiom_counts(connection))
    assert counts == Counter({AxiomKind.LOGICAL: 2, AxiomKind.DECLARATION: 2, AxiomKind.ANNOTATION: 1})
