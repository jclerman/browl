#!/usr/bin/env bash
# Content comparison for one ontology: load it into OLS4, serve OLS4's API, run content.py, stop OLS4.
# Usage (from the repo root, browl's stack stopped): OLS=<ols4 checkout> research/S-5/content.sh <id> <prefix> <file> \
#   <relation-property-IRI|-> <iris-file> [classes]
# SKIP_LOAD=1 serves the database the last load left in $OLS/out instead of loading again.
# Writes research/S-5/data/content-<id>.jsonl.
set -euo pipefail
: "${OLS:?set OLS to the OLS4 checkout}"
id=$1 prefix=$2 file=$3 prop=$4 iris=$5 n=${6:-50}
HERE=research/S-5
hier=$prop; [ "$prop" = - ] && hier=
export HOST_UID HOST_GID
HOST_UID=$(id -u) HOST_GID=$(id -g)
DC="docker compose --project-directory $OLS -p ols4"
# Stop OLS4 before the load replaces its data directory, and again on exit.
$DC down >/dev/null 2>&1 || true
trap '$DC down >/dev/null 2>&1' EXIT
[ -n "${SKIP_LOAD:-}" ] || $HERE/ols4-load.sh "$id" "$prefix" "$file" $hier
gunzip -c "$OLS/out/text_tagger_db.bin.gz" > "$OLS/out/text_tagger_db.bin"
$DC up -d ols4-postgres ols4-backend >/dev/null 2>&1
for _ in $(seq 300); do curl -fsS -o /dev/null 2>/dev/null "http://localhost:8080/api/v2/ontologies/$id" && break; sleep 1; done
JAVA_HOME=${JAVA_HOME:-$(/usr/libexec/java_home -v 21)} research/S-1/.venv/bin/python $HERE/content.py "$file" "$id" "$prop" "$iris" "$n" > "$HERE/data/content-$id.jsonl"
tail -1 "$HERE/data/content-$id.jsonl"
