"""How deep an axiom's nodes and its annotations may nest, and the trees annotation nodes form (DESIGN.md §2.8).

Shared by the OWLAPI side (`browl.axioms`) and the annotations panel's query (`browl.annotations`).
"""

from collections.abc import Callable, Hashable, Iterable, Mapping

MAX_DEPTH = 50
"""The deepest an axiom's nodes may nest (the axiom is depth 1) for its entity's page to show its axioms: deeper, and
the panel says they can't be shown (`browl.owlapi.FragmentError`). It keeps the recursion in converting, rendering and
JSON (whose parser stops at about 200 levels, two per node in a list) well within bounds (DESIGN.md §2.8)."""

MAX_ANNOTATION_DEPTH = MAX_DEPTH - 1
"""The deepest an axiom's or an annotation value's annotations nest, its own being depth 1: deeper ones are left out,
so an annotation's nodes stay within `MAX_DEPTH` (DESIGN.md §2.8, Axiom annotations)."""


def annotation_sources[T: Hashable](sources: Mapping[T, Iterable[T]], key: Callable[[T], str] = str) -> dict[T, T]:
    """Return the source each annotation node keeps, so they form trees no deeper than `MAX_ANNOTATION_DEPTH`.

    An annotation node is a reified axiom or an `owl:Annotation`: whatever has an `owl:annotatedSource`. One whose
    source isn't such a node (an entity, or a blank node's axiom) is at depth 1; one on a node at depth n, at n + 1. A
    node keeps one source, on the shallowest path to it, the first by `key` among equals, so a node with several
    sources (which OWL's RDF mapping doesn't make) is shown once. A node in a cycle that nothing outside it leads to,
    or deeper than `MAX_ANNOTATION_DEPTH`, keeps none, and is left out (DESIGN.md §2.8, Axiom annotations).

    Args:
        sources: Each annotation node's `owl:annotatedSource`s.
        key: How to order nodes, so the choice is the same each time.

    Returns:
        The source each node that isn't left out keeps.
    """
    kept: dict[T, T] = {}
    on: dict[T, list[T]] = {}
    frontier: list[T] = []
    for node, each in sources.items():
        outside = sorted((source for source in each if source not in sources), key=key)
        if outside:
            kept[node] = outside[0]
            frontier.append(node)
        for source in each:
            if source in sources:
                on.setdefault(source, []).append(node)
    depth = 1
    while frontier and depth < MAX_ANNOTATION_DEPTH:
        deeper = []
        for source in sorted(frontier, key=key):
            for node in on.get(source, ()):
                if node not in kept:
                    kept[node] = source
                    deeper.append(node)
        frontier = deeper
        depth += 1
    return kept
