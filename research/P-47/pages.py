"""P-47: N-3 for individuals' pages (p95 under 300 ms), on a TDB2 store S-7's Fuseki serves.

Usage: poetry run python research/P-47/pages.py URL NAME [--runs N]

Runs browl's app in a test client, with a load job whose only stage pre-caches every hook from Fuseki's `gen-1`
(generation 1, no imports) into data/NAME-pages/, and builds its usage table. Then, for every named individual, times
its page (`/_browl/iri?iri=…`: header, Overview and hierarchy column) and its Usage tab's panel, each `--runs` times
after a warm-up, server and client in one process. Writes data/NAME-pages.json. Start Fuseki with
research/S-7/fuseki.sh; see research/S-7/README.md.
"""

import argparse
import json
import shutil
import sqlite3
import statistics
import time
from pathlib import Path

import httpx2
from fastapi.testclient import TestClient

from browl import owlapi
from browl.app import create_app
from browl.derived import EntityType, derived_path
from browl.jobs import JobContext, Stage
from browl.precache.stage import pre_cache
from browl.settings import Settings

HERE = Path(__file__).parent
DATA = HERE / "data"
ROBOT_JAR = HERE.parents[1] / ".cache" / "robot.jar"


def summary(values: list[float]) -> dict:
    ordered = sorted(values)
    p95 = ordered[min(len(ordered) - 1, round(0.95 * (len(ordered) - 1)))]
    return {"p50": round(statistics.median(ordered), 1), "p95": round(p95, 1), "max": round(ordered[-1], 1)}


def timed(client: TestClient, params: dict[str, str]) -> tuple[float, httpx2.Response]:
    start = time.perf_counter()
    response = client.get("/_browl/iri", params=params)
    return (time.perf_counter() - start) * 1000, response


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("url")
    parser.add_argument("name")
    parser.add_argument("--runs", type=int, default=5)
    args = parser.parse_args()
    data_dir = DATA / f"{args.name}-pages"
    shutil.rmtree(data_dir, ignore_errors=True)
    settings = Settings.model_validate(
        {
            "BASE_URL": "http://purl.obolibrary.org/obo/",
            "AUTH_DISABLED": True,
            "DATA_DIR": str(data_dir),
            "FUSEKI_URL": args.url,
        }
    )

    async def stage(context: JobContext) -> None:
        async with httpx2.AsyncClient(base_url=args.url, timeout=3600) as http:
            await pre_cache(context, http)

    owlapi.start_jvm(ROBOT_JAR, "4g")
    app = create_app(settings, start_jvm=False, stages={Stage.PRE_CACHING: stage}, usage_tables=True)
    with TestClient(app) as client:
        start = time.perf_counter()
        client.portal.call(app.state.jobs.start)
        client.portal.call(app.state.jobs.wait)
        print(f"pre-cached in {time.perf_counter() - start:.0f} s", flush=True)
        with sqlite3.connect(derived_path(data_dir, 1)) as connection:
            iris = [
                iri
                for (iri,) in connection.execute(
                    "SELECT iri FROM entity WHERE type = ? ORDER BY iri", (EntityType.NAMED_INDIVIDUAL,)
                )
            ]
        usage = {"tab": "usage", "panel": "1"}
        while "Preparing" in timed(client, {"iri": iris[0], **usage})[1].text:  # the usage table's build
            time.sleep(1)
        rows = []
        for iri in iris:
            row: dict = {"iri": iri}
            for kind, extra in (("page", {}), ("usage", usage)):
                timed(client, {"iri": iri, **extra})  # warm
                runs = [timed(client, {"iri": iri, **extra}) for _ in range(args.runs)]
                assert all(response.status_code == 200 for _, response in runs), (iri, kind)
                row[kind] = [round(ms, 1) for ms, _ in runs]
                row[f"{kind}_bytes"] = len(runs[0][1].content)
            rows.append(row)
    result = {
        "individuals": len(rows),
        "page_ms": summary([ms for row in rows for ms in row["page"]]),
        "usage_ms": summary([ms for row in rows for ms in row["usage"]]),
        "slowest": sorted(rows, key=lambda row: -max(row["page"]))[:5],
    }
    (DATA / f"{args.name}-pages.json").write_text(json.dumps(result, indent=1))
    print(json.dumps({key: value for key, value in result.items() if key != "slowest"}, indent=1))


if __name__ == "__main__":
    main()
