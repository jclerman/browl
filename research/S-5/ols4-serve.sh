#!/usr/bin/env bash
# Serve the database ols4-load.sh left in $OLS/out (PostgreSQL, backend, frontend), then measure idle memory and page
# load from 1 and 16 clients, sampling each container's memory every second.
# Usage: OLS=<ols4 checkout> research/S-5/ols4-serve.sh <id> <iris-file>
# Writes research/S-5/data/ols4-<id>-{serve.jsonl,mem.tsv}. Leaves the stack running; stop it with
#   (cd $OLS && HOST_UID=$(id -u) HOST_GID=$(id -g) docker compose down)
set -euo pipefail
: "${OLS:?set OLS to the OLS4 checkout}"
id=$1 iris=$2
HERE=$(cd "$(dirname "$0")" && pwd -P)
DATA=$HERE/data
export HOST_UID HOST_GID
HOST_UID=$(id -u) HOST_GID=$(id -g)
DC="docker compose --project-directory $OLS -p ols4"

# The Compose file mounts out/text_tagger_db.bin, but the pipeline writes only the .gz (README).
gunzip -c "$OLS/out/text_tagger_db.bin.gz" > "$OLS/out/text_tagger_db.bin"
$DC down >/dev/null 2>&1 || true
t0=$(date +%s)
$DC up -d ols4-postgres ols4-backend ols4-frontend >/dev/null 2>&1
until curl -fsS -o /dev/null "http://localhost:8080/api/v2/ontologies/$id"; do sleep 1; done
echo "{\"ready_after_up_s\": $(($(date +%s) - t0))}" | tee "$DATA/ols4-$id-serve.jsonl"

containers="ols4-ols4-postgres-1 ols4-ols4-backend-1 ols4-ols4-frontend-1"
sample() {  # sample <phase>: one line per container per second until killed
  while :; do
    docker stats --no-stream --format "{{.Name}}\t{{.MemUsage}}" $containers | sed "s/^/$1\t/"
    sleep 1
  done
}
: > "$DATA/ols4-$id-mem.tsv"
sleep 30  # let the backend settle after start-up
sample idle >> "$DATA/ols4-$id-mem.tsv" & s=$!; sleep 20; kill $s

net=ols4_default
gen() {
  docker run --rm --network $net -v "$HERE:/s5:ro" -v "$(cd "$(dirname "$iris")" && pwd -P):/iris:ro" browl-app \
    python /s5/pageload.py ols4 "$id" "$@"
}
gen once "$(head -1 "$iris")" | tee -a "$DATA/ols4-$id-serve.jsonl"
for c in 1 16; do
  sample "load-$c" >> "$DATA/ols4-$id-mem.tsv" & s=$!
  gen $c 60 "/iris/$(basename "$iris")" | tee -a "$DATA/ols4-$id-serve.jsonl"
  kill $s
done
python3 "$HERE/memsummary.py" "$DATA/ols4-$id-mem.tsv" | tee -a "$DATA/ols4-$id-serve.jsonl"
