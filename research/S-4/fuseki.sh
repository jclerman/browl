#!/usr/bin/env bash
# Start Fuseki (no UI) on a TDB2 store as dataset /ds; print seconds until it answers a query.
# Usage: ./fuseki.sh data/tdb-<name> [heap]   (needs Java 21)
set -euo pipefail
cd "$(dirname "$0")"
pkill -f 'fuseki-server.jar' 2>/dev/null || true
start=$(python3 -c 'import time; print(time.time())')
"$JAVA_HOME/bin/java" "-Xmx${2:-4g}" -cp lib/apache-jena-fuseki-6.2.0/fuseki-server.jar \
  org.apache.jena.fuseki.main.cmds.FusekiServerPlainCmd --loc "$1" /ds > "data/fuseki.log" 2>&1 &
until curl -s -o /dev/null -m 2 --data-urlencode 'query=ASK {}' http://localhost:3030/ds/sparql; do sleep 0.05; done
python3 -c "import time; print(f'fuseki ready in {time.time() - $start:.2f}s')"
