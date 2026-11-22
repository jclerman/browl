"""P-27c: the Usage tab's pages with their count, read from the usage table, on a TDB2 store S-7's Fuseki serves (N-3).

Usage: poetry run python research/P-27c/pages.py URL NAME [--top N] [--sample N] [--runs N] [--deep]

Reads the usage table bench.py built (data/NAME/usages/1.sqlite3) and the derived database (data/NAME/derived), and
times what the tab's page does once the table is ready (`browl.usage_tab.read_usage_tab`): the count and the page's
rows from the table (`browl.usages.read_table_page`), the page's values, fragments and the OWLAPI's parse
(`read_usage_page`), and the links and HTML. The `--top` most-used entities and a random `--sample` of the rest, each
`--runs` times after a warm-up; page 1, and with `--deep` the middle and last pages too. It checks that each page holds
one axiom per usage. Writes data/NAME-pages.json.
"""

import argparse
import asyncio
import json
import math
import random
import sqlite3
import statistics
import time
from pathlib import Path

import httpx2

import browl.overview  # noqa: F401 -- registers the templates' `shown_name`
from browl import owlapi
from browl.derived import DerivedReader
from browl.fuseki import FusekiClient
from browl.links import read_entity_links
from browl.pages import templates
from browl.precache.keys import build_keys
from browl.precache.stage import HOOKS
from browl.settings import Settings
from browl.usage_tab import PAGE_USAGES, UsageTab, read_usage_page
from browl.usage_tables import UsageTables, table_path
from browl.usages import UsageStore, read_table_page

HERE = Path(__file__).parent
DATA = HERE / "data"
ROBOT_JAR = HERE.parents[1] / ".cache" / "robot.jar"
GENERATION = 1


def chosen(data_dir: Path, top: int, sample: int) -> list[tuple[str, int]]:
    with sqlite3.connect(table_path(data_dir, GENERATION)) as table:
        counts = table.execute(
            "SELECT entity, listed FROM usage_count WHERE listed > 0 ORDER BY listed DESC"
        ).fetchall()
    rest = counts[top:]
    picked = counts[:top] + random.Random(7).sample(rest, min(sample, len(rest)))
    with sqlite3.connect(data_dir / "derived" / f"{GENERATION}.sqlite3") as derived:
        iris = {
            number: derived.execute("SELECT iri FROM entity WHERE id = ?", (number,)).fetchone()[0]
            for number, _ in picked
        }
    return [(iris[number], listed) for number, listed in picked]


async def page_with_count(store: UsageStore, iri: str, page: int) -> dict:
    """Do what `read_usage_tab` does for a page once the table is ready, timing each step."""
    times = {}
    start = time.perf_counter()
    first = (page - 1) * PAGE_USAGES
    table = await read_table_page(store, iri, first, first + PAGE_USAGES)
    assert table is not None
    times["table"] = time.perf_counter()
    continued = table.previous is not None and bool(table.usages) and table.previous.subject == table.usages[0].subject
    groups = await read_usage_page(store, iri, table.usages, continued)
    times["page"] = time.perf_counter()
    mentioned = [g.subject for g in groups if g.subject] + [i for g in groups for a in g.axioms for i in a.iris()]
    links = await store.derived.read(GENERATION, lambda c: read_entity_links(c, mentioned, "_browl", ""))
    pages = max(1, math.ceil(table.listed / PAGE_USAGES))
    tab = UsageTab(count=table.count, page=page, pages=pages, groups=groups, links=links)
    html = templates.env.get_template("usage.html").module.usage_panel(tab, None, iri)
    times["links_html"] = time.perf_counter()
    steps, previous = {}, start
    for step, at in times.items():
        steps[step] = round((at - previous) * 1000, 1)
        previous = at
    return {
        "iri": iri,
        "page": page,
        "count": table.count,
        "total_ms": round((previous - start) * 1000, 1),
        "steps_ms": steps,
        "usages": len(table.usages),
        "axioms": sum(len(g.axioms) for g in groups),
        "html_bytes": len(str(html)),
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
    parser.add_argument("--deep", action="store_true")
    args = parser.parse_args()
    data_dir = DATA / args.name
    settings = Settings.model_validate(
        {"BASE_URL": "http://purl.obolibrary.org/obo/", "AUTH_DISABLED": True, "DATA_DIR": str(data_dir)}
    )
    keys = build_keys(settings, HOOKS)
    with sqlite3.connect(data_dir / "derived" / f"{GENERATION}.sqlite3") as derived:
        stored = dict(derived.execute("SELECT component, key FROM build_key"))
    if stored != keys:  # P-27b's bench built it, before a later fix to the owner map's hook
        print(f"the derived database's keys differ in {sorted(k for k in keys if stored.get(k) != keys[k])}")
        keys = stored
    owlapi.start_jvm(ROBOT_JAR, args.heap)
    async with httpx2.AsyncClient(base_url=args.url, timeout=600) as http:
        sparql = FusekiClient(http)
        tables = UsageTables(data_dir, sparql, keys)
        tables.key = "bench"  # as bench.py built it
        reader = DerivedReader(data_dir, keys)
        store = UsageStore(sparql, reader, GENERATION, "", tables)
        rows = []
        for iri, listed in chosen(data_dir, args.top, args.sample):
            last = max(1, math.ceil(listed / PAGE_USAGES))
            for page in sorted({1, (last + 1) // 2, last} if args.deep else {1}):
                await page_with_count(store, iri, page)  # warm
                runs = [await page_with_count(store, iri, page) for _ in range(args.runs)]
                row = max(runs, key=lambda run: run["total_ms"]) | {"runs_ms": [run["total_ms"] for run in runs]}
                rows.append(row)
                print(
                    json.dumps({key: row[key] for key in ("iri", "page", "count", "total_ms", "steps_ms")}), flush=True
                )
        reader.close()
        await tables.close()
    first = [row for row in rows if row["page"] == 1]
    top = first[0]
    result = {
        "entities": len(first),
        "most_used": {"iri": top["iri"], "count": top["count"], "first_page_with_count_ms": summary(top["runs_ms"])},
        "first_page_with_count_ms": summary([ms for row in first for ms in row["runs_ms"]]),
        "top_first_page_ms": summary([ms for row in first[: args.top] for ms in row["runs_ms"]]),
        "deep_pages_ms": summary([ms for row in rows if row["page"] > 1 for ms in row["runs_ms"]] or [0]),
        "table_ms": summary([row["steps_ms"]["table"] for row in rows]),
        "slowest": sorted(rows, key=lambda row: -row["total_ms"])[:8],
        "mismatched": [row for row in rows if row["axioms"] != row["usages"]],
    }
    (DATA / f"{args.name}-pages.json").write_text(json.dumps(result, indent=1))
    print(json.dumps({key: value for key, value in result.items() if key != "slowest"}, indent=1))


asyncio.run(main())
