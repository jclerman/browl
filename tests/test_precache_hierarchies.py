"""Tests for the hierarchies pre-caching hook (P-12): equivalence groups, edges, and child and descendant counts."""

import random
import uuid
from collections.abc import Sequence
from contextlib import closing
from pathlib import Path

import httpx2
import pytest

from browl.fuseki import UNION_FROM
from browl.jobs import JobContext
from browl.jobtypes import ImportResult, Reasoner
from browl.loading import dataset_name
from browl.precache.core import Hook
from browl.precache.entities import entities
from browl.precache.hierarchies import count_descendants, group_nodes, hierarchies, strongly_connected
from browl.precache.stage import HOOKS, pre_caching_stage
from browl.settings import Settings
from tests.helpers import (
    EDGE_END,
    EDGE_HEADER,
    END,
    ENTITY_END,
    ENTITY_HEADER,
    HEADER,
    OWL,
    connect,
    fuseki,
    job,
    make_settings,
)

pytestmark = pytest.mark.anyio

EX = "http://example.org/"
RDFS = "http://www.w3.org/2000/01/rdf-schema#"
SUB_CLASS = f"<{RDFS}subClassOf>"
SUB_PROPERTY = f"<{RDFS}subPropertyOf>"
EQUIVALENT = f"<{OWL}equivalentClass>"
HOOKS_USED: Sequence[Hook] = (entities, hierarchies)

Tables = tuple[set[tuple[str, str, str]], dict[str, str], dict[tuple[str, str], tuple[int, int]]]


def tables(settings: Settings, generation: int, hooks: Sequence[Hook] = HOOKS_USED) -> Tables:
    """Return the `edge`, `equiv_group` and `counts` tables, by IRI.

    The edges are (hierarchy, parent, child); the groups {member: representative}; the counts
    {(hierarchy, node): (children, descendants)}.
    """
    with closing(connect(settings, generation, hooks)) as connection:
        iri = dict(connection.execute("SELECT id, iri FROM entity"))
        edges = {(h, iri[p], iri[c]) for h, p, c in connection.execute("SELECT hierarchy, parent, child FROM edge")}
        groups = {iri[e]: iri[r] for e, r in connection.execute("SELECT entity, representative FROM equiv_group")}
        counts = {
            (h, iri[n]): (c, d)
            for h, n, c, d in connection.execute("SELECT hierarchy, node, children, descendants FROM counts")
        }
    return edges, groups, counts


def entity_result(*entities: tuple[str, str]) -> str:
    """Return the entity list's result for these (local name, OWL class name) pairs, in `EX`."""
    return ENTITY_HEADER + "".join(f"<{EX}{name}>\t<{OWL}{kind}>\t\n" for name, kind in entities) + ENTITY_END


def edge_result(*edges: tuple[str, str, str]) -> str:
    """Return the edges query's result for these (child, relation, parent) rows; names without `:` are in `EX`."""

    def term(name: str) -> str:
        return name if name.startswith("<") else f"<{EX}{name}>"

    return EDGE_HEADER + "".join(f"{term(c)}\t{r}\t{term(p)}\t\n" for c, r, p in edges) + EDGE_END


def ex(*names: str) -> list[str]:
    """Return the IRIs of these names in `EX`."""
    return [f"{EX}{name}" for name in names]


# Unit tests of the counting and grouping.


def test_multiple_parents() -> None:
    """Check that a node with two parents counts under both (U-8)."""
    # 0 and 1 are both parents of 2, which has the child 3.
    children, descendants = count_descendants(4, [(2, 0), (2, 1), (3, 2)])
    assert children == [1, 1, 1, 0]
    assert descendants == [2, 2, 1, 0]


def test_diamond() -> None:
    """Check that a diamond's bottom counts once under the shared ancestor (U-8)."""
    # 0 is the top; 1 and 2 its children; 3 the child of both.
    children, descendants = count_descendants(4, [(1, 0), (2, 0), (3, 1), (3, 2)])
    assert children == [2, 1, 1, 0]
    assert descendants == [3, 1, 1, 0]


def test_count_descendants_rejects_cycle() -> None:
    """Check that counting fails on a cycle, which `group_nodes` removes first."""
    with pytest.raises(ValueError, match="cycle"):
        count_descendants(3, [(0, 1), (1, 2), (2, 1)])


def test_count_descendants_matches_closure() -> None:
    """Check the counts against a plain transitive closure on random DAGs."""
    rng = random.Random(12)
    for _ in range(50):
        count = rng.randint(1, 40)
        edges = sorted({(c, p) for c in range(count) for p in range(c) if rng.random() < 0.15})
        below: list[set[int]] = [set() for _ in range(count)]
        for node in reversed(range(count)):  # a child's number is higher than its parents'; go from the highest
            for child, parent in edges:
                if parent == node:
                    below[node] |= below[child] | {child}
        children, descendants = count_descendants(count, edges)
        assert descendants == [len(each) for each in below]
        assert children == [sum(1 for _, p in edges if p == node) for node in range(count)]


def test_group_nodes_equivalent() -> None:
    """Check that equivalent entities form one node, the lowest number, whose edges are its members' (U-9)."""
    # 1 ≡ 3; 0 is the parent of 1; 2 the child of 3, and 4 of both 1 and 3.
    nodes = group_nodes(5, [(1, 0), (2, 3), (4, 1), (4, 3)], [(3, 1)])
    assert nodes.representative == [0, 1, 2, 1, 4]
    assert nodes.size == [1, 2, 1, 0, 1]
    assert nodes.edges == [(1, 0), (2, 1), (4, 1)]  # each once
    assert nodes.cycles == 0
    assert count_descendants(5, nodes.edges)[1] == [3, 2, 0, 0, 0]  # 1 and 3 count once under 0


def test_group_nodes_cycles() -> None:
    """Check that the members of a cycle of edges form one node, as do cycles that a merge makes."""
    # 0 → 1 → 2 → 0 is a cycle; 3 ≡ 4, and 3 ⊑ 5 ⊑ 4 is a cycle only once they're merged; 6 is under 2.
    nodes = group_nodes(7, [(0, 1), (1, 2), (2, 0), (3, 5), (5, 4), (6, 2)], [(3, 4)])
    assert nodes.representative == [0, 0, 0, 3, 3, 3, 6]
    assert nodes.edges == [(6, 0)]
    assert nodes.cycles == 2


def test_strongly_connected_deep() -> None:
    """Check that a chain deeper than Python's recursion limit, closed into a cycle, is one component."""
    count = 50_000
    [component] = strongly_connected([(n, n + 1) for n in range(count - 1)] + [(count - 1, 0)])
    assert sorted(component) == list(range(count))
    assert strongly_connected([(n, n + 1) for n in range(count - 1)]) == []


# The hook, against a stubbed Fuseki.


async def test_hierarchies(data_dir: Path) -> None:
    """Check the tables for multiple parents, a diamond, equivalent classes and roots under `owl:Thing`.

    An edge whose ends aren't both declared with the hierarchy's type, or that ends at a top entity, is left out; a
    sub-property edge makes no class edge, and a subclass edge no property edge.
    """
    settings = make_settings()
    classes = [(name, "Class") for name in ("Top", "Left", "Right", "Bottom", "Same", "Alias", "Child", "Free")]
    properties = [("p", "ObjectProperty"), ("q", "ObjectProperty"), ("d", "DatatypeProperty")]
    properties += [("a", "AnnotationProperty"), ("b", "AnnotationProperty"), ("Top", "ObjectProperty")]
    thing = f"<{OWL}Thing>"
    edges = [
        ("Top", SUB_CLASS, thing),  # a root
        ("Left", SUB_CLASS, "Top"),
        ("Right", SUB_CLASS, "Top"),
        ("Bottom", SUB_CLASS, "Left"),
        ("Bottom", SUB_CLASS, "Right"),  # a diamond
        ("Same", SUB_CLASS, "Left"),
        ("Alias", EQUIVALENT, "Same"),  # Alias and Same are one node
        ("Child", SUB_CLASS, "Alias"),
        ("Child", SUB_CLASS, "Same"),
        ("Free", SUB_CLASS, "undeclared"),  # so Free is a root
        ("q", SUB_PROPERTY, "p"),
        ("q", SUB_PROPERTY, f"<{OWL}topObjectProperty>"),
        ("q", SUB_PROPERTY, "d"),  # different hierarchies
        ("Top", SUB_PROPERTY, "p"),  # punned: an object property here
        ("b", SUB_PROPERTY, "a"),
        ("b", SUB_CLASS, "a"),  # not a class edge
    ]
    queries: list[str] = []
    async with fuseki(HEADER + END, entity_result(*classes, *properties), edge_result(*edges), queries=queries) as http:
        await pre_caching_stage(job(settings, 1, [ImportResult(f"{EX}i", triples=1)]), http, hooks=HOOKS_USED)
    assert UNION_FROM in queries[2]  # the imports' edges too
    found_edges, groups, counts = tables(settings, 1)
    top, left, right, bottom, same, alias, child, free = ex(
        "Top", "Left", "Right", "Bottom", "Same", "Alias", "Child", "Free"
    )
    p, q, a, b = ex("p", "q", "a", "b")
    assert found_edges == {
        ("class", top, left),
        ("class", top, right),
        ("class", left, bottom),
        ("class", right, bottom),
        ("class", left, alias),  # the group's representative is its first IRI
        ("class", alias, child),
        ("object_property", p, q),
        ("object_property", p, top),
        ("annotation_property", a, b),
    }
    assert groups == {alias: alias, same: alias}
    assert counts == {
        ("class", top): (2, 5),  # Bottom once; Alias and Same as one
        ("class", left): (2, 3),
        ("class", right): (1, 1),
        ("class", bottom): (0, 0),
        ("class", alias): (1, 1),
        ("class", child): (0, 0),
        ("class", free): (0, 0),
        ("object_property", p): (2, 2),
        ("object_property", q): (0, 0),
        ("object_property", top): (0, 0),
        ("data_property", f"{EX}d"): (0, 0),
        ("annotation_property", a): (1, 1),
        ("annotation_property", b): (0, 0),
    }


async def test_top_entities_left_out(data_dir: Path) -> None:
    """Check that `owl:Thing` is in no hierarchy, even if declared, so its children are roots."""
    settings = make_settings()
    entity_list = ENTITY_HEADER + f"<{OWL}Thing>\t<{OWL}Class>\t\n<{EX}A>\t<{OWL}Class>\t\n" + ENTITY_END
    async with fuseki(HEADER + END, entity_list, edge_result(("A", SUB_CLASS, f"<{OWL}Thing>"))) as http:
        await pre_caching_stage(job(settings, 1), http, hooks=HOOKS_USED)
    assert tables(settings, 1) == (set(), {}, {("class", f"{EX}A"): (0, 0)})


async def test_cycle(data_dir: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Check that classes in a subclass cycle form one node, with a warning."""
    settings = make_settings()
    classes = [(name, "Class") for name in ("A", "B", "C")]
    edges = [("A", SUB_CLASS, "B"), ("B", SUB_CLASS, "A"), ("C", SUB_CLASS, "B")]
    async with fuseki(HEADER + END, entity_result(*classes), edge_result(*edges)) as http:
        await pre_caching_stage(job(settings, 1), http, hooks=HOOKS_USED)
    a, b, c = ex("A", "B", "C")
    assert tables(settings, 1) == ({("class", a, c)}, {a: a, b: a}, {("class", a): (1, 1), ("class", c): (0, 0)})
    assert "The class hierarchy has 1 cycles" in caplog.text


# The hook, on a real Fuseki.

FAMILY = f"""@prefix owl: <{OWL}> .
@prefix rdfs: <{RDFS}> .
@prefix ex: <{EX}h#> .
ex:Animal a owl:Class ; rdfs:subClassOf owl:Thing .
ex:Pet a owl:Class ; rdfs:subClassOf ex:Animal .
ex:Dog a owl:Class ;
    rdfs:subClassOf ex:Pet , [ a owl:Restriction ; owl:onProperty ex:has ; owl:someValuesFrom ex:Tail ] .
ex:Hound a owl:Class ; owl:equivalentClass ex:Dog , [ owl:intersectionOf ( ex:Dog ex:Pet ) ] .
ex:Tail a owl:Class .
ex:has a owl:ObjectProperty .
ex:hasPart a owl:ObjectProperty ; rdfs:subPropertyOf ex:has .
"""


@pytest.mark.fuseki
async def test_hierarchies_on_real_fuseki(fuseki_url: str, data_dir: Path) -> None:
    """Check the hook on a real Fuseki: blank-node parents and equivalents are left out, and imports are seen."""
    settings = make_settings()
    generation = 900_000 + uuid.uuid4().int % 100_000
    name = dataset_name(generation)
    imported = f"<{EX}h#Puppy> a <{OWL}Class> ; <{RDFS}subClassOf> <{EX}h#Dog> .\n"
    upload = data_dir / "family.ttl"
    upload.write_text(FAMILY)
    async with httpx2.AsyncClient(base_url=fuseki_url, timeout=60) as http:
        (await http.post("/$/datasets", data={"dbName": name, "dbType": "mem"})).raise_for_status()
        try:
            for graph, content in [(None, FAMILY), (f"{EX}imported", imported)]:
                response = await http.post(
                    f"/{name}/data",
                    params={} if graph is None else {"graph": graph},
                    content=content.encode(),
                    headers={"Content-Type": "text/turtle"},
                )
                response.raise_for_status()
            context = JobContext(
                generation, settings, Reasoner.NONE, upload=upload, imports=[ImportResult(f"{EX}imported", triples=2)]
            )
            await pre_caching_stage(context, http, hooks=HOOKS)
        finally:
            await http.delete(f"/$/datasets/{name}")
    found_edges, groups, counts = tables(settings, generation, HOOKS)
    animal, pet, dog, hound, tail, puppy, has, has_part = (
        f"{EX}h#{local}" for local in ("Animal", "Pet", "Dog", "Hound", "Tail", "Puppy", "has", "hasPart")
    )
    assert found_edges == {
        ("class", animal, pet),
        ("class", pet, dog),
        ("class", dog, puppy),
        ("object_property", has, has_part),
    }
    assert groups == {dog: dog, hound: dog}
    assert counts == {
        ("class", animal): (1, 3),
        ("class", pet): (1, 2),
        ("class", dog): (1, 1),
        ("class", puppy): (0, 0),
        ("class", tail): (0, 0),
        ("object_property", has): (1, 1),
        ("object_property", has_part): (0, 0),
    }
