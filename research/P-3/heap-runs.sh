#!/usr/bin/env bash
# For each heap size: restart Fuseki with a GC log, run the bulk pre-caching queries, then page load from 1 and 16
# clients, sampling Fuseki's container memory every second.
# Usage (from the repo root, stack running, dataset loaded, data/iris.txt sampled):
#   research/P-3/heap-runs.sh <dataset> <heap>...      e.g. research/P-3/heap-runs.sh ncbitaxon 2g 1g 512m
# Extra JVM options for Fuseki come from FUSEKI_TEST_OPTS.
set -u
HERE=research/P-3
DATA=$HERE/data
DC="docker compose -f docker-compose.yml -f $HERE/fuseki-gc.yml"
dataset=$1
shift
mkdir -p "$DATA"
docker compose cp "$HERE/pageload.py" app:/tmp/pageload.py >/dev/null 2>&1
docker compose cp "$DATA/iris.txt" app:/tmp/iris.txt >/dev/null 2>&1
for heap in "$@"; do
  echo "=== heap $heap ${FUSEKI_TEST_OPTS:-}"
  docker compose exec -T app rm -f /fuseki/gc.log
  FUSEKI_HEAP=$heap $DC up -d fuseki >/dev/null 2>&1
  until [ "$(docker inspect -f '{{.State.Health.Status}}' browl-fuseki-1)" = healthy ]; do sleep 1; done
  (while :; do docker stats --no-stream --format '{{.MemUsage}}' browl-fuseki-1 | cut -d/ -f1; sleep 1; done) \
    > "$DATA/mem-$heap.txt" 2>/dev/null &
  sampler=$!
  docker compose exec -T app python /tmp/pageload.py "$dataset" bulk
  for c in 1 16; do docker compose exec -T app python /tmp/pageload.py "$dataset" $c 60 /tmp/iris.txt; done
  kill $sampler
  echo "fuseki: $(docker inspect -f 'running={{.State.Running}} restarts={{.RestartCount}}' browl-fuseki-1)"
  docker compose exec -T app cat /fuseki/gc.log > "$DATA/gc-$heap.log"
  python3 - "$DATA/mem-$heap.txt" "$DATA/gc-$heap.log" <<'PY'
import re
import sys

def mib(s):
    n, u = re.match(r"([\d.]+)\s*(\w+)", s.strip()).groups()
    return float(n) * {"B": 1 / 2**20, "KiB": 1 / 1024, "MiB": 1, "GiB": 1024}[u]

mem = [mib(line) for line in open(sys.argv[1]) if line.strip()]
print(f"container memory: max {max(mem):.0f} MiB")
gc = open(sys.argv[2]).read()
after = [int(a) * (1024 if u == "G" else 1) for a, u in re.findall(r"->(\d+)([MG])\(", gc)]
print(f"heap after GC: max {max(after)} MiB over {len(after)} GCs; System.gc() full GCs {gc.count('System.gc()')}")
PY
done
