#!/usr/bin/env bash
# browl's side for one ontology: load into TDB2 (P-3's load.sh), run S-4's bulk pre-caching queries, then measure idle
# memory and page load from 1 and 16 clients, sampling Fuseki's and the app's memory every second.
# Usage (from the repo root, OLS4's stack stopped): research/S-5/browl-run.sh <dataset> <file> <iris-file>
# Writes research/S-5/data/browl-<dataset>-{run.jsonl,mem.tsv}. Leaves the stack running and the dataset registered.
set -euo pipefail
dataset=$1 file=$2 iris=$3
HERE=research/S-5
DATA=$HERE/data
out=$DATA/browl-$dataset-run.jsonl
docker compose up -d >/dev/null 2>&1
until [ "$(docker inspect -f '{{.State.Health.Status}}' browl-fuseki-1)" = healthy ]; do sleep 1; done
docker compose exec -T app curl -fsS -o /dev/null -X DELETE "http://fuseki:3030/\$/datasets/$dataset" 2>/dev/null || true

t0=$(date +%s)
research/P-3/load.sh "$file" "$dataset" | tee "$DATA/browl-$dataset-load.txt"
t1=$(date +%s)
docker compose cp research/P-3/pageload.py app:/tmp/p3load.py >/dev/null 2>&1
docker compose exec -T app python /tmp/p3load.py "$dataset" bulk | tee -a "$DATA/browl-$dataset-load.txt"
t2=$(date +%s)
store=$(docker compose exec -T app du -sm "/fuseki/databases/$dataset" | cut -f1)
echo "{\"load_s\": $((t1 - t0)), \"bulk_s\": $((t2 - t1)), \"store_mb\": $store}" | tee "$out"

# Restart Fuseki so idle memory isn't left over from the load and bulk queries.
docker compose restart fuseki >/dev/null 2>&1
until [ "$(docker inspect -f '{{.State.Health.Status}}' browl-fuseki-1)" = healthy ]; do sleep 1; done
containers="browl-fuseki-1 browl-app-1"
sample() {
  while :; do
    docker stats --no-stream --format "{{.Name}}\t{{.MemUsage}}" $containers | sed "s/^/$1\t/"
    sleep 1
  done
}
: > "$DATA/browl-$dataset-mem.tsv"
sleep 30
sample idle >> "$DATA/browl-$dataset-mem.tsv" & s=$!; sleep 20; kill $s

gen() {
  docker run --rm --network browl_default -v "$PWD/$HERE:/s5:ro" -v "$(cd "$(dirname "$iris")" && pwd -P):/iris:ro" \
    browl-app python /s5/pageload.py browl "$dataset" "$@"
}
gen once "$(head -1 "$iris")" | tee -a "$out"
for c in 1 16; do
  sample "load-$c" >> "$DATA/browl-$dataset-mem.tsv" & s=$!
  gen $c 60 "/iris/$(basename "$iris")" | tee -a "$out"
  kill $s
done
python3 $HERE/memsummary.py "$DATA/browl-$dataset-mem.tsv" | tee -a "$out"
