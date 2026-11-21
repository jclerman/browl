"""Build browl's derived database (entities and names hooks) from a Fuseki serving a store as /gen-1, plus a name order.

Usage: poetry run python research/S-7/precache.py URL NAME

Writes data/NAME.sqlite: browl's `entity` and `name_value` tables, and `name_order`, the spike's addition: each entity
IRI once, ranked by casefolded display name then IRI, the order the Usage tab sorts subjects in and walks `entity` in.
"""

import asyncio
import json
import shutil
import sqlite3
import sys
import tempfile
import time
from contextlib import closing
from pathlib import Path

import httpx2

from browl.jobs import JobContext
from browl.precache.entities import entities
from browl.precache.names import names
from browl.precache.stage import pre_cache
from browl.settings import Settings

DATA = Path(__file__).parent / "data"


def add_name_order(path: Path) -> dict[str, float]:
    start = time.perf_counter()
    with closing(sqlite3.connect(path)) as connection:
        rows = connection.execute("SELECT iri, display_name FROM entity WHERE type <> 'ontology'").fetchall()
        best: dict[str, str] = {}
        for iri, name in rows:  # a punned IRI once, under its first name
            best.setdefault(iri, name)
        ordered = sorted(best.items(), key=lambda item: (item[1].casefold(), item[0]))
        connection.execute(
            "CREATE TABLE name_order"
            " (rank INTEGER PRIMARY KEY, iri TEXT NOT NULL, name TEXT NOT NULL, key TEXT NOT NULL)"
        )
        connection.executemany(
            "INSERT INTO name_order VALUES (?, ?, ?, ?)",
            ((rank, iri, name, name.casefold()) for rank, (iri, name) in enumerate(ordered)),
        )
        connection.execute("CREATE UNIQUE INDEX name_order_iri ON name_order (iri)")
        connection.commit()
        size = dict(
            connection.execute(
                "SELECT name, sum(pgsize) FROM dbstat WHERE name IN ('name_order', 'name_order_iri') GROUP BY name"
            )
        )
    return {
        "name_order_s": round(time.perf_counter() - start, 1),
        "name_order_rows": len(ordered),
        "name_order_MiB": {name: round(value / 2**20, 1) for name, value in size.items()},
    }


async def main() -> None:
    url, name = sys.argv[1:3]
    DATA.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory() as data_dir:
        settings = Settings.model_validate(
            {"BASE_URL": "http://example.org/", "DATA_DIR": data_dir, "AUTH_DISABLED": True}
        )
        start = time.perf_counter()
        async with httpx2.AsyncClient(base_url=url, timeout=600) as http:
            await pre_cache(JobContext(1, settings), http, [entities, names])
        elapsed = time.perf_counter() - start
        target = DATA / f"{name}.sqlite"
        shutil.copy(Path(data_dir) / "derived" / "1.sqlite3", target)
    result = {"precache_s": round(elapsed, 1), **add_name_order(target)}
    with closing(sqlite3.connect(target)) as connection:
        result["entities"] = connection.execute("SELECT count(*) FROM entity").fetchone()[0]
    print(json.dumps(result))


asyncio.run(main())
