"""The ontology header, for the derived database's `meta` row (DESIGN.md §2.5, The ontology's IRI).

The `meta` component of the derived database's build keys covers this module's import closure and `SETTINGS`
(`browl.precache.keys`).
"""

import logging
from collections.abc import Iterable
from dataclasses import dataclass

from browl.derived import Meta
from browl.fuseki import FusekiClient
from browl.jobs import JobContext, StageError
from browl.names import BUILTIN_PREFIXES, best_values, expand_curie
from browl.precache.core import PREFIXES
from browl.rdf import IRI, Literal, RDFSyntaxError, Term
from browl.settings import Settings

logger = logging.getLogger(__name__)

SETTINGS = frozenset({"extra_prefixes", "language_ranking", "name_properties"})
"""The `Settings` fields the header reads."""

ONTOLOGY_HEADER = """SELECT ?ontology ?property ?value
WHERE {{
  ?ontology rdf:type owl:Ontology .
  FILTER isIRI(?ontology)
  FILTER NOT EXISTS {{ ?importer owl:imports ?ontology }}
  OPTIONAL {{ ?ontology ?property ?value FILTER (?property IN ({properties})) }}
}}"""
"""The ontology's IRI and its header values for `OntologyHeader` (`{properties}`: the properties' IRIs, in `<>`),
from its own (default) graph: an ontology it imports, as an OFN upload's converted Turtle may declare, is left out,
and so are an import's titles, in its own named graph."""

HEADER_MAX_ROWS = 10_000
"""The most rows the ontology header query may return, a few per ontology in practice: an upload that declares
millions of ontologies, or values, fails pre-caching instead of holding the job lock while they're all read."""

_LOGGED_IRIS = 10  # the most other ontology IRIs a warning lists

OWL_VERSION_IRI = "http://www.w3.org/2002/07/owl#versionIRI"
OWL_VERSION_INFO = "http://www.w3.org/2002/07/owl#versionInfo"
DCTERMS_TITLE = "http://purl.org/dc/terms/title"
DC_TITLE = "http://purl.org/dc/elements/1.1/title"


@dataclass(frozen=True)
class OntologyHeader:
    """What the ontology says about itself, for the `meta` row; each None if it doesn't say.

    Attributes:
        iri: The ontology's IRI.
        version_iri: Its version IRI.
        title: Its display name (`ontology_header`); pages fall back to the IRI without one.
        version_info: Its `owl:versionInfo`, the lexical form.
    """

    iri: str | None = None
    version_iri: str | None = None
    title: str | None = None
    version_info: str | None = None


def title_properties(settings: Settings) -> list[str]:
    """Return the IRIs of the properties a title is taken from, best first: dcterms:title, dc:title, `NAME_PROPERTIES`.

    A name property's CURIE is expanded with the built-in prefixes and `EXTRA_PREFIXES`; one that's still not an IRI is
    left out, with a warning.
    """
    prefixes = BUILTIN_PREFIXES | settings.extra_prefixes
    properties = [DCTERMS_TITLE, DC_TITLE]
    for name in settings.name_properties:
        try:
            properties.append(IRI(expand_curie(name, prefixes)))
        except RDFSyntaxError:
            logger.warning("The name property %r isn't an IRI or a known CURIE; it's left out of the title", name)
    return list(dict.fromkeys(properties))


async def ontology_header(sparql: FusekiClient, dataset: str, settings: Settings) -> OntologyHeader:
    """Return the ontology's IRI, version IRI, title and version info, from its own (default) graph.

    If the default graph declares several ontologies (none importing another), the first IRI in order is taken, so the
    choice is stable, and a warning logged; likewise for several version IRIs. The title is a literal of the first
    property in `title_properties` that has one, chosen by language (`LANGUAGE_RANKING`, `browl.names.language_rank`)
    then lexical order; a warning is logged if several tie on language. The version info is a literal too, chosen the
    same way, with a warning if there are several.

    Raises:
        SparqlError: The query failed, or its result is incomplete.
        StageError: The query returned more than `HEADER_MAX_ROWS` rows.
    """
    titles = title_properties(settings)
    properties = ", ".join(f"<{iri}>" for iri in [OWL_VERSION_IRI, OWL_VERSION_INFO, *titles])
    iri: str | None = None
    others: set[str] = set()
    values: dict[str, list[Term]] = {}  # iri's
    async with sparql.bulk_select(dataset, ONTOLOGY_HEADER.format(properties=properties), PREFIXES) as stream:
        columns = [stream.variables.index(name) for name in ("ontology", "property", "value")]
        count = 0
        async for row in stream:
            count += 1
            if count > HEADER_MAX_ROWS:
                raise StageError(f"the ontology header query returned more than {HEADER_MAX_ROWS:,} rows")
            ontology, prop, value = (row[column] for column in columns)
            if ontology is None:
                continue
            if iri is None or str(ontology) < iri:
                if iri is not None:
                    others.add(iri)
                iri, values = str(ontology), {}
            elif ontology != iri:
                others.add(str(ontology))
                continue
            if prop is not None and value is not None:
                values.setdefault(str(prop), []).append(value)
    if iri is None:
        return OntologyHeader()
    if others:
        logger.warning(
            "The ontology declares %d ontology IRIs; taking %r, not %r%s",
            len(others) + 1,
            iri,
            sorted(others)[:_LOGGED_IRIS],
            " …" if len(others) > _LOGGED_IRIS else "",
        )

    versions = sorted({str(version) for version in values.get(OWL_VERSION_IRI, []) if isinstance(version, IRI)})
    if len(versions) > 1:
        logger.warning("The ontology %r has several version IRIs; taking %r", iri, versions[0])

    title = None
    ranking = settings.language_ranking
    for title_property in titles:
        best = best_values(_literals(values.get(title_property, [])), ranking)
        if best:
            title = best[0]
            if len(best) > 1:
                logger.warning(
                    "The ontology %r has several titles (%s) of the best language; taking %r",
                    iri,
                    title_property,
                    title,
                )
            break

    infos = _literals(values.get(OWL_VERSION_INFO, []))
    best_infos = best_values(infos, ranking)
    if len({info.value for info in infos}) > 1:
        logger.warning(
            "The ontology %r has several owl:versionInfo values %r; taking %r",
            iri,
            sorted({info.value for info in infos}),
            best_infos[0],
        )
    return OntologyHeader(iri, versions[0] if versions else None, title, best_infos[0] if best_infos else None)


def _literals(values: Iterable[Term]) -> list[Literal]:
    return [value for value in values if isinstance(value, Literal)]


async def header_meta(context: JobContext, sparql: FusekiClient, dataset: str) -> Meta:
    """Return a job's `meta` row: its generation, reasoner and start time, and the ontology header (`ontology_header`).

    Raises:
        SparqlError: The query failed, or its result is incomplete.
        StageError: The query returned more than `HEADER_MAX_ROWS` rows.
    """
    header = await ontology_header(sparql, dataset, context.settings)
    return Meta(
        context.generation,
        context.reasoner,
        context.started_at,
        header.iri,
        header.version_iri,
        header.title,
        header.version_info,
    )
