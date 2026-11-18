"""List an OWL Functional Syntax (OFN) document's imports and convert it to Turtle, in one OWLAPI parse (§2.4).

Only the OWLAPI reads OFN, so it lists the imports: a separate scan could read a crafted file differently and miss one
that the OWLAPI would then load (PR #70's review). Every document load but the file's own is refused, before anything is
opened, so the parse reads no other file and sends no request. The Turtle written keeps the import declarations, except
relative ones, which can't be fetched; it's what the TDB2 loader and ROBOT read: nothing else reads the OFN file.

The parse holds the whole ontology, so it runs in its own process (`python -m browl.ofn`), with its own heap limit and
time limit, like ROBOT's, never in the app's JVM.
"""

import asyncio
import json
import sys
from dataclasses import dataclass
from pathlib import Path

from browl.jobs import StageError
from browl.process import NO_NETWORK_JAVA_OPTIONS, run_process
from browl.robot import KILLED_STATUS, OUT_OF_MEMORY_STATUS, killed_reason
from browl.settings import Settings

OUT_OF_MEMORY = "OutOfMemoryError"


@dataclass(frozen=True)
class Listing:
    """What a document says about imports, as the parser that reads it next reads it.

    Attributes:
        imports: The IRIs it imports, each once.
        names: IRIs it's known by, which an import of it may name: an OFN document's ontology and version IRIs; an RDF
            document's ontologies that import something.
    """

    imports: list[str]
    names: frozenset[str] = frozenset()


async def convert_ofn(source: Path, output: Path, settings: Settings) -> Listing:
    """Convert an OFN document to Turtle at `output`, returning its imports; on failure, nothing is left there.

    Runs within the reasoner limits (`REASONER_TIMEOUT_S`, `REASONER_MAX_HEAP`), as ROBOT's conversion did, and in the
    sandbox, if there is one (`SANDBOX_SOCKET`).

    Raises:
        StageError: The OWLAPI couldn't read it, or a limit was hit.
    """
    partial = output.with_name(f"{output.stem}.partial{output.suffix}")
    listed = output.with_name(f"{output.stem}.imports.json")
    command = [
        sys.executable,
        "-m",
        "browl.ofn",
        str(settings.robot_jar),
        settings.reasoner_max_heap,
        str(source),
        str(partial),
        str(listed),
    ]
    try:
        completed = await run_process(
            command,
            timeout_s=settings.reasoner_timeout_s,
            watch=[output.parent],
            name="OFN conversion",
            sandbox=settings.sandbox_socket,
        )
        limit = f"REASONER_TIMEOUT_S ({settings.reasoner_timeout_s} s)"
        if reason := completed.limit_reason("the OFN conversion", limit):
            raise StageError(reason)
        if completed.status == OUT_OF_MEMORY_STATUS and any(OUT_OF_MEMORY in line for line in completed.tail):
            raise StageError(
                f"memory limit: the OFN conversion needed more than REASONER_MAX_HEAP ({settings.reasoner_max_heap})"
            )
        if completed.status == KILLED_STATUS:
            raise StageError(killed_reason("the OFN conversion"))
        if not completed.ok:
            raise StageError(f"parse error: the OWLAPI couldn't read it as OWL Functional Syntax:\n{completed.output}")
        found = json.loads(await asyncio.to_thread(listed.read_text, encoding="utf-8"))
        await asyncio.to_thread(partial.replace, output)
    except BaseException:
        await asyncio.to_thread(partial.unlink, missing_ok=True)
        raise
    finally:
        await asyncio.to_thread(listed.unlink, missing_ok=True)
    return Listing(found["imports"], frozenset(found["names"]))


def _main(robot_jar: str, max_heap: str, source: str, output: str, listed: str) -> int:
    """Parse `source`, refusing every other document, save it as Turtle at `output`, and its imports at `listed`.

    The imports, and the ontology's IRI and version IRI, are written to a file, as JSON, since the OWLAPI logs to
    standard output.
    """
    import jpype  # only the child process starts a JVM

    from browl.owlapi import _java, _OnlyDocumentFactory

    jpype.startJVM(f"-Xmx{max_heap}", "-XX:+ExitOnOutOfMemoryError", *NO_NETWORK_JAVA_OPTIONS, classpath=[robot_jar])
    java = _java()
    file_class = jpype.JClass("java.io.File")
    source_ = jpype.JClass("org.semanticweb.owlapi.io.FileDocumentSource")(
        file_class(source), jpype.JClass("org.semanticweb.owlapi.formats.FunctionalSyntaxDocumentFormat")()
    )
    manager = java.OWLManager.createOWLOntologyManager()
    factories = manager.getOntologyFactories()
    [factory] = factories
    factories.set(_OnlyDocumentFactory(factory, source_))
    try:
        ontology = manager.loadOntologyFromOntologyDocument(source_, java.loader_configuration)
    except java.OWLOntologyCreationException as error:
        sys.stderr.write(f"{error.getMessage()}\n")
        return 1
    declarations = list(ontology.getImportsDeclarations())
    for declaration in declarations:
        # A relative import can't be fetched, and the Turtle would resolve it against its base, to an IRI other than
        # the one listed, which the catalog wouldn't map; so it's left out.
        if not declaration.getIRI().isAbsolute():
            manager.applyChange(jpype.JClass("org.semanticweb.owlapi.model.RemoveImport")(ontology, declaration))
    turtle = jpype.JClass("org.semanticweb.owlapi.formats.TurtleDocumentFormat")()
    parsed = manager.getOntologyFormat(ontology)
    if parsed is not None and parsed.isPrefixOWLOntologyFormat():
        turtle.copyPrefixesFrom(parsed.asPrefixOWLOntologyFormat())
    target = jpype.JClass("org.semanticweb.owlapi.io.FileDocumentTarget")(file_class(output))
    manager.saveOntology(ontology, turtle, target)
    ontology_id = ontology.getOntologyID()
    names = [str(iri.get()) for iri in (ontology_id.getOntologyIRI(), ontology_id.getVersionIRI()) if iri.isPresent()]
    found = {"imports": [str(declaration.getIRI()) for declaration in declarations], "names": names}
    Path(listed).write_text(json.dumps(found), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(_main(*sys.argv[1:]))
