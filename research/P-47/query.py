"""P-47: time owner-map.rq, or a variant of it, alone, on a TDB2 store S-7's Fuseki serves.

Usage: poetry run python research/P-47/query.py URL QUERY_FILE [--runs N]

Streams the query's result through browl's bulk SELECT client (`browl.fuseki.FusekiClient.bulk_select`, TSV parsing
included) from dataset `gen-1`, counting its rows and distinct nodes, and prints the seconds of each run.
"""

import argparse
import asyncio
import json
import time
from pathlib import Path

import httpx2

from browl.fuseki import FusekiClient
from browl.precache.core import PREFIXES


async def run(url: str, query: str) -> dict:
    async with httpx2.AsyncClient(base_url=url, timeout=3600) as http:
        start = time.monotonic()
        rows = 0
        nodes = set()
        async with FusekiClient(http).bulk_select("gen-1", query, PREFIXES, "") as stream:
            node = stream.variables.index("node")
            async for row in stream:
                rows += 1
                nodes.add(row[node])
        return {"s": round(time.monotonic() - start, 2), "rows": rows, "nodes": len(nodes)}


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("url")
    parser.add_argument("query", type=Path)
    parser.add_argument("--runs", type=int, default=2)
    args = parser.parse_args()
    for _ in range(args.runs):
        print(json.dumps(await run(args.url, args.query.read_text())), flush=True)


asyncio.run(main())
