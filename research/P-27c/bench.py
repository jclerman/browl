"""P-27c: the usage table's bulk build, timed on a TDB2 store S-7's Fuseki serves, and its size.

Usage: poetry run python research/P-27c/bench.py URL NAME [--runs N]

Builds the usage table (`browl.usage_tables.build_file`, `browl.precache.usage_table.fill_usage_table`) for generation 1
(dataset `gen-1`, no imports) into data/NAME/usages/, from the derived database P-27b's bench built
(research/P-27b/data/NAME/derived, linked as data/NAME/derived). Times the build's phases (preparing the ranks and
counting, the scan, and the writing: the roots, positions and counts), samples the process's resident memory, and
records the file's size and rows. Writes data/NAME-build.json. Start Fuseki with research/S-7/fuseki.sh; see
research/S-7/README.md.
"""

import argparse
import asyncio
import json
import os
import resource
import sqlite3
import subprocess
import time
from pathlib import Path

import httpx2

from browl.fuseki import FusekiClient
from browl.precache.stage import on_thread
from browl.precache.usage_table import Progress, fill_usage_table
from browl.usage_tables import build_file, table_path

HERE = Path(__file__).parent
DATA = HERE / "data"


def rss_mib() -> float:
    out = subprocess.run(["ps", "-o", "rss=", "-p", str(os.getpid())], capture_output=True, text=True, check=True)
    return int(out.stdout.strip()) / 1024


async def build(url: str, name: str) -> dict:
    data_dir = DATA / name
    async with httpx2.AsyncClient(base_url=url) as http:
        sparql = FusekiClient(http)
        progress = Progress()
        marks: dict[str, float] = {}
        peak = [rss_mib()]

        async def sample() -> None:
            while True:
                peak.append(rss_mib())
                if progress.triples is not None and "counted" not in marks:
                    marks["counted"] = time.monotonic()
                await asyncio.sleep(0.5)

        sampler = asyncio.create_task(sample())
        start = time.monotonic()
        async with on_thread(build_file(data_dir, 1, "bench"), "usage-table") as db:
            await fill_usage_table(sparql, "gen-1", "", db, progress)
            marks["filled"] = time.monotonic()
        end = time.monotonic()
        sampler.cancel()
    path = table_path(data_dir, 1)
    with sqlite3.connect(path) as connection:
        rows = connection.execute("SELECT count(*) FROM usage").fetchone()[0]
        entities = connection.execute("SELECT count(*) FROM usage_count").fetchone()[0]
        top = connection.execute("SELECT entity, listed, count FROM usage_count ORDER BY count DESC LIMIT 5").fetchall()
        sizes = dict(connection.execute("SELECT name, sum(pgsize) FROM dbstat GROUP BY name"))
    scan_s = progress.scanned_at - progress.started
    return {
        "total_s": round(end - start, 1),
        "prepare_and_count_s": round(marks.get("counted", progress.started) - progress.started, 1),
        "scan_s": round(scan_s, 1),
        "write_s": round(end - progress.scanned_at, 1),
        "write_share": round((end - progress.scanned_at) / (end - progress.started), 2),
        "triples": progress.triples,
        "usages": rows,
        "entities_with_usages": entities,
        "top": top,
        "file_MiB": round(path.stat().st_size / 2**20, 1),
        "table_MiB": {key: round(value / 2**20, 1) for key, value in sizes.items()},
        "bytes_per_usage": round(sizes.get("usage", 0) / max(rows, 1), 1),
        "peak_rss_MiB": round(max(peak), 0),
        "start_rss_MiB": round(peak[0], 0),
        "maxrss_MiB": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 2**20, 0),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("url")
    parser.add_argument("name")
    parser.add_argument("--runs", type=int, default=1)
    args = parser.parse_args()
    results = []
    for _ in range(args.runs):
        result = asyncio.run(build(args.url, args.name))
        print(json.dumps(result), flush=True)
        results.append(result)
    (DATA / f"{args.name}-build.json").write_text(json.dumps(results, indent=2))


main()
