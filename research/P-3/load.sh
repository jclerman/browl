#!/usr/bin/env bash
# Load an RDF file into a new TDB2 store with tdb2.xloader, inside the app container, and register it with Fuseki.
# Usage (from the repo root, stack running): research/P-3/load.sh <file> <dataset> [graph-IRI]
# Without a graph IRI the triples go in the default graph; with one, in that named graph. REGISTER=0 leaves the store
# unregistered, so add-import.sh can add to it first.
set -euo pipefail
file=$1 dataset=$2 graph=${3:-}
register=${REGISTER:-1}
name=$(basename "$file")
docker compose cp "$file" "app:/data/$name" >/dev/null 2>&1
docker compose exec -T app bash -s "$name" "$dataset" "$graph" "$register" <<'EOF'
set -euo pipefail
name=$1 dataset=$2 graph=$3 register=$4
cd /data
t0=$(date +%s)
if [ -n "$graph" ]; then
  # xloader has no --graph option, but keeps the graph of N-Quads input.
  $JENA_HOME/bin/riot --out=nt "$name" | sed "s| \.\$| <$graph> .|" | gzip -1 > input.nq.gz
else
  $JENA_HOME/bin/riot --out=nt "$name" | gzip -1 > input.nt.gz
fi
t1=$(date +%s); echo "convert: $((t1 - t0))s"
rm -rf "/fuseki/databases/$dataset" xtmp
JVM_ARGS=-Xmx2g $JENA_HOME/bin/tdb2.xloader --loc "/fuseki/databases/$dataset" --tmpdir xtmp input.* > xloader.log 2>&1
t2=$(date +%s); echo "xloader: $((t2 - t1))s, store $(du -sh "/fuseki/databases/$dataset" | cut -f1)"
rm -rf xtmp input.* "$name"
[ "$register" = 1 ] || exit 0
curl -fsS -o /dev/null -w "register $dataset: %{http_code}\n" -X POST -d "dbName=$dataset" -d dbType=tdb2 \
  'http://fuseki:3030/$/datasets'
EOF
