"""The OWLAPI in the app's JVM, reached through JPype, with every Java call on one dedicated thread (DESIGN.md §2.1).

The JVM starts once, with the app, and lives as long as the process: JPype can't restart it. One thread makes all Java
calls: S-1 found that parallel threads gave no extra throughput. Java objects stay inside this module and the other
bridge modules, which give the functions that wrap them Python types.
"""

import asyncio
import logging
import os
import threading
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import jpype

logger = logging.getLogger(__name__)

FRAGMENT_IRI = "urn:browl:fragment"
"""Document IRI of each fragment ontology; fragments have no IRI of their own."""

_java_thread: threading.Thread | None = None


def _record_java_thread() -> None:
    global _java_thread
    _java_thread = threading.current_thread()


_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="java", initializer=_record_java_thread)


class FragmentError(Exception):
    """The OWLAPI couldn't parse a fragment."""


@jpype.JImplements("org.semanticweb.owlapi.model.OWLOntologyFactory", deferred=True)
class _OnlyDocumentFactory:
    """An ontology factory that loads one document source, through another factory, and refuses to load any other.

    A pattern match on the N-Triples can't find every import to ignore (an IRI can be written with escapes), so imports
    are refused where they're loaded instead.
    """

    def __init__(self, factory: Any, source: Any) -> None:
        """Load `source`, only, with `factory`."""
        self._factory = factory
        self._source = source

    @jpype.JOverride
    def canLoad(self, source: Any) -> bool:  # noqa: N802
        """Accept every source, to refuse all but one in `loadOWLOntology`.

        A source refused here makes the manager raise an error that the missing-import strategy doesn't cover, so an
        import would fail the whole fragment.
        """
        return True

    @jpype.JOverride
    def canCreateFromDocumentIRI(self, iri: Any) -> bool:  # noqa: N802
        """Pass on: creating an empty ontology reads nothing."""
        return bool(self._factory.canCreateFromDocumentIRI(iri))

    @jpype.JOverride
    def createOWLOntology(self, manager: Any, ontology_id: Any, document_iri: Any, handler: Any) -> Any:  # noqa: N802
        """Pass on: creating an empty ontology reads nothing."""
        return self._factory.createOWLOntology(manager, ontology_id, document_iri, handler)

    @jpype.JOverride
    def loadOWLOntology(self, manager: Any, source: Any, handler: Any, configuration: Any) -> Any:  # noqa: N802
        """Load the one document source, refusing any other (document sources don't override `equals`)."""
        if not source.equals(self._source):
            raise jpype.JClass("org.semanticweb.owlapi.model.OWLOntologyCreationException")(
                f"browl doesn't load {source.getDocumentIRI()}"
            )
        return self._factory.loadOWLOntology(manager, source, handler, configuration)

    @jpype.JOverride
    def setLock(self, lock: Any) -> None:  # noqa: N802
        """Pass the manager's lock on; JPype's proxies can't call this default method themselves."""
        self._factory.setLock(lock)


async def run_java[*Ts, T](function: Callable[[*Ts], T], *args: *Ts) -> T:
    """Call a function on the Java thread, which makes every Java call, and return its result."""
    return await asyncio.get_running_loop().run_in_executor(_executor, function, *args)


def check_java_thread() -> None:
    """Check that the caller is on the Java thread.

    Raises:
        RuntimeError: Called on another thread, such as the event loop's.
    """
    if threading.current_thread() is not _java_thread:
        raise RuntimeError("Java calls must run on the Java thread, through run_java")


def start_jvm(robot_jar: Path, max_heap: str) -> None:
    """Start the JVM with `robot.jar` on its class path and a heap limit, unless this process has started it already.

    Call this on the main thread, at startup: it blocks for about half a second. Java calls then run on the Java thread
    (`run_java`), but the JVM must not start there: JPype's shutdown at exit then waits forever.

    On running out of heap the JVM ends the process, so that the app restarts (its container's restart policy) instead
    of carrying on without JPype's reference-cleanup thread, which the error can also kill (S-1, DESIGN.md §2.4).

    Raises:
        FileNotFoundError: `robot_jar` doesn't exist.
        RuntimeError: The JVM is running without `robot_jar` on its class path, or isn't running and this isn't the
            main thread.
    """
    if not robot_jar.is_file():
        raise FileNotFoundError(f"ROBOT_JAR: no such file: {robot_jar}")
    robot_jar = robot_jar.resolve()
    if jpype.isJVMStarted():
        class_path = str(jpype.JClass("java.lang.System").getProperty("java.class.path"))
        if robot_jar not in {Path(entry).resolve() for entry in class_path.split(os.pathsep)}:
            raise RuntimeError(f"the JVM is already running, without {robot_jar} on its class path")
        logger.info("JVM already running; its heap limit stays as it was")
        return
    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError("start the JVM on the main thread")
    # -Xrs and interrupt=False leave SIGINT and SIGTERM to Python, so Uvicorn shuts down gracefully; with the JVM's own
    # handlers, either signal ends the process at once.
    jpype.startJVM(
        f"-Xmx{max_heap}", "-XX:+ExitOnOutOfMemoryError", "-Xrs", classpath=[str(robot_jar)], interrupt=False
    )
    logger.info("JVM started with -Xmx%s and %s", max_heap, robot_jar)


@contextmanager
def ntriples_ontology(ntriples: bytes) -> Iterator[Any]:
    """Parse N-Triples as a fresh OWLAPI ontology, removed from its manager on leaving the block.

    Call this on the Java thread (`run_java`). Imports aren't followed: a fragment's `owl:imports` axioms are kept, but
    nothing is fetched. The manager's only ontology factory refuses to load any document but the fragment, so each
    import fails to load, silently, without the OWLAPI ever opening its IRI.

    Yields:
        The `OWLOntology`.

    Raises:
        FragmentError: The OWLAPI couldn't parse the N-Triples.
    """
    check_java_thread()
    missing_imports = jpype.JClass("org.semanticweb.owlapi.model.MissingImportHandlingStrategy")
    configuration = jpype.JClass("org.semanticweb.owlapi.model.OWLOntologyLoaderConfiguration")()
    configuration = configuration.setMissingImportHandlingStrategy(missing_imports.SILENT)
    source = jpype.JClass("org.semanticweb.owlapi.io.StreamDocumentSource")(
        jpype.JClass("java.io.ByteArrayInputStream")(ntriples),
        jpype.JClass("org.semanticweb.owlapi.model.IRI").create(FRAGMENT_IRI),
        jpype.JClass("org.semanticweb.owlapi.formats.NTriplesDocumentFormat")(),
        "application/n-triples",
    )
    manager = jpype.JClass("org.semanticweb.owlapi.apibinding.OWLManager").createOWLOntologyManager()
    factories = manager.getOntologyFactories()
    [factory] = factories  # OWLManager's one factory, which loads documents from any source, including IRIs
    factories.set(_OnlyDocumentFactory(factory, source))
    try:
        ontology = manager.loadOntologyFromOntologyDocument(source, configuration)
    except jpype.JClass("org.semanticweb.owlapi.model.OWLOntologyCreationException") as error:
        raise FragmentError(f"the OWLAPI couldn't parse the fragment: {error}") from error
    try:
        yield ontology
    finally:
        manager.removeOntology(ontology)
