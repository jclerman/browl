"""An entity page's Usage tab (U-26, DESIGN.md §2.8): its usages (`browl.usages`), by subject, `PAGE_USAGES` to a page.

A page counts the entity's usages first. With at most `MAX_STREAMED_USAGES`, it reads them all, sorts them by subject
and takes its slice; with more, the tab shows the count and says it's preparing them, which the usage table (P-27c)
will serve. The page's usages are then fetched as two fragments (`fetch_usage_fragments`) and parsed by the OWLAPI
(`usage_axioms`): one of the usages with a subject, whose axioms are matched to their subjects, and one of those with
none. Each line is an axiom in Manchester syntax; one found only through its annotations shows them too. The tab's
label carries the count, which the page fetches after it renders (`read_count`), so the Overview never waits for it.
"""

import logging
import math
import sqlite3
from collections.abc import Iterable, Sequence
from itertools import batched
from typing import Any

import jpype
from fastapi import Request
from pydantic import BaseModel, ConfigDict

from browl import owl
from browl.axioms import MAX_FRAGMENT_IRIS, TooManyAxiomsError, axioms_about, convert_axioms, is_declaration
from browl.derived import UsageRootKind
from browl.fuseki import SparqlError, filled_query
from browl.jobs import JobState
from browl.links import EntityLink, read_entity_links
from browl.overview import ready_store
from browl.owlapi import FragmentError, check_java_thread, ntriples_ontology, run_java
from browl.pages import link_root, read_derived
from browl.precache.core import PREFIXES, logged_iri, sparql_file
from browl.rdf import IRI, BlankNode, RDFSyntaxError, ntriples_iris, ntriples_term
from browl.tasks import task_group
from browl.usages import (
    MAX_STREAMED_USAGES,
    TooManyUsagesError,
    Usage,
    UsageCounts,
    UsageSource,
    UsageStore,
    count_usages,
    read_usages,
)

logger = logging.getLogger(__name__)

PAGE_USAGES = 50
"""How many usages a page of the tab shows: one line each, as each is one axiom."""

USAGE_LABEL = "Usage"
"""The tab's label, before its count."""

_TRIPLES = PREFIXES + sparql_file("usage-triples.rq")
_CLOSURES = PREFIXES + sparql_file("usage-closures.rq")
_REIFIED = PREFIXES + sparql_file("usage-reified.rq")
_TYPES = PREFIXES + sparql_file("declaration-types.rq")
_NO_TRIPLES = "VALUES (?subject ?predicate ?object) { }"
_NO_ROOTS = "VALUES ?root { }"
_NO_AXIOMS = "VALUES ?axiom { }"
_NO_IRIS = "VALUES ?iri { }"
_BATCH = 500  # the most IRIs one SQLite query looks up, well under its limit on a statement's parameters
_NAMED, _NO_SUBJECT, _NOT_ENTITY = range(3)  # the order of a usage's subject's group


class PageNotFoundError(LookupError):
    """A page of the tab past its last, which gets a 404."""


class UsageGroup(BaseModel):
    """A subject's usages on a page, or those with no single subject.

    Attributes:
        subject: The IRI whose axioms they are; None for the usages with no single subject, such as general class
            axioms that mention a property.
        continued: Whether the subject's usages started on an earlier page.
        axioms: The usages' axioms, sorted by kind and text (`browl.axioms.convert_axioms`), a symmetric one between
            two written with the subject first (`browl.owl.Axiom.with_first`); with their annotations only where the
            usage is found through them alone (`browl.owl.Axiom.annotations`).
    """

    model_config = ConfigDict(frozen=True)

    subject: IRI | None
    continued: bool = False
    axioms: list[owl.AnyAxiom]


class UsageTab(BaseModel):
    """A page of an entity's Usage tab, and how the page shows each IRI it mentions.

    Attributes:
        count: How many usages the entity has (`browl.usages.count_usages`); None if they couldn't be counted.
        page: The page's number, from 1.
        pages: How many pages there are, from the count; None if they couldn't be counted.
        groups: The page's usages, by subject, in the order shown: by the subject's display name, case-folded, then IRI;
            then those with no single subject; then the subjects that aren't entities. None if they couldn't be read,
            are too many to stream (`preparing`), or mention too many IRIs (`too_many`), when the page says so.
        preparing: Whether the entity has more usages than `MAX_STREAMED_USAGES`, which wait for the usage table
            (P-27c).
        too_many: Whether the page's axioms mention more IRIs than `browl.axioms.MAX_FRAGMENT_IRIS`.
        links: Each IRI the page mentions, by IRI (`browl.links.read_entity_links`).
    """

    model_config = ConfigDict(frozen=True)

    count: int | None
    page: int
    pages: int | None
    groups: list[UsageGroup] | None
    preparing: bool = False
    too_many: bool = False
    links: dict[str, EntityLink] = {}


def usage_label(count: int | None) -> str:
    """Return the tab's label, with the count if it's known, e.g. "Usage (1,280)"."""
    return USAGE_LABEL if count is None else f"{USAGE_LABEL} ({count:,})"


def _store(request: Request, state: JobState) -> UsageStore:
    generation, clause = ready_store(state)
    return UsageStore(request.app.state.sparql, request.app.state.derived, generation, clause)


async def read_count(request: Request, state: JobState, iri: str) -> int | None:
    """Return how many usages an entity has (`browl.usages.count_usages`); None if the count failed, which is logged.

    Raises:
        NotCurrentError: No generation is ready, or its derived database isn't current.
    """
    counts: UsageCounts = request.app.state.usage_counts
    try:
        return await count_usages(_store(request, state), iri, counts)
    except (SparqlError, RDFSyntaxError):
        logger.exception("Couldn't count the usages of %s", logged_iri(iri))
        return None


async def read_usage_tab(request: Request, state: JobState, iri: str, page: int) -> UsageTab:
    """Return a page of an entity's Usage tab, from the store of the job state's ready generation.

    A failure to read, fetch or parse the usages fails only the tab's content, not the page: the full error is logged,
    and the page shows a short message instead.

    Args:
        request: The request.
        state: The job state the page read its entity with, so the tab shows the same generation.
        iri: The entity's IRI.
        page: The page's number, from 1.

    Raises:
        PageNotFoundError: The page is past the last.
        NotCurrentError: No generation is ready, or its derived database isn't current.
    """
    store = _store(request, state)
    count = await read_count(request, state, iri)
    if count is None:
        return UsageTab(count=None, page=page, pages=None, groups=None)
    pages = max(1, math.ceil(count / PAGE_USAGES))
    if page > pages:
        raise PageNotFoundError(f"page {page} of {pages}")
    preparing = UsageTab(count=count, page=page, pages=pages, groups=None, preparing=True)
    if count > MAX_STREAMED_USAGES:
        return preparing
    try:
        usages = await read_usages(store, iri, MAX_STREAMED_USAGES)
    except TooManyUsagesError:  # more listed than counted, which the counts' rules don't allow
        logger.warning("Preparing the usages of %s, which has more than its count of %d", logged_iri(iri), count)
        return preparing
    except (SparqlError, RDFSyntaxError):
        logger.exception("Couldn't read the usages of %s", logged_iri(iri))
        return UsageTab(count=count, page=page, pages=pages, groups=None)
    ordered = await store.derived.read(store.generation, lambda connection: sorted_usages(connection, usages))
    try:
        shown = await read_usage_page(store, iri, ordered, page)
    except TooManyAxiomsError as error:
        logger.warning("Not showing page %d of the usages of %s: %s", page, logged_iri(iri), error)
        return UsageTab(count=count, page=page, pages=pages, groups=None, too_many=True)
    except (SparqlError, RDFSyntaxError, FragmentError):
        logger.exception("Couldn't show page %d of the usages of %s", page, logged_iri(iri))
        return UsageTab(count=count, page=page, pages=pages, groups=None)
    mentioned: list[str] = [group.subject for group in shown if group.subject is not None]
    mentioned += (each for group in shown for axiom in group.axioms for each in axiom.iris())
    prefix: str = request.app.state.settings.reserved_prefix
    root = link_root(request.scope)
    links = await read_derived(
        request, lambda connection: read_entity_links(connection, mentioned, prefix, root), state
    )
    return UsageTab(count=count, page=page, pages=pages, groups=shown, links=links)


async def read_usage_page(store: UsageStore, iri: str, ordered: Sequence[Usage], page: int) -> list[UsageGroup]:
    """Return a page of an entity's usages, by subject, with their axioms.

    Args:
        store: Where the usages were read.
        iri: The entity's IRI.
        ordered: All its usages, in the tab's order (`sorted_usages`).
        page: The page's number, from 1.

    Raises:
        SparqlError: A query failed.
        RDFSyntaxError: An IRI isn't valid.
        TooManyAxiomsError: The page's fragments mention too many IRIs (`fetch_usage_fragments`).
        FragmentError: The OWLAPI couldn't parse a fragment, or convert an axiom (`usage_axioms`).
    """
    start = (page - 1) * PAGE_USAGES
    chunk = ordered[start : start + PAGE_USAGES]
    groups = _grouped(chunk)
    subject_triples, general_triples = await fetch_usage_fragments(store, iri, chunk)
    subjects = [(subject, len(each)) for subject, each in groups if subject is not None]
    by_subject, general = await run_java(usage_axioms, subject_triples, general_triples, iri, subjects)
    continued = bool(chunk) and start > 0 and ordered[start - 1].subject == chunk[0].subject
    shown = [
        UsageGroup(
            subject=subject,
            continued=continued and index == 0,
            axioms=general if subject is None else by_subject[subject],
        )
        for index, (subject, _) in enumerate(groups)
    ]
    if general and all(subject is not None for subject, _ in groups):  # unclaimed axioms (`usage_axioms`)
        shown.append(UsageGroup(subject=None, axioms=general))
    return shown


def sorted_usages(connection: sqlite3.Connection, usages: Iterable[Usage]) -> list[Usage]:
    """Return usages in the tab's order (`UsageTab.groups`), from the subjects' display names in the derived database.

    A punned IRI's name is its first entity's; one without a display name sorts by its IRI. A subject's usages keep
    one order from request to request, by source, predicate, object and root, so a page always holds the same ones.
    """
    usages = list(usages)
    names = _sort_names(connection, {usage.subject for usage in usages if usage.subject is not None})

    def key(usage: Usage) -> tuple[int, str, str, str, str, str, int]:
        subject = usage.subject
        if subject is None:
            group, name = _NO_SUBJECT, ""
        elif subject in names:
            group, name = _NAMED, names[subject].casefold()
        else:
            group, name = _NOT_ENTITY, ""
        root = usage.root.id if usage.root is not None else 0
        object_ = "" if usage.object is None else repr(usage.object)
        return group, name, subject or "", usage.source, usage.predicate or "", object_, root

    return sorted(usages, key=key)


def _sort_names(connection: sqlite3.Connection, iris: Iterable[str]) -> dict[str, str]:
    """Return the name each IRI in `entity` sorts by: its first entity's display name, else its IRI."""
    names: dict[str, str] = {}
    for batch in batched(sorted(iris), _BATCH):
        rows = connection.execute(
            f"SELECT iri, display_name FROM entity WHERE iri IN ({', '.join('?' * len(batch))}) ORDER BY id", batch
        )
        for iri, display_name in rows:
            names.setdefault(iri, display_name or iri)
    return names


def _grouped(usages: Sequence[Usage]) -> list[tuple[IRI | None, list[Usage]]]:
    """Return sorted usages in runs by subject."""
    groups: list[tuple[IRI | None, list[Usage]]] = []
    for usage in usages:
        if groups and groups[-1][0] == usage.subject:
            groups[-1][1].append(usage)
        else:
            groups.append((usage.subject, [usage]))
    return groups


async def fetch_usage_fragments(store: UsageStore, iri: str, usages: Sequence[Usage]) -> tuple[bytes, bytes]:
    """Return, as N-Triples, the triples from which the OWLAPI can parse a page's usages' axioms (DESIGN.md §2.8).

    The first fragment holds the usages with a subject: a triple whose terms the usage holds, written here; one with a
    blank object, fetched (`sparql/usage-triples.rq`); and the blank nodes' closures, with the reified axioms and
    axiom annotations they lead to (`sparql/usage-closures.rq`), and the reified axioms' own triples
    (`sparql/usage-reified.rq`). The second holds the closures of the roots with no subject; it's empty if there are
    none. Each has the declaration types of every IRI either mentions (`sparql/declaration-types.rq`).

    Args:
        store: Where the usages were read.
        iri: The entity's IRI.
        usages: The page's usages.

    Raises:
        SparqlError: A query failed.
        RDFSyntaxError: An IRI isn't valid.
        TooManyAxiomsError: The fragments mention more than `browl.axioms.MAX_FRAGMENT_IRIS` IRIs.
    """
    entity = IRI(iri)
    lines: list[str] = []
    # The queries' VALUES rows: triples with a blank object, and blank nodes, as `<_:label>`.
    owned: list[str] = []
    closures: list[str] = []
    reified: list[str] = []
    general: list[str] = []
    for usage in usages:
        subject = usage.subject
        if usage.source == UsageSource.OBJECT:
            lines.append(f"<{subject}> <{usage.predicate}> <{entity}> .\n")
        elif usage.source == UsageSource.PREDICATE:
            if isinstance(usage.object, BlankNode):
                owned.append(f"(<{subject}> <{entity}> <_:{usage.object}>)")
                closures.append(f"<_:{usage.object}>")
            elif usage.object is not None:
                lines.append(f"<{subject}> <{entity}> {ntriples_term(usage.object)} .\n")
        elif usage.root is not None:
            label = f"<_:{usage.root.label}>"
            if subject is None:
                general.append(label)
            elif usage.root.kind == UsageRootKind.TRIPLE:
                owned.append(f"(<{subject}> <{usage.predicate}> {label})")
                closures.append(label)
            else:  # a reified axiom
                closures.append(label)
                reified.append(label)
    queries = [
        (_TRIPLES, _NO_TRIPLES, owned, True),
        (_CLOSURES, _NO_ROOTS, closures, True),
        (_REIFIED, _NO_AXIOMS, reified, True),
        (_CLOSURES, _NO_ROOTS, general, False),
    ]
    async with task_group() as group:
        tasks = [
            (group.create_task(store.sparql.construct(store.dataset, _filled(store, query, empty, values))), own)
            for query, empty, values, own in queries
            if values
        ]
    subject_triples = "".join(lines).encode() + b"".join(task.result() for task, own in tasks if own)
    general_triples = b"".join(task.result() for task, own in tasks if not own)
    mentioned = ntriples_iris(subject_triples) | ntriples_iris(general_triples)
    if len(mentioned) > MAX_FRAGMENT_IRIS:
        raise TooManyAxiomsError(f"the page mentions {len(mentioned)} IRIs, more than {MAX_FRAGMENT_IRIS}")
    if mentioned:
        values = [f"<{each}>" for each in sorted(mentioned)]
        types = await store.sparql.construct(store.dataset, _filled(store, _TYPES, _NO_IRIS, values))
        subject_triples += types
        general_triples += types if general_triples else b""
    return subject_triples, general_triples


def _filled(store: UsageStore, query: str, empty: str, rows: Iterable[str]) -> str:
    """Return a query with its dataset clause, and rows in its empty `VALUES` block."""
    return filled_query(query, store.dataset_clause, {empty: empty.replace("{ }", f"{{ {' '.join(rows)} }}")})


def usage_axioms(
    subject_triples: bytes, general_triples: bytes, iri: str, subjects: Sequence[tuple[str, int]]
) -> tuple[dict[str, list[owl.AnyAxiom]], list[owl.AnyAxiom]]:
    """Parse a page's fragments (`fetch_usage_fragments`) and return its axioms, by subject, and those with none.

    An axiom of the first fragment goes to the subject it's about (`browl.axioms.axioms_about`); one about two of the
    page's subjects, such as `DisjointClasses(A, B)`, to the first that has fewer axioms than usages. Axioms about none
    of them (none should be) are logged, and go with those of no subject. An axiom keeps its annotations only if the
    entity is in them and not in the rest of the axiom: the usage is found through them alone.

    Call this on the Java thread (`browl.owlapi.run_java`).

    Args:
        subject_triples: The usages with a subject, as N-Triples.
        general_triples: The usages with none; empty if there are none.
        iri: The entity's IRI.
        subjects: The page's subjects, each once, with how many usages each has on the page.

    Raises:
        FragmentError: The OWLAPI couldn't parse a fragment, or failed on it with any other Java exception, or an axiom
            holds an object of a kind no node type covers, or nests too deep.
    """
    check_java_thread()
    found: dict[str, list[Any]] = {subject: [] for subject, _ in subjects}
    unclaimed: list[Any] = []
    general: list[Any] = []
    try:
        if subjects:
            with ntriples_ontology(subject_triples) as ontology:
                unclaimed = _assign(ontology, subjects, found)
        if general_triples:
            with ntriples_ontology(general_triples) as ontology:
                general = [axiom for axiom in ontology.getAxioms() if _is_usage(axiom)]
        if unclaimed:
            logger.warning(
                "Showing %d axioms of the usages of %s with no subject, as they're about none of the page's subjects",
                len(unclaimed),
                logged_iri(iri),
            )
        by_subject = {subject: _usage_nodes(axioms, iri, subject) for subject, axioms in found.items()}
        return by_subject, _usage_nodes([*general, *unclaimed], iri)
    except jpype.JException as error:
        raise FragmentError(f"the OWLAPI failed on a Usage tab's fragment: {error}") from error


def _is_usage(axiom: Any) -> bool:
    """Return whether a fragment's axiom can be a usage: any but a declaration without annotations.

    The declaration types every page fetches (`fetch_usage_fragments`) are such; an annotated declaration is a usage of
    the entity its annotations name.
    """
    return axiom.isAnnotated() or not is_declaration(axiom)


def _assign(ontology: Any, subjects: Sequence[tuple[str, int]], found: dict[str, list[Any]]) -> list[Any]:
    """Add each axiom that can be a usage (`_is_usage`) to its subject's in `found`; return those about none."""
    claims: dict[Any, list[str]] = {}
    for subject, _ in subjects:
        for axiom in axioms_about(ontology, subject):
            claims.setdefault(axiom, []).append(subject)
    needed = dict(subjects)
    shared, unclaimed = [], []
    for axiom in ontology.getAxioms():
        if not _is_usage(axiom):
            continue
        owners = claims.get(axiom)
        if owners is None:
            unclaimed.append(axiom)
        elif len(owners) == 1:
            found[owners[0]].append(axiom)
        else:
            shared.append((axiom, owners))
    for axiom, owners in shared:
        owner = next((each for each in owners if len(found[each]) < needed[each]), owners[0])
        found[owner].append(axiom)
    return unclaimed


def _usage_nodes(axioms: Iterable[Any], iri: str, subject: str | None = None) -> list[owl.AnyAxiom]:
    """Return axioms as nodes, each with its annotations only if the entity is in them and not in the rest.

    With a subject, a symmetric axiom between two is written with the subject first (`browl.owl.Axiom.with_first`), and
    the axioms sorted again by kind and text, as `convert_axioms` sorts them.
    """
    nodes = [
        node.without_annotations() if node.annotations and iri in node.without_annotations().iris() else node
        for node in convert_axioms(axioms)
    ]
    if subject is None:
        return nodes
    return sorted((node.with_first(subject) for node in nodes), key=lambda node: (node.kind, node.manchester))
