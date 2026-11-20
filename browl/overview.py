"""An entity page's Overview tab (DESIGN.md §2.8): the entity's annotations, grouped by property, and its axioms.

Both are fetched from Fuseki at once. The annotations are grouped in Python (`browl.annotations`); the axioms are
converted into nodes on the Java thread (`browl.axioms`), for now a plain list until P-16b and P-16c group them. The
templates make their HTML (`templates/annotations.html`, `templates/axioms.html`), with a link for each entity they
mention, all read in one query.
"""

import asyncio
import logging
from dataclasses import dataclass

from fastapi import Request

from browl.annotations import (
    Annotations,
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
from browl.pages import link_root, read_derived
from browl.precache.core import has_loaded_imports, logged_iri
from browl.rdf import IRI, RDFSyntaxError

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AnnotationsPanel:
    """The entity's annotations, and how the page shows each IRI they mention.

    Attributes:
        groups: The annotations, by property, in the order shown (`browl.annotations.group_annotations`); None if
            they couldn't be read, or are too many to show, when the page says so.
        links: Each IRI they mention, properties included, by IRI (`browl.links.read_entity_links`).
        too_many: Whether they're too many to show (`browl.annotations.TooManyAnnotationsError`).
    """

    groups: list[Annotations] | None
    links: dict[str, EntityLink]
    too_many: bool = False


@dataclass(frozen=True)
class AxiomsPanel:
    """The entity's axioms, and how the page shows each IRI they mention.

    Attributes:
        axioms: The axioms (`browl.axioms.entity_axioms`); None if they couldn't be read, or are too many to show, when
            the page says so.
        links: Each IRI the axioms mention, by IRI (`browl.links.read_entity_links`).
        too_many: Whether the axioms are too many to show (`browl.axioms.TooManyAxiomsError`).
    """

    axioms: list[AnyAxiom] | None
    links: dict[str, EntityLink]
    too_many: bool = False


@dataclass(frozen=True)
class Overview:
    """An entity's Overview tab: its two panels (U-30).

    Attributes:
        annotations: The annotations panel.
        axioms: The logical-axioms panel.
    """

    annotations: AnnotationsPanel
    axioms: AxiomsPanel


def shown_name(iri: str, links: dict[str, EntityLink]) -> str:
    """Return the name a page shows for an IRI, as `templates/links.html`'s `named_iri` does.

    That's its link's name if it has a page, else its prefixed name if it's in a standard vocabulary (`rdfs:label`),
    else the IRI.
    """
    link = links.get(iri)
    if link is not None and link.href is not None:
        return link.name
    return prefixed_name(iri) or iri


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
    (annotations, too_many), axioms = await asyncio.gather(
        _read_annotations(request, dataset, iri, clause), _read_axioms(request, dataset, iri, clause)
    )
    mentioned = [each for axiom in axioms.axioms or () for each in axiom.iris()]
    for property_, value in annotations or ():
        mentioned.append(property_)
        if isinstance(value, IRIValue):
            mentioned.append(value.iri)
    prefix: str = request.app.state.settings.reserved_prefix
    root = link_root(request.scope)
    links = await read_derived(
        request, lambda connection: read_entity_links(connection, mentioned, prefix, root), state
    )
    if annotations is None:
        annotations_panel = AnnotationsPanel(None, {}, too_many=too_many)
    else:
        settings = request.app.state.settings
        groups = group_annotations(
            annotations,
            name_property_iris(settings),
            lambda each: shown_name(each, links),
            settings.language_ranking,
        )
        annotations_panel = AnnotationsPanel(groups, links)
    if axioms.axioms is not None:
        axioms = AxiomsPanel(axioms.axioms, links)
    return Overview(annotations_panel, axioms)


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


async def _read_axioms(request: Request, dataset: str, iri: str, clause: str) -> AxiomsPanel:
    """Return an entity's axioms panel, without its links."""
    try:
        fragment = await fetch_fragment(request.app.state.sparql, dataset, iri, clause)
        return AxiomsPanel(await run_java(entity_axioms, fragment, iri), {})
    except TooManyAxiomsError as error:
        logger.warning("Not showing the axioms of %s: %s", logged_iri(iri), error)
        return AxiomsPanel(None, {}, too_many=True)
    except (SparqlError, RDFSyntaxError, FragmentError):
        logger.exception("Couldn't show the axioms of %s", logged_iri(iri))
        return AxiomsPanel(None, {})
