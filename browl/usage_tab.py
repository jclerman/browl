"""An entity page's Usage tab (U-26, DESIGN.md §2.8): its usages (`browl.usages`), by subject, `PAGE_USAGES` to a page.

A page counts the entity's usages first. With at most `MAX_STREAMED_USAGES`, it reads them all, sorts them by subject
and takes its slice; with more, the tab shows the count and says it's preparing them, which the usage table (P-27c)
will serve. The page's usages are then fetched as two fragments (`fetch_usage_fragments`) and parsed by the OWLAPI
(`usage_axioms`): one of the usages with a subject, whose axioms are matched to their subjects, and one of those with
none; a third holds the reified axioms that annotate them. Each line is an axiom in Manchester syntax, with its
annotations (U-35). The tab's label carries the count, which the page fetches after it renders (`read_count`), so the
Overview never waits for it.
"""

import asyncio
import hashlib
import logging
import math
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from itertools import batched
from typing import Any, NamedTuple

import jpype
from fastapi import Request
from pydantic import BaseModel, ConfigDict

from browl import owl
from browl.axioms import (
    MAX_FRAGMENT_ANNOTATIONS,
    MAX_FRAGMENT_IRIS,
    LeftOut,
    TooManyAxiomsError,
    axioms_about,
    convert_axioms,
    fragment_iris,
    is_declaration,
    merged_annotations,
    pruned_annotations,
)
from browl.derived import UsageRootKind
from browl.fuseki import CONSTRUCT_MAX_BYTES, ResultTooLargeError, SparqlError, filled_query
from browl.jobs import JobState
from browl.links import LOOKUP_BATCH, EntityLink, read_entity_links
from browl.owlapi import FragmentError, check_java_thread, ntriples_ontology, run_java
from browl.pages import link_root, read_derived
from browl.precache.core import PREFIXES, logged_iri, sparql_file
from browl.precache.usage_table import SubjectGroup, sort_name
from browl.rdf import IRI, BlankNode, Literal, RDFSyntaxError, ntriples_iris, ntriples_term
from browl.tasks import caught, task_group
from browl.usage_tables import TableNotReadyError
from browl.usages import (
    MAX_STREAMED_USAGES,
    TooManyUsagesError,
    Usage,
    UsageCounts,
    UsageSource,
    UsageStore,
    count_usages,
    read_table_page,
    read_usages,
    read_values,
    ready_store,
)

logger = logging.getLogger(__name__)

PAGE_USAGES = 50
"""How many usages a page of the tab shows: one line each, as each is one axiom."""

USAGE_LABEL = "Usage"
"""The tab's label, before its count."""

POLL_S = 5
"""How often a page whose usages are being prepared asks for its tab again, until the usage table is ready."""

_TRIPLES = PREFIXES + sparql_file("usage-triples.rq")
_CLOSURES = PREFIXES + sparql_file("usage-closures.rq")
_REIFIED = PREFIXES + sparql_file("usage-reified.rq")
_ANNOTATIONS = PREFIXES + sparql_file("usage-annotations.rq")
_TYPES = PREFIXES + sparql_file("declaration-types.rq")
_NO_TRIPLES = "VALUES (?subject ?predicate ?object) { }"
_NO_ROOTS = "VALUES ?root { }"
_NO_AXIOMS = "VALUES ?axiom { }"
_NO_ANNOTATED = "VALUES (?subject ?predicate ?object ?digest) { }"
_NO_IRIS = "VALUES ?iri { }"


class PageNotFoundError(LookupError):
    """A page of the tab past its last, which gets a 404."""


class UsageGroup(BaseModel):
    """A subject's usages on a page, or those with no single subject.

    Attributes:
        subject: The IRI whose axioms they are; None for the usages with no single subject, such as general class
            axioms that mention a property.
        continued: Whether the subject's usages started on an earlier page.
        axioms: The usages' axioms, sorted by kind and text (`browl.axioms.convert_axioms`), a symmetric one between
            two written with the subject first (`browl.owl.Axiom.with_first`); with their annotations
            (`browl.owl.Axiom.annotations`).
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
        pages: How many pages there are: of the usages listed, once they're read, as the count can include some that
            aren't (`browl.usages`); else from the count. None if they couldn't be counted.
        groups: The page's usages, by subject, in the order shown: by the subject's display name, case-folded, then IRI;
            then those with no single subject; then the subjects that aren't entities. Empty if none of the usages
            counted can be listed. None if they couldn't be read, are too many to stream (`preparing`), or are too
            large to show (`too_many`), when the page says so.
        preparing: Whether the entity has more usages than `MAX_STREAMED_USAGES`, which wait for the usage table
            (`browl.usage_tables`), so the page polls until it's ready.
        remaining_s: While preparing, about how long the usage table's build has left, in seconds
            (`browl.usage_tables.UsageTables.remaining_s`); None if that isn't known.
        too_many: Whether the page's axioms are too large to show (`fetch_usage_fragments`).
        annotations_left_out: Why the page's axioms are shown without their annotations, if they are
            (`UsageFragments`); else None.
        links: Each IRI the page mentions, by IRI (`browl.links.read_entity_links`).
    """

    model_config = ConfigDict(frozen=True)

    count: int | None
    page: int
    pages: int | None
    groups: list[UsageGroup] | None
    preparing: bool = False
    remaining_s: float | None = None
    too_many: bool = False
    annotations_left_out: LeftOut | None = None
    links: dict[str, EntityLink] = {}


def remaining_text(seconds: float | None) -> str | None:
    """Return how long the usage table's build has left, as the page says it, e.g. "about 3 minutes"; None if unknown.

    Examples:
        >>> remaining_text(130)
        'about 3 minutes'
        >>> remaining_text(20)
        'less than a minute'
    """
    if seconds is None:
        return None
    if seconds < 60:
        return "less than a minute"
    minutes = math.ceil(seconds / 60)
    return f"about {minutes} minute{'s' if minutes > 1 else ''}"


def usage_label(count: int | None) -> str:
    """Return the tab's label, with the count if it's known, e.g. "Usage (1,280)"."""
    return USAGE_LABEL if count is None else f"{USAGE_LABEL} ({count:,})"


def _store(request: Request, state: JobState) -> UsageStore:
    generation, clause = ready_store(state)
    app = request.app.state
    return UsageStore(app.sparql, app.derived, generation, clause, getattr(app, "usage_tables", None))


async def read_count(request: Request, state: JobState, iri: str) -> int | None:
    """Return how many usages an entity has (`browl.usages.count_usages`); None if the count failed, which is logged.

    Raises:
        NotCurrentError: No generation is ready, or its derived database isn't current.
    """
    return await _read_count(request, _store(request, state), iri)


async def _read_count(request: Request, store: UsageStore, iri: str) -> int | None:
    counts: UsageCounts = request.app.state.usage_counts
    try:
        return await count_usages(store, iri, counts)
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
    start = (page - 1) * PAGE_USAGES
    table = None
    if store.tables is not None:
        try:
            table = await read_table_page(store, iri, start, start + PAGE_USAGES)
        except TableNotReadyError:
            pass
        except sqlite3.Error:
            logger.exception("Couldn't read the usage table for %s", logged_iri(iri))
            return UsageTab(count=None, page=page, pages=None, groups=None)
    count = table.count if table is not None else await _read_count(request, store, iri)
    if count is None:
        return UsageTab(count=None, page=page, pages=None, groups=None)
    pages = _pages(table.listed if table is not None else count)  # with the table, no blank pages (below)
    if page > pages:
        raise PageNotFoundError(f"page {page} of {pages}")

    def unshown(preparing: bool = False, too_many: bool = False) -> UsageTab:
        """Return the page without its usages, which can't be shown; the page says why."""
        remaining = store.tables.remaining_s(store.generation) if preparing and store.tables is not None else None
        return UsageTab(
            count=count,
            page=page,
            pages=pages,
            groups=None,
            preparing=preparing,
            remaining_s=remaining,
            too_many=too_many,
        )

    if table is not None:
        chunk = table.usages
        continued = table.previous is not None and bool(chunk) and table.previous.subject == chunk[0].subject
    else:
        if count > MAX_STREAMED_USAGES:
            failed = store.tables is not None and store.tables.failed(store.generation)
            return unshown(preparing=not failed)
        try:
            usages = await read_usages(store, iri, MAX_STREAMED_USAGES)
        except TooManyUsagesError:  # more listed than counted, which the counts' rules don't allow
            logger.warning("Preparing the usages of %s, which has more than its count of %d", logged_iri(iri), count)
            return unshown(preparing=True)
        except (SparqlError, RDFSyntaxError):
            logger.exception("Couldn't read the usages of %s", logged_iri(iri))
            return unshown()
        pages = _pages(len(usages))  # no blank pages for usages counted but not listed
        if page > pages:
            raise PageNotFoundError(f"page {page} of {pages} listed")
        ordered = await read_derived(request, lambda connection: sorted_usages(connection, usages), state)
        chunk = ordered[start : start + PAGE_USAGES]
        continued = bool(chunk) and start > 0 and ordered[start - 1].subject == chunk[0].subject
    try:
        shown, left_out = await read_usage_page(store, iri, chunk, continued)
    except TooManyAxiomsError as error:
        logger.warning("Not showing page %d of the usages of %s: %s", page, logged_iri(iri), error)
        return unshown(too_many=True)
    except (SparqlError, RDFSyntaxError, FragmentError):
        logger.exception("Couldn't show page %d of the usages of %s", page, logged_iri(iri))
        return unshown()
    mentioned: list[str] = [group.subject for group in shown if group.subject is not None]
    mentioned += (each for group in shown for axiom in group.axioms for each in axiom.iris())
    prefix: str = request.app.state.settings.reserved_prefix
    root = link_root(request.scope)
    links = await read_derived(
        request, lambda connection: read_entity_links(connection, mentioned, prefix, root), state
    )
    return UsageTab(count=count, page=page, pages=pages, groups=shown, annotations_left_out=left_out, links=links)


def _pages(usages: int) -> int:
    """Return how many pages that many usages take: at least one, which says if there are none."""
    return max(1, math.ceil(usages / PAGE_USAGES))


class UsagePage(NamedTuple):
    """A page of an entity's usages (`read_usage_page`).

    Attributes:
        groups: The usages, by subject, with their axioms.
        annotations_left_out: Why the axioms are shown without their annotations, if they are; else None.
    """

    groups: list[UsageGroup]
    annotations_left_out: LeftOut | None = None


async def read_usage_page(store: UsageStore, iri: str, chunk: Sequence[Usage], continued: bool) -> UsagePage:
    """Return a page of an entity's usages, by subject, with their axioms.

    Args:
        store: Where the usages were read.
        iri: The entity's IRI.
        chunk: The page's usages, in the tab's order (`sorted_usages`).
        continued: Whether the page's first subject's usages started on an earlier page.

    Raises:
        SparqlError: A query failed.
        RDFSyntaxError: An IRI isn't valid.
        TooManyAxiomsError: The page's fragments are too large, or mention too many IRIs (`fetch_usage_fragments`).
        FragmentError: The OWLAPI couldn't parse a fragment, or convert an axiom (`usage_axioms`).
    """
    groups = _grouped(chunk)
    fragments = await fetch_usage_fragments(store, iri, chunk)
    subjects: dict[str, int] = {subject: len(each) for subject, each in groups if subject is not None}
    by_subject, general = await run_java(
        usage_axioms, fragments.subject, fragments.general, iri, subjects, fragments.annotations
    )
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
    return UsagePage(shown, fragments.annotations_left_out)


def sorted_usages(connection: sqlite3.Connection, usages: Iterable[Usage]) -> list[Usage]:
    """Return usages in the tab's order (`UsageTab.groups`), from the subjects' display names in the derived database.

    A punned IRI's name is its first entity's; one without a display name sorts by its IRI. A subject's usages keep
    one order from request to request, by source, predicate, value (`Usage.index`) and root, so a page always holds
    the same ones. It's the usage table's order (`browl.precache.usage_table`).
    """
    usages = list(usages)
    names = _sort_names(connection, {usage.subject for usage in usages if usage.subject is not None})

    def key(usage: Usage) -> tuple[int, str, str, str, str, int, int]:
        subject = usage.subject
        if subject is None:
            group, name = SubjectGroup.NO_SUBJECT, ""
        elif subject in names:
            group, name = SubjectGroup.NAMED, names[subject]
        else:
            group, name = SubjectGroup.NOT_ENTITY, ""
        root = usage.root.id if usage.root is not None else 0
        return group, name, subject or "", usage.source, usage.predicate or "", usage.index, root

    return sorted(usages, key=key)


def _sort_names(connection: sqlite3.Connection, iris: Iterable[str]) -> dict[str, str]:
    """Return the name each IRI in `entity` sorts by (`browl.precache.usage_table.sort_name`)."""
    names: dict[str, str] = {}
    for batch in batched(sorted(iris), LOOKUP_BATCH):
        rows = connection.execute(
            f"SELECT iri, display_name FROM entity WHERE iri IN ({', '.join('?' * len(batch))}) ORDER BY id", batch
        )
        for iri, display_name in rows:
            if iri not in names:
                names[iri] = sort_name(iri, display_name)
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


class UsageFragments(NamedTuple):
    """The triples from which the OWLAPI can parse a page's usages' axioms, as N-Triples (`fetch_usage_fragments`).

    Attributes:
        subject: The usages with a subject.
        general: The usages with none; empty if there are none.
        annotations: The reified axioms that annotate the usages with a subject; empty if there are none, or they're
            left out.
        annotations_left_out: Why they're left out, if they are; else None.
    """

    subject: bytes
    general: bytes
    annotations: bytes = b""
    annotations_left_out: LeftOut | None = None


def _literal_digest(literal: Literal) -> str:
    """Return the digest `sparql/usage-annotations.rq` matches a literal by: its lexical form, tag and datatype's."""
    return hashlib.sha256(f"{literal.value} {literal.language or ''} {literal.datatype}".encode()).hexdigest()


async def fetch_usage_fragments(store: UsageStore, iri: str, usages: Sequence[Usage]) -> UsageFragments:
    """Return the triples from which the OWLAPI can parse a page's usages' axioms (DESIGN.md §2.8).

    The first fragment holds the usages with a subject: a triple whose terms are known, written here, a property's
    values read first (`browl.usages.read_values`); one with a blank object, fetched (`sparql/usage-triples.rq`);
    and the blank nodes' closures, with the reified axioms and
    axiom annotations they lead to (`sparql/usage-closures.rq`), and the reified axioms' own triples
    (`sparql/usage-reified.rq`). The second holds the closures of the roots with no subject; it's empty if there are
    none. The third holds the reified axioms, and their nested annotations, that may annotate the first's triples
    (`sparql/usage-annotations.rq`), which `usage_axioms` matches to them; if they'd take the fragments past their
    caps, or their query fails, they're left out, which is logged, so the axioms can still be shown. Each has the
    declaration types of every IRI any mentions (`sparql/declaration-types.rq`), and is pruned of cyclic or too deeply
    nested annotations (`browl.axioms.pruned_annotations`), in a thread, off the event loop.

    Args:
        store: Where the usages were read.
        iri: The entity's IRI.
        usages: The page's usages.

    Raises:
        SparqlError: A query failed.
        RDFSyntaxError: An IRI isn't valid.
        TooManyAxiomsError: The first two fragments, or a property's values, come to more than
            `browl.fuseki.CONSTRUCT_MAX_BYTES`, so the OWLAPI's heap can't hold them, or mention more than
            `browl.axioms.MAX_FRAGMENT_IRIS` IRIs, or hold more than `browl.axioms.MAX_FRAGMENT_ANNOTATIONS`
            annotations.
    """
    entity = IRI(iri)
    lines: list[str] = []
    # The queries' VALUES rows: triples with a blank object, and blank nodes, as `<_:label>`; and the triples whose
    # reified axioms are fetched, by their subject, predicate, object and a literal's digest.
    owned: list[str] = []
    closures: list[str] = []
    reified: list[str] = []
    general: list[str] = []
    annotated: dict[str, None] = {}
    try:
        property_values = await read_values(
            store,
            iri,
            {usage.subject for usage in usages if usage.source == UsageSource.PREDICATE and usage.subject is not None},
            max_chars=CONSTRUCT_MAX_BYTES,
        )
    except ResultTooLargeError as error:
        raise TooManyAxiomsError(f"the page's values are too large: {error}") from error
    for usage in usages:
        subject = usage.subject
        if usage.source == UsageSource.OBJECT:
            lines.append(f"<{subject}> <{usage.predicate}> <{entity}> .\n")
            annotated[f"(<{subject}> <{usage.predicate}> <{entity}> UNDEF)"] = None
        elif usage.source == UsageSource.PREDICATE:
            found = property_values.get(subject or "", [])
            if usage.index >= len(found):  # none should be: the values are those the usages were counted from
                logger.warning("No value %d of %s for %s", usage.index, logged_iri(iri), logged_iri(subject or ""))
                continue
            value = found[usage.index]
            if isinstance(value, BlankNode):
                owned.append(f"(<{subject}> <{entity}> <_:{value}>)")
                closures.append(f"<_:{value}>")
                annotated[f"(<{subject}> <{entity}> UNDEF UNDEF)"] = None
            else:
                lines.append(f"<{subject}> <{entity}> {ntriples_term(value)} .\n")
                if isinstance(value, Literal):
                    annotated[f'(<{subject}> <{entity}> UNDEF "{_literal_digest(value)}")'] = None
                else:
                    annotated[f"(<{subject}> <{entity}> <{value}> UNDEF)"] = None
        elif usage.root is not None:
            label = f"<_:{usage.root.label}>"
            if subject is None:
                general.append(label)
            elif usage.root.kind == UsageRootKind.TRIPLE:
                owned.append(f"(<{subject}> <{usage.predicate}> {label})")
                closures.append(label)
                annotated[f"(<{subject}> <{usage.predicate}> UNDEF UNDEF)"] = None
            else:  # a reified axiom
                closures.append(label)
                reified.append(label)
    async with task_group() as group:
        parts = [
            group.create_task(_construct(store, query, empty, rows))
            for query, empty, rows in (
                (_TRIPLES, _NO_TRIPLES, owned),
                (_CLOSURES, _NO_ROOTS, closures),
                (_REIFIED, _NO_AXIOMS, reified),
            )
        ]
        general_task = group.create_task(_construct(store, _CLOSURES, _NO_ROOTS, general))
        annotations_task = group.create_task(
            caught(_construct(store, _ANNOTATIONS, _NO_ANNOTATED, sorted(annotated)), SparqlError)
        )
    subject_triples = "".join(lines).encode() + b"".join(part.result() for part in parts)
    general_triples, annotation_triples, left_out = general_task.result(), b"", None
    if isinstance(result := annotations_task.result(), ResultTooLargeError):
        left_out = LeftOut.TOO_MANY
    elif isinstance(result, SparqlError):
        logger.error(
            "Showing usages of %s without their annotations, which couldn't be read", logged_iri(iri), exc_info=result
        )
        left_out = LeftOut.FAILED
    else:
        annotation_triples = result
    fragments, mentioned = await asyncio.to_thread(_checked, subject_triples, general_triples, annotation_triples)
    left_out = left_out or fragments.annotations_left_out
    if left_out == LeftOut.TOO_MANY:
        logger.warning("Showing usages of %s without their annotations, which are too many", logged_iri(iri))
    if mentioned:
        values = [f"<{each}>" for each in sorted(mentioned)]
        types = await store.sparql.construct(store.dataset, _filled(store, _TYPES, _NO_IRIS, values))
        fragments = UsageFragments(
            fragments.subject + types,
            fragments.general + types if fragments.general else b"",
            fragments.annotations + types if fragments.annotations else b"",
        )
    return fragments._replace(annotations_left_out=left_out)


def _checked(subject: bytes, general: bytes, annotations: bytes) -> tuple[UsageFragments, set[str]]:
    """Return a page's fragments pruned (`browl.axioms.pruned_annotations`), and the IRIs they mention.

    The annotations are left out, too many, if they'd take the fragments past their caps (`fetch_usage_fragments`).

    Raises:
        TooManyAxiomsError: The fragments are too large without the annotations.
    """
    # Each query's result is capped, but not the literals written here, nor the results together.
    if (size := len(subject) + len(general)) > CONSTRUCT_MAX_BYTES:
        raise TooManyAxiomsError(f"the page's fragments come to {size:,} bytes, more than {CONSTRUCT_MAX_BYTES:,}")
    (subject, in_subject), (general, in_general) = pruned_annotations(subject), pruned_annotations(general)
    if (count := in_subject + in_general) > MAX_FRAGMENT_ANNOTATIONS:
        raise TooManyAxiomsError(
            f"the page's axioms have {count:,} annotations, more than {MAX_FRAGMENT_ANNOTATIONS:,}"
        )
    mentioned = fragment_iris(subject, general, what="the page")
    too_many = UsageFragments(subject, general, annotations_left_out=LeftOut.TOO_MANY), mentioned
    if not annotations:
        return UsageFragments(subject, general), mentioned
    if size + len(annotations) > CONSTRUCT_MAX_BYTES:
        return too_many
    annotations, more = pruned_annotations(annotations)
    with_annotations = mentioned | ntriples_iris(annotations)
    if count + more > MAX_FRAGMENT_ANNOTATIONS or len(with_annotations) > MAX_FRAGMENT_IRIS:
        return too_many
    return UsageFragments(subject, general, annotations), with_annotations


async def _construct(store: UsageStore, query: str, empty: str, rows: Sequence[str]) -> bytes:
    """Return a CONSTRUCT or DESCRIBE query's result, with `rows` in its empty `VALUES` block; none without rows."""
    if not rows:
        return b""
    return await store.sparql.construct(store.dataset, _filled(store, query, empty, rows))


def _filled(store: UsageStore, query: str, empty: str, rows: Iterable[str]) -> str:
    """Return a query with its dataset clause, and rows in its empty `VALUES` block."""
    return filled_query(query, store.dataset_clause, {empty: empty.replace("{ }", f"{{ {' '.join(rows)} }}")})


def usage_axioms(
    subject_triples: bytes,
    general_triples: bytes,
    iri: str,
    subjects: Mapping[str, int],
    annotation_triples: bytes = b"",
) -> tuple[dict[str, list[owl.AnyAxiom]], list[owl.AnyAxiom]]:
    """Parse a page's fragments (`fetch_usage_fragments`) and return its axioms, by subject, and those with none.

    An axiom of the first fragment goes to the subject it's about (`browl.axioms.axioms_about`); one about two of the
    page's subjects, such as `DisjointClasses(A, B)`, to the first that has fewer axioms than usages. Axioms about none
    of them (none should be) are logged, and go with those of no subject. Each keeps its annotations, and gains those
    of the third fragment's axioms that are the same without their annotations. Those are parsed apart, as the OWLAPI
    makes an axiom of a reified one even without its triple, and the query fetches some that aren't the page's: the
    subject's others with a blank object and the same predicate.

    Call this on the Java thread (`browl.owlapi.run_java`).

    Args:
        subject_triples: The usages with a subject, as N-Triples.
        general_triples: The usages with none; empty if there are none.
        iri: The entity's IRI.
        subjects: The page's subjects, in order, with how many usages each has on the page.
        annotation_triples: The reified axioms that may annotate the first's; empty if there are none.

    Raises:
        FragmentError: The OWLAPI couldn't parse a fragment, or failed on it with any other Java exception, or an axiom
            holds an object of a kind no node type covers, or nests too deep.
    """
    check_java_thread()
    found: dict[str, list[Any]] = {subject: [] for subject in subjects}
    unclaimed: list[Any] = []
    general: list[Any] = []
    extra: dict[Any, list[Any]] = {}
    try:
        if subjects:
            with ntriples_ontology(subject_triples) as ontology:
                unclaimed = _assign(ontology, subjects, found)
            if annotation_triples:
                with ntriples_ontology(annotation_triples) as ontology:
                    for axiom in ontology.getAxioms():
                        if axiom.isAnnotated():
                            extra.setdefault(axiom.getAxiomWithoutAnnotations(), []).extend(axiom.getAnnotations())
        if general_triples:
            with ntriples_ontology(general_triples) as ontology:
                general = [axiom for axiom in ontology.getAxioms() if _is_usage(axiom)]
        if unclaimed:
            logger.warning(
                "Showing %d axioms of the usages of %s with no subject, as they're about none of the page's subjects",
                len(unclaimed),
                logged_iri(iri),
            )
        by_subject = {
            subject: _usage_nodes(merged_annotations(axioms, extra), subject) for subject, axioms in found.items()
        }
        return by_subject, _usage_nodes([*general, *merged_annotations(unclaimed, extra)])
    except jpype.JException as error:
        raise FragmentError(f"the OWLAPI failed on a Usage tab's fragment: {error}") from error


def _is_usage(axiom: Any) -> bool:
    """Return whether a fragment's axiom can be a usage: any but a declaration without annotations.

    The declaration types every page fetches (`fetch_usage_fragments`) are such; an annotated declaration is a usage of
    the entity its annotations name.
    """
    return axiom.isAnnotated() or not is_declaration(axiom)


def _assign(ontology: Any, subjects: Mapping[str, int], found: dict[str, list[Any]]) -> list[Any]:
    """Add each axiom that can be a usage (`_is_usage`) to its subject's in `found`; return those about none."""
    claims: dict[Any, list[str]] = {}
    for subject in subjects:
        for axiom in axioms_about(ontology, subject):
            claims.setdefault(axiom, []).append(subject)
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
        owner = next((each for each in owners if len(found[each]) < subjects[each]), owners[0])
        found[owner].append(axiom)
    return unclaimed


def _usage_nodes(axioms: Iterable[Any], subject: str | None = None) -> list[owl.AnyAxiom]:
    """Return axioms as nodes, with their annotations.

    With a subject, a symmetric axiom between two is written with the subject first (`browl.owl.Axiom.with_first`), and
    the axioms sorted again by kind and text, as `convert_axioms` sorts them.
    """
    nodes = convert_axioms(axioms)
    if subject is None:
        return nodes
    return sorted((node.with_first(subject) for node in nodes), key=lambda node: (node.kind, node.manchester))
