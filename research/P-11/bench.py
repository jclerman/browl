"""Time the entities and names pre-caching hooks against a Fuseki serving a large store as /gen-1.

Usage: poetry run python research/P-11/bench.py URL [--overlap] [--upload FILE]

--overlap lets each batch's insert run on the database's thread while the next batch is read, by patching
`DerivedWriter.run` so that a batch insert returns once the previous one is done instead of its own.
"""

import argparse
import asyncio
import functools
import json
import logging
import tempfile
import time
from pathlib import Path

import httpx2

from browl.jobs import JobContext
from browl.precache import core
from browl.precache.core import Hook, PreCacheContext
from browl.precache.entities import entities
from browl.precache.names import names
from browl.precache.stage import pre_cache
from browl.settings import Settings

OIO = "http://www.geneontology.org/formats/oboInOwl#"
BATCH_FUNCTIONS = {"_stage", "_insert_listed"}


def overlap() -> None:
    """Patch `DerivedWriter.run` so batch inserts don't wait for themselves, only for the one before."""

    async def run(self, function):
        loop = asyncio.get_running_loop()
        future = loop.run_in_executor(self._executor, function, self._connection)
        previous = getattr(self, "_previous", None)
        self._previous = future
        if previous is not None:
            await previous
        if isinstance(function, functools.partial) and function.func.__name__ in BATCH_FUNCTIONS:
            return None
        return await future

    core.DerivedWriter.run = run


def time_steps(times: dict[str, float]) -> None:
    """Time each of the names hook's database steps, and the batches staged, summed, into `times`."""
    from browl.precache import names as module

    for name in ["_set_curies", "_stage", "_store_name_values", "_set_display_names"]:
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
        times[hook.name] = round(time.perf_counter() - start, 1)

    function.__name__ = hook.name
    function.__module__ = hook.module
    return Hook(function, hook.settings)


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("url")
    parser.add_argument("--overlap", action="store_true")
    parser.add_argument("--upload", type=Path)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    if args.overlap:
        overlap()
    with tempfile.TemporaryDirectory() as data_dir:
        settings = Settings.model_validate(
            {
                "BASE_URL": "http://example.org/",
                "DATA_DIR": data_dir,
                "AUTH_DISABLED": True,
                "NAME_PROPERTIES": [
                    "rdfs:label",
                    *(f"oio:has{kind}Synonym" for kind in ["Exact", "Related", "Broad", "Narrow"]),
                ],
                "EXTRA_PREFIXES": {"oio": OIO},
            }
        )
        times: dict[str, float] = {}
        time_steps(times)
        context = JobContext(1, settings, upload=args.upload)
        if args.upload:
            from browl.prefixes import read_prefixes

            start = time.perf_counter()
            context.prefixes = read_prefixes(args.upload)
            times["prefixes"] = round(time.perf_counter() - start, 2)
        start = time.perf_counter()
        async with httpx2.AsyncClient(base_url=args.url, timeout=600) as http:
            await pre_cache(context, http, [timed(entities, times), timed(names, times)])
        times["total"] = round(time.perf_counter() - start, 1)
        path = Path(data_dir) / "derived" / "1.sqlite3"
        times["MiB"] = round(path.stat().st_size / 2**20)
        print(json.dumps({"overlap": args.overlap, **{k: round(v, 1) for k, v in times.items()}}))


asyncio.run(main())
