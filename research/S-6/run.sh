#!/usr/bin/env bash
# One setting of Fuseki's memory: recreate Fuseki with the given JVM options, environment and Compose memory limit, then
# measure idle, page load from 1 and 16 clients (60 s each) and S-4's bulk queries, sampling the container's memory
# every second or so (docker stats, and the cgroup's memory.stat) and taking JVM native memory tracking (NMT) summaries.
# Usage (from the repo root, the dataset loaded and registered, the browl-s6-jcmd image built; see README):
#   [HEAP=1g] [OPTS='-XX:...'] [ENVS='MALLOC_ARENA_MAX=2'] [MEM_LIMIT=1536m] [JETTY=1|<file>] \
#     research/S-6/run.sh <label> <dataset> <iris>
# Writes research/S-6/data/<label>.{yml,mem.tsv,run.jsonl,gc.log,log} and <label>.nmt-<phase>.txt.
set -u
label=$1 dataset=$2 iris=$3
HERE=research/S-6
DATA=$HERE/data
C=browl-fuseki-1
mkdir -p "$DATA"
out=$DATA/$label

# The Compose override for this setting. NMT costs a little memory of its own (it shows itself, as "Native Memory
# Tracking"); the GC log goes to the container's /tmp, so recreating the container clears it.
{
  echo "services:"
  echo "  fuseki:"
  echo "    environment:"
  echo "      JDK_JAVA_OPTIONS: -Xmx${HEAP:-1g} ${OPTS:-} -XX:NativeMemoryTracking=summary -Xlog:gc:file=/tmp/gc.log:uptime"
  for kv in ${ENVS:-}; do echo "      ${kv%%=*}: \"${kv#*=}\""; done
  [ -z "${MEM_LIMIT:-}" ] || echo "    mem_limit: $MEM_LIMIT"
  # JETTY=1: the image's own command, with docker/fuseki/jetty.xml; JETTY=<file>: the same with that file instead.
  # Otherwise Fuseki's own server setup, as before S-6.
  if [ -z "${JETTY:-}" ]; then
    echo "    command: [java, -XX:+ExitOnOutOfMemoryError, -cp, /opt/fuseki/fuseki-server.jar," \
      "org.apache.jena.fuseki.main.cmds.FusekiServerCmd, --port=3030, --base=/opt/fuseki/no-ui," \
      "--shiro=/opt/fuseki/shiro.ini]"
  elif [ "$JETTY" != 1 ]; then
    echo "    volumes: [\"$PWD/$JETTY:/opt/fuseki/jetty.xml:ro\"]"
  fi
} > "$out.yml"
DC="docker compose -f docker-compose.yml -f $out.yml"

healthy() {
  for _ in $(seq 120); do
    [ "$(docker inspect -f '{{.State.Health.Status}}' $C 2>/dev/null)" = healthy ] && return 0
    sleep 1
  done
  return 1
}
# Empty the Docker VM's page cache, so the store's pages are charged to this container's cgroup as Fuseki reads them, as
# in production, not to an earlier container's.
docker run --rm --privileged debian:trixie-20261104-slim sh -c 'sync; echo 3 > /proc/sys/vm/drop_caches'
$DC up -d --force-recreate fuseki >/dev/null 2>&1
healthy || { echo "{\"label\": \"$label\", \"error\": \"not healthy\"}" | tee "$out.run.jsonl"; exit 1; }
echo "{\"label\": \"$label\", \"dataset\": \"$dataset\", \"heap\": \"${HEAP:-1g}\", \"opts\": \"${OPTS:-}\", \"envs\": \"${ENVS:-}\", \"mem_limit\": \"${MEM_LIMIT:-}\"}" \
  | tee "$out.run.jsonl"

# Each sample: phase, time, docker stats' MemUsage, then the cgroup's anon, file, inactive_file and memory.current.
sample() {
  while :; do
    s=$(docker stats --no-stream --format '{{.MemUsage}}' $C 2>/dev/null | cut -d/ -f1)
    m=$(docker exec $C sh -c 'grep -E "^(anon|file|inactive_file) " /sys/fs/cgroup/memory.stat | cut -d" " -f2 \
      | tr "\n" "\t"; cat /sys/fs/cgroup/memory.current' 2>/dev/null)
    printf '%s\t%s\t%s\t%s\n' "$1" "$(date +%s)" "$s" "$m"
    sleep 1
  done
}
nmt() {
  docker run --rm --pid=container:$C browl-s6-jcmd jcmd 1 VM.native_memory summary > "$out.nmt-$1.txt" 2>&1
}
gen() {
  docker run --rm --network browl_default -v "$PWD/research:/r:ro" -v "$(cd "$(dirname "$iris")" && pwd -P):/iris:ro" \
    browl-app python "$@"
}

: > "$out.mem.tsv"
sleep 20
sample idle >> "$out.mem.tsv" & s=$!; sleep 20; kill $s
nmt idle
for c in 1 16; do
  sample "load-$c" >> "$out.mem.tsv" & s=$!
  [ $c = 16 ] && (sleep 45; nmt load-16) &
  gen /r/S-5/pageload.py browl "$dataset" $c 60 "/iris/$(basename "$iris")" | tee -a "$out.run.jsonl"
  kill $s
  wait
done
sample bulk >> "$out.mem.tsv" & s=$!
gen /r/P-3/pageload.py "$dataset" bulk > "$out.bulk.txt" 2>&1
rc=$?
kill $s
nmt bulk
python3 -c 'import json, sys; print(json.dumps({"bulk_rc": int(sys.argv[1]), "bulk": [json.loads(x) for x in sys.argv[2:]]}))' \
  "$rc" $(grep '^{' "$out.bulk.txt" | tr -d ' ') | tee -a "$out.run.jsonl"
[ $rc = 0 ] || tail -3 "$out.bulk.txt"

docker exec $C cat /tmp/gc.log > "$out.gc.log" 2>/dev/null
docker logs $C > "$out.log" 2>&1
docker inspect -f '{"oom_killed": {{.State.OOMKilled}}, "restarts": {{.RestartCount}}, "running": {{.State.Running}}}' $C \
  | tee -a "$out.run.jsonl"
