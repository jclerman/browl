#!/usr/bin/env bash
# Start Fuseki 6.2.0 (no UI) on port 3031, serving a TDB2 store as /gen-1; print seconds until it answers a query.
# Usage: ./fuseki.sh ../S-4/data/tdb-<name> [heap]   (needs Java 21 and S-4's lib/, from ../S-4/fetch.sh)
# Stops the Fuseki this script started last, if it's still running; nothing else.
set -euo pipefail
store=$(cd "$1" && pwd)
cd "$(dirname "$0")"
mkdir -p data
if [ -f data/fuseki.pid ]; then kill "$(cat data/fuseki.pid)" 2>/dev/null && sleep 1 || true; fi
start=$(python3 -c 'import time; print(time.time())')
"$JAVA_HOME/bin/java" "-Xmx${2:-8g}" -cp ../S-4/lib/apache-jena-fuseki-6.2.0/fuseki-server.jar \
  org.apache.jena.fuseki.main.cmds.FusekiServerPlainCmd --port 3031 --loc "$store" /gen-1 > data/fuseki.log 2>&1 &
echo $! > data/fuseki.pid
until curl -s -o /dev/null -m 2 --data-urlencode 'query=ASK {}' http://localhost:3031/gen-1/sparql; do
  kill -0 "$(cat data/fuseki.pid)" 2>/dev/null || { tail -3 data/fuseki.log; exit 1; }
  sleep 0.05
done
python3 -c "import time; print(f'fuseki ready in {time.time() - $start:.2f}s')"
