#!/usr/bin/env bash
# P-8: which RDF format that ROBOT writes (RDF/XML or Turtle) loads fastest into TDB2, with each Jena loader.
# Usage: research/P-8/load-formats.sh <ontology> <name> [robot heap]; run from the repo root with JENA_HOME set and
# robot.jar at .cache/robot.jar (`make robot-jar`). Results go to research/P-8/data/<name>-results.tsv.
set -euo pipefail
src=$1 name=$2 heap=${3:-24g}
out=research/P-8/data
mkdir -p "$out"
results="$out/$name-results.tsv"
printf 'format\tloader\tseconds\tstore\n' > "$results"
for format in owl ttl; do
  file="$out/$name.$format"
  if [ ! -f "$file" ]; then
    t0=$(date +%s)
    java "-Xmx$heap" -jar .cache/robot.jar convert --input "$src" --output "$file"
    echo "robot convert to $format: $(( $(date +%s) - t0 )) s, $(du -h "$file" | cut -f1)"
  fi
  for loader in tdbloader xloader; do
    loc="$out/tdb-$name-$format-$loader"
    rm -rf "$loc" "$out/xtmp"
    extra=()
    [ "$loader" = xloader ] && extra=(--tmpdir "$out/xtmp")
    t0=$(python3 -c 'import time; print(time.time())')
    JVM_ARGS=-Xmx4g "$JENA_HOME/bin/tdb2.$loader" --loc "$loc" "${extra[@]}" "$file" > "$out/$name-$format-$loader.log" 2>&1
    t1=$(python3 -c 'import time; print(time.time())')
    secs=$(python3 -c "print(f'{$t1 - $t0:.1f}')")
    printf '%s\t%s\t%s\t%s\n' "$format" "$loader" "$secs" "$(du -sh "$loc" | cut -f1)" | tee -a "$results"
    rm -rf "$loc" "$out/xtmp"
  done
done
