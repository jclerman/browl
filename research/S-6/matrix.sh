#!/usr/bin/env bash
# The settings measured, in order (README). Usage (from the repo root): research/S-6/matrix.sh [label-pattern]
# Runs only the settings whose label matches the pattern (grep -E), e.g. research/S-6/matrix.sh '^nc-mdm'.
set -u
N=(ncbitaxon research/P-3/data/iris.txt)
U=(uberon research/S-5/data/iris-uberon.txt)
want() { [[ $1 =~ ${PATTERN} ]]; }
PATTERN=${1:-.}
r() { want "$1" || return 0; echo "=== $1"; research/S-6/run.sh "$@"; }
s() { want "$1" || return 0; echo "=== $1"; env "${@:4}" research/S-6/run.sh "$1" "$2" "$3"; }

# 1. The direct-buffer limit, heap 1 GB
r nc-default "${N[@]}"
for m in 64m 128m 256m 512m; do s "nc-mdm$m" "${N[@]}" OPTS=-XX:MaxDirectMemorySize=$m; done
# 2. Other settings, one at a time against the default
s nc-arena2 "${N[@]}" ENVS=MALLOC_ARENA_MAX=2
s nc-mmap128k "${N[@]}" ENVS=MALLOC_MMAP_THRESHOLD_=131072
s nc-trim "${N[@]}" OPTS=-XX:TrimNativeHeapInterval=1000
s nc-serial "${N[@]}" OPTS=-XX:+UseSerialGC
s nc-jetty "${N[@]}" JETTY=1
# 3. The recommended setting (Jetty's output buffer, direct buffers capped), heap 768 MB, and Compose memory limits
R=(JETTY=1 OPTS=-XX:MaxDirectMemorySize=64m)
s nc-rec "${N[@]}" "${R[@]}"
s nc-rec-768m "${N[@]}" "${R[@]}" HEAP=768m
s nc-default-lim1536m "${N[@]}" MEM_LIMIT=1536m
s nc-rec-lim1536m "${N[@]}" "${R[@]}" MEM_LIMIT=1536m
s nc-rec-lim1g "${N[@]}" "${R[@]}" MEM_LIMIT=1g
s nc-rec-768m-lim1g "${N[@]}" "${R[@]}" HEAP=768m MEM_LIMIT=1g
# 4. Uberon
r ub-default "${U[@]}"
s ub-rec "${U[@]}" "${R[@]}"
