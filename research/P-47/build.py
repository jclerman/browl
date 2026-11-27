"""P-47: the entities and owner-map hooks' build times and sizes, on a TDB2 store S-7's Fuseki serves.

Usage: poetry run python research/P-47/build.py URL NAME LABEL [--runs N]

Builds a derived database with only the entities and owner-map hooks (`browl.precache.stage.pre_cache`, generation 1,
so dataset `gen-1`, no imports) into data/NAME-LABEL/, `--runs` times, timing each hook from the stage's progress
reports; the owner map's time includes the build's end (its last indexes, ANALYZE and the swap). Then records the
`entity` rows by type, the tables' sizes (SQLite's `dbstat`), the file's size, and the owner map's roots by kind.
Writes data/NAME-LABEL.json. LABEL names the code: run it with `PYTHONPATH` set to another checkout's root to time
that code (as `before`, main's). Start Fuseki with research/S-7/fuseki.sh; see research/S-7/README.md.
"""

import argparse
import asyncio
import json
import shutil
import sqlite3
import statistics
import time
from pathlib import Path

import httpx2

import browl
from browl.derived import derived_path
from browl.jobs import JobContext
from browl.jobtypes import Reasoner
from browl.precache.stage import FINISHING, HOOKS, pre_cache
from browl.settings import Settings

DATA = Path(__file__).parent / "data"
GENERATION = 1
USED = ("entities", "owner_map")


async def build(url: str, data_dir: Path) -> dict[str, float]:
    """Build the derived database once; return each hook's seconds."""
    shutil.rmtree(data_dir, ignore_errors=True)
    settings = Settings.model_validate(
        {"BASE_URL": "http://purl.obolibrary.org/obo/", "AUTH_DISABLED": True, "DATA_DIR": str(data_dir)}
    )
    hooks = tuple(hook for hook in HOOKS if hook.name in USED)
    marks: dict[str, float] = {}

    def progress(detail: str, fraction: float | None, iri: str | None) -> None:
        step = FINISHING if detail == FINISHING else detail.split(" · ")[0].split(", ")[-1]
        marks.setdefault(step, time.monotonic())

    context = JobContext(GENERATION, settings, Reasoner.NONE, on_progress=progress)
    async with httpx2.AsyncClient(base_url=url, timeout=3600) as http:
        start = time.monotonic()
        await pre_cache(context, http, hooks)
        end = time.monotonic()
    times = [marks[hook.name] for hook in hooks] + [end]
    return {"total": end - start} | {hook.name: times[i + 1] - times[i] for i, hook in enumerate(hooks)}


def sizes(data_dir: Path) -> dict:
    """Return the derived database's rows and sizes."""
    path = derived_path(data_dir, GENERATION)
    with sqlite3.connect(path) as connection:
        types = dict(connection.execute("SELECT type, count(*) FROM entity GROUP BY type ORDER BY type"))
        try:
            tables = dict(
                connection.execute(
                    "SELECT name, sum(pgsize) FROM dbstat WHERE name LIKE 'entity%' OR name LIKE 'usage_%'"
                    " OR name LIKE 'sqlite_autoindex_entity%' GROUP BY name ORDER BY name"
                )
            )
        except sqlite3.OperationalError:  # no dbstat
            tables = {}
        roots = dict(connection.execute("SELECT kind, count(*) FROM usage_root GROUP BY kind ORDER BY kind"))
        refs = connection.execute("SELECT count(*) FROM usage_ref").fetchone()[0]
    return {"file_bytes": path.stat().st_size, "entities": types, "table_bytes": tables, "roots": roots, "refs": refs}


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("url")
    parser.add_argument("name")
    parser.add_argument("label")
    parser.add_argument("--runs", type=int, default=3)
    args = parser.parse_args()
    data_dir = DATA / f"{args.name}-{args.label}"
    runs = []
    for _ in range(args.runs):
        runs.append(await build(args.url, data_dir))
        print(json.dumps({key: round(value, 2) for key, value in runs[-1].items()}), flush=True)
    result = {
        "code": str(Path(browl.__file__).parent),
        "runs_s": runs,
        "median_s": {key: round(statistics.median(run[key] for run in runs), 2) for key in runs[0]},
    } | sizes(data_dir)
    (DATA / f"{args.name}-{args.label}.json").write_text(json.dumps(result, indent=1))
    print(json.dumps({key: value for key, value in result.items() if key != "runs_s"}, indent=1))


asyncio.run(main())
