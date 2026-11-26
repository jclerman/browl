"""An entity page's Overview tab (DESIGN.md §2.8).

The entity's annotations (`browl.annotations`), with their axiom annotations, and axioms (`browl.axioms`), fetched
concurrently; one derived-database lookup gives the links for both.
"""

import logging
import sqlite3
from collections.abc import Iterable, Mapping
from typing import NamedTuple

from fastapi import Request
from pydantic import BaseModel, ConfigDict

from browl.annotations import (
    AnnotationGroup,
    AnnotationValue,
    IRIValue,
    TooManyAnnotationsError,
    TooManyAxiomAnnotationsError,
    annotated,
    fetch_annotations,
    fetch_value_annotations,
    group_annotations,
    shown_annotations,
)
from browl.axioms import LeftOut, TooManyAxiomsError, entity_axioms, fetch_fragment
from browl.derived import EntityType
from browl.frames import AxiomGroup, frame_groups
from browl.fuseki import SparqlError
from browl.jobs import JobState
from browl.links import EntityLink, read_entity_links
from browl.loading import dataset_name
from browl.names import name_property_iris
from browl.owl import Annotation, AnyAxiom, prefixed_name
from browl.owlapi import FragmentError, run_java
from browl.pages import link_root, read_derived, templates
from browl.precache.core import logged_iri
from browl.rdf import IRI, RDFSyntaxError
from browl.settings import Settings
from browl.tasks import caught, task_group
from browl.usages import ready_store

logger = logging.getLogger(__name__)

MAX_EQUIVALENTS = 1_000
"""The most members of a class's equivalence group a page reads and shows as its implied equivalents, as the group,
unlike the class's axioms, has no bound: a cycle of n subclass axioms makes a group of n."""


class AnnotationsPanel(BaseModel):
    """The entity's annotations.

    Attributes:
        groups: The annotations, by property, in the order shown (`browl.annotations.group_annotations`); None if
            they couldn't be read, or are too many to show, when the page says so.
        too_many: Whether they're too many to show (`browl.annotations.TooManyAnnotationsError`).
        annotations_left_out: Why the values are shown without their axiom annotations, if they are: too many
            (`browl.annotations.TooManyAxiomAnnotationsError`), or the query failed; else None.
    """

    model_config = ConfigDict(frozen=True)

    groups: list[AnnotationGroup] | None
    too_many: bool = False
    annotations_left_out: LeftOut | None = None


class AxiomsPanel(BaseModel):
    """The entity's logical axioms, as a frame's sections (`browl.frames`).

    Attributes:
        groups: The sections, in the order shown (`browl.frames.frame_groups`); None if the axioms couldn't be read, or
            are too many to show, when the page says so.
        too_many: Whether the axioms are too many to show (`browl.axioms.TooManyAxiomsError`).
        more_equivalents: Whether the class's equivalence group has more members than `MAX_EQUIVALENTS`, so not all
            its implied equivalents are shown.
        annotations_left_out: Why the axioms are shown without their annotations, if they are
            (`browl.axioms.Fragment`); else None.
    """

    model_config = ConfigDict(frozen=True)

    groups: list[AxiomGroup] | None
    too_many: bool = False
    more_equivalents: bool = False
    annotations_left_out: LeftOut | None = None


class Overview(BaseModel):
    """An entity's Overview tab: its two panels (U-30), and how the page shows each IRI they mention.

    Attributes:
        annotations: The annotations panel.
        axioms: The logical-axioms panel.
        links: Each IRI the panels mention, the annotations' properties included, by IRI
            (`browl.links.read_entity_links`).
    """

    model_config = ConfigDict(frozen=True)

    annotations: AnnotationsPanel
    axioms: AxiomsPanel
    links: dict[str, EntityLink]


def shown_name(iri: str, links: Mapping[str, EntityLink], text: str | None = None) -> str:
    """Return the name a page shows for an IRI (`templates/links.html`'s `named_iri` shows it).

    That's its link's name if it has a page, else its prefixed name if it's in a standard vocabulary (`rdfs:label`),
    else `text`, the IRI by default.
    """
    link = links.get(iri)
    if link is not None and link.href is not None:
        return link.name
    return prefixed_name(iri) or (iri if text is None else text)


def page_annotations(annotations: Iterable[Annotation], links: Mapping[str, EntityLink]) -> list[AnnotationGroup]:
    """Return axiom annotations as a page shows them (`browl.annotations.shown_annotations`), with `links`' names."""
    return shown_annotations(annotations, lambda each: shown_name(each, links))


class ReadAnnotations(NamedTuple):
    """An entity's annotations as `read_annotations` read them.

    Attributes:
        pairs: The (property, value) pairs, each value with its axiom annotations; None if they can't be shown.
        too_many: Whether they're too many to show.
        annotations_left_out: Why the values have no axiom annotations, if they've been left out; else None.
    """

    pairs: list[tuple[IRI, AnnotationValue]] | None
    too_many: bool = False
    annotations_left_out: LeftOut | None = None


class ReadAxioms(NamedTuple):
    """An entity's axioms as `_read_axioms` read them.

    Attributes:
        axioms: The axioms, with their annotations; None if they can't be shown.
        too_many: Whether they're too many to show.
        annotations_left_out: Why the axioms have no annotations, if they've been left out; else None.
    """

    axioms: list[AnyAxiom] | None
    too_many: bool = False
    annotations_left_out: LeftOut | None = None


# Globals, so `named_iri` and the annotations' sort share one rule; this module is imported with the entity pages.
templates.env.globals["shown_name"] = shown_name
templates.env.globals["page_annotations"] = page_annotations


async def read_overview(request: Request, state: JobState, iri: str) -> Overview:
    """Return an entity's Overview tab, from the store of the job state's ready generation.

    A failure to fetch or parse either panel's data fails only that panel, not the page: the full error is logged (for
    a fragment the OWLAPI couldn't parse, with its parser's log and stack trace), and the page shows a short message
    instead.

    Args:
        request: The request.
        state: The job state the page read its entity with, so the panels show the same generation.
        iri: The entity's IRI.

    Raises:
        NotCurrentError: No generation is ready, or its derived database isn't current (`browl.pages.read_derived`).
    """
    generation, clause = ready_store(state)
    dataset = dataset_name(generation)
    async with task_group() as group:
        annotations_task = group.create_task(read_annotations(request, dataset, iri, clause))
        axioms_task = group.create_task(_read_axioms(request, dataset, iri, clause))
    annotations = annotations_task.result()
    read_axioms = axioms_task.result()
    axioms = read_axioms.axioms
    mentioned = [each for axiom in axioms or () for each in axiom.iris()] + annotation_iris(annotations.pairs)
    settings: Settings = request.app.state.settings
    prefix = settings.reserved_prefix
    root = link_root(request.scope)

    def read_links(connection: sqlite3.Connection) -> tuple[dict[str, EntityLink], list[AxiomGroup] | None, bool]:
        """Read the links, and, as it's CPU-bound, group the axioms here too, off the event loop."""
        if axioms is None:
            return read_entity_links(connection, mentioned, prefix, root), None, False
        equivalents = _read_equivalents(connection, iri)
        more = len(equivalents) > MAX_EQUIVALENTS
        del equivalents[MAX_EQUIVALENTS:]
        links = read_entity_links(connection, [*mentioned, *equivalents], prefix, root)
        return links, frame_groups(axioms, iri, equivalents, lambda each: shown_name(each, links)), more

    links, axiom_groups, more_equivalents = await read_derived(request, read_links, state)
    return Overview(
        annotations=annotations_panel(annotations, links, settings),
        axioms=AxiomsPanel(
            groups=axiom_groups,
            too_many=read_axioms.too_many,
            more_equivalents=more_equivalents,
            annotations_left_out=read_axioms.annotations_left_out,
        ),
        links=links,
    )


async def read_annotations(request: Request, dataset: str, iri: str, clause: str) -> ReadAnnotations:
    """Return an entity's annotations (`browl.annotations.fetch_annotations`), with their axiom annotations.

    The two queries run at the same time. The annotations are None if they can't be shown: too many, or the query
    failed, which is logged. If the axiom annotations' query fails, or they're too many, the values are shown without
    them, which is logged too.
    """
    async with task_group() as group:
        pairs_task = group.create_task(_read_pairs(request, dataset, iri, clause))
        annotations_task = group.create_task(
            caught(
                fetch_value_annotations(request.app.state.sparql, dataset, iri, clause),
                TooManyAxiomAnnotationsError,
                SparqlError,
                RDFSyntaxError,
            )
        )
    pairs, too_many = pairs_task.result()
    if pairs is None:
        return ReadAnnotations(None, too_many=too_many)
    annotations = annotations_task.result()
    if isinstance(annotations, TooManyAxiomAnnotationsError):
        logger.warning(
            "Showing the annotations of %s without their axiom annotations: %s", logged_iri(iri), annotations
        )
        return ReadAnnotations(pairs, annotations_left_out=LeftOut.TOO_MANY)
    if isinstance(annotations, Exception):
        logger.error(
            "Couldn't show the axiom annotations on the annotations of %s", logged_iri(iri), exc_info=annotations
        )
        return ReadAnnotations(pairs, annotations_left_out=LeftOut.FAILED)
    return ReadAnnotations(annotated(pairs, annotations))


async def _read_pairs(
    request: Request, dataset: str, iri: str, clause: str
) -> tuple[list[tuple[IRI, AnnotationValue]] | None, bool]:
    """Return an entity's annotations, or None, and whether they're too many, if they can't be shown."""
    try:
        return await fetch_annotations(request.app.state.sparql, dataset, iri, clause), False
    except TooManyAnnotationsError as error:
        logger.warning("Not showing the annotations of %s: %s", logged_iri(iri), error)
        return None, True
    except (SparqlError, RDFSyntaxError):
        logger.exception("Couldn't show the annotations of %s", logged_iri(iri))
        return None, False


def annotation_iris(annotations: Iterable[tuple[IRI, AnnotationValue]] | None) -> list[str]:
    """Return the IRIs annotations mention, their properties', IRI values' and axiom annotations', for their links."""
    mentioned: list[str] = []
    for property_, value in annotations or ():
        mentioned.append(property_)
        if isinstance(value, IRIValue):
            mentioned.append(value.iri)
        mentioned += (each for annotation in value.annotations for each in annotation.iris())
    return mentioned


def annotations_panel(read: ReadAnnotations, links: Mapping[str, EntityLink], settings: Settings) -> AnnotationsPanel:
    """Return the annotations panel for annotations `read_annotations` read, with their IRIs' links."""
    if read.pairs is None:
        return AnnotationsPanel(groups=None, too_many=read.too_many)
    groups = group_annotations(
        read.pairs, name_property_iris(settings), lambda each: shown_name(each, links), settings.language_ranking
    )
    return AnnotationsPanel(groups=groups, annotations_left_out=read.annotations_left_out)


async def _read_axioms(request: Request, dataset: str, iri: str, clause: str) -> ReadAxioms:
    """Return an entity's axioms (`browl.axioms.entity_axioms`), whether they're too many, and why not annotated.

    The axioms are None if they can't be shown: too many, or reading them failed, which is logged. If the OWLAPI fails
    on the fragment with its annotations, such as a malformed annotation, it's read again without them, which is
    logged too.
    """
    try:
        fragment = await fetch_fragment(request.app.state.sparql, dataset, iri, clause)
        left_out = fragment.annotations_left_out
        try:
            axioms = await run_java(entity_axioms, fragment.ntriples, iri)
        except FragmentError:
            if not fragment.annotations:
                raise
            logger.exception(
                "Showing the axioms of %s without their annotations, which couldn't be read", logged_iri(iri)
            )
            axioms = await run_java(entity_axioms, fragment.axioms, iri)
            left_out = LeftOut.FAILED
    except TooManyAxiomsError as error:
        logger.warning("Not showing the axioms of %s: %s", logged_iri(iri), error)
        return ReadAxioms(None, too_many=True)
    except (SparqlError, RDFSyntaxError, FragmentError):
        logger.exception("Couldn't show the axioms of %s", logged_iri(iri))
        return ReadAxioms(None)
    return ReadAxioms(axioms, annotations_left_out=left_out)


def _read_equivalents(connection: sqlite3.Connection, iri: str) -> list[IRI]:
    """Return the IRIs of the other members of a class's equivalence group (the `equiv_group` table), if it's in one.

    At most `MAX_EQUIVALENTS` + 1 of them, so the caller can tell whether there are more; unsorted, so a huge group's
    query stops early.
    """
    rows = connection.execute(
        "SELECT o.iri FROM entity AS e JOIN equiv_group AS g ON g.entity = e.id"
        " JOIN equiv_group AS m ON m.representative = g.representative JOIN entity AS o ON o.id = m.entity"
        " WHERE e.iri = ? AND e.type = ? AND o.id != e.id LIMIT ?",
        (iri, EntityType.CLASS, MAX_EQUIVALENTS + 1),
    )
    equivalents = []
    for (each,) in rows:
        try:
            equivalents.append(IRI(each))
        except RDFSyntaxError:  # the loaders check IRIs, so it shouldn't happen; but it fails only this line
            logger.warning("Not showing an equivalent of %s with an invalid IRI: %s", logged_iri(iri), logged_iri(each))
    return equivalents
