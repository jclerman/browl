#!/usr/bin/env bash
# Add an import to an unregistered TDB2 store, in its own named graph, then register the store with Fuseki.
# Usage (from the repo root, stack running): research/P-3/add-import.sh <file> <dataset> <graph-IRI>
set -euo pipefail
file=$1 dataset=$2 graph=$3
name=$(basename "$file")
docker compose cp "$file" "app:/data/$name" >/dev/null 2>&1
docker compose exec -T app bash -s "$name" "$dataset" "$graph" <<'INNER'
set -euo pipefail
name=$1 dataset=$2 graph=$3
t0=$(date +%s)
$JENA_HOME/bin/tdb2.tdbloader --loc "/fuseki/databases/$dataset" --graph "$graph" "/data/$name" > /data/import.log 2>&1
echo "import: $(( $(date +%s) - t0 ))s, store $(du -sh "/fuseki/databases/$dataset" | cut -f1)"
rm -f "/data/$name"
curl -fsS -o /dev/null -w "register $dataset: %{http_code}\n" -X POST -d "dbName=$dataset" -d dbType=tdb2 \
  'http://fuseki:3030/$/datasets'
INNER
