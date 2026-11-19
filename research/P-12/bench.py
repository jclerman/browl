"""Time the hierarchies pre-caching hook against a Fuseki serving a large store as /gen-1.

Usage: poetry run python research/P-12/bench.py URL

Runs the entities hook (which `hierarchies` needs) then `hierarchies`, timing each, and the hierarchies hook's steps:
staging the edges query's batches, and per hierarchy its SQL join, grouping, counting and inserts.
"""

import asyncio
import json
import logging
import resource
import sqlite3
import sys
import tempfile
import time
from contextlib import closing
from pathlib import Path

import httpx2

from browl.jobs import JobContext
from browl.precache import hierarchies as module
from browl.precache.core import Hook, PreCacheContext
from browl.precache.entities import entities
from browl.precache.hierarchies import hierarchies
from browl.precache.stage import pre_cache
from browl.settings import Settings

STEPS = ["_stage", "_fill", "group_nodes", "count_descendants"]


def time_steps(times: dict[str, float]) -> None:
    """Time each step of the hierarchies hook, summed over its calls, into `times`."""
    for name in STEPS:
        step = getattr(module, name)

        def wrapper(*args, _step=step, _name=name):
            start = time.perf_counter()
            try:
                return _step(*args)
            finally:
                times[_name] = times.get(_name, 0) + time.perf_counter() - start

        wrapper.__name__ = name
        setattr(module, name, wrapper)


def timed(hook: Hook, times: dict[str, float]) -> Hook:
    """Return the hook, timing its run into `times`."""

    async def function(context: PreCacheContext) -> None:
        start = time.perf_counter()
        await hook(context)
        times[hook.name] = time.perf_counter() - start

    function.__name__ = hook.name
    function.__module__ = hook.module
    return Hook(function, hook.settings)


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    with tempfile.TemporaryDirectory() as data_dir:
        settings = Settings.model_validate(
            {"BASE_URL": "http://example.org/", "DATA_DIR": data_dir, "AUTH_DISABLED": True}
        )
        times: dict[str, float] = {}
        time_steps(times)
        start = time.perf_counter()
        async with httpx2.AsyncClient(base_url=sys.argv[1], timeout=600) as http:
            await pre_cache(JobContext(1, settings), http, [timed(entities, times), timed(hierarchies, times)])
        times["total"] = time.perf_counter() - start
        path = Path(data_dir) / "derived" / "1.sqlite3"
        with closing(sqlite3.connect(path)) as connection:
            rows = {
                table: connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                for table in ("entity", "edge", "equiv_group", "counts")
            }
            tables = dict(
                connection.execute(
                    "SELECT name, sum(pgsize) FROM dbstat WHERE name IN"
                    " ('edge', 'edge_child', 'equiv_group', 'equiv_group_representative', 'counts') GROUP BY name"
                )
            )
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 2**30  # bytes on macOS
        print(
            json.dumps(
                {
                    **{key: round(value, 1) for key, value in times.items()},
                    "rows": rows,
                    "MiB": {name: round(size / 2**20) for name, size in tables.items()},
                    "peak_rss_GiB": round(peak, 2),
                }
            )
        )


asyncio.run(main())
