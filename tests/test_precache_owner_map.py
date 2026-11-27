"""Tests for the owner-map pre-caching hook (P-27a) on a stubbed Fuseki; tests/test_usages.py uses a real one."""

from collections import Counter
from collections.abc import Sequence
from contextlib import closing
from pathlib import Path

import pytest

from browl.fuseki import UNION_FROM
from browl.jobtypes import ImportResult, StageError
from browl.precache import owner_map as owner_map_module
from browl.precache.core import Hook
from browl.precache.entities import entities
from browl.precache.owner_map import owner_map
from browl.precache.stage import pre_caching_stage
from browl.rdf import RDF, RDFS
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
    connect,
    fuseki,
    job,
    make_settings,
)

pytestmark = pytest.mark.anyio

HOOKS_USED: Sequence[Hook] = (entities, owner_map)
A, B, C, P, N = (f"<{EX}onto#{name}>" for name in ("A", "B", "C", "p", "n"))


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
    """Check the roots the stub's answer gives (`tests.helpers.OWNER_MAP_RESULT`).

    Two are owned by an IRI that isn't an entity, one a reified axiom naming A only in an annotation; A's own n-ary
    DisjointClasses isn't stored.
    """
    queries: list[str] = []
    async with fuseki(queries=queries) as http:
        await pre_caching_stage(job(settings, 1, [ImportResult(f"{EX}i", triples=1)]), http, hooks=HOOKS_USED)
    assert "?node ?predicate" in queries[-1] and UNION_FROM in queries[-1]  # the imports' blank nodes too
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


def entity_result(*rows: tuple[str, str]) -> str:
    """Return an entity-list result of (IRI, OWL type) rows."""
    return ENTITY_HEADER + "".join(f"{iri}\t<{OWL}{kind}>\t\n" for iri, kind in rows) + ENTITY_END


async def test_owner_map_annotations_copies_and_puns(settings: Settings) -> None:
    """Check annotations on a general class axiom or an annotation, a copied source, and a punned property.

    An annotation on a blank node's axiom is part of that node's root, so its owner and own axioms are the root's; a
    copy of a general class axiom's node, as a reified axiom's source, is no root, and what it names isn't the reified
    axiom's usage; a class that's also a property is named by a general class axiom as a property, which makes it a
    usage.
    """
    classes = [(iri, "Class") for iri in (A, B, C, P)]
    known = entity_result(*classes, (P, "ObjectProperty"), (N, "AnnotationProperty"))
    axiom, annotation = f"<{OWL}Axiom>", f"<{OWL}Annotation>"
    type_ = f"<{RDF}type>"
    source, property_, target = (f"<{OWL}annotated{name}>" for name in ("Source", "Property", "Target"))
    subclass = f"<{RDFS}subClassOf>"
    owner_map_rows = rows(
        # A general class axiom, p some A SubClassOf B, annotated with A and C: A's own still, and C's usage.
        ('"g"', f"<{OWL}onProperty>", P, "", ""),
        ('"g"', f"<{OWL}someValuesFrom>", A, "", ""),
        ('"g"', subclass, B, "", ""),
        ('"w"', type_, axiom, "", ""),
        ('"w"', source, "", '"g"', ""),
        ('"w"', property_, subclass, "", ""),
        ('"w"', target, B, "", ""),
        ('"w"', N, A, "", ""),
        ('"w"', N, C, "", ""),
        # A SubClassOf B, annotated with "v", itself annotated with C: the root is A's axiom.
        ('"x"', type_, axiom, "", ""),
        ('"x"', source, A, "", ""),
        ('"x"', property_, subclass, "", ""),
        ('"x"', target, B, "", ""),
        ('"x"', N, '"v"', "", ""),
        ('"y"', type_, annotation, "", ""),
        ('"y"', source, "", '"x"', ""),
        ('"y"', property_, N, "", ""),
        ('"y"', target, '"v"', "", ""),
        ('"y"', N, C, "", ""),
        # A reified axiom whose source is a copy of a general class axiom's node, without its main triple, annotated
        # with C, which the axiom names already: the root is only N's usage, as C is in more than its annotations.
        ('"z"', type_, axiom, "", ""),
        ('"z"', source, "", '"c"', ""),
        ('"z"', property_, subclass, "", ""),
        ('"z"', target, B, "", ""),
        ('"z"', N, C, "", ""),
        ('"c"', f"<{OWL}onProperty>", P, "", ""),
        ('"c"', f"<{OWL}someValuesFrom>", C, "", ""),
    )
    async with fuseki(ONTOLOGY_RESULT, known, owner_map_rows) as http:
        await pre_caching_stage(job(settings, 3), http, hooks=HOOKS_USED)
    roots, refs, blank = tables(settings, 3)
    a, b, c, p, n = (iri.strip("<>") for iri in (A, B, C, P, N))
    assert [(kind, label, owner, predicate) for _, kind, label, owner, _, predicate in roots] == [
        ("general", "g", None, None),
        ("axiom", "x", a, f"{RDFS}subClassOf"),
        ("axiom", "z", None, f"{RDFS}subClassOf"),
    ]
    assert sorted(refs) == sorted([(p, 1), (n, 1), (c, 1), (n, 2), (c, 2), (n, 3)])
    assert blank == [(a, 3, 0), (b, 4, 0), (c, 4, 0), (n, 1, 5), (p, 2, 0)]


async def test_owner_map_walk_budget(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that roots whose walks visit blank nodes more than `WALK_BUDGET` times over fail the stage."""
    monkeypatch.setattr(owner_map_module, "WALK_BUDGET", 0)
    shared = rows(  # two general class axioms that share a node, walked from each
        ('"g"', f"<{OWL}someValuesFrom>", "", '"s"', ""),
        ('"g"', f"<{RDFS}subClassOf>", B, "", ""),
        ('"h"', f"<{OWL}someValuesFrom>", "", '"s"', ""),
        ('"h"', f"<{RDFS}subClassOf>", A, "", ""),
        ('"s"', f"<{OWL}onProperty>", P, "", ""),
    )
    async with fuseki(ONTOLOGY_RESULT, entity_result((A, "Class"), (B, "Class")), shared) as http:
        with pytest.raises(StageError, match=r"share blank nodes too much, over 0 visits"):
            await pre_caching_stage(job(settings, 4), http, hooks=HOOKS_USED)


async def test_owner_map_walk_stops_at_anonymous_individuals(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Check that walks stop at anonymous individuals, so many roots that reach a large component of them are cheap.

    10,000 general class axioms, `(p value _:hub) SubClassOf A`, each reach a cycle of 1,000 anonymous individuals
    linked by p. Walked into, the cycle would cost 10 million visits; with no budget past one visit per node, the stage
    passes only as each walk stops at the hub, whose assertions are roots of their own.
    """
    monkeypatch.setattr(owner_map_module, "WALK_BUDGET", 0)
    referrers, cycle = 10_000, 1_000
    triples: list[tuple[str, str, str, str, str]] = []
    for i in range(referrers):
        triples += [
            (f'"g{i}"', f"<{OWL}hasValue>", "", '"n0"', ""),
            (f'"g{i}"', f"<{OWL}onProperty>", P, "", ""),
            (f'"g{i}"', f"<{RDFS}subClassOf>", A, "", ""),
        ]
    triples += [(f'"n{i}"', P, "", f'"n{(i + 1) % cycle}"', "") for i in range(cycle)]
    entity_rows = entity_result((A, "Class"), (P, "ObjectProperty"))
    async with fuseki(ONTOLOGY_RESULT, entity_rows, rows(*triples)) as http:
        await pre_caching_stage(job(settings, 9), http, hooks=HOOKS_USED)
    roots, refs, _ = tables(settings, 9)
    kinds = Counter(kind for _, kind, _, _, _, _ in roots)
    assert kinds == {"general": referrers, "assertion": cycle}
    # A's own general class axioms aren't its usages; each assertion names p, and the cycle's nodes nothing else.
    assert Counter(iri for iri, _ in refs) == {P.strip("<>"): referrers + cycle}


async def test_owner_map_owl_annotations_on_anonymous_individuals(settings: Settings) -> None:
    """Check that OWL's built-in annotation properties leave a node an anonymous individual, as `rdfs:label` does.

    `_:x Type A` and `_:x owl:deprecated true`, … are its assertions, so A's usage is listed, not one GENERAL root.
    """
    builtin = ("deprecated", "versionInfo", "priorVersion", "backwardCompatibleWith", "incompatibleWith")
    owner_map_rows = rows(
        ('"x"', f"<{RDF}type>", A, "", ""),
        *(('"x"', f"<{OWL}{name}>", '"v"', "", "") for name in builtin),
    )
    async with fuseki(ONTOLOGY_RESULT, entity_result((A, "Class")), owner_map_rows) as http:
        await pre_caching_stage(job(settings, 10), http, hooks=HOOKS_USED)
    roots, refs, _ = tables(settings, 10)
    assert [(kind, label, predicate) for _, kind, label, _, _, predicate in roots] == [("assertion", "x", f"{RDF}type")]
    assert refs == [(A.strip("<>"), 1)]


async def test_owner_map_anonymous_thing(settings: Settings) -> None:
    """Check that a node typed `owl:Thing` is an anonymous individual: its assertions are roots, which name p and B."""
    owner_map_rows = rows(
        ('"x"', f"<{RDF}type>", f"<{OWL}Thing>", "", ""),
        ('"x"', P, B, "", ""),
    )
    async with fuseki(ONTOLOGY_RESULT, entity_result((B, "Class"), (P, "ObjectProperty")), owner_map_rows) as http:
        await pre_caching_stage(job(settings, 11), http, hooks=HOOKS_USED)
    roots, refs, _ = tables(settings, 11)
    assert [(kind, label, predicate) for _, kind, label, _, _, predicate in roots] == [
        ("assertion", "x", P.strip("<>"))
    ]
    assert sorted(iri for iri, _ in refs) == [B.strip("<>"), P.strip("<>")]


async def test_owner_map_annotated_class_expression_assertion(settings: Settings) -> None:
    """Check that a reified axiom on an anonymous individual's class assertion to a class expression is left out.

    Its target is a copy of the expression, which the Usage tab's query couldn't find (DESIGN.md §2.5), so the
    assertion's root names what the expression does, p and B, but not C, in the reified axiom's annotation.
    """
    type_, source, property_, target = (
        f"<{iri}>"
        for iri in (f"{RDF}type", f"{OWL}annotatedSource", f"{OWL}annotatedProperty", f"{OWL}annotatedTarget")
    )
    restriction = [(f"<{OWL}onProperty>", P), (f"<{OWL}someValuesFrom>", B)]
    owner_map_rows = rows(
        ('"x"', type_, "", '"e"', ""),
        *(('"e"', predicate, item, "", "") for predicate, item in restriction),
        ('"c"', type_, f"<{OWL}Axiom>", "", ""),
        ('"c"', source, "", '"x"', ""),
        ('"c"', property_, type_, "", ""),
        ('"c"', target, "", '"f"', ""),
        ('"c"', N, C, "", ""),
        *(('"f"', predicate, item, "", "") for predicate, item in restriction),
    )
    known = entity_result((B, "Class"), (C, "Class"), (P, "ObjectProperty"), (N, "AnnotationProperty"))
    async with fuseki(ONTOLOGY_RESULT, known, owner_map_rows) as http:
        await pre_caching_stage(job(settings, 12), http, hooks=HOOKS_USED)
    roots, refs, _ = tables(settings, 12)
    assert [(kind, label, predicate) for _, kind, label, _, _, predicate in roots] == [("assertion", "x", f"{RDF}type")]
    assert sorted(iri for iri, _ in refs) == [B.strip("<>"), P.strip("<>")]


class CountedEdges(set):
    """A node's edges that count how many times they're iterated."""

    iterations = 0

    def __iter__(self):
        """Count an iteration, and iterate."""
        self.iterations += 1
        return super().__iter__()


def test_owner_map_reified_edges_scanned_once() -> None:
    """Check that finding the roots scans each node's edges a few times, not once per edge of a reified axiom.

    One reified axiom has 1,000 `owl:annotatedSource` edges to anonymous individuals, and 1,000 reified axioms each have
    the same blank node as source.
    """
    many = 1_000
    axiom, annotated = (f"{RDF}type", f"{OWL}Axiom"), f"{OWL}annotated"
    triples = owner_map_module._Triples()
    triples.children["c"] = CountedEdges(
        {axiom, (f"{annotated}Property", f"{RDF}type"), (f"{annotated}Target", A.strip("<>"))}
        | {(f"{annotated}Source", f"_:x{i}") for i in range(many)}
    )
    for i in range(many):
        triples.children[f"x{i}"] = CountedEdges({(f"{RDF}type", A.strip("<>"))})
        triples.children[f"a{i}"] = CountedEdges(
            {axiom, (f"{annotated}Source", "_:s"), (f"{annotated}Property", f"{OWL}onProperty")}
        )
    triples.children["s"] = CountedEdges({(f"{OWL}onProperty", P.strip("<>"))})
    owner_map_module._Walk(triples)
    assert max(edges.iterations for edges in triples.children.values()) <= 4
