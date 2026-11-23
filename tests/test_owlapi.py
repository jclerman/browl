"""Tests for the OWLAPI bridge: the JVM, the Java thread, and parsing fragments."""

import os
import threading
from pathlib import Path

import jpype
import pytest

from browl import owlapi
from tests.helpers import StaticServer

pytestmark = [pytest.mark.usefixtures("jvm"), pytest.mark.anyio]

OWL = "http://www.w3.org/2002/07/owl#"
RDF_TYPE = "<http://www.w3.org/1999/02/22-rdf-syntax-ns#type>"
FRAGMENT = f"""\
<http://example.org/A> {RDF_TYPE} <{OWL}Class> .
<http://example.org/B> {RDF_TYPE} <{OWL}Class> .
<http://example.org/p> {RDF_TYPE} <{OWL}ObjectProperty> .
<http://example.org/A> <http://www.w3.org/2000/01/rdf-schema#subClassOf> _:r .
_:r {RDF_TYPE} <{OWL}Restriction> .
_:r <{OWL}onProperty> <http://example.org/p> .
_:r <{OWL}someValuesFrom> <http://example.org/B> .
""".encode()


def axioms(ntriples: bytes) -> list[str]:
    """Parse a fragment and return its axioms, in functional syntax, sorted."""
    with owlapi.ntriples_ontology(ntriples) as ontology:
        return sorted(str(axiom) for axiom in ontology.getAxioms())


async def test_parse_fragment() -> None:
    """Check that N-Triples parse as an ontology with the axioms they state, blank nodes included."""
    assert await owlapi.run_java(axioms, FRAGMENT) == [
        "Declaration(Class(<http://example.org/A>))",
        "Declaration(Class(<http://example.org/B>))",
        "Declaration(ObjectProperty(<http://example.org/p>))",
        "SubClassOf(<http://example.org/A> ObjectSomeValuesFrom(<http://example.org/p> <http://example.org/B>))",
    ]


async def test_fragment_released() -> None:
    """Check that each fragment gets a fresh manager, from which it's removed after use."""

    def parse() -> tuple[object, int]:
        with owlapi.ntriples_ontology(FRAGMENT) as ontology:
            manager = ontology.getOWLOntologyManager()
            assert manager.getOntologies().size() == 1
        return manager, manager.getOntologies().size()

    first, after = await owlapi.run_java(parse)
    second, _ = await owlapi.run_java(parse)
    assert after == 0
    assert not first.equals(second)


IMPORTS = f"<{OWL}imports>"


@pytest.mark.parametrize(
    ("triple", "imported"),
    [
        ("<http://example.org/o> {imports} <{server}/plain.owl> .", "{server}/plain.owl"),
        ("<http://example.org/o> {imports} <{server}/\\u0065scaped.owl> .", "{server}/escaped.owl"),
        (
            "<http://example.org/o> <http://www.w3.org/2002/07/owl\\u0023imports> <{server}/predicate.owl> .",
            "{server}/predicate.owl",
        ),
        ("_:o {imports} <{server}/blank.owl> .", "{server}/blank.owl"),
        ("<http://example.org/o> {imports} <{file}> .", "{file}"),
    ],
    ids=["plain", "escaped-iri", "escaped-predicate", "blank-node-subject", "file"],
)
async def test_imports_not_fetched(triple: str, imported: str, site: StaticServer, tmp_path: Path) -> None:
    """Check that an import is kept as a declaration but never fetched, however its triple is written.

    The imported IRIs point at a local HTTP server, which records any request, and at a file holding an ontology, which
    would be imported if it were read.
    """
    server, requests = site.url, site.requested
    file = tmp_path / "imported.nt"
    file.write_text(f"<http://example.org/imported> {RDF_TYPE} <{OWL}Ontology> .\n")
    values = {"imports": IMPORTS, "server": server, "file": file.as_uri()}
    ntriples = f"<http://example.org/o> {RDF_TYPE} <{OWL}Ontology> .\n{triple.format(**values)}\n".encode()

    def parse() -> tuple[list[str], int]:
        with owlapi.ntriples_ontology(ntriples + FRAGMENT) as fragment:
            return [str(declaration.getIRI()) for declaration in fragment.getImportsDeclarations()], (
                fragment.getImportsClosure().size()
            )

    assert await owlapi.run_java(parse) == ([imported.format(**values)], 1)
    assert requests == []


async def test_fragment_manager_loads_nothing_else(site: StaticServer) -> None:
    """Check that a fragment's manager refuses to load any other document, but can still create ontologies."""
    server, requests = site.url, site.requested

    def use_manager() -> tuple[str, int]:
        with owlapi.ntriples_ontology(FRAGMENT) as fragment:
            manager = fragment.getOWLOntologyManager()
            iri = jpype.JClass("org.semanticweb.owlapi.model.IRI")
            with pytest.raises(jpype.JException) as refused:
                manager.loadOntologyFromOntologyDocument(iri.create(f"{server}/other.owl"))
            created = manager.createOntology(iri.create("http://example.org/created"))
            return str(refused.value.getMessage()), created.getAxiomCount()

    assert await owlapi.run_java(use_manager) == (f"browl doesn't load {server}/other.owl", 0)
    assert requests == []


@pytest.mark.parametrize(
    "ntriples",
    [b"Error 500: Server Error\n", FRAGMENT + b"<http://example.org/A> <http://exa"],
    ids=["error-text", "cut"],
)
async def test_parse_error(ntriples: bytes) -> None:
    """Check that the OWLAPI parses fragments strictly as N-Triples, so text that isn't raises FragmentError."""
    with pytest.raises(owlapi.FragmentError, match="couldn't parse"):
        await owlapi.run_java(axioms, ntriples)


async def test_java_thread() -> None:
    """Check that Java calls run on one dedicated thread, not the event loop's, and are refused anywhere else."""
    names = {await owlapi.run_java(lambda: threading.current_thread().name) for _ in range(5)}
    assert len(names) == 1
    assert names.pop() != threading.current_thread().name
    with pytest.raises(RuntimeError, match="Java thread"):
        axioms(FRAGMENT)


def test_jvm_settings(robot_jar: Path) -> None:
    """Check that the JVM has robot.jar on its class path, and leaves SIGINT and SIGTERM to Python (-Xrs)."""
    class_path = str(jpype.JClass("java.lang.System").getProperty("java.class.path"))
    assert str(robot_jar.resolve()) in class_path.split(os.pathsep)
    arguments = jpype.JClass("java.lang.management.ManagementFactory").getRuntimeMXBean().getInputArguments()
    assert {"-Xrs", "-XX:+ExitOnOutOfMemoryError"} <= {str(argument) for argument in arguments}


def test_start_jvm_again(robot_jar: Path) -> None:
    """Check that starting the running JVM again with the same robot.jar is allowed, and with another is refused."""
    owlapi.start_jvm(robot_jar, "1g")
    with pytest.raises(RuntimeError, match="without"):
        owlapi.start_jvm(Path(__file__), "1g")


def test_start_jvm_no_robot_jar(tmp_path: Path) -> None:
    """Check that a missing robot.jar is reported by its setting's name."""
    with pytest.raises(FileNotFoundError, match="ROBOT_JAR"):
        owlapi.start_jvm(tmp_path / "robot.jar", "1g")


def test_start_jvm_off_main_thread(monkeypatch: pytest.MonkeyPatch, robot_jar: Path) -> None:
    """Check that the JVM isn't started off the main thread, where JPype's shutdown would hang at exit."""
    monkeypatch.setattr(jpype, "isJVMStarted", lambda: False)
    errors: list[Exception] = []

    def start() -> None:
        try:
            owlapi.start_jvm(robot_jar, "1g")
        except RuntimeError as error:
            errors.append(error)

    thread = threading.Thread(target=start)
    thread.start()
    thread.join()
    assert [str(error) for error in errors] == ["start the JVM on the main thread"]
