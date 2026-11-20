"""An entity page's Overview tab (DESIGN.md §2.8).

The entity's annotations (`browl.annotations`) and axioms (`browl.axioms`), fetched concurrently; one derived-database
lookup gives the links for both.
"""

import asyncio
import logging
from collections.abc import Mapping

from fastapi import Request
from pydantic import BaseModel, ConfigDict

from browl.annotations import (
    AnnotationGroup,
    AnnotationValue,
    IRIValue,
    TooManyAnnotationsError,
    fetch_annotations,
    group_annotations,
)
from browl.axioms import TooManyAxiomsError, entity_axioms, fetch_fragment
from browl.derived import NotCurrentError
from browl.fuseki import UNION_FROM, SparqlError, dataset_clause
from browl.jobs import JobState
from browl.links import EntityLink, read_entity_links
from browl.loading import dataset_name
from browl.names import name_property_iris
from browl.owl import AnyAxiom, prefixed_name
from browl.owlapi import FragmentError, run_java
from browl.pages import link_root, read_derived, templates
from browl.precache.core import has_loaded_imports, logged_iri
from browl.rdf import IRI, RDFSyntaxError
from browl.settings import Settings

logger = logging.getLogger(__name__)


class AnnotationsPanel(BaseModel):
    """The entity's annotations.

    Attributes:
        groups: The annotations, by property, in the order shown (`browl.annotations.group_annotations`); None if
            they couldn't be read, or are too many to show, when the page says so.
        too_many: Whether they're too many to show (`browl.annotations.TooManyAnnotationsError`).
    """

    model_config = ConfigDict(frozen=True)

    groups: list[AnnotationGroup] | None
    too_many: bool = False


class AxiomsPanel(BaseModel):
    """The entity's axioms.

    Attributes:
        axioms: The axioms (`browl.axioms.entity_axioms`); None if they couldn't be read, or are too many to show, when
            the page says so.
        too_many: Whether the axioms are too many to show (`browl.axioms.TooManyAxiomsError`).
    """

    model_config = ConfigDict(frozen=True)

    axioms: list[AnyAxiom] | None
    too_many: bool = False


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


# A global, so `named_iri` and the annotations' sort share one rule; this module is imported with the entity pages.
templates.env.globals["shown_name"] = shown_name


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
    generation = state.ready_generation
    if generation is None:
        raise NotCurrentError("no generation is ready")
    # Pages are served only once the latest job is ready, so its imports are the ready generation's; if a new job has
    # started meanwhile, seeing the imports' graphs too is always right, if slower.
    clause = dataset_clause(has_loaded_imports(state.imports)) if state.generation == generation else UNION_FROM
    dataset = dataset_name(generation)
    try:
        # A task group, unlike `gather`, cancels the other read when one fails unexpectedly.
        async with asyncio.TaskGroup() as group:
            annotations_task = group.create_task(_read_annotations(request, dataset, iri, clause))
            axioms_task = group.create_task(_read_axioms(request, dataset, iri, clause))
    except ExceptionGroup as errors:
        raise errors.exceptions[0]  # noqa: B904 -- the read's own error, with the group as its context
    annotations, too_many_annotations = annotations_task.result()
    axioms, too_many_axioms = axioms_task.result()
    mentioned = [each for axiom in axioms or () for each in axiom.iris()]
    for property_, value in annotations or ():
        mentioned.append(property_)
        if isinstance(value, IRIValue):
            mentioned.append(value.iri)
    settings: Settings = request.app.state.settings
    prefix = settings.reserved_prefix
    root = link_root(request.scope)
    links = await read_derived(
        request, lambda connection: read_entity_links(connection, mentioned, prefix, root), state
    )
    groups = None
    if annotations is not None:
        groups = group_annotations(
            annotations,
            name_property_iris(settings),
            lambda each: shown_name(each, links),
            settings.language_ranking,
        )
    return Overview(
        annotations=AnnotationsPanel(groups=groups, too_many=too_many_annotations),
        axioms=AxiomsPanel(axioms=axioms, too_many=too_many_axioms),
        links=links,
    )


async def _read_annotations(
    request: Request, dataset: str, iri: str, clause: str
) -> tuple[list[tuple[IRI, AnnotationValue]] | None, bool]:
    """Return an entity's annotations (`browl.annotations.fetch_annotations`), and whether they're too many.

    The annotations are None if they can't be shown: too many, or the query failed, which is logged.
    """
    try:
        return await fetch_annotations(request.app.state.sparql, dataset, iri, clause), False
    except TooManyAnnotationsError as error:
        logger.warning("Not showing the annotations of %s: %s", logged_iri(iri), error)
        return None, True
    except (SparqlError, RDFSyntaxError):
        logger.exception("Couldn't show the annotations of %s", logged_iri(iri))
        return None, False


async def _read_axioms(request: Request, dataset: str, iri: str, clause: str) -> tuple[list[AnyAxiom] | None, bool]:
    """Return an entity's axioms (`browl.axioms.entity_axioms`), and whether they're too many.

    The axioms are None if they can't be shown: too many, or reading them failed, which is logged.
    """
    try:
        fragment = await fetch_fragment(request.app.state.sparql, dataset, iri, clause)
        return await run_java(entity_axioms, fragment, iri), False
    except TooManyAxiomsError as error:
        logger.warning("Not showing the axioms of %s: %s", logged_iri(iri), error)
        return None, True
    except (SparqlError, RDFSyntaxError, FragmentError):
        logger.exception("Couldn't show the axioms of %s", logged_iri(iri))
        return None, False
