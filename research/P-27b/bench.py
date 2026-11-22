"""P-27b: the Usage tab's first page, with its count, on a TDB2 store S-7's Fuseki serves (N-3: p95 under 300 ms).

Usage: poetry run python research/P-27b/bench.py URL NAME [--top N] [--sample N] [--runs N]

Builds browl's derived database for the store (`browl.precache.stage.pre_cache`, generation 1, so dataset `gen-1`) into
data/NAME/, unless it's there, then times what the tab's first page does (`browl.usage_tab.read_usage_tab`): the
count, the stream, the sort, the page's fragments, the OWLAPI's parse and the links, and its HTML. Entities with at
most 20,000 usages: the largest `--top`, and a random `--sample`, by S-7's census (research/S-7/data/NAME.sqlite).
Each is timed `--runs` times after a warm-up. It also checks that each page holds one axiom per usage. Writes
data/NAME-pages.json. Start Fuseki with research/S-7/fuseki.sh; see research/S-7/README.md, Reproducing.
"""

import argparse
import asyncio
import json
import random
import sqlite3
import statistics
import time
from pathlib import Path

import httpx2

from browl import owlapi
from browl.derived import DerivedReader, derived_path
from browl.fuseki import FusekiClient
from browl.jobs import JobContext
from browl.jobtypes import Reasoner
from browl.links import read_entity_links
from browl.pages import templates
from browl.precache.keys import build_keys
from browl.precache.stage import HOOKS, pre_cache
from browl.settings import Settings
from browl.usage_tab import UsageTab, read_usage_page, sorted_usages
from browl.usages import MAX_STREAMED_USAGES, UsageCounts, UsageStore, count_usages, read_usages

HERE = Path(__file__).parent
DATA = HERE / "data"
S7 = HERE.parent / "S-7" / "data"
ROBOT_JAR = HERE.parents[1] / ".cache" / "robot.jar"
GENERATION = 1


def chosen(name: str, top: int, sample: int) -> list[str]:
    with sqlite3.connect(S7 / f"{name}.sqlite") as connection:
        rows = connection.execute(
            "SELECT iri, total FROM usage_size WHERE entity AND total BETWEEN 1 AND ? ORDER BY total DESC",
            (MAX_STREAMED_USAGES,),
        ).fetchall()
    rest = rows[top:]
    return [iri for iri, _ in rows[:top]] + [iri for iri, _ in random.Random(7).sample(rest, min(sample, len(rest)))]


async def first_page(store: UsageStore, counts: UsageCounts, iri: str) -> dict:
    """Do what `read_usage_tab` does for page 1, timing each step; return the times and the page's shape."""
    times = {}
    start = time.perf_counter()
    count = await count_usages(store, iri, counts)
    times["count"] = time.perf_counter()
    usages = await read_usages(store, iri)
    times["stream"] = time.perf_counter()
    ordered = await store.derived.read(GENERATION, lambda c: sorted_usages(c, usages))
    times["sort"] = time.perf_counter()
    groups = await read_usage_page(store, iri, ordered, 1)
    times["page"] = time.perf_counter()
    mentioned = [g.subject for g in groups if g.subject] + [i for g in groups for a in g.axioms for i in a.iris()]
    links = await store.derived.read(GENERATION, lambda c: read_entity_links(c, mentioned, "_browl", ""))
    tab = UsageTab(count=count, page=1, pages=max(1, -(-count // 50)), groups=groups, links=links)
    html = templates.env.get_template("usage.html").module.usage_panel(tab, None)
    times["links_html"] = time.perf_counter()
    steps, previous = {}, start
    for step, at in times.items():
        steps[step] = round((at - previous) * 1000, 1)
        previous = at
    chunk = ordered[:50]
    short = [
        (g.subject, len(g.axioms), sum(u.subject == g.subject for u in chunk))
        for g in groups
        if len(g.axioms) != sum(u.subject == g.subject for u in chunk)
    ]
    return {
        "iri": iri,
        "count": count,
        "listed": len(usages),
        "total_ms": round((previous - start) * 1000, 1),
        "steps_ms": steps,
        "axioms": sum(len(g.axioms) for g in groups),
        "html_bytes": len(str(html)),
        "mismatched_groups": short,
    }


def summary(values: list[float]) -> dict:
    ordered = sorted(values)
    p95 = ordered[min(len(ordered) - 1, round(0.95 * (len(ordered) - 1)))]
    return {"p50": round(statistics.median(ordered), 1), "p95": round(p95, 1), "max": round(ordered[-1], 1)}


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("url")
    parser.add_argument("name")
    parser.add_argument("--top", type=int, default=30)
    parser.add_argument("--sample", type=int, default=150)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--heap", default="4g")
    args = parser.parse_args()
    data_dir = DATA / args.name
    settings = Settings.model_validate(
        {"BASE_URL": "http://purl.obolibrary.org/obo/", "AUTH_DISABLED": True, "DATA_DIR": str(data_dir)}
    )
    keys = build_keys(settings, HOOKS)
    async with httpx2.AsyncClient(base_url=args.url, timeout=600) as http:
        if not derived_path(data_dir, GENERATION).exists():
            start = time.perf_counter()
            await pre_cache(JobContext(GENERATION, settings, Reasoner.NONE), http)
            print(f"pre-cached in {time.perf_counter() - start:.0f} s", flush=True)
        owlapi.start_jvm(ROBOT_JAR, args.heap)
        reader = DerivedReader(data_dir, keys)
        store = UsageStore(FusekiClient(http), reader, GENERATION, "")
        counts = UsageCounts()
        rows = []
        for iri in chosen(args.name, args.top, args.sample):
            await first_page(store, counts, iri)  # warm
            runs = [await first_page(store, counts, iri) for _ in range(args.runs)]
            row = max(runs, key=lambda run: run["total_ms"]) | {"runs_ms": [run["total_ms"] for run in runs]}
            rows.append(row)
            print(json.dumps({key: row[key] for key in ("iri", "count", "total_ms", "steps_ms")}), flush=True)
        reader.close()
    totals = [ms for row in rows for ms in row["runs_ms"]]
    result = {
        "entities": len(rows),
        "first_page_with_count_ms": summary(totals),
        "count_ms": summary([row["steps_ms"]["count"] for row in rows]),
        "largest_count": max(row["count"] for row in rows),
        "slowest": sorted(rows, key=lambda row: -row["total_ms"])[:8],
        "mismatched": [row for row in rows if row["mismatched_groups"]],
        "counted_not_listed": [
            (row["iri"], row["count"], row["listed"]) for row in rows if row["count"] != row["listed"]
        ],
    }
    DATA.mkdir(exist_ok=True)
    (DATA / f"{args.name}-pages.json").write_text(json.dumps(result, indent=1))
    print(json.dumps({key: value for key, value in result.items() if key != "slowest"}, indent=1))


asyncio.run(main())
