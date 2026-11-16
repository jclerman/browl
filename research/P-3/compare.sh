#!/usr/bin/env bash
# Default graph vs named graphs, for page queries. Expects two datasets loaded with load.sh from the same file:
#   ncbitaxon-dg  in the default graph, with Uberon added as an import:
#                 REGISTER=0 research/P-3/load.sh research/S-3/data/ncbitaxon.owl ncbitaxon-dg
#                 research/P-3/add-import.sh research/S-1/data/downloads/uberon.owl ncbitaxon-dg \
#                   http://purl.obolibrary.org/obo/uberon.owl
#   ncbitaxon-ng  in a named graph:
#                 research/P-3/load.sh research/S-3/data/ncbitaxon.owl ncbitaxon-ng http://purl.obolibrary.org/obo/ncbitaxon.owl
# Usage (from the repo root, stack running, data/iris.txt sampled): research/P-3/compare.sh
set -eu
HERE=research/P-3
DC="docker compose -f docker-compose.yml -f $HERE/fuseki-gc.yml"
wait_healthy() { until [ "$(docker inspect -f '{{.State.Health.Status}}' browl-fuseki-1)" = healthy ]; do sleep 1; done; }
docker compose cp "$HERE/pageload.py" app:/tmp/pageload.py >/dev/null 2>&1
docker compose cp "$HERE/data/iris.txt" app:/tmp/iris.txt >/dev/null 2>&1

echo "=== default-graph store, 2g heap"
FUSEKI_HEAP=2g $DC up -d fuseki >/dev/null 2>&1
wait_healthy
count() {  # count <dataset clause> <pattern>
  docker compose exec -T app curl -fsS -H 'Accept: text/tab-separated-values' \
    --data-urlencode "query=SELECT (COUNT(*) AS ?n) $1 WHERE { $2 }" http://fuseki:3030/ncbitaxon-dg/sparql | tail -1
}
merge='FROM <urn:x-arq:DefaultGraph> FROM <urn:x-arq:UnionGraph>'
for iri in http://purl.obolibrary.org/obo/NCBITaxon_9606 http://purl.obolibrary.org/obo/UBERON_0000948; do
  p="<$iri> ?p ?o"
  echo "$iri: plain=$(count '' "$p") from=$(count "$merge" "$p") union=$(count '' "{ $p } UNION { GRAPH ?g { $p } }")"
done
for mode in plain from union; do
  for c in 1 16; do docker compose exec -T -e MODE=$mode app python /tmp/pageload.py ncbitaxon-dg $c 60 /tmp/iris.txt; done
done

echo "=== named-graph store, union default graph, 2g heap"
FUSEKI_HEAP=2g FUSEKI_TEST_OPTS=-Dtdb:unionDefaultGraph=true $DC up -d fuseki >/dev/null 2>&1
wait_healthy
for c in 1 16; do docker compose exec -T app python /tmp/pageload.py ncbitaxon-ng $c 60 /tmp/iris.txt; done
