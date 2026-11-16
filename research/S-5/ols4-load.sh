#!/usr/bin/env bash
# Run OLS4's dataload on one ontology file, timed, and report its store size and per-step trace.
# Usage: OLS=<ols4 checkout> research/S-5/ols4-load.sh <id> <prefix> <file> [hierarchical-property-IRI]
# The ontology IRI and base URI default to OBO's; set ONTO_URI and BASE_URI for other ontologies. NF_MEM (e.g. 7.GB)
# replaces the 5 GB memory cap on each step in OLS4's local Nextflow config; Java steps get 90% of it as heap.
# Writes research/S-5/data/ols4-<id>-{dataload.log,trace.tsv,load.json}. Leaves the database in $OLS/out for
# ols4-serve.sh. Uses the arm64 Nextflow image built from research/S-5/Dockerfile.nextflow (see README).
set -euo pipefail
: "${OLS:?set OLS to the OLS4 checkout}"
id=$1 prefix=$2 file=$3 hier=${4:-}
HERE=$(cd "$(dirname "$0")" && pwd -P)
DATA=$HERE/data
mkdir -p "$DATA" "$OLS/local"

# The file must be under $OLS (the only host path the containers see), given as an absolute path (README).
src="$OLS/local/$(basename "$file")"
[ -e "$src" ] || cp -c "$file" "$src" 2>/dev/null || cp "$file" "$src"
hier_json=""
[ -n "$hier" ] && hier_json="\"hierarchical_property\": [\"$hier\"],"
cat > "$OLS/local/$id.json" <<EOF
{"ontologies": [{
  "id": "$id", "preferredPrefix": "$prefix", "title": "$id (browl S-5 test copy)",
  "uri": "${ONTO_URI:-http://purl.obolibrary.org/obo/$id.owl}",
  "definition_property": ["http://purl.obolibrary.org/obo/IAO_0000115"],
  "synonym_property": ["http://www.geneontology.org/formats/oboInOwl#hasExactSynonym",
    "http://www.geneontology.org/formats/oboInOwl#hasRelatedSynonym",
    "http://www.geneontology.org/formats/oboInOwl#hasNarrowSynonym",
    "http://www.geneontology.org/formats/oboInOwl#hasBroadSynonym"],
  $hier_json
  "base_uri": ["${BASE_URI:-http://purl.obolibrary.org/obo/${prefix}_}"],
  "oboSlims": false,
  "ontology_purl": "$src"
}]}
EOF

# dataload.sh with the arm64 Nextflow image and a trace file; nothing else changed.
sed -e 's|ghcr.io/ebispot/ols4-nextflow:dev|ols4-nextflow:arm64|' \
    -e 's|-c \\"$OLS_NF_CONFIG\\" -resume|-c \\"$OLS_NF_CONFIG\\" -with-trace \\"$TMP_DIR/trace.tsv\\"|' \
    "$OLS/dataload.sh" > "$OLS/dataload-s5.sh"
chmod +x "$OLS/dataload-s5.sh"
grep -q 'ols4-nextflow:arm64' "$OLS/dataload-s5.sh" && grep -q with-trace "$OLS/dataload-s5.sh"

nf_config=""
if [ -n "${NF_MEM:-}" ]; then
  sed "s/memory = 5.GB/memory = $NF_MEM/" "$OLS/dataload/nextflow/local_nextflow.config" > "$OLS/local/nextflow-$NF_MEM.config"
  nf_config="$OLS/local/nextflow-$NF_MEM.config"
fi
rm -rf "$OLS/out" "$OLS/tmp"
t0=$(date +%s)
OLS4_CONFIG="$OLS/local/$id.json" OLS_NF_CONFIG=$nf_config "$OLS/dataload-s5.sh" > "$DATA/ols4-$id-dataload.log" 2>&1 || status=$?
t1=$(date +%s)
cp "$OLS/tmp/trace.tsv" "$DATA/ols4-$id-trace.tsv" 2>/dev/null || true
report=$(grep -E 'FAILED|Issues Found' "$OLS/out/loading_report.txt" 2>/dev/null | tr '\n' ' ' || echo "no report")
# publishDir symlinks into Nextflow's work directory, so follow links (-L) when sizing.
pg_mb=$(du -smL "$OLS/out/postgres" 2>/dev/null | cut -f1 || echo 0)
tagger_mb=$(du -smL "$OLS/out/text_tagger_db.bin.gz" 2>/dev/null | cut -f1 || echo 0)
work_mb=$(du -sm "$OLS/tmp/work" | cut -f1)
printf '{"id": "%s", "exit": %s, "dataload_s": %s, "postgres_mb": %s, "text_tagger_gz_mb": %s, "work_dir_mb": %s, "report": "%s"}\n' \
  "$id" "${status:-0}" $((t1 - t0)) "$pg_mb" "$tagger_mb" "$work_mb" "$report" | tee "$DATA/ols4-$id-load.json"
