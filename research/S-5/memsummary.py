"""Summarize a memory-sample file (phase, container, docker stats MemUsage per line): median and max MiB per phase.

Usage: python3 research/S-5/memsummary.py <mem.tsv>
"""

import json
import re
import statistics
import sys
from collections import defaultdict
from pathlib import Path


def mib(s: str) -> float:
    n, u = re.match(r"([\d.]+)\s*(\w+)", s.strip()).groups()  # type: ignore[union-attr]
    return float(n) * {"B": 1 / 2**20, "kB": 1 / 1024, "KiB": 1 / 1024, "MiB": 1, "MB": 1, "GiB": 1024, "GB": 1024}[u]


samples: dict[tuple[str, str], list[float]] = defaultdict(list)
for line in Path(sys.argv[1]).read_text().splitlines():
    parts = line.rstrip("\n").split("\t")
    if len(parts) == 3:
        phase, name, usage = parts
        samples[(phase, name)].append(mib(usage.split("/")[0]))
for (phase, name), v in samples.items():
    print(
        json.dumps(
            {"phase": phase, "container": name, "mib_median": round(statistics.median(v)), "mib_max": round(max(v))}
        )
    )
