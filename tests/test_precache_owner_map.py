"""Tests for the owner-map pre-caching hook (P-27a) on a stubbed Fuseki; tests/test_usages.py uses a real one."""

from collections.abc import Sequence
from contextlib import closing
from pathlib import Path

import pytest

from browl.fuseki import UNION_FROM
from browl.jobtypes import ImportResult
from browl.precache.core import Hook
from browl.precache.entities import entities
from browl.precache.owner_map import owner_map
from browl.precache.stage import pre_caching_stage
from browl.settings import Settings
from tests.helpers import (
    ENTITY_END,
    ENTITY_HEADER,
    EX,
    LABEL,
    ONTOLOGY_RESULT,
    OWL,
    OWNER_MAP_END,
    OWNER_MAP_HEADER,
    OWNER_MAP_RESULT,
    connect,
    fuseki,
    job,
    make_settings,
)

pytestmark = pytest.mark.anyio

HOOKS_USED: Sequence[Hook] = (entities, owner_map)
RDF = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
RDFS = "http://www.w3.org/2000/01/rdf-schema#"
A, B, P = f"<{EX}onto#A>", f"<{EX}onto#B>", f"<{EX}onto#p>"


@pytest.fixture
def settings(data_dir: Path) -> Settings:
    """Return the test settings, with `DATA_DIR` an empty directory."""
    return make_settings()


def tables(settings: Settings, generation: int) -> tuple[list[tuple], list[tuple[str, int]], list[tuple]]:
    """Return `usage_root`, `usage_ref` (by IRI) and `usage_blank` (by IRI)."""
    with closing(connect(settings, generation, HOOKS_USED)) as connection:
        roots = connection.execute(
            "SELECT r.id, r.kind, r.label, o.iri, r.owner_iri, r.predicate FROM usage_root AS r"
            " LEFT JOIN entity AS o ON o.id = r.owner ORDER BY r.id"
        ).fetchall()
        refs = connection.execute(
            "SELECT e.iri, f.root FROM usage_ref AS f JOIN entity AS e ON e.id = f.entity ORDER BY f.entity, f.root"
        ).fetchall()
        blank = connection.execute(
            "SELECT e.iri, b.object, b.predicate FROM usage_blank AS b JOIN entity AS e ON e.id = b.entity"
            " ORDER BY e.iri"
        ).fetchall()
    return roots, refs, blank


async def test_owner_map(settings: Settings) -> None:
    """Check the roots the stub's answer gives (`OWNER_MAP_RESULT`).

    Two are owned by an IRI that isn't an entity, one a reified axiom naming A only in an annotation; A's own n-ary
    DisjointClasses isn't stored.
    """
    queries: list[str] = []
    async with fuseki(queries=queries) as http:
        await pre_caching_stage(job(settings, 1, [ImportResult(f"{EX}i", triples=1)]), http, hooks=HOOKS_USED)
    assert "?node ?predicate" in queries[-1] and UNION_FROM in queries[-1]  # the imports' blank nodes too
    assert OWNER_MAP_RESULT  # the stub's
    roots, refs, blank = tables(settings, 1)
    assert roots == [
        (1, "triple", "r", None, f"{EX}x", f"{RDFS}subClassOf"),
        (2, "axiom", "a", None, f"{EX}x", None),
    ]
    assert refs == [(f"{EX}onto#A", 1), (f"{EX}onto#A", 2)]
    assert blank == [(f"{EX}onto#A", 3, 0)]  # someValuesFrom, the annotation's value, rdf:first


def rows(*triples: tuple[str, str, str, str, str]) -> str:
    """Return an owner-map result of (node, predicate, object, blank, parent) rows, each cell in TSV syntax."""
    return OWNER_MAP_HEADER + "".join("\t".join(row) + "\t\n" for row in triples) + OWNER_MAP_END


async def test_owner_map_shared_and_own(settings: Settings) -> None:
    """Check shared blank nodes, repeated rows, owners, reified axioms' parts, and a property's unowned axioms.

    A blank node shared by two subjects' axioms is a root each; rows given twice count once; a root isn't a usage of
    its owner, nor a reified axiom one of its source, property or target, whose blank target is no root; a property's
    n-ary axiom is its own, and a general class axiom that mentions it its usage.
    """
    entity_rows = "".join(
        f"{iri}\t<{OWL}{kind}>\t\n" for iri, kind in [(A, "Class"), (B, "Class"), (P, "ObjectProperty")]
    )
    some = ('"s"', f"<{OWL}someValuesFrom>", B, "", "")
    owner_map_rows = rows(
        # A SubClassOf p some B and B SubClassOf p some B share the node; B's own isn't its usage.
        ('"s"', f"<{RDFS}subClassOf>", "", "", A),
        ('"s"', f"<{RDFS}subClassOf>", "", "", B),
        ('"s"', f"<{OWL}onProperty>", P, "", ""),
        some,
        some,
        # A reified axiom of A SubClassOf B, annotated with B: B is its target, counted from the main triple.
        ('"x"', f"<{RDF}type>", f"<{OWL}Axiom>", "", ""),
        ('"x"', f"<{OWL}annotatedSource>", A, "", ""),
        ('"x"', f"<{OWL}annotatedProperty>", f"<{RDFS}subClassOf>", "", ""),
        ('"x"', f"<{OWL}annotatedTarget>", B, "", ""),
        ('"x"', LABEL, B, "", ""),
        # A reified axiom whose target is a copy of its main triple's blank node, which isn't a root.
        ('"y"', f"<{RDF}type>", f"<{OWL}Axiom>", "", ""),
        ('"y"', f"<{OWL}annotatedSource>", A, "", ""),
        ('"y"', f"<{OWL}annotatedTarget>", "", '"t"', ""),
        ('"t"', f"<{OWL}someValuesFrom>", B, "", ""),
        # DisjointObjectProperties(p, …): p's own. A general class axiom: p's usage, but not A's or B's.
        ('"d"', f"<{RDF}type>", f"<{OWL}AllDisjointProperties>", "", ""),
        ('"d"', f"<{OWL}members>", "", '"m"', ""),
        ('"m"', f"<{RDF}first>", P, "", ""),
        ('"m"', f"<{RDF}rest>", f"<{RDF}nil>", "", ""),
        ('"g"', f"<{OWL}onProperty>", P, "", ""),
        ('"g"', f"<{OWL}someValuesFrom>", A, "", ""),
        ('"g"', f"<{RDFS}subClassOf>", B, "", ""),
    )
    async with fuseki(ONTOLOGY_RESULT, f"{ENTITY_HEADER}{entity_rows}{ENTITY_END}", owner_map_rows) as http:
        await pre_caching_stage(job(settings, 2), http, hooks=HOOKS_USED)
    roots, refs, blank = tables(settings, 2)
    a, b, p = (iri.strip("<>") for iri in (A, B, P))
    assert [(kind, label, owner) for _, kind, label, owner, _, _ in roots] == [
        ("triple", "s", a),
        ("triple", "s", b),
        ("general", "g", None),
    ]
    assert refs == [(b, 1), (p, 1), (p, 2), (p, 3)]  # x names B only as its target, so it's no usage
    assert blank == [(a, 3, 0), (b, 5, 0), (p, 3, 0)]
