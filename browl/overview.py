"""An entity page's Overview tab (DESIGN.md §2.8): for now, until P-16 groups them, the entity's axioms as a plain list.

The axioms are fetched from Fuseki and converted into nodes on the Java thread (`browl.axioms`); the template makes
their HTML from the nodes' tokens (`templates/axioms.html`), with a link for each entity they mention.
"""

import logging
from dataclasses import dataclass

from fastapi import Request

from browl.axioms import TooManyAxiomsError, entity_axioms, fetch_fragment
from browl.derived import NotCurrentError
from browl.fuseki import UNION_FROM, SparqlError, dataset_clause
from browl.jobs import JobState
from browl.links import EntityLink, read_entity_links
from browl.loading import dataset_name
from browl.owl import Axiom
from browl.owlapi import FragmentError, run_java
from browl.pages import link_root, read_derived
from browl.precache.core import has_loaded_imports, logged_iri
from browl.rdf import RDFSyntaxError

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AxiomsPanel:
    """The entity's axioms, and how the page shows each IRI they mention.

    Attributes:
        axioms: The axioms (`browl.axioms.entity_axioms`); None if they couldn't be read, or are too many to show, when
            the page says so.
        links: Each IRI the axioms mention, by IRI (`browl.links.read_entity_links`).
        too_many: Whether the axioms are too many to show (`browl.axioms.TooManyAxiomsError`).
    """

    axioms: list[Axiom] | None
    links: dict[str, EntityLink]
    too_many: bool = False


async def read_axioms_panel(request: Request, state: JobState, iri: str) -> AxiomsPanel:
    """Return an entity's axioms, from the store of the job state's ready generation, and their links.

    A failure to fetch or parse them fails only this panel, not the page: the full error is logged (for a fragment the
    OWLAPI couldn't parse, with its parser's log and stack trace), and the page shows a short message instead.

    Args:
        request: The request.
        state: The job state the page read its entity with, so the panel shows the same generation.
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
    try:
        fragment = await fetch_fragment(request.app.state.sparql, dataset_name(generation), iri, clause)
        axioms = await run_java(entity_axioms, fragment, iri)
    except TooManyAxiomsError as error:
        logger.warning("Not showing the axioms of %s: %s", logged_iri(iri), error)
        return AxiomsPanel(None, {}, too_many=True)
    except (SparqlError, RDFSyntaxError, FragmentError):
        logger.exception("Couldn't show the axioms of %s", logged_iri(iri))
        return AxiomsPanel(None, {})
    prefix: str = request.app.state.settings.reserved_prefix
    root = link_root(request.scope)
    mentioned = [each for axiom in axioms for each in axiom.iris()]
    links = await read_derived(
        request, lambda connection: read_entity_links(connection, mentioned, prefix, root), state
    )
    return AxiomsPanel(axioms, links)
