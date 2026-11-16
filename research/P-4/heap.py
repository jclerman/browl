"""P-4: confirm APP_JVM_MAX_HEAP by parsing N-Triples through browl's OWLAPI bridge at decreasing heap sizes.

A page's fragment is at most a few thousand triples, so a whole ontology is a generous upper bound. Each heap size runs
in its own process, since the JVM's heap is fixed at start and it exits on running out (-XX:+ExitOnOutOfMemoryError).

Run from the repo root, with JAVA_HOME pointing at Java 21:
    poetry run python research/P-4/heap.py research/P-4/data/uberon.nt research/S-1/lib/robot.jar 1g 512m 384m 256m
"""

import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path


def child(ntriples_path: str, robot_jar: str, heap: str) -> None:
    from browl import owlapi

    owlapi.start_jvm(Path(robot_jar), heap)
    ntriples = Path(ntriples_path).read_bytes()

    def parse() -> dict[str, object]:
        import jpype

        runtime = jpype.JClass("java.lang.Runtime").getRuntime()
        system = jpype.JClass("java.lang.System")
        start = time.perf_counter()
        with owlapi.ntriples_ontology(ntriples) as ontology:
            parse_s = time.perf_counter() - start
            for _ in range(3):
                system.gc()
            retained_mb = (runtime.totalMemory() - runtime.freeMemory()) / 2**20
            axioms = ontology.getAxiomCount()
        return {"axioms": axioms, "parse_s": round(parse_s, 1), "retained_mb": round(retained_mb)}

    print(json.dumps({"heap": heap, "ok": True, **asyncio.run(owlapi.run_java(parse))}), flush=True)


def main(ntriples_path: str, robot_jar: str, *heaps: str) -> None:
    with Path(ntriples_path).open("rb") as lines:
        triples = sum(1 for _ in lines)
    print(json.dumps({"file": ntriples_path, "triples": triples}), flush=True)
    for heap in heaps:
        result = subprocess.run(
            [sys.executable, __file__, "--child", ntriples_path, robot_jar, heap],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode:
            error = (result.stderr.strip().splitlines() or ["?"])[-1]
            print(json.dumps({"heap": heap, "ok": False, "exit": result.returncode, "error": error}), flush=True)
        else:
            print(result.stdout.strip(), flush=True)


if __name__ == "__main__":
    if sys.argv[1] == "--child":
        child(*sys.argv[2:])
    else:
        main(*sys.argv[1:])
