"""P-47: how many triples have an IRI subject that isn't in `entity`, before and after, on a store S-7's Fuseki serves.

Usage: poetry run python research/P-47/subjects.py URL NAME

Counts each IRI subject's triples (one GROUP BY query), and sums those whose subject isn't in the `entity` table of
data/NAME-before/ and data/NAME-after/ (built by build.py), as S-7 counted Uberon's 261, from 70 subjects
(research/S-7/README.md, section 8). Prints them, and the after build's undeclared subjects with the most triples.
"""

import asyncio
import sqlite3
import sys
from pathlib import Path

import httpx2

from browl.derived import derived_path
from browl.fuseki import FusekiClient
from browl.rdf import IRI, Literal

DATA = Path(__file__).parent / "data"
QUERY = "SELECT ?s (COUNT(*) AS ?n) WHERE { ?s ?p ?o FILTER isIRI(?s) } GROUP BY ?s"


async def main() -> None:
    url, name = sys.argv[1:3]
    async with httpx2.AsyncClient(base_url=url, timeout=600) as http:
        result = await FusekiClient(http).select("gen-1", QUERY)
    counts = {}
    for subject, n in result.rows:
        assert isinstance(subject, IRI) and isinstance(n, Literal)
        counts[str(subject)] = int(n.value)
    for label in ("before", "after"):
        with sqlite3.connect(derived_path(DATA / f"{name}-{label}", 1)) as connection:
            declared = {iri for (iri,) in connection.execute("SELECT iri FROM entity")}
        left = {iri: n for iri, n in counts.items() if iri not in declared}
        print(f"{label}: {sum(left.values()):,} triples from {len(left):,} subjects not in entity")
        if label == "after":
            for iri, n in sorted(left.items(), key=lambda item: -item[1])[:10]:
                print(f"  {n:4} {iri}")


asyncio.run(main())
