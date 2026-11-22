"""P-27c: how fast browl's bulk SELECT client reads every triple of a store (Python's side of the usage table's build).

Usage: poetry run python research/P-27c/scan.py URL
"""

import asyncio
import sys
import time

import httpx2

from browl.fuseki import FusekiClient

QUERY = """SELECT ?s ?p ?o ?blank WHERE {
  ?s ?p ?o
  BIND (IF(isBlank(?o), <http://jena.apache.org/ARQ/function#bnode>(?o), ?unbound) AS ?blank)
}"""


async def main(url: str) -> None:
    async with httpx2.AsyncClient(base_url=url) as http:
        client = FusekiClient(http)
        start = time.perf_counter()
        rows = 0
        async with client.bulk_select("gen-1", QUERY) as stream:
            async for _ in stream:
                rows += 1
        elapsed = time.perf_counter() - start
        print(f"{rows:,} rows in {elapsed:.1f} s: {elapsed / rows * 1e6:.2f} us per row")


asyncio.run(main(sys.argv[1]))
